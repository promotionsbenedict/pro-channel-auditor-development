from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import AdminAction, AuditJob, CreditWallet, Payment, User
from pca.domain.enums import AuditJobStatus, PaymentStatus, TransactionType, UserRole, UserStatus
from pca.logging_setup import redact_any
from pca.services import ledger
from pca.services.users import get_user_by_telegram_id


class AdminRequiredError(PermissionError):
    pass


class AdminOperationError(Exception):
    pass


def require_admin(user: User | None) -> User:
    if user is None or not user.is_admin:
        raise AdminRequiredError("administrator privileges required")
    return user


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


async def platform_stats(session: AsyncSession, admin: User) -> PlatformStats:
    require_admin(admin)
    since = datetime.now(UTC) - timedelta(days=30)
    total, active, suspended = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(User.last_active_at >= since),
                func.count().filter(User.status == UserStatus.SUSPENDED.value),
            ).select_from(User)
        )
    ).one()
    completed, failed, running = (
        await session.execute(
            select(
                func.count().filter(AuditJob.status == AuditJobStatus.COMPLETED.value),
                func.count().filter(AuditJob.status == AuditJobStatus.FAILED.value),
                func.count().filter(
                    AuditJob.status.in_(
                        [
                            AuditJobStatus.PENDING.value,
                            AuditJobStatus.QUEUED.value,
                            AuditJobStatus.RUNNING.value,
                            AuditJobStatus.WAITING_FOR_TELEGRAM.value,
                        ]
                    )
                ),
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
    available, reserved = (
        await session.execute(
            select(
                func.coalesce(func.sum(CreditWallet.available_credits), 0),
                func.coalesce(func.sum(CreditWallet.reserved_credits), 0),
            )
        )
    ).one()
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
    )


async def set_user_status(
    session: AsyncSession, admin: User, *, telegram_user_id: int, status: UserStatus
) -> User:
    require_admin(admin)
    if status not in (UserStatus.ACTIVE, UserStatus.SUSPENDED):
        raise AdminOperationError("only active/suspended can be set by an administrator")
    target = await get_user_by_telegram_id(session, telegram_user_id)
    if target is None:
        raise AdminOperationError("user not found")
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
    session: AsyncSession, admin: User, *, telegram_user_id: int, credits: int, reason: str
) -> ledger.LedgerResult:
    require_admin(admin)
    if not reason.strip():
        raise AdminOperationError("a reason is required")
    target = await get_user_by_telegram_id(session, telegram_user_id)
    if target is None:
        raise AdminOperationError("user not found")
    result = await ledger.add_credits(
        session,
        user_id=target.id,
        credits=credits,
        tx_type=TransactionType.ADMIN_GRANT,
        reference=f"admin:{uuid.uuid4().hex}",
        actor_user_id=admin.id,
        note=reason,
    )
    await log_action(
        session,
        admin,
        "credits.grant",
        target_id=telegram_user_id,
        metadata={"credits": credits, "reason": reason[:200], "transaction_id": result.transaction.id},
    )
    return result


async def recent_actions(session: AsyncSession, admin: User, limit: int = 10) -> list[AdminAction]:
    require_admin(admin)
    return list(
        (await session.execute(select(AdminAction).order_by(AdminAction.id.desc()).limit(limit))).scalars()
    )
