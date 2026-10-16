from __future__ import annotations

import pytest
from sqlalchemy import select

from pca.db.models import AdminAction, AuditJob, SecurityEvent
from pca.domain.enums import AuditJobStatus
from pca.services import admin as admin_service
from pca.services import ledger, security_events, support
from tests.conftest import make_user
from tests.test_audit_engine import _setup, keyring, settings  # noqa: F401 (fixtures)


async def _admin(sm, tg_id: int = 7001):
    return await make_user(sm, tg_id, admin=True)


async def test_support_ticket_lifecycle_and_limits(sessionmaker):
    customer = await make_user(sessionmaker, 7100)
    admin = await _admin(sessionmaker)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(support.SupportError) as too_long:
            await support.create_ticket(s, customer, "x" * 2001)
        assert too_long.value.code == "too_long"
        with pytest.raises(support.SupportError):
            await support.create_ticket(s, customer, "   ")
        first = await support.create_ticket(s, customer, "  Payment ref PCA-1 not credited  ")
        assert first.message == "Payment ref PCA-1 not credited"
        await support.create_ticket(s, customer, "two")
        await support.create_ticket(s, customer, "three")
        with pytest.raises(support.SupportError):
            await support.create_ticket(s, customer, "four")
        assert await support.open_count(s) == 3

    async with sessionmaker() as s, s.begin():
        found = await support.get_with_user(s, first.id)
        assert found is not None
        ticket, owner = found
        assert owner.id == customer.id
        await support.answer(s, ticket, admin, "Credited now, sorry for the delay.")
    async with sessionmaker() as s:
        assert await support.open_count(s) == 2
        assert all(t.id != first.id for t, _ in await support.list_open(s))


async def test_debit_requires_reason_and_cannot_overdraw(sessionmaker):
    admin = await _admin(sessionmaker)
    customer = await make_user(sessionmaker, 7200)
    async with sessionmaker() as s, s.begin():
        await admin_service.grant_credits(
            s, admin, telegram_user_id=7200, credits=20, reason="welcome", promotional=True
        )
    async with sessionmaker() as s, s.begin():
        with pytest.raises((admin_service.AdminOperationError, ValueError)):
            await admin_service.debit_credits(s, admin, telegram_user_id=7200, credits=5, reason="  ")
    async with sessionmaker() as s, s.begin():
        with pytest.raises((admin_service.AdminOperationError, ledger.LedgerError)):
            await admin_service.debit_credits(s, admin, telegram_user_id=7200, credits=21, reason="refund reversal")
    async with sessionmaker() as s, s.begin():
        await admin_service.debit_credits(s, admin, telegram_user_id=7200, credits=8, reason="chargeback")
    async with sessionmaker() as s:
        assert (await ledger.reconcile_wallet(s, customer.id)).ok
        overview = await admin_service.customer_overview(s, admin, telegram_user_id=7200)
        assert overview is not None
        actions = (await s.execute(select(AdminAction.action).where(AdminAction.admin_user_id == admin.id))).scalars().all()
    assert len(actions) >= 2


async def test_non_admin_is_rejected(sessionmaker):
    customer = await make_user(sessionmaker, 7300)
    async with sessionmaker() as s:
        with pytest.raises(admin_service.AdminRequiredError):
            await admin_service.platform_stats(s, customer)
        with pytest.raises(admin_service.AdminRequiredError):
            await admin_service.debit_credits(s, customer, telegram_user_id=7300, credits=1, reason="x")


async def test_admin_cancel_queued_releases_credits_and_retry_rules(sessionmaker, settings, keyring):
    admin = await _admin(sessionmaker)
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 7400, pending=15)
    async with sessionmaker() as s, s.begin():
        job = await admin_service.retry_job(s, admin, job_id=job_id)
        assert job.status == AuditJobStatus.QUEUED.value
    async with sessionmaker() as s, s.begin():
        job = await admin_service.cancel_job(s, settings, admin, job_id=job_id)
        assert job.status == AuditJobStatus.CANCELLED.value
    async with sessionmaker() as s, s.begin():
        with pytest.raises(admin_service.AdminOperationError):
            await admin_service.retry_job(s, admin, job_id=job_id)
        with pytest.raises(admin_service.AdminOperationError):
            await admin_service.cancel_job(s, settings, admin, job_id=job_id)
    async with sessionmaker() as s:
        assert (await ledger.reconcile_wallet(s, user_id)).ok
        wallet = await ledger.get_wallet(s, user_id)
        assert wallet is not None and wallet.reserved_credits == 0
        assert (await s.get(AuditJob, job_id)).cancel_requested


async def test_stats_and_health_run_on_real_data(sessionmaker, settings, keyring):
    admin = await _admin(sessionmaker)
    await _setup(sessionmaker, settings, keyring, 7500, pending=5)
    async with sessionmaker() as s:
        stats = await admin_service.platform_stats(s, admin)
        health = await admin_service.system_health(s, admin)
    assert stats is not None and health is not None


async def test_security_events_redact_secrets(sessionmaker):
    await security_events.record_event_detached(
        sessionmaker,
        "connect.init_data_rejected",
        severity="nonsense",
        source_ip="203.0.113.9",
        detail={"session": "1BVtsOK-secret", "password": "hunter2", "reason": "bad hash"},
    )
    async with sessionmaker() as s:
        event = (await s.execute(select(SecurityEvent).order_by(SecurityEvent.id.desc()))).scalars().first()
        recent = await security_events.recent_events(s)
    assert event is not None and event.severity == "warning"
    assert "1BVtsOK-secret" not in str(event.detail) and "hunter2" not in str(event.detail)
    assert event.detail.get("reason") == "bad hash"
    assert recent and recent[0].id == event.id
