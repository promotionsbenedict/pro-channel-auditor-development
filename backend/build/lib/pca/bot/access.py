from __future__ import annotations

from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import keyboards, texts
from pca.bot.ui import respond
from pca.config import Settings
from pca.db.models import User
from pca.services.subscription import MembershipResult, MembershipStatus, SubscriptionConfig, SubscriptionService
from pca.services.system_settings import load_subscription_config


async def check_membership(
    session: AsyncSession,
    subscription: SubscriptionService,
    settings: Settings,
    user: User,
    *,
    fresh: bool,
) -> tuple[MembershipResult, SubscriptionConfig]:
    config = await load_subscription_config(session, settings)
    result = await subscription.check(
        telegram_user_id=user.telegram_user_id, is_admin=user.is_admin, config=config, fresh=fresh
    )
    return result, config


async def show_gate(event: Message | CallbackQuery, result: MembershipResult, config: SubscriptionConfig) -> None:
    if result.status == MembershipStatus.UNAVAILABLE:
        await respond(event, texts.SUBSCRIPTION_UNAVAILABLE, keyboards.retry_verification())
        return
    await respond(event, config.gate_text or texts.DEFAULT_GATE_TEXT, keyboards.subscription_gate(config.channel_url))
