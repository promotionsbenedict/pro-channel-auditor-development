from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from pca.domain.enums import TransactionType
from pca.services import ledger

from .conftest import make_audit_job, make_user


async def _grant(sm, user_id: int, credits: int, reference: str) -> ledger.LedgerResult:
    async with sm() as s, s.begin():
        return await ledger.add_credits(
            s, user_id=user_id, credits=credits, tx_type=TransactionType.PURCHASE, reference=reference
        )


async def _wallet(sm, user_id: int):
    async with sm() as s:
        return await ledger.get_wallet(s, user_id)


async def test_purchase_is_idempotent_by_reference(sessionmaker):
    user = await make_user(sessionmaker, 1)
    first = await _grant(sessionmaker, user.id, 50, "pay:abc")
    second = await _grant(sessionmaker, user.id, 50, "pay:abc")
    assert first.created and not second.created
    assert first.transaction.id == second.transaction.id
    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (50, 0)


async def test_concurrent_duplicate_purchase_credits_once(sessionmaker):
    user = await make_user(sessionmaker, 2)
    results = await asyncio.gather(*[_grant(sessionmaker, user.id, 30, "pay:race") for _ in range(8)])
    assert sum(r.created for r in results) == 1
    assert (await _wallet(sessionmaker, user.id)).available_credits == 30


@pytest.mark.parametrize("bad", [0, -5, 2.5, True])
async def test_rejects_non_positive_or_non_integer_credits(sessionmaker, bad):
    user = await make_user(sessionmaker, 3)
    with pytest.raises(ValueError):
        await _grant(sessionmaker, user.id, bad, f"pay:{bad}")


async def test_debit_types_cannot_be_used_to_add(sessionmaker):
    user = await make_user(sessionmaker, 4)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ValueError):
            await ledger.add_credits(
                s, user_id=user.id, credits=5, tx_type=TransactionType.AUDIT_RESERVE, reference="x"
            )


async def test_reserve_requires_sufficient_balance(sessionmaker):
    user = await make_user(sessionmaker, 5)
    await _grant(sessionmaker, user.id, 10, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ledger.InsufficientCreditsError) as exc:
            await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=11)
    assert exc.value.required == 11 and exc.value.available == 10
    assert (await _wallet(sessionmaker, user.id)).available_credits == 10


async def test_reserve_settle_partial_charge_returns_remainder(sessionmaker):
    user = await make_user(sessionmaker, 6)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)

    async with sessionmaker() as s, s.begin():
        await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=40)
    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (60, 40)

    async with sessionmaker() as s, s.begin():
        result = await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=25)
    assert result.created and result.transaction.amount == -25
    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (75, 0)

    async with sessionmaker() as s:
        assert (await ledger.reconcile_wallet(s, user.id)).ok


async def test_reserve_is_idempotent_per_audit(sessionmaker):
    user = await make_user(sessionmaker, 7)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)
    for _ in range(3):
        async with sessionmaker() as s, s.begin():
            await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=40)
    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (60, 40)


async def test_finalization_happens_exactly_once(sessionmaker):
    user = await make_user(sessionmaker, 8)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)
    async with sessionmaker() as s, s.begin():
        await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=40)

    async with sessionmaker() as s, s.begin():
        await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=40)
    async with sessionmaker() as s, s.begin():
        replay = await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=40)
    assert not replay.created

    async with sessionmaker() as s, s.begin():
        with pytest.raises(ledger.AlreadyFinalizedError):
            await ledger.release_audit(s, user_id=user.id, audit_job_id=job_id)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ledger.AlreadyFinalizedError):
            await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=10)

    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (60, 0)


async def test_concurrent_settle_and_release_finalize_once(sessionmaker):
    user = await make_user(sessionmaker, 9)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)
    async with sessionmaker() as s, s.begin():
        await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=40)

    async def settle():
        async with sessionmaker() as s, s.begin():
            return await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=40)

    async def release():
        async with sessionmaker() as s, s.begin():
            return await ledger.release_audit(s, user_id=user.id, audit_job_id=job_id)

    outcomes = await asyncio.gather(settle(), release(), return_exceptions=True)
    assert sum(isinstance(o, ledger.LedgerResult) for o in outcomes) == 1
    assert sum(isinstance(o, ledger.AlreadyFinalizedError) for o in outcomes) == 1
    async with sessionmaker() as s:
        rec = await ledger.reconcile_wallet(s, user.id)
    assert rec.ok and rec.reserved == 0


async def test_settle_cannot_exceed_reservation(sessionmaker):
    user = await make_user(sessionmaker, 10)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_id = await make_audit_job(sessionmaker, user.id)
    async with sessionmaker() as s, s.begin():
        await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=40)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ValueError):
            await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=41)


async def test_settle_without_reservation_fails(sessionmaker):
    user = await make_user(sessionmaker, 11)
    job_id = await make_audit_job(sessionmaker, user.id)
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ledger.ReservationNotFoundError):
            await ledger.settle_audit(s, user_id=user.id, audit_job_id=job_id, charge=0)


async def test_concurrent_reservations_never_overspend(sessionmaker):
    user = await make_user(sessionmaker, 12)
    await _grant(sessionmaker, user.id, 100, "pay:1")
    job_ids = [await make_audit_job(sessionmaker, user.id, -2000 - i) for i in range(6)]

    async def reserve(job_id):
        async with sessionmaker() as s, s.begin():
            return await ledger.reserve_for_audit(s, user_id=user.id, audit_job_id=job_id, credits=30)

    outcomes = await asyncio.gather(*[reserve(j) for j in job_ids], return_exceptions=True)
    assert sum(isinstance(o, ledger.LedgerResult) for o in outcomes) == 3
    assert all(isinstance(o, ledger.LedgerResult | ledger.InsufficientCreditsError) for o in outcomes)
    wallet = await _wallet(sessionmaker, user.id)
    assert (wallet.available_credits, wallet.reserved_credits) == (10, 90)


async def test_admin_debit_cannot_go_negative(sessionmaker):
    user = await make_user(sessionmaker, 13)
    admin = await make_user(sessionmaker, 99, admin=True)
    await _grant(sessionmaker, user.id, 5, "pay:1")
    async with sessionmaker() as s, s.begin():
        with pytest.raises(ledger.InsufficientCreditsError):
            await ledger.admin_debit(
                s, user_id=user.id, credits=6, reference="d1", actor_user_id=admin.id, note="test"
            )


async def test_database_rejects_negative_wallet(sessionmaker):
    user = await make_user(sessionmaker, 14)
    async with sessionmaker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text("UPDATE credit_wallets SET available_credits = -1 WHERE user_id = :u"), {"u": user.id}
            )


async def test_reconcile_detects_tampering(sessionmaker):
    user = await make_user(sessionmaker, 15)
    await _grant(sessionmaker, user.id, 20, "pay:1")
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE credit_wallets SET available_credits = 999 WHERE user_id = :u"), {"u": user.id})
    async with sessionmaker() as s:
        rec = await ledger.reconcile_wallet(s, user.id)
    assert not rec.ok and rec.ledger_available == 20
