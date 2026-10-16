from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, ChatMemberAdministrator, ChatMemberOwner
from redis.asyncio import Redis

from pca.bot.app import build_dispatcher
from pca.config import Settings, get_settings
from pca.services.connection import keyring_from_settings
from pca.db.session import create_engine, create_sessionmaker
from pca.logging_setup import configure_logging
from pca.services.subscription import RedisCache, SubscriptionService
from pca.services.users import bootstrap_admins

logger = logging.getLogger("pca.bot")

HEARTBEAT_INTERVAL_SECONDS = 20

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


async def resolve_required_channel(bot: Bot, settings: Settings) -> Settings:
    """Fill in the numeric channel id from the public username and verify the bot can check membership."""
    if settings.required_channel_id is None and settings.required_channel_username:
        try:
            chat = await bot.get_chat(f"@{settings.required_channel_username.lstrip('@')}")
        except TelegramAPIError as exc:
            logger.error("could not resolve required channel @%s: %s", settings.required_channel_username, exc.message)
            return settings
        logger.info("resolved required channel @%s to id %s", settings.required_channel_username, chat.id)
        settings = settings.model_copy(update={"required_channel_id": chat.id})

    if settings.required_channel_id is not None:
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(settings.required_channel_id, me.id)
            if not isinstance(member, ChatMemberAdministrator | ChatMemberOwner):
                logger.error(
                    "bot is not an administrator of the required channel; membership checks will fail. "
                    "Add @%s as a channel admin.",
                    me.username,
                )
        except TelegramAPIError as exc:
            logger.error("cannot inspect bot role in required channel: %s", exc.message)
    return settings


async def heartbeat(path: Path) -> None:
    while True:
        with contextlib.suppress(OSError):
            path.write_text(str(int(time.time())))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


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
    settings = await resolve_required_channel(bot, settings)
    subscription = SubscriptionService(bot, RedisCache(redis), cache_ttl_seconds=settings.subscription_cache_seconds)
    keyring = keyring_from_settings(settings) if settings.session_keys_raw.get_secret_value() else None
    gateway = None
    if settings.telegram_api_id and settings.telegram_api_hash.get_secret_value():
        from pca.telegram.gateway import TelethonGateway

        gateway = TelethonGateway(settings)
    if not settings.connect_enabled:
        logger.warning("account connection disabled: set PCA_PUBLIC_BASE_URL, Telegram API and session keys")
    payment_provider = None
    if settings.paystack_secret_key.get_secret_value():
        from pca.services.payments import PaystackClient

        payment_provider = PaystackClient(settings)
    else:
        logger.warning("payments disabled: set PCA_PAYSTACK_SECRET_KEY")
    dp = build_dispatcher(
        settings=settings,
        sessionmaker=sessionmaker,
        subscription=subscription,
        storage=RedisStorage(redis),
        keyring=keyring,
        gateway=gateway,
        payment_provider=payment_provider,
    )

    heartbeat_task = asyncio.create_task(heartbeat(Path(settings.heartbeat_path)))
    try:
        await bot.set_my_commands(COMMANDS)
        logger.info("starting polling as @%s", settings.bot_username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        heartbeat_task.cancel()
        if payment_provider is not None:
            await payment_provider.aclose()
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
