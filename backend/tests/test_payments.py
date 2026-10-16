from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from pca.config import Settings
from pca.db.models import CreditPackage, CreditTransaction, Payment
from pca.domain.enums import PaymentStatus, TransactionType
from pca.services import ledger, payments
from pca.services.payments import InitializedCheckout, PaymentError, VerifiedTransaction
from tests.conftest import make_user

SECRET = "sk_test_unit"


class FakeProvider:
    def __init__(self) -> None:
        self.results: dict[str, VerifiedTransaction] = {}
        self.initialized: list[dict] = []
        self.verify_calls = 0
        self.fail_initialize = False

    async def initialize(self, **kwargs) -> InitializedCheckout:
        if self.fail_initialize:
            raise PaymentError("provider_unreachable")
        self.initialized.append(kwargs)
        return InitializedCheckout(authorization_url=f"https://checkout.test/{kwargs['reference']}", access_code="ac")

    async def verify(self, reference: str) -> VerifiedTransaction:
        self.verify_calls += 1
        return self.results.get(
            reference, VerifiedTransaction(reference=reference, status="ongoing", amount=0, currency="NGN")
        )

    def pay(self, payment: Payment, *, amount: int | None = None, status: str = "success") -> None:
        self.results[payment.reference] = VerifiedTransaction(
            reference=payment.reference,
            status=status,
            amount=payment.amount if amount is None else amount,
            currency=payment.currency,
            transaction_id=f"tx_{payment.id}",
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", bot_token="1:x", paystack_secret_key=SECRET, _env_file=None)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


async def _package(sm, *, credits: int = 500, price_minor: int = 250_000, active: bool = True) -> int:
    async with sm() as s, s.begin():
        pkg = CreditPackage(name=f"P{credits}", credits=credits, price_minor=price_minor, currency="NGN", active=active)
        s.add(pkg)
        await s.flush()
        return pkg.id


async def _available(sm, user_id: int) -> int:
    async with sm() as s:
        wallet = await ledger.get_wallet(s, user_id)
        return wallet.available_credits if wallet else 0


async def _purchase_count(sm, payment_id: int) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(CreditTransaction)
                .where(CreditTransaction.payment_id == payment_id, CreditTransaction.transaction_type == TransactionType.PURCHASE.value)
            )
        ).scalar_one()


async def _checkout(sm, settings, provider, tg_id: int, **pkg_kwargs):
    user = await make_user(sm, tg_id)
    package_id = await _package(sm, **pkg_kwargs)
    async with sm() as s:
        db_user = await s.merge(user)
        payment = await payments.create_checkout(s, settings, provider, user=db_user, package_id=package_id)
    return user, payment


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def test_signature_verification():
    body = b'{"event":"charge.success"}'
    assert payments.verify_signature(SECRET, body, _sign(body))
    assert not payments.verify_signature(SECRET, body, _sign(body, "other"))
    assert not payments.verify_signature(SECRET, body + b" ", _sign(body))
    assert not payments.verify_signature(SECRET, body, None)
    assert not payments.verify_signature("", body, _sign(body, ""))


async def test_checkout_prices_from_database(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7001, credits=500, price_minor=250_000)
    assert payment.status == PaymentStatus.INITIALIZED.value
    assert payment.amount == 250_000 and payment.credits == 500
    assert payment.authorization_url.endswith(payment.reference)
    assert payment.mode == "test"
    sent = provider.initialized[0]
    assert sent["amount"] == 250_000 and sent["reference"] == payment.reference
    assert sent["email"] == f"tg7001@{settings.paystack_customer_email_domain}"


async def test_inactive_package_rejected(sessionmaker, settings, provider):
    user = await make_user(sessionmaker, 7002)
    package_id = await _package(sessionmaker, active=False)
    async with sessionmaker() as s:
        with pytest.raises(PaymentError) as exc:
            await payments.create_checkout(s, settings, provider, user=await s.merge(user), package_id=package_id)
    assert exc.value.code == "package_unavailable"


async def test_initialize_failure_marks_payment_failed(sessionmaker, settings, provider):
    provider.fail_initialize = True
    user = await make_user(sessionmaker, 7003)
    package_id = await _package(sessionmaker)
    async with sessionmaker() as s:
        with pytest.raises(PaymentError):
            await payments.create_checkout(s, settings, provider, user=await s.merge(user), package_id=package_id)
    async with sessionmaker() as s:
        row = (await s.execute(select(Payment).where(Payment.user_id == user.id))).scalar_one()
    assert row.status == PaymentStatus.FAILED.value and row.failure_reason == "provider_unreachable"


async def test_success_credits_exactly_once(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7004)
    provider.pay(payment)
    for _ in range(3):
        async with sessionmaker() as s:
            await payments.verify_and_settle(s, provider, payment.reference)
    assert await _available(sessionmaker, user.id) == 500
    assert await _purchase_count(sessionmaker, payment.id) == 1


async def test_concurrent_settlement_credits_once(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7005)
    provider.pay(payment)

    async def settle():
        async with sessionmaker() as s:
            return await payments.verify_and_settle(s, provider, payment.reference)

    outcomes = await asyncio.gather(*(settle() for _ in range(6)))
    assert sum(o.credited for o in outcomes) == 1
    assert await _available(sessionmaker, user.id) == 500
    assert await _purchase_count(sessionmaker, payment.id) == 1


async def test_amount_mismatch_never_credits(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7006)
    provider.pay(payment, amount=100)
    async with sessionmaker() as s:
        outcome = await payments.verify_and_settle(s, provider, payment.reference)
    assert outcome.status == PaymentStatus.FAILED.value and not outcome.credited
    assert outcome.payment.failure_reason == "amount_mismatch"
    assert await _available(sessionmaker, user.id) == 0


async def test_failed_then_late_success_is_credited(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7007)
    provider.pay(payment, status="failed")
    async with sessionmaker() as s:
        assert (await payments.verify_and_settle(s, provider, payment.reference)).status == "failed"
    provider.pay(payment)
    async with sessionmaker() as s:
        outcome = await payments.verify_and_settle(s, provider, payment.reference)
    assert outcome.credited and await _available(sessionmaker, user.id) == 500


async def test_webhook_rejects_bad_signature_without_verifying(sessionmaker, settings, provider):
    user, payment = await _checkout(sessionmaker, settings, provider, 7008)
    provider.pay(payment)
    body = json.dumps({"event": "charge.success", "data": {"reference": payment.reference}}).encode()
    async with sessionmaker() as s:
        result = await payments.handle_webhook(s, settings, provider, body=body, signature="bad")
    assert not result.accepted and provider.verify_calls == 0
    assert await _available(sessionmaker, user.id) == 0


async def test_webhook_payload_is_not_trusted(sessionmaker, settings, provider):
    """A validly signed event still only credits what Paystack's verify endpoint confirms."""
    user, payment = await _checkout(sessionmaker, settings, provider, 7009)
    body = json.dumps(
        {"event": "charge.success", "data": {"reference": payment.reference, "amount": payment.amount}}
    ).encode()
    async with sessionmaker() as s:
        result = await payments.handle_webhook(s, settings, provider, body=body, signature=_sign(body))
    assert result.accepted and provider.verify_calls == 1
    assert result.outcome.status == PaymentStatus.INITIALIZED.value
    assert await _available(sessionmaker, user.id) == 0

    provider.pay(payment)
    async with sessionmaker() as s:
        result = await payments.handle_webhook(s, settings, provider, body=body, signature=_sign(body))
    assert result.outcome.credited and await _available(sessionmaker, user.id) == 500


async def test_webhook_ignores_foreign_references(sessionmaker, settings, provider):
    body = json.dumps({"event": "charge.success", "data": {"reference": "someone_else_123"}}).encode()
    async with sessionmaker() as s:
        result = await payments.handle_webhook(s, settings, provider, body=body, signature=_sign(body))
    assert result.accepted and result.outcome is None and provider.verify_calls == 0


async def test_user_cannot_check_another_users_payment(sessionmaker, settings, provider):
    _, payment = await _checkout(sessionmaker, settings, provider, 7010)
    other = await make_user(sessionmaker, 7011)
    async with sessionmaker() as s:
        with pytest.raises(PaymentError) as exc:
            await payments.check_user_payment(s, provider, user=await s.merge(other), payment_id=payment.id)
    assert exc.value.code == "not_found"


async def _age(sm, payment_id: int, minutes: int) -> None:
    async with sm() as s, s.begin():
        await s.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )


async def test_reconcile_credits_missed_webhook_and_expires_stale(sessionmaker, settings, provider):
    user, paid = await _checkout(sessionmaker, settings, provider, 7012)
    _, stale = await _checkout(sessionmaker, settings, provider, 7013)
    _, fresh = await _checkout(sessionmaker, settings, provider, 7014)
    provider.pay(paid)
    await _age(sessionmaker, paid.id, 10)
    await _age(sessionmaker, stale.id, settings.payment_pending_expiry_minutes + 5)

    async with sessionmaker() as s:
        outcomes = await payments.reconcile_pending(s, settings, provider)
    by_ref = {o.payment.reference: o for o in outcomes}
    assert by_ref[paid.reference].credited
    assert by_ref[stale.reference].status == PaymentStatus.ABANDONED.value
    assert fresh.reference not in by_ref
    assert await _available(sessionmaker, user.id) == 500

    async with sessionmaker() as s:
        assert await payments.reconcile_pending(s, settings, provider) == []


async def test_paystack_client_parses_verify():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert request.url.path == "/transaction/verify/pca_abc"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"reference": "pca_abc", "status": "success", "amount": 250000, "currency": "NGN", "id": 42},
            },
        )

    settings = Settings(environment="test", bot_token="1:x", paystack_secret_key=SECRET, _env_file=None)
    client = payments.PaystackClient(settings, transport=httpx.MockTransport(handler))
    try:
        verified = await client.verify("pca_abc")
    finally:
        await client.aclose()
    assert verified.status == "success" and verified.amount == 250000 and verified.transaction_id == "42"
