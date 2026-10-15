from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


async def respond(
    event: Message | CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Reply to a command, or edit the originating message for button presses."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    return
                logger.debug("edit failed, sending new message: %s", exc.message)
        if event.bot is not None:
            await event.bot.send_message(
                event.from_user.id, text, reply_markup=reply_markup, disable_web_page_preview=True
            )
        return
    await event.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
