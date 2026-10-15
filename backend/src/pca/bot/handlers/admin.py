"""Administrator commands. Gated by IsAdmin at router level and require_admin in every service call."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import texts
from pca.bot.texts import esc
from pca.config import Settings
from pca.db.models import User
from pca.domain.enums import UserStatus
from pca.services import admin as admin_service
from pca.services import pricing
from pca.services import system_settings as sysset

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, db_user: User | None = None) -> bool:
        return db_user is not None and db_user.is_admin


router.message.filter(IsAdmin())


def _parse_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else None


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, db_user: User) -> None:
    stats = await admin_service.platform_stats(session, db_user)
    await message.answer(texts.admin_stats_text(stats))


@router.message(Command("admin_pricing"))
async def cmd_pricing(message: Message, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    tiers = await pricing.list_tiers(session)
    if not tiers:
        await message.answer("No active pricing tiers. Audits cannot be quoted until one is created.")
        return
    lines = ["<b>Active pricing tiers</b>"]
    for tier in tiers:
        lines.append(
            f"#{tier.id}: {tier.minimum_requests:,}–{tier.maximum_requests:,} requests → "
            f"{tier.credits_required} credits"
        )
    await message.answer("\n".join(lines))


@router.message(Command("tier_set"))
async def cmd_tier_set(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    parts = (command.args or "").split()
    values = [_parse_int(p) for p in parts]
    if len(values) != 3 or any(v is None for v in values):
        await message.answer("Usage: /tier_set &lt;min&gt; &lt;max&gt; &lt;credits&gt;")
        return
    minimum, maximum, credits = values
    try:
        tier = await pricing.upsert_tier(session, minimum=minimum, maximum=maximum, credits=credits)
    except pricing.PricingError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await admin_service.log_action(
        session, db_user, "pricing.tier_set", target_id=tier.id,
        metadata={"min": minimum, "max": maximum, "credits": credits},
    )
    await session.commit()
    await message.answer(f"Saved tier #{tier.id}: {minimum:,}–{maximum:,} → {credits} credits")


@router.message(Command("tier_off"))
async def cmd_tier_off(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    tier_id = _parse_int(command.args or "")
    if tier_id is None:
        await message.answer("Usage: /tier_off &lt;tier_id&gt;")
        return
    tier = await pricing.deactivate_tier(session, tier_id)
    if tier is None:
        await message.answer("No active tier with that id.")
        return
    await admin_service.log_action(session, db_user, "pricing.tier_off", target_id=tier_id)
    await session.commit()
    await message.answer(f"Deactivated tier #{tier_id}.")


async def _set_status(message: Message, command: CommandObject, session: AsyncSession, db_user: User, status: UserStatus) -> None:
    target = _parse_int(command.args or "")
    if target is None:
        await message.answer(f"Usage: /{command.command} &lt;telegram_user_id&gt;")
        return
    try:
        await admin_service.set_user_status(session, db_user, telegram_user_id=target, status=status)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"User <code>{target}</code> is now {status.value}.")


@router.message(Command("suspend"))
async def cmd_suspend(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    await _set_status(message, command, session, db_user, UserStatus.SUSPENDED)


@router.message(Command("unsuspend"))
async def cmd_unsuspend(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    await _set_status(message, command, session, db_user, UserStatus.ACTIVE)


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    parts = (command.args or "").split(maxsplit=2)
    target = _parse_int(parts[0]) if parts else None
    credits = _parse_int(parts[1]) if len(parts) > 1 else None
    reason = parts[2] if len(parts) > 2 else ""
    if target is None or credits is None or credits <= 0 or not reason.strip():
        await message.answer("Usage: /grant &lt;telegram_user_id&gt; &lt;credits&gt; &lt;reason&gt;")
        return
    try:
        await admin_service.grant_credits(session, db_user, telegram_user_id=target, credits=credits, reason=reason)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"Granted {credits} credits to <code>{target}</code>.")


@router.message(Command("sub_channel"))
async def cmd_sub_channel(
    message: Message, command: CommandObject, session: AsyncSession, db_user: User, bot: Bot
) -> None:
    admin_service.require_admin(db_user)
    chat_id = _parse_int(command.args or "")
    if chat_id is None:
        await message.answer("Usage: /sub_channel &lt;numeric_chat_id&gt; (e.g. -1001234567890)")
        return
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError as exc:
        await message.answer(f"Telegram rejected the channel: {esc(exc.message)}")
        return
    if not isinstance(bot_member, ChatMemberAdministrator | ChatMemberOwner):
        await message.answer("The bot must be an administrator of that channel to verify memberships.")
        return
    await sysset.set_setting(session, sysset.SUB_CHANNEL_ID, chat.id, actor_user_id=db_user.id)
    if chat.username:
        await sysset.set_setting(session, sysset.SUB_CHANNEL_USERNAME, chat.username, actor_user_id=db_user.id)
        await sysset.set_setting(
            session, sysset.SUB_CHANNEL_URL, f"https://t.me/{chat.username}", actor_user_id=db_user.id
        )
    await admin_service.log_action(
        session, db_user, "subscription.channel", target_id=chat.id, metadata={"username": chat.username}
    )
    await session.commit()
    await message.answer(f"Required channel set to <b>{esc(chat.title or chat.id)}</b> (<code>{chat.id}</code>).")


@router.message(Command("sub_enforce"))
async def cmd_sub_enforce(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await message.answer("Usage: /sub_enforce on|off")
        return
    await sysset.set_setting(session, sysset.SUB_ENFORCED, arg == "on", actor_user_id=db_user.id)
    await admin_service.log_action(session, db_user, "subscription.enforced", metadata={"enforced": arg == "on"})
    await session.commit()
    await message.answer(f"Mandatory subscription is now <b>{arg}</b>.")


@router.message(Command("sub_status"))
async def cmd_sub_status(message: Message, session: AsyncSession, db_user: User, settings: Settings) -> None:
    admin_service.require_admin(db_user)
    config = await sysset.load_subscription_config(session, settings)
    await message.answer(
        "<b>Subscription settings</b>\n"
        f"Enforced: {config.enforced}\n"
        f"Channel id: <code>{config.channel_id}</code>\n"
        f"Channel URL: {esc(config.channel_url)}"
    )


@router.message(Command("admin_log"))
async def cmd_admin_log(message: Message, session: AsyncSession, db_user: User) -> None:
    actions = await admin_service.recent_actions(session, db_user)
    if not actions:
        await message.answer("No administrator actions recorded.")
        return
    lines = ["<b>Recent admin actions</b>"]
    for action in actions:
        lines.append(f"{action.created_at:%Y-%m-%d %H:%M} · {esc(action.action)} · {esc(action.target_id or '-')}")
    await message.answer("\n".join(lines))
