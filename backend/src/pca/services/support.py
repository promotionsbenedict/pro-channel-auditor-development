"""Support tickets: customers write in from the bot, administrators reply or close from the bot."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import SupportTicket, User
from pca.domain.enums import SupportTicketStatus

MAX_MESSAGE_CHARS = 2000
MAX_OPEN_PER_USER = 3


class SupportError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _clean(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise SupportError("empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise SupportError("too_long")
    return text


async def create_ticket(session: AsyncSession, user: User, message: str) -> SupportTicket:
    body = _clean(message)
    open_count = (
        await session.execute(
            select(func.count()).where(
                SupportTicket.user_id == user.id, SupportTicket.status == SupportTicketStatus.OPEN.value
            )
        )
    ).scalar_one()
    if open_count >= MAX_OPEN_PER_USER:
        raise SupportError("too_many_open")
    ticket = SupportTicket(user_id=user.id, message=body)
    session.add(ticket)
    await session.flush()
    return ticket


async def list_open(session: AsyncSession, limit: int = 10) -> list[tuple[SupportTicket, User]]:
    rows = await session.execute(
        select(SupportTicket, User)
        .join(User, User.id == SupportTicket.user_id)
        .where(SupportTicket.status == SupportTicketStatus.OPEN.value)
        .order_by(SupportTicket.id)
        .limit(limit)
    )
    return [(ticket, user) for ticket, user in rows.all()]


async def get_with_user(session: AsyncSession, ticket_id: int) -> tuple[SupportTicket, User] | None:
    row = (
        await session.execute(
            select(SupportTicket, User)
            .join(User, User.id == SupportTicket.user_id)
            .where(SupportTicket.id == ticket_id)
            .with_for_update(of=SupportTicket)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def answer(session: AsyncSession, ticket: SupportTicket, admin: User, reply: str) -> SupportTicket:
    if ticket.status == SupportTicketStatus.CLOSED.value:
        raise SupportError("closed")
    ticket.reply = _clean(reply)
    ticket.replied_by_user_id = admin.id
    ticket.status = SupportTicketStatus.ANSWERED.value
    await session.flush()
    return ticket


async def close(session: AsyncSession, ticket: SupportTicket) -> SupportTicket:
    ticket.status = SupportTicketStatus.CLOSED.value
    await session.flush()
    return ticket


async def open_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).where(SupportTicket.status == SupportTicketStatus.OPEN.value)
        )
    ).scalar_one()
