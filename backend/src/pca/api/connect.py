"""HTTPS endpoints behind the account-connection Mini App.

Every API call carries the Mini App's signed initData in a header, so a leaked
link alone is useless: the flow only advances for the Telegram user it was
issued to. Responses carry stable error codes; the page maps them to text.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pca.config import Settings
from pca.services.security_events import record_event_detached
from pca.security.webapp import InitDataError, WebAppIdentity, verify_init_data
from pca.services import connect as flow

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
CONNECT_CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org"
)
_PAGE_HEADERS = {"Content-Security-Policy": CONNECT_CSP, "Referrer-Policy": "no-referrer"}
_GENERAL_LIMIT_PER_HOUR = 600

router = APIRouter()


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=128)


class PhoneBody(TokenBody):
    phone: str = Field(min_length=5, max_length=32)


class CodeBody(TokenBody):
    code: str = Field(min_length=4, max_length=16)


class PasswordBody(TokenBody):
    srp_id: str = Field(min_length=1, max_length=21)
    a: str = Field(min_length=512, max_length=512)
    m1: str = Field(min_length=64, max_length=64)


def _error(code: str, status: int, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse({"ok": False, "error": code, "retry_after": retry_after}, status_code=status, headers=headers)


def _client_ip(request: Request) -> str:
    # Caddy on this host is the only ingress; it sets X-Forwarded-For with the real client last-hop.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


async def _over_limit(request: Request, bucket: str, limit: int) -> bool:
    redis = request.app.state.redis
    key = f"pca:connect:{bucket}:{_client_ip(request)}:{int(time.time() // 3600)}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 3700)
    except Exception:
        log.warning("rate limiter unavailable", exc_info=True)
        return False
    return count > limit


def _origin_ok(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin or not settings.public_base_url:
        return True
    expected = urlsplit(settings.public_base_url)
    got = urlsplit(origin)
    return (got.scheme, got.netloc) == (expected.scheme, expected.netloc)


Step = Callable[[AsyncSession, WebAppIdentity], Awaitable[dict[str, Any]]]


async def _run(request: Request, step: Step, *, code_sending: bool = False) -> JSONResponse:
    state = request.app.state
    settings: Settings = state.settings
    if not settings.connect_enabled:
        return _error("connect_disabled", 503)
    if not _origin_ok(request, settings):
        return _error("bad_origin", 403)
    if await _over_limit(request, "all", _GENERAL_LIMIT_PER_HOUR):
        return _error("rate_limited", 429, 3600)
    if code_sending and await _over_limit(request, "codes", settings.connect_max_starts_per_ip_hour):
        return _error("rate_limited", 429, 3600)
    try:
        identity = verify_init_data(
            request.headers.get(INIT_DATA_HEADER, ""),
            settings.bot_token.get_secret_value(),
            max_age_seconds=settings.connect_init_data_max_age_seconds,
        )
    except InitDataError as exc:
        await record_event_detached(
            state.sessionmaker,
            "connect.init_data_rejected",
            severity="warning",
            source_ip=_client_ip(request),
            detail={"reason": str(exc)[:80]},
        )
        return _error("unauthorized", 401)

    async with state.sessionmaker() as session:
        try:
            result = await step(session, identity)
            await session.commit()
        except flow.ConnectError as exc:
            await session.commit()
            return _error(exc.code, exc.status, exc.retry_after)
        except Exception:
            await session.rollback()
            log.exception("connect step failed")
            return _error("internal", 500)

    if result.get("status") == "completed" and result.get("account"):
        await _notify_connected(request, identity.telegram_user_id, result["account"])
    return JSONResponse({"ok": True, **result})


async def _notify_connected(request: Request, telegram_user_id: int, account: dict[str, Any]) -> None:
    notifier = getattr(request.app.state, "notify_connected", None)
    if notifier is None:
        return
    try:
        await notifier(telegram_user_id, account)
    except Exception:
        log.warning("could not send connection confirmation", exc_info=True)


@router.get("/connect", include_in_schema=False)
async def connect_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "connect.html", media_type="text/html", headers=_PAGE_HEADERS)


@router.post("/api/connect/status")
async def status(request: Request, body: TokenBody) -> JSONResponse:
    return await _run(request, lambda s, i: flow.get_status(s, body.token, i.telegram_user_id))


@router.post("/api/connect/phone")
async def phone(request: Request, body: PhoneBody) -> JSONResponse:
    st = request.app.state
    return await _run(
        request,
        lambda s, i: flow.submit_phone(s, st.settings, st.keyring, st.gateway, body.token, i.telegram_user_id, body.phone),
        code_sending=True,
    )


@router.post("/api/connect/resend")
async def resend(request: Request, body: TokenBody) -> JSONResponse:
    st = request.app.state
    return await _run(
        request,
        lambda s, i: flow.resend_code(s, st.settings, st.keyring, st.gateway, body.token, i.telegram_user_id),
        code_sending=True,
    )


@router.post("/api/connect/code")
async def code(request: Request, body: CodeBody) -> JSONResponse:
    st = request.app.state
    return await _run(
        request,
        lambda s, i: flow.submit_code(s, st.settings, st.keyring, st.gateway, body.token, i.telegram_user_id, body.code),
    )


@router.post("/api/connect/password/params")
async def password_params(request: Request, body: TokenBody) -> JSONResponse:
    st = request.app.state
    return await _run(
        request, lambda s, i: flow.password_params(s, st.keyring, st.gateway, body.token, i.telegram_user_id)
    )


@router.post("/api/connect/password")
async def password(request: Request, body: PasswordBody) -> JSONResponse:
    st = request.app.state
    return await _run(
        request,
        lambda s, i: flow.submit_password(
            s,
            st.settings,
            st.keyring,
            st.gateway,
            body.token,
            i.telegram_user_id,
            srp_id=body.srp_id,
            a_hex=body.a,
            m1_hex=body.m1,
        ),
    )


@router.post("/api/connect/cancel")
async def cancel(request: Request, body: TokenBody) -> JSONResponse:
    return await _run(request, lambda s, i: flow.cancel(s, body.token, i.telegram_user_id))
