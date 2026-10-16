"""Subscription-gated customer features. Every handler here passes AccessGateMiddleware."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import keyboards, texts
from pca.bot.keyboards import ChanCb, MenuCb
from pca.bot.ui import respond
from pca.config import Settings
from pca.db.models import CreditPackage, User
from pca.security.crypto import Keyring
from pca.services import channels as channel_service
from pca.services import ledger
from pca.services.audits import list_user_audits
from pca.services.connect import connect_url, issue_link
from pca.services.users import get_active_connection
from pca.telegram.errors import SessionInvalidError, TelegramGatewayError
from pca.telegram.types import AccountGateway

log = logging.getLogger(__name__)

router = Router(name="customer")
router.message.filter(F.chat.type == "private")

SENSITIVE = {"sensitive": True}


@router.message(Command("wallet"))
@router.callback_query(MenuCb.filter(F.action == "wallet"))
async def show_wallet(event: Message | CallbackQuery, session: AsyncSession, db_user: User) -> None:
    wallet = await ledger.get_wallet(session, db_user.id)
    transactions = await ledger.recent_transactions(session, db_user.id, limit=5)
    await respond(
        event,
        texts.wallet_text(
            wallet.available_credits if wallet else 0, wallet.reserved_credits if wallet else 0, transactions
        ),
        keyboards.back_to_menu(),
    )


@router.message(Command("my_audits"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "audits"), flags=SENSITIVE)
async def show_audits(event: Message | CallbackQuery, session: AsyncSession, db_user: User) -> None:
    await respond(event, texts.audits_text(await list_user_audits(session, db_user.id)), keyboards.back_to_menu())


@router.callback_query(MenuCb.filter(F.action == "account"))
async def show_account(event: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    connection = await get_active_connection(session, db_user.id)
    await respond(event, texts.connected_account_text(connection), keyboards.account_menu(connection is not None))


@router.message(Command("connect"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "connect"), flags=SENSITIVE)
async def start_connect(
    event: Message | CallbackQuery, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    if not settings.connect_enabled:
        await respond(event, texts.CONNECT_NOT_ENABLED, keyboards.back_to_menu())
        return
    token = await issue_link(session, settings, db_user)
    await session.commit()
    already = await get_active_connection(session, db_user.id) is not None
    await respond(
        event,
        texts.connect_intro(settings.connect_link_ttl_seconds // 60, reconnecting=already),
        keyboards.connect_webapp(connect_url(settings, token)),
    )


async def _connection_or_explain(event, session: AsyncSession, db_user: User, keyring, gateway):
    connection = await get_active_connection(session, db_user.id)
    if connection is None:
        await respond(event, texts.CHANNELS_REQUIRES_CONNECTION, keyboards.account_menu(False))
        return None
    if keyring is None or gateway is None:
        await respond(event, texts.CONNECT_NOT_ENABLED, keyboards.back_to_menu())
        return None
    return connection


async def _report_gateway_error(event, session: AsyncSession, exc: TelegramGatewayError) -> None:
    await session.commit()
    if isinstance(exc, SessionInvalidError):
        await respond(event, texts.SESSION_EXPIRED, keyboards.reconnect())
    elif exc.code == "flood":
        await respond(event, texts.telegram_busy(exc.retry_after), keyboards.back_to_menu())
    else:
        log.warning("telegram call failed", extra={"code": exc.code})
        await respond(event, texts.TELEGRAM_UNAVAILABLE, keyboards.back_to_menu())


@router.message(Command("channels"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "channels"), flags=SENSITIVE)
async def show_channels(
    event: Message | CallbackQuery,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    connection = await _connection_or_explain(event, session, db_user, keyring, gateway)
    if connection is None:
        return
    try:
        rows = await channel_service.refresh_channels(session, keyring, gateway, connection)
    except TelegramGatewayError as exc:
        await _report_gateway_error(event, session, exc)
        return
    await session.commit()
    items = [(auth.id, channel.title, auth.can_manage_join_requests) for auth, channel in rows]
    await respond(event, texts.channels_text(len(items), sum(1 for i in items if i[2])), keyboards.channel_list(items, "view"))


@router.callback_query(ChanCb.filter(F.action == "view"), flags=SENSITIVE)
async def view_channel(
    query: CallbackQuery,
    callback_data: ChanCb,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    connection = await _connection_or_explain(query, session, db_user, keyring, gateway)
    if connection is None:
        return
    found = await channel_service.get_authorization(session, connection, callback_data.id)
    if found is None:
        await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
        return
    auth, channel = found
    try:
        auth = await channel_service.verify_channel(session, keyring, gateway, connection, auth, channel)
    except TelegramGatewayError as exc:
        if exc.code in {"channel_unavailable", "insufficient_rights"}:
            await session.commit()
            await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
            return
        await _report_gateway_error(query, session, exc)
        return
    await session.commit()
    await respond(query, texts.channel_detail_text(channel, auth), keyboards.channel_detail(auth.id, auth.can_manage_join_requests))


@router.message(Command("audit"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "audit"), flags=SENSITIVE)
async def start_audit(
    event: Message | CallbackQuery,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    connection = await get_active_connection(session, db_user.id)
    if connection is None:
        await respond(event, texts.AUDIT_REQUIRES_CONNECTION, keyboards.account_menu(False))
        return
    rows = await channel_service.list_channels(session, connection)
    eligible = [(auth.id, channel.title, True) for auth, channel in rows if auth.can_manage_join_requests]
    if not eligible:
        await respond(event, texts.AUDIT_NO_ELIGIBLE, keyboards.account_menu(True))
        return
    await respond(event, texts.AUDIT_PICK, keyboards.channel_list(eligible, "audit"))


@router.callback_query(ChanCb.filter(F.action == "audit"), flags=SENSITIVE)
async def choose_audit_channel(
    query: CallbackQuery,
    callback_data: ChanCb,
    session: AsyncSession,
    db_user: User,
    keyring: Keyring | None,
    gateway: AccountGateway | None,
) -> None:
    connection = await _connection_or_explain(query, session, db_user, keyring, gateway)
    if connection is None:
        return
    found = await channel_service.get_authorization(session, connection, callback_data.id)
    if found is None:
        await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
        return
    auth, channel = found
    try:
        auth = await channel_service.ensure_can_audit(session, keyring, gateway, connection, auth, channel)
    except channel_service.ChannelPermissionError:
        await session.commit()
        await respond(query, texts.channel_detail_text(channel, auth), keyboards.channel_detail(auth.id, False))
        return
    except TelegramGatewayError as exc:
        if exc.code in {"channel_unavailable", "insufficient_rights"}:
            await session.commit()
            await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
            return
        await _report_gateway_error(query, session, exc)
        return
    await session.commit()
    await respond(query, texts.audit_ready_text(channel, auth), keyboards.back_to_menu())


@router.message(Command("compare"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "compare"), flags=SENSITIVE)
async def start_compare(event: Message | CallbackQuery) -> None:
    await respond(event, texts.COMPARE_EMPTY, keyboards.back_to_menu())


@router.message(Command("buy"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "buy"), flags=SENSITIVE)
async def start_buy(event: Message | CallbackQuery, session: AsyncSession) -> None:
    packages = list(
        (
            await session.execute(
                select(CreditPackage)
                .where(CreditPackage.active.is_(True))
                .order_by(CreditPackage.sort_order, CreditPackage.id)
            )
        ).scalars()
    )
    text = texts.buy_packages_text(packages) if packages else texts.BUY_UNAVAILABLE
    await respond(event, text, keyboards.back_to_menu())
