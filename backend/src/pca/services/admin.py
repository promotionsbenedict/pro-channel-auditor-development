from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.db.models import (
    AdminAction,
    AuditJob,
    Channel,
    CreditPackage,
    CreditTransaction,
    CreditWallet,
    Payment,
    SecurityEvent,
    SupportTicket,
    TelegramConnection,
    User,
)
from pca.domain.enums import (
    AuditJobStatus,
    ConnectionStatus,
    PaymentStatus,
    SupportTicketStatus,
    TransactionType,
    UserRole,
    UserStatus,
)
from pca.logging_setup import redact_any
from pca.services import audit_engine, ledger, reports
from pca.services import payments as payment_service
from pca.services.users import get_user_by_telegram_id

ACTIVE_JOB_STATUSES = (
    AuditJobStatus.PENDING.value,
    AuditJobStatus.QUEUED.value,
    AuditJobStatus.RUNNING.value,
    AuditJobStatus.WAITING_FOR_TELEGRAM.value,
)


class AdminRequiredError(PermissionError):
    pass


class AdminOperationError(Exception):
    pass


def require_admin(user: User | None) -> User:
    if user is None or not user.is_admin:
        raise AdminRequiredError("administrator privileges required")
    return user


def _now() -> datetime:
    return datetime.now(UTC)


async def log_action(
    session: AsyncSession,
    admin: User,
    action: str,
    *,
    target_id: str | int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AdminAction:
    require_admin(admin)
    entry = AdminAction(
        admin_user_id=admin.id,
        action=action,
        target_id=str(target_id) if target_id is not None else None,
        meta=redact_any(metadata or {}),
    )
    session.add(entry)
    await session.flush()
    return entry


# --------------------------------------------------------------------------- overview


@dataclass(frozen=True)
class PlatformStats:
    total_customers: int
    active_30d: int
    suspended: int
    audits_completed: int
    audits_failed: int
    audits_active: int
    revenue_by_currency: dict[str, int]
    credits_available: int
    credits_reserved: int
    audits_partial: int = 0
    audits_cancelled: int = 0
    connected_accounts: int = 0
    credits_sold: int = 0
    credits_consumed: int = 0
    credits_promotional: int = 0
    payments_success: int = 0
    payments_pending: int = 0
    payments_failed: int = 0
    open_tickets: int = 0
    security_events_24h: int = 0


async def platform_stats(session: AsyncSession, admin: User) -> PlatformStats:
    require_admin(admin)
    now = _now()
    since = now - timedelta(days=30)
    total, active, suspended = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(User.last_active_at >= since),
                func.count().filter(User.status == UserStatus.SUSPENDED.value),
            ).select_from(User)
        )
    ).one()
    completed, partial, failed, cancelled, running = (
        await session.execute(
            select(
                func.count().filter(AuditJob.status == AuditJobStatus.COMPLETED.value),
                func.count().filter(AuditJob.status == AuditJobStatus.PARTIAL.value),
                func.count().filter(AuditJob.status == AuditJobStatus.FAILED.value),
                func.count().filter(AuditJob.status == AuditJobStatus.CANCELLED.value),
                func.count().filter(AuditJob.status.in_(ACTIVE_JOB_STATUSES)),
            ).select_from(AuditJob)
        )
    ).one()
    revenue_rows = (
        await session.execute(
            select(Payment.currency, func.sum(Payment.amount))
            .where(Payment.status == PaymentStatus.SUCCESS.value)
            .group_by(Payment.currency)
        )
    ).all()
    pay_ok, pay_pending, pay_failed = (
        await session.execute(
            select(
                func.count().filter(Payment.status == PaymentStatus.SUCCESS.value),
                func.count().filter(Payment.status == PaymentStatus.INITIALIZED.value),
                func.count().filter(
                    Payment.status.in_((PaymentStatus.FAILED.value, PaymentStatus.ABANDONED.value))
                ),
            ).select_from(Payment)
        )
    ).one()
    available, reserved = (
        await session.execute(
            select(
                func.coalesce(func.sum(CreditWallet.available_credits), 0),
                func.coalesce(func.sum(CreditWallet.reserved_credits), 0),
            )
        )
    ).one()
    tx = CreditTransaction
    sold, promo, settled = (
        await session.execute(
            select(
                func.coalesce(func.sum(tx.available_delta).filter(tx.transaction_type == TransactionType.PURCHASE.value), 0),
                func.coalesce(
                    func.sum(tx.available_delta).filter(
                        tx.transaction_type.in_((TransactionType.PROMO.value, TransactionType.ADMIN_GRANT.value))
                    ),
                    0,
                ),
                func.coalesce(func.sum(tx.amount).filter(tx.transaction_type == TransactionType.AUDIT_SETTLE.value), 0),
            )
        )
    ).one()
    connected = (
        await session.execute(
            select(func.count()).where(TelegramConnection.connection_status == ConnectionStatus.ACTIVE.value)
        )
    ).scalar_one()
    tickets = (
        await session.execute(
            select(func.count()).where(SupportTicket.status == SupportTicketStatus.OPEN.value)
        )
    ).scalar_one()
    security = (
        await session.execute(select(func.count()).where(SecurityEvent.created_at >= now - timedelta(hours=24)))
    ).scalar_one()
    return PlatformStats(
        total_customers=total,
        active_30d=active,
        suspended=suspended,
        audits_completed=completed,
        audits_failed=failed,
        audits_active=running,
        revenue_by_currency={currency: int(amount) for currency, amount in revenue_rows},
        credits_available=int(available),
        credits_reserved=int(reserved),
        audits_partial=partial,
        audits_cancelled=cancelled,
        connected_accounts=connected,
        credits_sold=int(sold),
        credits_consumed=abs(int(settled)),
        credits_promotional=int(promo),
        payments_success=pay_ok,
        payments_pending=pay_pending,
        payments_failed=pay_failed,
        open_tickets=tickets,
        security_events_24h=security,
    )


@dataclass(frozen=True)
class SystemHealth:
    database_ok: bool
    queued_jobs: int
    running_jobs: int
    stale_leases: int
    oldest_queued_minutes: int | None
    stuck_payments: int
    wallet_drift_users: list[int] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.database_ok and self.stale_leases == 0 and not self.wallet_drift_users


async def system_health(session: AsyncSession, admin: User, *, drift_sample: int = 50) -> SystemHealth:
    """Operational checks that indicate a stuck worker, stuck payments or ledger drift."""
    require_admin(admin)
    now = _now()
    queued, running, stale, oldest = (
        await session.execute(
            select(
                func.count().filter(
                    AuditJob.status.in_((AuditJobStatus.QUEUED.value, AuditJobStatus.WAITING_FOR_TELEGRAM.value))
                ),
                func.count().filter(AuditJob.status == AuditJobStatus.RUNNING.value),
                func.count().filter(
                    AuditJob.status == AuditJobStatus.RUNNING.value, AuditJob.lease_expires_at < now
                ),
                func.min(AuditJob.created_at).filter(AuditJob.status == AuditJobStatus.QUEUED.value),
            ).select_from(AuditJob)
        )
    ).one()
    stuck_payments = (
        await session.execute(
            select(func.count()).where(
                Payment.status == PaymentStatus.INITIALIZED.value,
                Payment.created_at < now - timedelta(hours=2),
            )
        )
    ).scalar_one()
    drift: list[int] = []
    recent_wallet_users = (
        await session.execute(select(CreditWallet.user_id).order_by(CreditWallet.updated_at.desc()).limit(drift_sample))
    ).scalars()
    for user_id in recent_wallet_users:
        reconciliation = await ledger.reconcile_wallet(session, user_id)
        if not reconciliation.ok:
            drift.append(user_id)
    return SystemHealth(
        database_ok=True,
        queued_jobs=queued,
        running_jobs=running,
        stale_leases=stale,
        oldest_queued_minutes=int((now - oldest).total_seconds() // 60) if oldest else None,
        stuck_payments=stuck_payments,
        wallet_drift_users=drift,
    )


# --------------------------------------------------------------------------- customers


@dataclass(frozen=True)
class CustomerOverview:
    user: User
    available: int
    reserved: int
    active_connections: int
    audits_total: int
    audits_active: int
    payments_success: int
    last_payments: list[Payment]


async def _target(session: AsyncSession, telegram_user_id: int) -> User:
    target = await get_user_by_telegram_id(session, telegram_user_id)
    if target is None:
        raise AdminOperationError("user not found")
    return target


async def customer_overview(session: AsyncSession, admin: User, *, telegram_user_id: int) -> CustomerOverview:
    require_admin(admin)
    target = await _target(session, telegram_user_id)
    wallet = await ledger.get_wallet(session, target.id)
    connections = (
        await session.execute(
            select(func.count()).where(
                TelegramConnection.user_id == target.id,
                TelegramConnection.connection_status == ConnectionStatus.ACTIVE.value,
            )
        )
    ).scalar_one()
    total, active = (
        await session.execute(
            select(func.count(), func.count().filter(AuditJob.status.in_(ACTIVE_JOB_STATUSES))).where(
                AuditJob.user_id == target.id
            )
        )
    ).one()
    paid = (
        await session.execute(
            select(func.count()).where(Payment.user_id == target.id, Payment.status == PaymentStatus.SUCCESS.value)
        )
    ).scalar_one()
    return CustomerOverview(
        user=target,
        available=wallet.available_credits if wallet else 0,
        reserved=wallet.reserved_credits if wallet else 0,
        active_connections=connections,
        audits_total=total,
        audits_active=active,
        payments_success=paid,
        last_payments=await payment_service.payment_history(session, target.id, limit=3),
    )


async def set_user_status(
    session: AsyncSession, admin: User, *, telegram_user_id: int, status: UserStatus
) -> User:
    require_admin(admin)
    if status not in (UserStatus.ACTIVE, UserStatus.SUSPENDED):
        raise AdminOperationError("only active/suspended can be set by an administrator")
    target = await _target(session, telegram_user_id)
    if target.id == admin.id:
        raise AdminOperationError("you cannot change your own status")
    if target.role == UserRole.ADMIN.value and status == UserStatus.SUSPENDED:
        raise AdminOperationError("administrators cannot be suspended; revoke the role first")
    previous = target.status
    target.status = status.value
    await log_action(
        session,
        admin,
        "user.status",
        target_id=telegram_user_id,
        metadata={"from": previous, "to": status.value},
    )
    return target


async def grant_credits(
    session: AsyncSession,
    admin: User,
    *,
    telegram_user_id: int,
    credits: int,
    reason: str,
    promotional: bool = False,
) -> ledger.LedgerResult:
    require_admin(admin)
    if not reason.strip():
        raise AdminOperationError("a reason is required")
    if credits <= 0 or credits > 1_000_000:
        raise AdminOperationError("credits must be between 1 and 1,000,000")
    target = await _target(session, telegram_user_id)
    tx_type = TransactionType.PROMO if promotional else TransactionType.ADMIN_GRANT
    result = await ledger.add_credits(
        session,
        user_id=target.id,
        credits=credits,
        tx_type=tx_type,
        reference=f"admin:{uuid.uuid4().hex}",
        actor_user_id=admin.id,
        note=reason[:255],
    )
    await log_action(
        session,
        admin,
        "credits.promo" if promotional else "credits.grant",
        target_id=telegram_user_id,
        metadata={"credits": credits, "reason": reason[:200], "transaction_id": result.transaction.id},
    )
    return result


async def debit_credits(
    session: AsyncSession, admin: User, *, telegram_user_id: int, credits: int, reason: str
) -> ledger.LedgerResult:
    require_admin(admin)
    if not reason.strip():
        raise AdminOperationError("a reason is required")
    if credits <= 0:
        raise AdminOperationError("credits must be positive")
    target = await _target(session, telegram_user_id)
    try:
        result = await ledger.admin_debit(
            session,
            user_id=target.id,
            credits=credits,
            reference=f"admin:{uuid.uuid4().hex}",
            actor_user_id=admin.id,
            note=reason[:255],
        )
    except ledger.InsufficientCreditsError as exc:
        raise AdminOperationError("the customer does not have that many available credits") from exc
    await log_action(
        session,
        admin,
        "credits.debit",
        target_id=telegram_user_id,
        metadata={"credits": credits, "reason": reason[:200], "transaction_id": result.transaction.id},
    )
    return result


# --------------------------------------------------------------------------- audit jobs


async def failed_audits(session: AsyncSession, admin: User, limit: int = 10) -> list[tuple[AuditJob, Channel, User]]:
    require_admin(admin)
    rows = await session.execute(
        select(AuditJob, Channel, User)
        .join(Channel, Channel.id == AuditJob.channel_id)
        .join(User, User.id == AuditJob.user_id)
        .where(AuditJob.status.in_((AuditJobStatus.FAILED.value, AuditJobStatus.PARTIAL.value)))
        .order_by(AuditJob.id.desc())
        .limit(limit)
    )
    return [tuple(row) for row in rows.all()]  # type: ignore[misc]


async def active_audits(session: AsyncSession, admin: User, limit: int = 15) -> list[tuple[AuditJob, Channel, User]]:
    require_admin(admin)
    rows = await session.execute(
        select(AuditJob, Channel, User)
        .join(Channel, Channel.id == AuditJob.channel_id)
        .join(User, User.id == AuditJob.user_id)
        .where(AuditJob.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(AuditJob.id)
        .limit(limit)
    )
    return [tuple(row) for row in rows.all()]  # type: ignore[misc]


async def _lock_job(session: AsyncSession, job_id: int) -> AuditJob:
    job = (await session.execute(select(AuditJob).where(AuditJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise AdminOperationError("audit not found")
    return job


async def cancel_job(session: AsyncSession, settings: Settings, admin: User, *, job_id: int) -> AuditJob:
    """Stop an audit. Reserved credits are released by finalisation; a running worker stops at its next page."""
    require_admin(admin)
    job = await _lock_job(session, job_id)
    if job.status not in ACTIVE_JOB_STATUSES:
        raise AdminOperationError(f"audit is already {job.status}")
    job.cancel_requested = True
    lease_live = job.lease_expires_at is not None and job.lease_expires_at > _now()
    if job.status != AuditJobStatus.RUNNING.value or not lease_live:
        await audit_engine.finalize(session, settings, job, AuditJobStatus.CANCELLED, "cancelled_by_admin")
    await log_action(session, admin, "audit.cancel", target_id=job_id, metadata={"status": job.status})
    return job


async def retry_job(session: AsyncSession, admin: User, *, job_id: int) -> AuditJob:
    """Make a waiting or stalled audit eligible to run immediately. Finished audits cannot be revived
    because their reservation is already settled or released; the customer starts a new audit instead."""
    require_admin(admin)
    job = await _lock_job(session, job_id)
    now = _now()
    if job.status not in ACTIVE_JOB_STATUSES:
        raise AdminOperationError(f"audit is {job.status}; finished audits cannot be retried")
    if job.cancel_requested:
        raise AdminOperationError("audit has a pending cancellation")
    if job.status == AuditJobStatus.RUNNING.value and job.lease_expires_at and job.lease_expires_at > now:
        raise AdminOperationError("audit is running on a live worker")
    previous = job.status
    job.status = AuditJobStatus.QUEUED.value
    job.not_before = None
    job.locked_by = None
    job.lease_expires_at = None
    await session.flush()
    await log_action(session, admin, "audit.retry", target_id=job_id, metadata={"from": previous})
    return job


async def restore_audit_charge(session: AsyncSession, admin: User, *, job_id: int, reason: str) -> ledger.LedgerResult:
    """Refund an audit that was settled as completed but never reached its disclosed scope.

    Only under-covered completed audits qualify, and the (REFUND, audit) reference makes the
    refund idempotent, so repeating the command cannot credit the customer twice."""
    require_admin(admin)
    if not reason.strip():
        raise AdminOperationError("a reason is required")
    job = await _lock_job(session, job_id)
    if job.status != AuditJobStatus.COMPLETED.value or not job.charged_credits:
        raise AdminOperationError("only a completed, charged audit can be refunded")
    expected = job.expected_requests or 0
    target = min(job.scan_limit, expected) if job.scan_limit and expected else (job.scan_limit or expected)
    if not target or job.scanned_count >= target * reports.MIN_COVERAGE:
        raise AdminOperationError("this audit reached its expected coverage; no automatic refund applies")
    result = await ledger.add_credits(
        session,
        user_id=job.user_id,
        credits=job.charged_credits,
        tx_type=TransactionType.REFUND,
        reference=f"audit-refund:{job.id}",
        actor_user_id=admin.id,
        note=f"Incomplete scan {job.scanned_count}/{target}: {reason}"[:255],
    )
    if result.created:
        await log_action(
            session,
            admin,
            "audit.refund",
            target_id=job_id,
            metadata={"credits": job.charged_credits, "scanned": job.scanned_count, "target": target, "reason": reason[:200]},
        )
    return result


# --------------------------------------------------------------------------- packages


async def list_packages(session: AsyncSession, admin: User) -> list[CreditPackage]:
    require_admin(admin)
    return list(
        (await session.execute(select(CreditPackage).order_by(CreditPackage.active.desc(), CreditPackage.sort_order, CreditPackage.id))).scalars()
    )


async def save_package(
    session: AsyncSession,
    admin: User,
    *,
    package_id: int | None,
    name: str,
    credits: int,
    price_naira: int,
    sort_order: int = 0,
) -> CreditPackage:
    require_admin(admin)
    try:
        package = await payment_service.upsert_package(
            session,
            package_id=package_id,
            name=name,
            credits=credits,
            price_minor=price_naira * 100,
            sort_order=sort_order,
        )
    except payment_service.PaymentError as exc:
        raise AdminOperationError(f"package rejected: {exc.code}") from exc
    await log_action(
        session,
        admin,
        "package.save",
        target_id=package.id,
        metadata={"name": package.name, "credits": credits, "price_naira": price_naira},
    )
    return package


async def disable_package(session: AsyncSession, admin: User, *, package_id: int) -> CreditPackage:
    require_admin(admin)
    try:
        package = await payment_service.deactivate_package(session, package_id)
    except payment_service.PaymentError as exc:
        raise AdminOperationError("package not found") from exc
    await log_action(session, admin, "package.disable", target_id=package_id)
    return package


async def recent_actions(session: AsyncSession, admin: User, limit: int = 10) -> list[AdminAction]:
    require_admin(admin)
    return list(
        (await session.execute(select(AdminAction).order_by(AdminAction.id.desc()).limit(limit))).scalars()
    )
