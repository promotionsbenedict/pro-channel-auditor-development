"""Subscription-gated customer features. Every handler here passes AccessGateMiddleware."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import keyboards, texts
from pca.bot.keyboards import MenuCb
from pca.bot.ui import respond
from pca.db.models import CreditPackage, User
from pca.services import ledger
from pca.services.audits import list_user_audits
from pca.services.users import get_active_connection

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
    await respond(
        event,
        texts.connected_account_text(
            connection.connected_account_id if connection else None, connection.connected_at if connection else None
        ),
        keyboards.back_to_menu(),
    )


@router.message(Command("connect"))
@router.callback_query(MenuCb.filter(F.action == "connect"))
async def start_connect(event: Message | CallbackQuery) -> None:
    await respond(event, texts.CONNECT_NOT_ENABLED, keyboards.back_to_menu())


@router.message(Command("audit"), flags=SENSITIVE)
@router.callback_query(MenuCb.filter(F.action == "audit"), flags=SENSITIVE)
async def start_audit(event: Message | CallbackQuery, session: AsyncSession, db_user: User) -> None:
    if await get_active_connection(session, db_user.id) is None:
        await respond(event, texts.AUDIT_REQUIRES_CONNECTION, keyboards.back_to_menu())
        return
    await respond(event, texts.CONNECT_NOT_ENABLED, keyboards.back_to_menu())


@router.callback_query(MenuCb.filter(F.action == "channels"))
async def show_channels(event: CallbackQuery) -> None:
    await respond(event, texts.CHANNELS_REQUIRES_CONNECTION, keyboards.back_to_menu())


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
