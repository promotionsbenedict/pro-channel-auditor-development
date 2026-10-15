"""Request pipeline.

Outer (per update): DatabaseMiddleware opens a session and registers the user.
Inner (per handler): AccessGateMiddleware enforces account status and the
mandatory subscription. It is default-deny: every handler is gated unless it is
explicitly flagged ``public``. Handlers flagged ``sensitive`` force a live
membership check that ignores the cache.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.bot import texts
from pca.bot.access import check_membership, show_gate
from pca.bot.ui import respond
from pca.config import Settings
from pca.db.models import User
from pca.domain.enums import UserStatus
from pca.services.users import register_user

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        async with self._sessionmaker() as session:
            data["session"] = session
            data["db_user"] = None
            if tg_user is not None and not tg_user.is_bot:
                data["db_user"] = await register_user(
                    session,
                    telegram_user_id=tg_user.id,
                    username=tg_user.username,
                    bootstrap_admin_ids=self._settings.admin_telegram_ids,
                )
                await session.commit()
            return await handler(event, data)


class AccessGateMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if get_flag(data, "public"):
            return await handler(event, data)

        user: User | None = data.get("db_user")
        if user is None or not isinstance(event, Message | CallbackQuery):
            return None

        if user.status == UserStatus.SUSPENDED.value:
            await respond(event, texts.SUSPENDED)
            return None
        if user.status == UserStatus.DELETION_REQUESTED.value:
            await respond(event, texts.DELETION_PENDING)
            return None

        result, config = await check_membership(
            data["session"],
            data["subscription"],
            data["settings"],
            user,
            fresh=bool(get_flag(data, "sensitive")),
        )
        if not result.allowed:
            await show_gate(event, result, config)
            return None
        data["membership"] = result
        return await handler(event, data)


class ErrorShieldMiddleware(BaseMiddleware):
    """Never leak stack traces to customers."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("unhandled error in bot handler")
            if isinstance(event, Message | CallbackQuery):
                try:
                    await respond(event, texts.GENERIC_ERROR)
                except Exception:
                    logger.warning("failed to deliver error notice")
            return None
