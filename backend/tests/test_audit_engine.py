from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pca.config import Settings
from pca.db.models import AuditJob, AuditRequester, Channel, ChannelAuthorization, PricingTier, TelegramConnection
from pca.domain.enums import AuditJobStatus, ConnectionStatus, TransactionType
from pca.security.crypto import Keyring, generate_key
from pca.services import audit_engine as eng
from pca.services import ledger
from pca.services.connection import seal_session
from pca.telegram.errors import SessionInvalidError, TelegramGatewayError
from pca.telegram.types import JoinRequester, JoinRequestPage, ScanCursor
from tests.conftest import make_user

ACCOUNT_ID = 777


class FakeScan:
    """Serves `total` requesters newest-first, honouring the keyset cursor. Pages overlap by
    one row on purpose to prove de-duplication."""

    def __init__(self, total: int, *, fail_at_call: dict[int, Exception] | None = None) -> None:
        self.total = total
        self.calls = 0
        self.fail_at_call = fail_at_call or {}

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor: ScanCursor, limit):
        self.calls += 1
        if self.calls in self.fail_at_call:
            raise self.fail_at_call[self.calls]
        start = 0 if not cursor.offset_user_id else cursor.offset_user_id - 1  # overlap by one
        ids = list(range(start + 1, min(start + limit, self.total) + 1))
        items = [
            JoinRequester(
                user_id=i,
                requested_at=1_700_000_000 - i,
                last_seen="online" if i % 2 else "recently",
                was_online=None,
                has_photo=i % 3 != 0,
                has_username=True,
                is_premium=False,
                is_deleted=i % 10 == 0,
                is_bot=False,
                is_scam=False,
                is_fake=False,
                via_chatlist=False,
            )
            for i in ids
        ]
        nxt = ScanCursor(1_700_000_000 - ids[-1], ids[-1], 1) if ids and ids[-1] < self.total else None
        return JoinRequestPage(total=self.total, items=items, next_cursor=nxt)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        bot_token="1:x",
        audit_page_size=10,
        audit_page_delay_seconds=0,
        audit_flood_inline_max_seconds=5,
        _env_file=None,
    )


@pytest.fixture
def keyring() -> Keyring:
    return Keyring.parse(f"k1:{generate_key()}", "k1")


async def _setup(sm, settings, keyring, tg_id: int, *, pending: int, credits: int = 100):
    user = await make_user(sm, tg_id)
    async with sm() as s, s.begin():
        if not (await s.execute(select(func.count()).select_from(PricingTier))).scalar_one():
            s.add(PricingTier(minimum_requests=1, maximum_requests=10_000_000, credits_required=5, active=True))
        if credits:
            await ledger.add_credits(
                s, user_id=user.id, credits=credits, tx_type=TransactionType.ADMIN_GRANT, reference=f"seed-{tg_id}"
            )
        sealed = seal_session(keyring, "SESSION", user_id=user.id, account_id=ACCOUNT_ID)
        conn = TelegramConnection(
            user_id=user.id,
            connection_status=ConnectionStatus.ACTIVE.value,
            connected_account_id=ACCOUNT_ID,
            encrypted_session=sealed.blob,
            encryption_key_id=sealed.key_id,
        )
        channel = Channel(telegram_channel_id=-100_000 - tg_id, title="Chan")
        s.add_all([conn, channel])
        await s.flush()
        auth = ChannelAuthorization(
            user_id=user.id,
            channel_id=channel.id,
            telegram_connection_id=conn.id,
            authorization_status="verified",
            can_manage_join_requests=True,
            access_hash=42,
            pending_requests=pending,
        )
        s.add(auth)
        await s.flush()
        job = await eng.create_audit(s, settings, user_id=user.id, conn=conn, auth=auth, channel=channel, chat_id=None)
        return user.id, job.id


async def _claim(sm, settings, worker="w1") -> int | None:
    async with sm() as s, s.begin():
        job = await eng.claim_next(s, settings, worker)
        return job.id if job else None


async def _run(sm, settings, keyring, gw, job_id, worker="w1"):
    async def nosleep(_):
        return None

    await eng.run_job(sm, settings, keyring, gw, job_id, worker, eng.NullNotifier(), sleep=nosleep)


async def _wallet(sm, user_id):
    async with sm() as s:
        return await ledger.get_wallet(s, user_id)


async def test_full_scan_dedupes_and_charges_once(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9101, pending=35)
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (95, 5)

    claimed = await _claim(sessionmaker, settings)
    assert claimed == job_id
    await _run(sessionmaker, settings, keyring, FakeScan(35), job_id)

    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        assert job.scanned_count == 35
        assert job.charged_credits == 5
        assert job.summary["scanned"] == 35
        assert job.summary["last_seen"]["online"] == 18
        refs = (
            await s.execute(select(func.count()).where(AuditRequester.audit_job_id == job_id, AuditRequester.requester_ref.is_not(None)))
        ).scalar_one()
        assert refs == 0, "dedupe hashes are cleared at finalisation"
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (95, 0)


async def test_insufficient_credits_blocks_creation(sessionmaker, settings, keyring):
    with pytest.raises(ledger.InsufficientCreditsError):
        await _setup(sessionmaker, settings, keyring, 9102, pending=10, credits=0)


async def test_second_active_audit_on_same_channel_is_refused(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9103, pending=10)
    async with sessionmaker() as s, s.begin():
        job = await s.get(AuditJob, job_id)
        conn = await s.get(TelegramConnection, job.telegram_connection_id)
        auth = await s.get(ChannelAuthorization, job.channel_authorization_id)
        channel = await s.get(Channel, job.channel_id)
        with pytest.raises(eng.AuditError) as err:
            await eng.create_audit(s, settings, user_id=user_id, conn=conn, auth=auth, channel=channel, chat_id=None)
        assert err.value.code == "already_running"


async def test_long_flood_wait_parks_then_resumes(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9104, pending=25)
    gw = FakeScan(25, fail_at_call={2: TelegramGatewayError("flood", retry_after=600)})
    await _claim(sessionmaker, settings)
    await _run(sessionmaker, settings, keyring, gw, job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.WAITING_FOR_TELEGRAM.value
        assert job.scanned_count == 10 and job.checkpoint is not None
        assert job.not_before > datetime.now(UTC) + timedelta(seconds=500)
    assert await _claim(sessionmaker, settings) is None, "not runnable before not_before"
    async with sessionmaker() as s, s.begin():
        (await s.get(AuditJob, job_id)).not_before = datetime.now(UTC) - timedelta(seconds=1)
    assert await _claim(sessionmaker, settings, "w2") == job_id
    await _run(sessionmaker, settings, keyring, gw, job_id, "w2")
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value and job.scanned_count == 25


async def test_crashed_worker_lease_is_reclaimed(sessionmaker, settings, keyring):
    _, job_id = await _setup(sessionmaker, settings, keyring, 9105, pending=12)
    await _claim(sessionmaker, settings, "dead")
    assert await _claim(sessionmaker, settings, "w2") is None, "live lease blocks reclaim"
    async with sessionmaker() as s, s.begin():
        (await s.get(AuditJob, job_id)).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await _claim(sessionmaker, settings, "w2") == job_id
    await _run(sessionmaker, settings, keyring, FakeScan(12), job_id, "w2")
    async with sessionmaker() as s:
        assert (await s.get(AuditJob, job_id)).status == AuditJobStatus.COMPLETED.value


async def test_revoked_session_mid_scan_is_partial_and_not_charged(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9106, pending=30)
    gw = FakeScan(30, fail_at_call={2: SessionInvalidError("session_invalid")})
    await _claim(sessionmaker, settings)
    await _run(sessionmaker, settings, keyring, gw, job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.PARTIAL.value
        assert job.error_code == "session_invalid" and job.charged_credits == 0
        assert job.summary["scanned"] == 10
        conn = await s.get(TelegramConnection, job.telegram_connection_id)
        assert conn.connection_status != ConnectionStatus.ACTIVE.value
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)


async def test_cancel_queued_releases_and_deletes(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9107, pending=10)
    async with sessionmaker() as s, s.begin():
        job = await eng.request_cancel(s, settings, user_id, job_id)
        assert job.status == AuditJobStatus.CANCELLED.value
    async with sessionmaker() as s, s.begin():
        await eng.request_cancel(s, settings, user_id, job_id)  # idempotent
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)


async def test_cancel_running_is_honoured_by_worker(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9108, pending=50)
    await _claim(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        job = await eng.request_cancel(s, settings, user_id, job_id)
        assert job.status == AuditJobStatus.RUNNING.value and job.cancel_requested
    await _run(sessionmaker, settings, keyring, FakeScan(50), job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.CANCELLED.value
        n = (await s.execute(select(func.count()).where(AuditRequester.audit_job_id == job_id))).scalar_one()
        assert n == 0


async def test_other_user_cannot_cancel(sessionmaker, settings, keyring):
    _, job_id = await _setup(sessionmaker, settings, keyring, 9109, pending=10)
    other = await make_user(sessionmaker, 9110)
    async with sessionmaker() as s, s.begin():
        assert await eng.request_cancel(s, settings, other.id, job_id) is None


def test_last_seen_never_merges_recently_into_24h():
    from telethon import types

    from pca.telegram.gateway import classify_last_seen

    now = 1_800_000_000
    assert classify_last_seen(types.UserStatusRecently(), deleted=False, now=now)[0] == "recently"
    off = types.UserStatusOffline(was_online=datetime.fromtimestamp(now - 3600, UTC))
    assert classify_last_seen(off, deleted=False, now=now)[0] == "offline_24h"
    off = types.UserStatusOffline(was_online=datetime.fromtimestamp(now - 3 * 86400, UTC))
    assert classify_last_seen(off, deleted=False, now=now)[0] == "last_week"
    assert classify_last_seen(None, deleted=False, now=now)[0] == "hidden"
    assert classify_last_seen(types.UserStatusOnline(expires=None), deleted=True, now=now)[0] == "older"
