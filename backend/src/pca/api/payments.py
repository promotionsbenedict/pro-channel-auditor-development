"""Paystack webhook and post-checkout landing page.

The callback page never grants credits on its own; it only triggers the same
server-side verification the webhook uses, then tells the user to return to Telegram.
"""

from __future__ import annotations

import logging
import re
from html import escape as esc

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from pca.domain.enums import PaymentStatus
from pca.services import payments
from pca.services.payments import PaymentError, SettlementOutcome

log = logging.getLogger(__name__)

router = APIRouter()

MAX_WEBHOOK_BYTES = 64 * 1024
_REFERENCE_RE = re.compile(r"^pca_[0-9a-f]{24}$")


async def notify_outcome(request: Request, outcome: SettlementOutcome | None) -> None:
    if outcome is None or not outcome.credited:
        return
    notifier = getattr(request.app.state, "notify_payment", None)
    if notifier is None:
        return
    try:
        await notifier(outcome.payment)
    except Exception:
        log.warning("payment notification failed", exc_info=True)


@router.post("/webhooks/paystack")
async def paystack_webhook(request: Request) -> JSONResponse:
    provider = request.app.state.payment_provider
    if provider is None:
        return JSONResponse({"ok": False}, status_code=503)
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"ok": False}, status_code=413)
    async with request.app.state.sessionmaker() as session:
        try:
            result = await payments.handle_webhook(
                session,
                request.app.state.settings,
                provider,
                body=body,
                signature=request.headers.get("x-paystack-signature"),
            )
        except PaymentError as exc:
            # Non-2xx makes Paystack retry later; reconciliation is the second safety net.
            log.warning("webhook verification failed", extra={"code": exc.code})
            return JSONResponse({"ok": False}, status_code=502)
    if not result.accepted:
        log.warning("paystack webhook rejected: bad signature")
        return JSONResponse({"ok": False}, status_code=401)
    await notify_outcome(request, result.outcome)
    return JSONResponse({"ok": True})


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Pro Channel Auditor</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}}
main{{max-width:420px;background:#1e293b;border-radius:16px;padding:28px;text-align:center}}
h1{{font-size:1.25rem;margin:0 0 12px}}p{{line-height:1.5;color:#cbd5e1;margin:0 0 20px}}
a{{display:inline-block;background:#38bdf8;color:#0f172a;text-decoration:none;font-weight:600;
padding:12px 20px;border-radius:10px}}
</style></head>
<body><main><h1>{title}</h1><p>{message}</p><a href="https://t.me/{bot}">Return to Telegram</a></main></body></html>"""


def _page(title: str, message: str, bot: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=esc(title), message=esc(message), bot=esc(bot.lstrip("@"))))


@router.get("/payments/callback")
async def payment_callback(request: Request, reference: str = "") -> HTMLResponse:
    settings = request.app.state.settings
    bot = settings.bot_username
    provider = request.app.state.payment_provider
    if not _REFERENCE_RE.match(reference) or provider is None:
        return _page("Payment received", "Return to Telegram and open My Wallet to see your balance.", bot)
    outcome: SettlementOutcome | None = None
    async with request.app.state.sessionmaker() as session:
        try:
            outcome = await payments.verify_and_settle(session, provider, reference)
        except PaymentError:
            outcome = None
    await notify_outcome(request, outcome)
    if outcome is not None and outcome.status == PaymentStatus.SUCCESS.value:
        return _page("Payment confirmed", f"{outcome.payment.credits} credits have been added to your wallet.", bot)
    if outcome is not None and outcome.status in {PaymentStatus.FAILED.value, PaymentStatus.ABANDONED.value}:
        return _page("Payment not completed", "No money was taken for credits. You can try again from Buy Credits.", bot)
    return _page(
        "Confirming your payment",
        "We're still waiting for Paystack to confirm this payment. Your credits will appear automatically.",
        bot,
    )
