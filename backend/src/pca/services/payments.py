"""Paystack credit purchases.

Trust model: neither the browser redirect nor the webhook body is proof of payment.
A webhook only *triggers* settlement; credits are granted solely after the server
re-fetches the transaction from Paystack (GET /transaction/verify/:reference) and
the verified amount, currency and reference match the payment row we created.

Settlement locks the payment row (SELECT ... FOR UPDATE) and the ledger entry is keyed
by ("purchase", "payment:<id>"), so concurrent webhooks, callbacks and reconciliation
sweeps can never credit the same payment twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.db.models import CreditPackage, Payment, User
from pca.domain.enums import PaymentStatus, TransactionType
from pca.services import ledger

log = logging.getLogger(__name__)

MAX_OPEN_CHECKOUTS_PER_USER = 5
RECONCILE_BATCH = 50
_FINAL_STATUSES = {PaymentStatus.SUCCESS.value, PaymentStatus.FAILED.value, PaymentStatus.ABANDONED.value}
_PAYSTACK_FAILED = {"failed", "reversed"}
_PAYSTACK_ABANDONED = {"abandoned"}


class PaymentError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class InitializedCheckout:
    authorization_url: str
    access_code: str


@dataclass(frozen=True)
class VerifiedTransaction:
    reference: str
    status: str
    amount: int
    currency: str
    transaction_id: str | None = None
    gateway_response: str | None = None


class PaymentProvider(Protocol):
    async def initialize(
        self, *, email: str, amount: int, currency: str, reference: str, callback_url: str, metadata: dict[str, Any]
    ) -> InitializedCheckout: ...

    async def verify(self, reference: str) -> VerifiedTransaction: ...


class PaystackClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        secret = settings.paystack_secret_key.get_secret_value()
        if not secret:
            raise PaymentError("not_configured", "PCA_PAYSTACK_SECRET_KEY is not set")
        self._client = httpx.AsyncClient(
            base_url=settings.paystack_base_url,
            headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise PaymentError("provider_unreachable", type(exc).__name__) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise PaymentError("provider_bad_response", f"HTTP {response.status_code}") from exc
        if response.status_code >= 400 or not body.get("status"):
            raise PaymentError("provider_rejected", str(body.get("message") or response.status_code)[:200])
        return body.get("data") or {}

    async def initialize(
        self, *, email: str, amount: int, currency: str, reference: str, callback_url: str, metadata: dict[str, Any]
    ) -> InitializedCheckout:
        payload: dict[str, Any] = {
            "email": email,
            "amount": amount,
            "currency": currency,
            "reference": reference,
            "metadata": metadata,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        data = await self._call("POST", "/transaction/initialize", json=payload)
        url = data.get("authorization_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PaymentError("provider_bad_response", "missing authorization_url")
        return InitializedCheckout(authorization_url=url, access_code=str(data.get("access_code") or ""))

    async def verify(self, reference: str) -> VerifiedTransaction:
        data = await self._call("GET", f"/transaction/verify/{reference}")
        return VerifiedTransaction(
            reference=str(data.get("reference") or ""),
            status=str(data.get("status") or "unknown"),
            amount=int(data.get("amount") or 0),
            currency=str(data.get("currency") or ""),
            transaction_id=str(data["id"]) if data.get("id") is not None else None,
            gateway_response=(str(data["gateway_response"])[:64] if data.get("gateway_response") else None),
        )


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Paystack signs the raw request body with HMAC-SHA512 using the secret key."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def callback_url(settings: Settings) -> str:
    if settings.paystack_callback_url:
        return settings.paystack_callback_url
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/") + "/payments/callback"
    return ""


def customer_email(settings: Settings, user: User) -> str:
    if user.billing_email:
        return user.billing_email
    return f"tg{user.telegram_user_id}@{settings.paystack_customer_email_domain}"


def new_reference() -> str:
    return f"pca_{secrets.token_hex(12)}"


async def list_active_packages(session: AsyncSession) -> list[CreditPackage]:
    return list(
        (
            await session.execute(
                select(CreditPackage)
                .where(CreditPackage.active.is_(True))
                .order_by(CreditPackage.sort_order, CreditPackage.id)
            )
        ).scalars()
    )


async def create_checkout(
    session: AsyncSession, settings: Settings, provider: PaymentProvider, *, user: User, package_id: int
) -> Payment:
    """Create a payment row priced from the database, then open a Paystack checkout for it.

    The row is committed before calling Paystack so a crash between the two leaves an
    auditable 'initialized' row that reconciliation will verify and resolve.
    """
    package = await session.get(CreditPackage, package_id)
    if package is None or not package.active:
        raise PaymentError("package_unavailable")

    cutoff = datetime.now(UTC) - timedelta(minutes=settings.payment_pending_expiry_minutes)
    open_count = (
        await session.execute(
            select(func.count())
            .select_from(Payment)
            .where(
                Payment.user_id == user.id,
                Payment.status == PaymentStatus.INITIALIZED.value,
                Payment.created_at >= cutoff,
            )
        )
    ).scalar_one()
    if open_count >= MAX_OPEN_CHECKOUTS_PER_USER:
        raise PaymentError("too_many_open_checkouts")

    payment = Payment(
        user_id=user.id,
        reference=new_reference(),
        amount=package.price_minor,
        currency=package.currency,
        credits=package.credits,
        package_id=package.id,
        status=PaymentStatus.INITIALIZED.value,
        mode=settings.paystack_mode,
    )
    session.add(payment)
    await session.commit()

    try:
        checkout = await provider.initialize(
            email=customer_email(settings, user),
            amount=payment.amount,
            currency=payment.currency,
            reference=payment.reference,
            callback_url=callback_url(settings),
            metadata={"payment_id": payment.id, "package_id": package.id, "credits": payment.credits},
        )
    except PaymentError as exc:
        payment.status = PaymentStatus.FAILED.value
        payment.failure_reason = exc.code[:64]
        await session.commit()
        raise

    payment.authorization_url = checkout.authorization_url[:512]
    await session.commit()
    return payment


@dataclass(frozen=True)
class SettlementOutcome:
    payment: Payment
    status: str
    credited: bool


async def _lock_payment(session: AsyncSession, reference: str) -> Payment | None:
    return (
        await session.execute(
            select(Payment)
            .where(Payment.reference == reference)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def apply_verification(session: AsyncSession, verified: VerifiedTransaction) -> SettlementOutcome | None:
    """Apply an independently verified Paystack result. Idempotent; caller commits."""
    payment = await _lock_payment(session, verified.reference)
    if payment is None:
        return None
    payment.last_checked_at = datetime.now(UTC)
    payment.provider_status = verified.status[:32]

    if payment.status == PaymentStatus.SUCCESS.value:
        return SettlementOutcome(payment, payment.status, credited=False)
    # A payment we marked failed/abandoned can still be credited if Paystack later confirms it (late payment).
    if payment.status in _FINAL_STATUSES and verified.status != "success":
        return SettlementOutcome(payment, payment.status, credited=False)

    if verified.status == "success":
        if verified.amount != payment.amount or verified.currency.upper() != payment.currency.upper():
            payment.status = PaymentStatus.FAILED.value
            payment.failure_reason = "amount_mismatch"
            log.error(
                "paystack amount mismatch",
                extra={"payment_id": payment.id, "expected": payment.amount, "got": verified.amount},
            )
            return SettlementOutcome(payment, payment.status, credited=False)
        result = await ledger.add_credits(
            session,
            user_id=payment.user_id,
            credits=payment.credits,
            tx_type=TransactionType.PURCHASE,
            reference=f"payment:{payment.id}",
            payment_id=payment.id,
            note=f"Paystack {payment.reference}",
        )
        payment.status = PaymentStatus.SUCCESS.value
        payment.failure_reason = None
        payment.verified_at = payment.verified_at or datetime.now(UTC)
        if verified.transaction_id:
            payment.provider_transaction_id = verified.transaction_id[:64]
        return SettlementOutcome(payment, payment.status, credited=result.created)

    if verified.status in _PAYSTACK_FAILED:
        payment.status = PaymentStatus.FAILED.value
        payment.failure_reason = (verified.gateway_response or verified.status)[:64]
    elif verified.status in _PAYSTACK_ABANDONED:
        payment.status = PaymentStatus.ABANDONED.value
    return SettlementOutcome(payment, payment.status, credited=False)


async def verify_and_settle(session: AsyncSession, provider: PaymentProvider, reference: str) -> SettlementOutcome | None:
    verified = await provider.verify(reference)
    if verified.reference and verified.reference != reference:
        raise PaymentError("reference_mismatch")
    verified = VerifiedTransaction(
        reference=reference,
        status=verified.status,
        amount=verified.amount,
        currency=verified.currency,
        transaction_id=verified.transaction_id,
        gateway_response=verified.gateway_response,
    )
    outcome = await apply_verification(session, verified)
    await session.commit()
    return outcome


async def check_user_payment(
    session: AsyncSession, provider: PaymentProvider, *, user: User, payment_id: int
) -> SettlementOutcome:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise PaymentError("not_found")
    if payment.status != PaymentStatus.INITIALIZED.value:
        return SettlementOutcome(payment, payment.status, credited=False)
    outcome = await verify_and_settle(session, provider, payment.reference)
    if outcome is None:
        raise PaymentError("not_found")
    return outcome


@dataclass(frozen=True)
class WebhookResult:
    accepted: bool
    outcome: SettlementOutcome | None = None


async def handle_webhook(
    session: AsyncSession, settings: Settings, provider: PaymentProvider, *, body: bytes, signature: str | None
) -> WebhookResult:
    if not verify_signature(settings.paystack_secret_key.get_secret_value(), body, signature):
        return WebhookResult(accepted=False)
    try:
        event = json.loads(body)
    except ValueError:
        return WebhookResult(accepted=True)
    if not isinstance(event, dict) or event.get("event") not in {"charge.success", "charge.failed"}:
        return WebhookResult(accepted=True)
    data = event.get("data")
    reference = data.get("reference") if isinstance(data, dict) else None
    if not isinstance(reference, str) or not reference.startswith("pca_"):
        return WebhookResult(accepted=True)
    outcome = await verify_and_settle(session, provider, reference)
    return WebhookResult(accepted=True, outcome=outcome)


async def reconcile_pending(
    session: AsyncSession, settings: Settings, provider: PaymentProvider, *, now: datetime | None = None
) -> list[SettlementOutcome]:
    """Verify stale 'initialized' payments; credit late successes, mark the rest abandoned.

    Covers missed webhooks and users who paid but never tapped "check status".
    """
    now = now or datetime.now(UTC)
    recheck_before = now - timedelta(minutes=5)
    expire_before = now - timedelta(minutes=settings.payment_pending_expiry_minutes)
    references = list(
        (
            await session.execute(
                select(Payment.reference)
                .where(
                    Payment.status == PaymentStatus.INITIALIZED.value,
                    Payment.created_at < recheck_before,
                    (Payment.last_checked_at.is_(None)) | (Payment.last_checked_at < recheck_before),
                )
                .order_by(Payment.created_at)
                .limit(RECONCILE_BATCH)
            )
        ).scalars()
    )
    outcomes: list[SettlementOutcome] = []
    for reference in references:
        try:
            outcome = await verify_and_settle(session, provider, reference)
        except PaymentError as exc:
            await session.rollback()
            log.warning("reconcile verify failed", extra={"reference": reference, "code": exc.code})
            outcome = None
            if exc.code != "provider_rejected":
                continue
        if outcome is not None and outcome.status != PaymentStatus.INITIALIZED.value:
            outcomes.append(outcome)
            continue
        payment = await _lock_payment(session, reference)
        if payment is None:
            continue
        if payment.status == PaymentStatus.INITIALIZED.value and payment.created_at < expire_before:
            payment.status = PaymentStatus.ABANDONED.value
            payment.failure_reason = "expired"
            outcomes.append(SettlementOutcome(payment, payment.status, credited=False))
        payment.last_checked_at = now
        await session.commit()
    return outcomes


async def payment_history(session: AsyncSession, user_id: int, limit: int = 10) -> list[Payment]:
    return list(
        (
            await session.execute(
                select(Payment).where(Payment.user_id == user_id).order_by(Payment.created_at.desc()).limit(limit)
            )
        ).scalars()
    )


async def upsert_package(
    session: AsyncSession,
    *,
    package_id: int | None,
    name: str,
    credits: int,
    price_minor: int,
    currency: str = "NGN",
    sort_order: int = 0,
) -> CreditPackage:
    name = name.strip()
    if not name or len(name) > 64:
        raise PaymentError("invalid_name")
    if credits <= 0 or credits > 1_000_000:
        raise PaymentError("invalid_credits")
    if price_minor < 10_000 or price_minor > 100_000_000_00:
        raise PaymentError("invalid_price")
    currency = currency.upper()
    if currency != "NGN":
        raise PaymentError("invalid_currency")
    if package_id is None:
        package = CreditPackage(name=name, credits=credits, price_minor=price_minor, currency=currency, sort_order=sort_order)
        session.add(package)
    else:
        package = await session.get(CreditPackage, package_id)
        if package is None:
            raise PaymentError("not_found")
        package.name, package.credits, package.price_minor, package.sort_order = name, credits, price_minor, sort_order
        package.active = True
    await session.flush()
    return package


async def deactivate_package(session: AsyncSession, package_id: int) -> CreditPackage:
    package = await session.get(CreditPackage, package_id)
    if package is None:
        raise PaymentError("not_found")
    package.active = False
    await session.flush()
    return package
