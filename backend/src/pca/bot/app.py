from __future__ import annotations

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.bot.handlers import admin, customer, public
from pca.bot.middlewares import AccessGateMiddleware, DatabaseMiddleware, ErrorShieldMiddleware
from pca.config import Settings
from pca.security.crypto import Keyring
from pca.telegram.types import AccountGateway
from pca.services.subscription import SubscriptionService


def build_dispatcher(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    subscription: SubscriptionService,
    storage=None,
    keyring: Keyring | None = None,
    gateway: AccountGateway | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=storage) if storage is not None else Dispatcher()
    dp["settings"] = settings
    dp["subscription"] = subscription
    dp["keyring"] = keyring
    dp["gateway"] = gateway

    dp.update.outer_middleware(ErrorShieldMiddleware())
    dp.update.outer_middleware(DatabaseMiddleware(sessionmaker, settings))
    dp.message.middleware(AccessGateMiddleware())
    dp.callback_query.middleware(AccessGateMiddleware())

    # Admin router first so admin commands resolve before any customer fallback.
    dp.include_routers(admin.router, public.router, customer.router)
    return dp
