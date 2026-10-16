"""HTTPS service: health/readiness and the account-connection Mini App. Phase 4 adds Paystack webhooks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text

from pca.api import connect
from pca.config import Settings, get_settings
from pca.db.session import create_engine, create_sessionmaker
from pca.logging_setup import configure_logging
from pca.services.connect import expire_stale_flows
from pca.services.connection import keyring_from_settings
from html import escape as esc

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}
# The connect page is displayed inside Telegram's Mini App frame; its CSP frame-ancestors governs embedding.
FRAMEABLE_PREFIX = "/connect"
EXPIRY_SWEEP_SECONDS = 60


async def _sweep_expired_flows(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(EXPIRY_SWEEP_SECONDS)
        try:
            async with app.state.sessionmaker() as session:
                await expire_stale_flows(session)
                await session.commit()
        except Exception:
            log.warning("expired-flow sweep failed", exc_info=True)


def create_app(settings: Settings | None = None, *, gateway: Any = None, bot: Bot | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        engine = create_engine(settings.database_url, pool_size=5, max_overflow=5)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.redis = Redis.from_url(settings.redis_url)
        app.state.keyring = keyring_from_settings(settings) if settings.session_keys_raw.get_secret_value() else None
        app.state.gateway = gateway
        if app.state.gateway is None and settings.connect_enabled:
            from pca.telegram.gateway import TelethonGateway

            app.state.gateway = TelethonGateway(settings)

        notify_bot = bot
        owns_bot = False
        if notify_bot is None and settings.bot_token.get_secret_value():
            notify_bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            owns_bot = True

        async def notify_connected(telegram_user_id: int, account: dict[str, Any]) -> None:
            if notify_bot is None:
                return
            name = esc(account.get("display_name") or "your account")
            await notify_bot.send_message(
                telegram_user_id,
                f"✅ <b>Telegram account connected</b>\n\n{name} is now linked. "
                "Open /start and tap 📢 My Channels to load the channels you manage.\n\n"
                "You can revoke access at any time with /disconnect.",
            )

        app.state.notify_connected = notify_connected
        sweeper = asyncio.create_task(_sweep_expired_flows(app))
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            if owns_bot and notify_bot is not None:
                await notify_bot.session.close()
            await app.state.redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Pro Channel Auditor",
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        frameable = request.url.path == FRAMEABLE_PREFIX or request.url.path.startswith(FRAMEABLE_PREFIX + "/")
        for name, value in SECURITY_HEADERS.items():
            if name == "X-Frame-Options" and frameable:
                continue
            response.headers.setdefault(name, value)
        if settings.environment == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=422)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        try:
            await request.app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse({"status": "ok" if healthy else "degraded", **checks}, status_code=200 if healthy else 503)

    app.include_router(connect.router)
    app.mount("/connect/static", StaticFiles(directory=connect.STATIC_DIR), name="connect-static")
    return app
