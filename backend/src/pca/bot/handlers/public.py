"""Handlers reachable without subscription: onboarding, help, support, privacy, disconnect, deletion."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import keyboards, texts
from pca.bot.access import check_membership, show_gate
from pca.bot.keyboards import MenuCb, PrivacyCb, SubCb
from pca.bot.ui import respond
from pca.config import Settings
from pca.db.models import User
from pca.domain.enums import UserStatus
from pca.services.subscription import SubscriptionService
from pca.services.system_settings import get_support_contact
from pca.security.crypto import Keyring
from pca.services.connection import disconnect
from pca.services.users import request_deletion
from pca.telegram.types import AccountGateway

router = Router(name="public")
router.message.filter(F.chat.type == "private")

PUBLIC = {"public": True}


async def _home(
    event: Message | CallbackQuery,
    session: AsyncSession,
    db_user: User,
    subscription: SubscriptionService,
    settings: Settings,
) -> None:
    if db_user.status == UserStatus.SUSPENDED.value:
        await respond(event, texts.SUSPENDED)
        return
    if db_user.status == UserStatus.DELETION_REQUESTED.value:
        await respond(event, texts.DELETION_PENDING)
        return
    result, config = await check_membership(session, subscription, settings, db_user, fresh=True)
    if result.allowed:
        await respond(event, texts.WELCOME, keyboards.main_menu())
    else:
        await show_gate(event, result, config)


@router.message(CommandStart(), flags=PUBLIC)
async def cmd_start(
    message: Message, session: AsyncSession, db_user: User, subscription: SubscriptionService, settings: Settings
) -> None:
    await _home(message, session, db_user, subscription, settings)


@router.callback_query(MenuCb.filter(F.action == "home"), flags=PUBLIC)
async def cb_home(
    query: CallbackQuery, session: AsyncSession, db_user: User, subscription: SubscriptionService, settings: Settings
) -> None:
    await _home(query, session, db_user, subscription, settings)


@router.callback_query(SubCb.filter(F.action == "verify"), flags=PUBLIC)
async def cb_verify(
    query: CallbackQuery, session: AsyncSession, db_user: User, subscription: SubscriptionService, settings: Settings
) -> None:
    if db_user.status != UserStatus.ACTIVE.value:
        await respond(query, texts.SUSPENDED if db_user.status == UserStatus.SUSPENDED.value else texts.DELETION_PENDING)
        return
    result, config = await check_membership(session, subscription, settings, db_user, fresh=True)
    if result.allowed:
        await respond(query, texts.WELCOME, keyboards.main_menu())
        return
    if result.status == "not_subscribed":
        await query.answer(texts.NOT_YET_JOINED_ALERT, show_alert=True)
        return
    await show_gate(query, result, config)


@router.message(Command("help"), flags=PUBLIC)
@router.callback_query(MenuCb.filter(F.action == "help"), flags=PUBLIC)
async def show_help(event: Message | CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    await respond(event, texts.help_text(await get_support_contact(session, settings)), keyboards.help_menu())


@router.message(Command("support"), flags=PUBLIC)
async def show_support(message: Message, session: AsyncSession, settings: Settings) -> None:
    await respond(message, texts.support_text(await get_support_contact(session, settings)), keyboards.back_to_menu())


@router.message(Command("privacy"), flags=PUBLIC)
@router.callback_query(PrivacyCb.filter(F.action == "show"), flags=PUBLIC)
async def show_privacy(event: Message | CallbackQuery) -> None:
    await respond(event, texts.PRIVACY, keyboards.privacy_menu())


@router.callback_query(PrivacyCb.filter(F.action == "delete"), flags=PUBLIC)
async def ask_deletion(query: CallbackQuery) -> None:
    await respond(query, texts.DELETE_CONFIRM, keyboards.confirm_deletion())


@router.callback_query(PrivacyCb.filter(F.action == "delete_confirm"), flags=PUBLIC)
async def confirm_deletion(
    query: CallbackQuery,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    await disconnect(session, keyring, gateway, db_user.id, reason="user_deleted")
    await request_deletion(session, db_user)
    await session.commit()
    await respond(query, texts.DELETE_DONE)


@router.message(Command("disconnect"), flags=PUBLIC)
async def cmd_disconnect(
    message: Message,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    revoked, remote_ok = await disconnect(session, keyring, gateway, db_user.id)
    await session.commit()
    await respond(message, texts.disconnect_result(revoked, remote_ok), keyboards.back_to_menu())
