from __future__ import annotations

import pytest
from sqlalchemy import select

from pca.config import Settings
from pca.db.models import AuditJob, CreditTransaction
from pca.domain.enums import AuditJobStatus, TransactionType
from pca.services import admin as admin_service
from pca.services import reports
from pca.telegram.types import JoinRequestPage, ScanCursor
from tests.conftest import make_user
from tests.test_audit_engine import FakeScan, _claim, _run, _setup, _wallet, keyring  # noqa: F401


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        bot_token="1:x",
        audit_page_size=10,
        audit_page_delay_seconds=0,
        audit_flood_inline_max_seconds=5,
        audit_stall_confirmations=3,
        audit_stall_backoff_seconds=1,
        _env_file=None,
    )


class ShortPageScan(FakeScan):
    """Telegram returning fewer rows than requested although more remain."""

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor, limit):
        return await super().fetch_join_requests(session, channel_id, access_hash, cursor, min(limit, 3))


class TransientEmptyScan(FakeScan):
    """One empty page in the middle of the list, then normal service."""

    def __init__(self, total: int, empty_calls: set[int]) -> None:
        super().__init__(total)
        self.empty_calls = empty_calls

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor, limit):
        page = await super().fetch_join_requests(session, channel_id, access_hash, cursor, limit)
        if self.calls in self.empty_calls:
            return JoinRequestPage(total=self.total, items=[], next_cursor=None)
        return page


class TruncatedScan(FakeScan):
    """Serves only `available` rows, then empty pages forever while still reporting `total`."""

    def __init__(self, total: int, available: int) -> None:
        super().__init__(total)
        self.available = available

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor, limit):
        saved, self.total = self.total, self.available
        try:
            page = await super().fetch_join_requests(session, channel_id, access_hash, cursor, limit)
        finally:
            self.total = saved
        # Like the real gateway: the cursor follows the last row of any non-empty page, and only
        # an empty page signals the end of the list.
        nxt = None
        if page.items:
            last = page.items[-1].user_id
            nxt = ScanCursor(1_700_000_000 - last, last, 1)
        return JoinRequestPage(total=saved, items=page.items, next_cursor=nxt)


async def _run_job(sm, settings, keyring, gw, tg_id, pending):
    user_id, job_id = await _setup(sm, settings, keyring, tg_id, pending=pending)
    assert await _claim(sm, settings) == job_id
    await _run(sm, settings, keyring, gw, job_id)
    async with sm() as s:
        return user_id, job_id, await s.get(AuditJob, job_id)


async def test_short_pages_do_not_end_the_scan(sessionmaker, settings, keyring):
    _, _, job = await _run_job(sessionmaker, settings, keyring, ShortPageScan(35), 9301, 35)
    assert job.status == AuditJobStatus.COMPLETED.value
    assert job.scanned_count == 35


async def test_transient_empty_page_is_retried(sessionmaker, settings, keyring):
    gw = TransientEmptyScan(35, empty_calls={2})
    _, _, job = await _run_job(sessionmaker, settings, keyring, gw, 9302, 35)
    assert job.status == AuditJobStatus.COMPLETED.value
    assert job.scanned_count == 35
    assert job.charged_credits == 5


async def test_premature_end_is_partial_and_uncharged(sessionmaker, settings, keyring):
    user_id, _, job = await _run_job(sessionmaker, settings, keyring, TruncatedScan(300, 199), 9303, 300)
    assert job.status == AuditJobStatus.PARTIAL.value
    assert job.error_code == "incomplete_scan"
    assert job.scanned_count == 199
    assert not job.charged_credits
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)


async def test_ninety_seven_percent_is_no_longer_complete(sessionmaker, settings, keyring):
    _, _, job = await _run_job(sessionmaker, settings, keyring, TruncatedScan(100, 97), 9304, 100)
    assert job.status == AuditJobStatus.PARTIAL.value
    assert job.error_code == "incomplete_scan"


async def test_ninety_nine_percent_with_confirmed_end_completes(sessionmaker, settings, keyring):
    # 693 of 700 read (99%) once the list end is confirmed by empty pages.
    _, _, job = await _run_job(sessionmaker, settings, keyring, TruncatedScan(700, 693), 9307, 700)
    assert job.status == AuditJobStatus.COMPLETED.value


class WithdrawingScan(FakeScan):
    """Requests are withdrawn during the scan: the live total reported shrinks."""

    def __init__(self, total: int, live_total: int) -> None:
        super().__init__(live_total)
        self.quoted = total

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor, limit):
        page = await super().fetch_join_requests(session, channel_id, access_hash, cursor, limit)
        return JoinRequestPage(total=self.total, items=page.items, next_cursor=page.next_cursor)


async def test_withdrawn_requests_judged_against_live_total(sessionmaker, settings, keyring):
    # 100 quoted at start, 40 withdrawn mid-scan: all 60 remaining were read.
    _, _, job = await _run_job(sessionmaker, settings, keyring, WithdrawingScan(100, 60), 9308, 100)
    assert job.status == AuditJobStatus.COMPLETED.value
    assert job.scanned_count == 60


class StuckCursorScan(FakeScan):
    """Always returns the same full page of already-seen rows: a stuck cursor, not an end."""

    async def fetch_join_requests(self, session, channel_id, access_hash, cursor, limit):
        self.calls += 1
        page = await FakeScan(self.total).fetch_join_requests(session, channel_id, access_hash, ScanCursor(0, 0, 0), limit)
        return page


async def test_stuck_full_page_cursor_ends_without_infinite_loop(sessionmaker, settings, keyring):
    gw = StuckCursorScan(500)
    user_id, _, job = await _run_job(sessionmaker, settings, keyring, gw, 9309, 500)
    assert job.status == AuditJobStatus.PARTIAL.value
    assert job.error_code == "pagination_stalled"
    assert gw.calls <= 3 + settings.audit_stall_confirmations + 1
    assert not job.charged_credits
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)


def _view(**overrides) -> reports.ReportView:
    from datetime import UTC, datetime

    base = dict(
        job_id=1,
        channel_title="Chan",
        channel_username=None,
        status="completed",
        error_code=None,
        created_at=datetime.now(UTC),
        completed_at=None,
        expected=20636,
        scan_limit=None,
        scanned=199,
        flood_waits=0,
        charged_credits=11,
        summary={"scanned": 199},
    )
    base.update(overrides)
    return reports.ReportView(**base)


def test_report_never_calls_a_199_of_20636_scan_complete():
    view = _view()
    assert view.status_label == "Incomplete scan"
    text = reports.status_explanation(view)
    assert "199 of 20,636" in text
    assert "not a complete audit" in text
    assert "restored" in text


def test_report_complete_within_disclosed_limit_is_complete():
    view = _view(scan_limit=5000, scanned=5000)
    assert view.status_label == "Completed"
    assert "disclosed scan limit" in reports.status_explanation(view)


async def test_refund_for_incomplete_charged_audit_is_idempotent(sessionmaker, settings, keyring):
    user_id, job_id, _ = await _run_job(sessionmaker, settings, keyring, FakeScan(35), 9305, 35)
    admin = await make_user(sessionmaker, 9399, admin=True)
    async with sessionmaker() as s, s.begin():
        job = await s.get(AuditJob, job_id)
        job.scanned_count = 3  # simulate the historical false "completed" audit
    before = (await _wallet(sessionmaker, user_id)).available_credits

    for _ in range(2):
        async with sessionmaker() as s, s.begin():
            await admin_service.restore_audit_charge(s, admin, job_id=job_id, reason="short scan")

    after = (await _wallet(sessionmaker, user_id)).available_credits
    assert after == before + 5
    async with sessionmaker() as s:
        refunds = (
            await s.execute(
                select(CreditTransaction).where(
                    CreditTransaction.user_id == user_id, CreditTransaction.transaction_type == TransactionType.REFUND.value
                )
            )
        ).scalars().all()
    assert len(refunds) == 1


async def test_refund_refused_for_fully_scanned_audit(sessionmaker, settings, keyring):
    _, job_id, _ = await _run_job(sessionmaker, settings, keyring, FakeScan(35), 9306, 35)
    admin = await make_user(sessionmaker, 9398, admin=True)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(admin_service.AdminOperationError):
            await admin_service.restore_audit_charge(s, admin, job_id=job_id, reason="x")


def test_scan_cursor_import_is_used():
    assert ScanCursor(0, 0, 0).as_dict()


# --------------------------------------------------------------------------- resilience

from datetime import UTC, datetime, timedelta  # noqa: E402

from sqlalchemy import func  # noqa: E402

from pca.services import audit_engine as eng  # noqa: E402
from pca.telegram.errors import TelegramGatewayError  # noqa: E402


async def _release_count(sm, job_id: int) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count()).where(
                    CreditTransaction.audit_job_id == job_id,
                    CreditTransaction.transaction_type == TransactionType.AUDIT_RELEASE.value,
                )
            )
        ).scalar_one()


async def _settle_count(sm, job_id: int) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count()).where(
                    CreditTransaction.audit_job_id == job_id,
                    CreditTransaction.transaction_type == TransactionType.AUDIT_SETTLE.value,
                )
            )
        ).scalar_one()


async def _resume(sm, settings, keyring, gw, job_id, worker="w2"):
    async with sm() as s, s.begin():
        (await s.get(AuditJob, job_id)).not_before = datetime.now(UTC) - timedelta(seconds=1)
    assert await _claim(sm, settings, worker) == job_id
    await _run(sm, settings, keyring, gw, job_id, worker)


async def test_inline_flood_wait_resumes_without_losing_progress(sessionmaker, settings, keyring):
    gw = FakeScan(35, fail_at_call={3: TelegramGatewayError("flood", retry_after=2)})
    _, _, job = await _run_job(sessionmaker, settings, keyring, gw, 9401, 35)
    assert job.status == AuditJobStatus.COMPLETED.value
    assert job.scanned_count == 35
    assert job.flood_waits == 1
    assert job.paused_seconds >= 2, "flood wait is excluded from the run-time budget"


@pytest.mark.parametrize("code", ["network", "telegram_unavailable"])
async def test_transient_errors_park_and_resume_from_checkpoint(sessionmaker, settings, keyring, code):
    tg_id = 9402 if code == "network" else 9403
    user_id, job_id = await _setup(sessionmaker, settings, keyring, tg_id, pending=35)
    gw = FakeScan(35, fail_at_call={3: TelegramGatewayError(code)})
    await _claim(sessionmaker, settings)
    await _run(sessionmaker, settings, keyring, gw, job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.WAITING_FOR_TELEGRAM.value
        assert job.scanned_count == 19 and job.checkpoint is not None
        parked_checkpoint = dict(job.checkpoint)
    assert await _release_count(sessionmaker, job_id) == 0, "credits stay reserved while waiting"
    await _resume(sessionmaker, settings, keyring, gw, job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        assert job.scanned_count == 35
    assert parked_checkpoint["offset_user_id"] == 19
    assert await _settle_count(sessionmaker, job_id) == 1


async def test_worker_crash_resumes_from_persisted_checkpoint(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9404, pending=35)
    gw = FakeScan(35, fail_at_call={3: RuntimeError("boom")})
    await _claim(sessionmaker, settings, "dead")
    await _run(sessionmaker, settings, keyring, gw, job_id, "dead")
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.WAITING_FOR_TELEGRAM.value
        assert job.scanned_count == 19
    await _resume(sessionmaker, settings, keyring, gw, job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        assert job.scanned_count == 35, "no rows lost or duplicated across the restart"
    assert await _settle_count(sessionmaker, job_id) == 1


async def test_lease_expiry_restart_keeps_checkpoint_and_progress(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9405, pending=35)
    await _claim(sessionmaker, settings, "dead")
    async with sessionmaker() as s, s.begin():
        await eng.run_step(s, settings, keyring, FakeScan(35), job_id, "dead")
        await eng.run_step(s, settings, keyring, FakeScan(35), job_id, "dead")
    async with sessionmaker() as s, s.begin():
        (await s.get(AuditJob, job_id)).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await _claim(sessionmaker, settings, "w2") == job_id
    async with sessionmaker() as s:
        assert (await s.get(AuditJob, job_id)).scanned_count > 0
    await _run(sessionmaker, settings, keyring, FakeScan(35), job_id, "w2")
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value and job.scanned_count == 35


async def test_long_scan_with_long_flood_waits_is_not_cut_by_duration_limit(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9406, pending=25)
    await _claim(sessionmaker, settings)
    limit = settings.audit_max_duration_seconds
    async with sessionmaker() as s, s.begin():
        job = await s.get(AuditJob, job_id)
        # Started well past the limit in wall time, but nearly all of it was Telegram-imposed waiting.
        job.started_at = datetime.now(UTC) - timedelta(seconds=limit + 7200)
        job.paused_seconds = limit + 7200 - 60
    await _run(sessionmaker, settings, keyring, FakeScan(25), job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        assert job.scanned_count == 25


async def test_active_time_over_limit_stops_and_releases(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9407, pending=25)
    await _claim(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        job = await s.get(AuditJob, job_id)
        job.started_at = datetime.now(UTC) - timedelta(seconds=settings.audit_max_duration_seconds + 600)
    await _run(sessionmaker, settings, keyring, FakeScan(25), job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.error_code == "time_limit"
        assert job.status == AuditJobStatus.FAILED.value  # nothing scanned yet
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)


async def test_wall_clock_ceiling_stops_even_when_all_time_was_paused(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9408, pending=25)
    await _claim(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        job = await s.get(AuditJob, job_id)
        job.started_at = datetime.now(UTC) - timedelta(seconds=settings.audit_max_wall_seconds + 60)
        job.paused_seconds = settings.audit_max_wall_seconds
    await _run(sessionmaker, settings, keyring, FakeScan(25), job_id)
    async with sessionmaker() as s:
        assert (await s.get(AuditJob, job_id)).error_code == "time_limit"
    assert await _release_count(sessionmaker, job_id) == 1


async def test_stalled_pagination_backs_off_instead_of_spinning(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9409, pending=500)
    await _claim(sessionmaker, settings)
    sleeps: list[float] = []

    async def record(seconds):
        sleeps.append(seconds)

    gw = StuckCursorScan(500)
    await eng.run_job(sessionmaker, settings, keyring, gw, job_id, "w1", eng.NullNotifier(), sleep=record)
    assert all(s >= 1 for s in sleeps), "every stalled retry waits at least a second"
    stall_sleeps = sleeps[-(settings.audit_stall_confirmations - 1):]
    assert stall_sleeps == sorted(stall_sleeps) and len(set(stall_sleeps)) == len(stall_sleeps), "backoff grows"
    assert gw.calls <= 3 + settings.audit_stall_confirmations + 1, "bounded number of attempts"
    async with sessionmaker() as s:
        assert (await s.get(AuditJob, job_id)).error_code == "pagination_stalled"


async def test_endless_flood_waits_are_bounded_and_wait_each_time(sessionmaker, settings, keyring):
    settings = settings.model_copy(update={"audit_max_consecutive_floods": 3})
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9410, pending=25)
    await _claim(sessionmaker, settings)
    sleeps: list[float] = []

    async def record(seconds):
        sleeps.append(seconds)

    always_flood = {i: TelegramGatewayError("flood", retry_after=2) for i in range(1, 50)}
    gw = FakeScan(25, fail_at_call=always_flood)
    await eng.run_job(sessionmaker, settings, keyring, gw, job_id, "w1", eng.NullNotifier(), sleep=record)
    assert gw.calls == 4
    assert all(s >= 3 for s in sleeps), "each flood wait is honoured (retry_after + 1)"
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.error_code == "flood_limit"
    assert await _release_count(sessionmaker, job_id) == 1


async def test_incomplete_audit_releases_reserved_credits_exactly_once(sessionmaker, settings, keyring):
    user_id, _, job = await _run_job(sessionmaker, settings, keyring, TruncatedScan(300, 199), 9411, 300)
    assert job.status == AuditJobStatus.PARTIAL.value
    assert await _release_count(sessionmaker, job.id) == 1
    assert await _settle_count(sessionmaker, job.id) == 0
    async with sessionmaker() as s, s.begin():
        again = await s.get(AuditJob, job.id)
        await eng.finalize(s, settings, again, AuditJobStatus.FAILED, "internal_error")
        await eng.finalize(s, settings, again, AuditJobStatus.COMPLETED)
    assert await _release_count(sessionmaker, job.id) == 1, "re-finalising never releases twice"
    assert await _settle_count(sessionmaker, job.id) == 0
    w = await _wallet(sessionmaker, user_id)
    assert (w.available_credits, w.reserved_credits) == (100, 0)
