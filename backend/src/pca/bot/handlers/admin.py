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
from pca.services import pricing, security_events, support
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


def _naira(minor: int) -> str:
    return f"₦{minor / 100:,.2f}"


def _parse_adjustment(command: CommandObject) -> tuple[int, int, str] | None:
    parts = (command.args or "").split(maxsplit=2)
    target = _parse_int(parts[0]) if parts else None
    credits = _parse_int(parts[1]) if len(parts) > 1 else None
    reason = parts[2].strip() if len(parts) > 2 else ""
    if target is None or credits is None or credits <= 0 or not reason:
        return None
    return target, credits, reason


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    parsed = _parse_adjustment(command)
    if parsed is None:
        await message.answer("Usage: /promo &lt;telegram_user_id&gt; &lt;credits&gt; &lt;reason&gt;")
        return
    target, credits, reason = parsed
    try:
        await admin_service.grant_credits(
            session, db_user, telegram_user_id=target, credits=credits, reason=reason, promotional=True
        )
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"Added {credits} promotional credits to <code>{target}</code>.")


@router.message(Command("debit"))
async def cmd_debit(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    parsed = _parse_adjustment(command)
    if parsed is None:
        await message.answer("Usage: /debit &lt;telegram_user_id&gt; &lt;credits&gt; &lt;reason&gt;")
        return
    target, credits, reason = parsed
    try:
        await admin_service.debit_credits(session, db_user, telegram_user_id=target, credits=credits, reason=reason)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"Removed {credits} credits from <code>{target}</code>.")


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    target = _parse_int(command.args or "")
    if target is None:
        await message.answer("Usage: /user &lt;telegram_user_id&gt;")
        return
    try:
        info = await admin_service.customer_overview(session, db_user, telegram_user_id=target)
    except admin_service.AdminOperationError as exc:
        await message.answer(f"Rejected: {esc(exc)}")
        return
    user = info.user
    lines = [
        f"<b>Customer</b> <code>{user.telegram_user_id}</code>"
        + (f" (@{esc(user.username)})" if user.username else ""),
        f"Role: {esc(user.role)} · Status: {esc(user.status)}",
        f"Joined: {user.created_at:%Y-%m-%d} · Last active: "
        + (f"{user.last_active_at:%Y-%m-%d %H:%M}" if user.last_active_at else "never"),
        f"Credits: {info.available} available · {info.reserved} reserved",
        f"Connected accounts: {info.active_connections}",
        f"Audits: {info.audits_total} total · {info.audits_active} active",
        f"Successful payments: {info.payments_success}",
    ]
    if user.deletion_requested_at:
        lines.append(f"Deletion requested: {user.deletion_requested_at:%Y-%m-%d}")
    for payment in info.last_payments:
        lines.append(f"• {payment.created_at:%Y-%m-%d} {_naira(payment.amount)} · {esc(payment.status)} · {payment.credits} cr")
    await message.answer("\n".join(lines))


@router.message(Command("health"))
async def cmd_health(message: Message, session: AsyncSession, db_user: User) -> None:
    health = await admin_service.system_health(session, db_user)
    drift = ", ".join(str(u) for u in health.wallet_drift_users[:10]) or "none"
    oldest = f"{health.oldest_queued_minutes} min" if health.oldest_queued_minutes is not None else "-"
    await message.answer(
        f"<b>System health: {'OK' if health.healthy else 'ATTENTION'}</b>\n\n"
        f"Database: {'reachable' if health.database_ok else 'unreachable'}\n"
        f"Queued/waiting audits: {health.queued_jobs} (oldest queued {oldest})\n"
        f"Running audits: {health.running_jobs} · stale leases: {health.stale_leases}\n"
        f"Payments pending over 2h: {health.stuck_payments}\n"
        f"Wallets out of balance with the ledger: {drift}"
    )


def _job_line(job, channel, user) -> str:  # type: ignore[no-untyped-def]
    return (
        f"#{job.id} · {esc(job.status)} · {esc(channel.title)} · user <code>{user.telegram_user_id}</code> · "
        f"{job.scanned_count:,} scanned" + (f" · {esc(job.error_code)}" if job.error_code else "")
    )


@router.message(Command("jobs"))
async def cmd_jobs(message: Message, session: AsyncSession, db_user: User) -> None:
    rows = await admin_service.active_audits(session, db_user)
    if not rows:
        await message.answer("No active audits.")
        return
    await message.answer("<b>Active audits</b>\n" + "\n".join(_job_line(*row) for row in rows))


@router.message(Command("failures"))
async def cmd_failures(message: Message, session: AsyncSession, db_user: User) -> None:
    rows = await admin_service.failed_audits(session, db_user)
    if not rows:
        await message.answer("No failed or partial audits.")
        return
    await message.answer("<b>Recent failed and partial audits</b>\n" + "\n".join(_job_line(*row) for row in rows))


@router.message(Command("job_cancel"))
async def cmd_job_cancel(
    message: Message, command: CommandObject, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    job_id = _parse_int(command.args or "")
    if job_id is None:
        await message.answer("Usage: /job_cancel &lt;audit_id&gt;")
        return
    try:
        job = await admin_service.cancel_job(session, settings, db_user, job_id=job_id)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    if job.status == "cancelled":
        await message.answer(f"Audit #{job_id} cancelled and its reserved credits released.")
    else:
        await message.answer(f"Cancellation requested for audit #{job_id}; the worker stops at its next page.")


@router.message(Command("job_retry"))
async def cmd_job_retry(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    job_id = _parse_int(command.args or "")
    if job_id is None:
        await message.answer("Usage: /job_retry &lt;audit_id&gt;")
        return
    try:
        await admin_service.retry_job(session, db_user, job_id=job_id)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"Audit #{job_id} re-queued; a worker will pick it up shortly.")


@router.message(Command("packages"))
async def cmd_packages(message: Message, session: AsyncSession, db_user: User) -> None:
    packages = await admin_service.list_packages(session, db_user)
    if not packages:
        await message.answer(
            "No credit packages yet. Customers cannot buy credits until one exists.\n"
            "Create one: /package_set new &lt;credits&gt; &lt;price_naira&gt; &lt;name&gt;"
        )
        return
    lines = ["<b>Credit packages</b>"]
    for package in packages:
        state = "active" if package.active else "off"
        lines.append(f"#{package.id} · {esc(package.name)} · {package.credits} credits · {_naira(package.price_minor)} · {state}")
    await message.answer("\n".join(lines))


@router.message(Command("package_set"))
async def cmd_package_set(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    parts = (command.args or "").split(maxsplit=3)
    usage = "Usage: /package_set &lt;id|new&gt; &lt;credits&gt; &lt;price_naira&gt; &lt;name&gt;"
    if len(parts) != 4:
        await message.answer(usage)
        return
    package_id = None if parts[0].lower() == "new" else _parse_int(parts[0])
    credits, price = _parse_int(parts[1]), _parse_int(parts[2])
    if (parts[0].lower() != "new" and package_id is None) or credits is None or price is None:
        await message.answer(usage)
        return
    try:
        package = await admin_service.save_package(
            session, db_user, package_id=package_id, name=parts[3], credits=credits, price_naira=price
        )
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(
        f"Saved package #{package.id}: {esc(package.name)} · {package.credits} credits · {_naira(package.price_minor)}"
    )


@router.message(Command("package_off"))
async def cmd_package_off(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    package_id = _parse_int(command.args or "")
    if package_id is None:
        await message.answer("Usage: /package_off &lt;package_id&gt;")
        return
    try:
        await admin_service.disable_package(session, db_user, package_id=package_id)
    except admin_service.AdminOperationError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc)}")
        return
    await session.commit()
    await message.answer(f"Package #{package_id} is no longer offered. Existing payments are unaffected.")


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    rows = await support.list_open(session)
    if not rows:
        await message.answer("No open support tickets.")
        return
    lines = ["<b>Open support tickets</b>"]
    for ticket, user in rows:
        who = f"@{esc(user.username)}" if user.username else f"<code>{user.telegram_user_id}</code>"
        preview = ticket.message if len(ticket.message) <= 300 else ticket.message[:300] + "…"
        lines.append(f"\n<b>#{ticket.id}</b> · {who} · {ticket.created_at:%Y-%m-%d %H:%M}\n{esc(preview)}")
    lines.append("\nReply: /reply &lt;ticket_id&gt; &lt;message&gt;")
    await message.answer("\n".join(lines))


@router.message(Command("reply"))
async def cmd_reply(message: Message, command: CommandObject, session: AsyncSession, db_user: User, bot: Bot) -> None:
    admin_service.require_admin(db_user)
    parts = (command.args or "").split(maxsplit=1)
    ticket_id = _parse_int(parts[0]) if parts else None
    if ticket_id is None or len(parts) < 2:
        await message.answer("Usage: /reply &lt;ticket_id&gt; &lt;message&gt;")
        return
    found = await support.get_with_user(session, ticket_id)
    if found is None:
        await message.answer("No ticket with that id.")
        return
    ticket, customer = found
    try:
        await support.answer(session, ticket, db_user, parts[1])
    except support.SupportError as exc:
        await session.rollback()
        await message.answer(f"Rejected: {esc(exc.code)}")
        return
    await admin_service.log_action(session, db_user, "support.reply", target_id=ticket_id)
    await session.commit()
    try:
        await bot.send_message(customer.telegram_user_id, texts.support_reply_text(ticket.id, ticket.reply or ""))
    except TelegramAPIError as exc:
        await message.answer(f"Reply saved, but Telegram could not deliver it: {esc(exc.message)}")
        return
    await message.answer(f"Reply sent for ticket #{ticket_id}.")


@router.message(Command("close_ticket"))
async def cmd_close_ticket(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    ticket_id = _parse_int(command.args or "")
    if ticket_id is None:
        await message.answer("Usage: /close_ticket &lt;ticket_id&gt;")
        return
    found = await support.get_with_user(session, ticket_id)
    if found is None:
        await message.answer("No ticket with that id.")
        return
    await support.close(session, found[0])
    await admin_service.log_action(session, db_user, "support.close", target_id=ticket_id)
    await session.commit()
    await message.answer(f"Ticket #{ticket_id} closed.")


@router.message(Command("security"))
async def cmd_security(message: Message, session: AsyncSession, db_user: User) -> None:
    admin_service.require_admin(db_user)
    events = await security_events.recent_events(session)
    if not events:
        await message.answer("No security events recorded.")
        return
    lines = ["<b>Recent security events</b>"]
    for event in events:
        lines.append(
            f"{event.created_at:%Y-%m-%d %H:%M} · {esc(event.severity)} · {esc(event.event_type)}"
            + (f" · {esc(event.source_ip)}" if event.source_ip else "")
        )
    await message.answer("\n".join(lines))


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
