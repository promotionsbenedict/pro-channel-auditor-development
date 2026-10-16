"""Sends audit progress and completion messages to the customer through the bot."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.db.models import AuditJob, User
from pca.domain.enums import AuditJobStatus

log = logging.getLogger("pca.worker.notifier")

_END_TEXT = {
    AuditJobStatus.COMPLETED.value: "Audit complete",
    AuditJobStatus.PARTIAL.value: "Audit finished with partial results",
    AuditJobStatus.CANCELLED.value: "Audit cancelled",
    AuditJobStatus.FAILED.value: "Audit failed",
}


def progress_text(job: AuditJob) -> str:
    total = job.expected_requests
    scanned = job.scanned_count
    if total:
        pct = min(100, round(scanned * 100 / total))
        line = f"{scanned:,} of about {total:,} requests scanned ({pct}%)"
    else:
        line = f"{scanned:,} requests scanned"
    if job.status == AuditJobStatus.WAITING_FOR_TELEGRAM.value:
        line += "\nTelegram asked us to slow down; the audit resumes automatically."
    return f"<b>Audit #{job.id} in progress</b>\n{line}"


def finished_text(job: AuditJob, channel_title: str) -> str:
    head = _END_TEXT.get(job.status, "Audit finished")
    lines = [f"<b>{head}</b> for {escape(channel_title or 'your channel')}", f"Requests scanned: {job.scanned_count:,}"]
    if job.charged_credits is not None:
        lines.append(f"Credits charged: {job.charged_credits}")
    if job.status in (AuditJobStatus.COMPLETED.value, AuditJobStatus.PARTIAL.value):
        lines.append(f"Open the report from My audits, or send /report_{job.id}.")
    return "\n".join(lines)


class BotNotifier:
    def __init__(self, bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker

    async def _chat_id(self, job: AuditJob) -> int | None:
        if job.progress_chat_id:
            return job.progress_chat_id
        async with self.sessionmaker() as session:
            user = await session.get(User, job.user_id)
            return user.telegram_user_id if user else None

    async def progress(self, job: AuditJob) -> None:
        chat_id = await self._chat_id(job)
        if chat_id is None:
            return
        text = progress_text(job)
        if job.progress_message_id:
            try:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=job.progress_message_id)
                return
            except TelegramBadRequest as exc:
                if "not modified" in exc.message:
                    return
        msg = await self.bot.send_message(chat_id, text)
        async with self.sessionmaker() as session:
            row = await session.get(AuditJob, job.id)
            if row is not None:
                row.progress_chat_id = chat_id
                row.progress_message_id = msg.message_id
                await session.commit()

    async def finished(self, job: AuditJob, channel_title: str) -> None:
        chat_id = await self._chat_id(job)
        if chat_id is None:
            return
        try:
            await self.bot.send_message(chat_id, finished_text(job, channel_title))
        except TelegramAPIError as exc:
            log.warning("could not deliver audit result: %s", exc.message)
