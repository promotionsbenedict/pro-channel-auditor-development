"""Concurrency-safe prepaid credit ledger.

Every balance change locks the wallet row (SELECT ... FOR UPDATE), writes one
immutable credit_transactions row, and updates the wallet in the same database
transaction. Callers own the transaction boundary and must commit.

Invariant (checked by reconcile_wallet):
    wallet.available_credits == SUM(available_delta)
    wallet.reserved_credits  == SUM(reserved_delta)
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import CreditTransaction, CreditWallet
from pca.domain.enums import TransactionType

_CREDIT_TYPES = {
    TransactionType.PURCHASE,
    TransactionType.ADMIN_GRANT,
    TransactionType.REFUND,
    TransactionType.PROMO,
}


class LedgerError(Exception):
    pass


class InsufficientCreditsError(LedgerError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(f"required {required}, available {available}")
        self.required = required
        self.available = available


class ReservationNotFoundError(LedgerError):
    pass


class AlreadyFinalizedError(LedgerError):
    pass


@dataclass(frozen=True)
class LedgerResult:
    transaction: CreditTransaction
    created: bool


@dataclass(frozen=True)
class Reconciliation:
    available: int
    reserved: int
    ledger_available: int
    ledger_reserved: int

    @property
    def ok(self) -> bool:
        return self.available == self.ledger_available and self.reserved == self.ledger_reserved


def _audit_reference(audit_job_id: int) -> str:
    return f"audit:{audit_job_id}"


def _require_positive_int(value: int, name: str, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")


async def lock_wallet(session: AsyncSession, user_id: int) -> CreditWallet:
    await session.execute(
        pg_insert(CreditWallet).values(user_id=user_id).on_conflict_do_nothing(index_elements=[CreditWallet.user_id])
    )
    return (
        await session.execute(
            select(CreditWallet)
            .where(CreditWallet.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _find(
    session: AsyncSession, wallet_id: int, tx_type: TransactionType, reference: str
) -> CreditTransaction | None:
    return (
        await session.execute(
            select(CreditTransaction).where(
                CreditTransaction.wallet_id == wallet_id,
                CreditTransaction.transaction_type == tx_type.value,
                CreditTransaction.reference == reference,
            )
        )
    ).scalar_one_or_none()


async def _apply(
    session: AsyncSession,
    wallet: CreditWallet,
    *,
    tx_type: TransactionType,
    reference: str,
    available_delta: int,
    reserved_delta: int,
    audit_job_id: int | None = None,
    payment_id: int | None = None,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> CreditTransaction:
    new_available = wallet.available_credits + available_delta
    new_reserved = wallet.reserved_credits + reserved_delta
    if new_available < 0:
        raise InsufficientCreditsError(required=-available_delta, available=wallet.available_credits)
    if new_reserved < 0:
        raise LedgerError("reserved balance would become negative")

    wallet.available_credits = new_available
    wallet.reserved_credits = new_reserved
    tx = CreditTransaction(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        amount=available_delta + reserved_delta,
        available_delta=available_delta,
        reserved_delta=reserved_delta,
        transaction_type=tx_type.value,
        reference=reference,
        audit_job_id=audit_job_id,
        payment_id=payment_id,
        actor_user_id=actor_user_id,
        note=note[:255] if note else None,
    )
    session.add(tx)
    await session.flush()
    return tx


async def add_credits(
    session: AsyncSession,
    *,
    user_id: int,
    credits: int,
    tx_type: TransactionType,
    reference: str,
    payment_id: int | None = None,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> LedgerResult:
    """Idempotently add credits. Re-using a (type, reference) pair returns the original entry."""
    if tx_type not in _CREDIT_TYPES:
        raise ValueError(f"{tx_type} cannot add credits")
    _require_positive_int(credits, "credits")
    wallet = await lock_wallet(session, user_id)
    existing = await _find(session, wallet.id, tx_type, reference)
    if existing is not None:
        return LedgerResult(existing, created=False)
    tx = await _apply(
        session,
        wallet,
        tx_type=tx_type,
        reference=reference,
        available_delta=credits,
        reserved_delta=0,
        payment_id=payment_id,
        actor_user_id=actor_user_id,
        note=note,
    )
    return LedgerResult(tx, created=True)


async def admin_debit(
    session: AsyncSession, *, user_id: int, credits: int, reference: str, actor_user_id: int, note: str
) -> LedgerResult:
    _require_positive_int(credits, "credits")
    wallet = await lock_wallet(session, user_id)
    existing = await _find(session, wallet.id, TransactionType.ADMIN_DEBIT, reference)
    if existing is not None:
        return LedgerResult(existing, created=False)
    tx = await _apply(
        session,
        wallet,
        tx_type=TransactionType.ADMIN_DEBIT,
        reference=reference,
        available_delta=-credits,
        reserved_delta=0,
        actor_user_id=actor_user_id,
        note=note,
    )
    return LedgerResult(tx, created=True)


async def reserve_for_audit(
    session: AsyncSession, *, user_id: int, audit_job_id: int, credits: int
) -> LedgerResult:
    """Move credits from available to reserved for one audit job (at most once)."""
    _require_positive_int(credits, "credits", allow_zero=True)
    wallet = await lock_wallet(session, user_id)
    reference = _audit_reference(audit_job_id)
    existing = await _find(session, wallet.id, TransactionType.AUDIT_RESERVE, reference)
    if existing is not None:
        if existing.reserved_delta != credits:
            raise LedgerError("audit already reserved with a different amount")
        return LedgerResult(existing, created=False)
    tx = await _apply(
        session,
        wallet,
        tx_type=TransactionType.AUDIT_RESERVE,
        reference=reference,
        available_delta=-credits,
        reserved_delta=credits,
        audit_job_id=audit_job_id,
    )
    return LedgerResult(tx, created=True)


async def _finalize(
    session: AsyncSession, *, user_id: int, audit_job_id: int, charge: int, tx_type: TransactionType
) -> LedgerResult:
    wallet = await lock_wallet(session, user_id)
    reference = _audit_reference(audit_job_id)
    reservation = await _find(session, wallet.id, TransactionType.AUDIT_RESERVE, reference)
    if reservation is None:
        raise ReservationNotFoundError(reference)

    finalized = (
        await session.execute(
            select(CreditTransaction).where(
                CreditTransaction.audit_job_id == audit_job_id,
                CreditTransaction.transaction_type.in_(
                    [TransactionType.AUDIT_SETTLE.value, TransactionType.AUDIT_RELEASE.value]
                ),
            )
        )
    ).scalar_one_or_none()
    if finalized is not None:
        if finalized.transaction_type == tx_type.value and (-finalized.amount) == charge:
            return LedgerResult(finalized, created=False)
        raise AlreadyFinalizedError(f"audit {audit_job_id} already finalized as {finalized.transaction_type}")

    reserved_amount = reservation.reserved_delta
    if charge < 0 or charge > reserved_amount:
        raise ValueError("charge must be between 0 and the reserved amount")
    tx = await _apply(
        session,
        wallet,
        tx_type=tx_type,
        reference=reference,
        available_delta=reserved_amount - charge,
        reserved_delta=-reserved_amount,
        audit_job_id=audit_job_id,
    )
    return LedgerResult(tx, created=True)


async def settle_audit(session: AsyncSession, *, user_id: int, audit_job_id: int, charge: int) -> LedgerResult:
    """Charge up to the reserved amount exactly once; any unused reservation returns to available."""
    _require_positive_int(charge, "charge", allow_zero=True)
    return await _finalize(
        session, user_id=user_id, audit_job_id=audit_job_id, charge=charge, tx_type=TransactionType.AUDIT_SETTLE
    )


async def release_audit(session: AsyncSession, *, user_id: int, audit_job_id: int) -> LedgerResult:
    """Return the full reservation to available (failed / cancelled-before-work audits)."""
    return await _finalize(
        session, user_id=user_id, audit_job_id=audit_job_id, charge=0, tx_type=TransactionType.AUDIT_RELEASE
    )


async def get_wallet(session: AsyncSession, user_id: int) -> CreditWallet | None:
    return (await session.execute(select(CreditWallet).where(CreditWallet.user_id == user_id))).scalar_one_or_none()


async def recent_transactions(session: AsyncSession, user_id: int, limit: int = 5) -> list[CreditTransaction]:
    return list(
        (
            await session.execute(
                select(CreditTransaction)
                .where(CreditTransaction.user_id == user_id)
                .order_by(CreditTransaction.id.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def reconcile_wallet(session: AsyncSession, user_id: int) -> Reconciliation:
    wallet = await get_wallet(session, user_id)
    sums = (
        await session.execute(
            select(
                func.coalesce(func.sum(CreditTransaction.available_delta), 0),
                func.coalesce(func.sum(CreditTransaction.reserved_delta), 0),
            ).where(CreditTransaction.user_id == user_id)
        )
    ).one()
    return Reconciliation(
        available=wallet.available_credits if wallet else 0,
        reserved=wallet.reserved_credits if wallet else 0,
        ledger_available=int(sums[0]),
        ledger_reserved=int(sums[1]),
    )
