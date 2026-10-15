from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from redis.asyncio import Redis

from pca.bot.app import build_dispatcher
from pca.config import get_settings
from pca.db.session import create_engine, create_sessionmaker
from pca.logging_setup import configure_logging
from pca.services.subscription import RedisCache, SubscriptionService
from pca.services.users import bootstrap_admins

logger = logging.getLogger("pca.bot")

COMMANDS = [
    BotCommand(command="start", description="Main menu"),
    BotCommand(command="audit", description="Start a new audit"),
    BotCommand(command="my_audits", description="Your audits"),
    BotCommand(command="compare", description="Compare channels"),
    BotCommand(command="wallet", description="Credit balance"),
    BotCommand(command="buy", description="Buy credits"),
    BotCommand(command="connect", description="Connect your Telegram account"),
    BotCommand(command="disconnect", description="Disconnect your Telegram account"),
    BotCommand(command="help", description="How it works"),
    BotCommand(command="privacy", description="Privacy and data deletion"),
    BotCommand(command="support", description="Contact support"),
]


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.bot_token.get_secret_value():
        raise SystemExit("PCA_BOT_TOKEN is not set")

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url)

    async with sessionmaker() as session:
        promoted = await bootstrap_admins(session, settings.admin_telegram_ids)
        await session.commit()
        if promoted:
            logger.info("bootstrapped %d administrator(s)", promoted)

    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    subscription = SubscriptionService(bot, RedisCache(redis), cache_ttl_seconds=settings.subscription_cache_seconds)
    dp = build_dispatcher(
        settings=settings, sessionmaker=sessionmaker, subscription=subscription, storage=RedisStorage(redis)
    )

    try:
        await bot.set_my_commands(COMMANDS)
        logger.info("starting polling as @%s", settings.bot_username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
