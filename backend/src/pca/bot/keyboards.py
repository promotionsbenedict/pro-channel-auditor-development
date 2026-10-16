from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder


class MenuCb(CallbackData, prefix="m"):
    action: str


class SubCb(CallbackData, prefix="sub"):
    action: str


class PrivacyCb(CallbackData, prefix="prv"):
    action: str


MAIN_MENU: tuple[tuple[str, str], ...] = (
    ("🔍 New Audit", "audit"),
    ("📊 My Audits", "audits"),
    ("⚖️ Compare Channels", "compare"),
    ("💳 Buy Credits", "buy"),
    ("💰 My Wallet", "wallet"),
    ("🔗 Connect Telegram Account", "connect"),
    ("👤 Connected Account", "account"),
    ("📢 My Channels", "channels"),
    ("📚 Help & Support", "help"),
)


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, action in MAIN_MENU:
        builder.button(text=label, callback_data=MenuCb(action=action))
    builder.adjust(2, 2, 1, 1, 1, 1, 1)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    return builder.as_markup()


class ChanCb(CallbackData, prefix="ch"):
    action: str
    id: int


def connect_webapp(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔐 Open secure connection page", web_app=WebAppInfo(url=url))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def account_menu(connected: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if connected:
        builder.button(text="📢 My Channels", callback_data=MenuCb(action="channels"))
        builder.button(text="🔄 Reconnect", callback_data=MenuCb(action="connect"))
    else:
        builder.button(text="🔗 Connect Telegram Account", callback_data=MenuCb(action="connect"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def channel_list(items: list[tuple[int, str, bool]], action: str) -> InlineKeyboardMarkup:
    """items: (authorization id, title, can audit)."""
    builder = InlineKeyboardBuilder()
    for auth_id, title, can_audit in items[:50]:
        mark = "✅" if can_audit else "🚫"
        builder.button(text=f"{mark} {title[:48]}", callback_data=ChanCb(action=action, id=auth_id))
    if action == "view":
        builder.button(text="🔄 Refresh", callback_data=MenuCb(action="channels"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def channel_detail(auth_id: int, can_audit: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_audit:
        builder.button(text="🔍 Audit this channel", callback_data=ChanCb(action="audit", id=auth_id))
    builder.button(text="🔄 Re-check permissions", callback_data=ChanCb(action="view", id=auth_id))
    builder.button(text="⬅️ My Channels", callback_data=MenuCb(action="channels"))
    builder.adjust(1)
    return builder.as_markup()


def reconnect() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Connect again", callback_data=MenuCb(action="connect"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def subscription_gate(channel_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Join Our Channel", url=channel_url)
    builder.button(text="✅ I've Joined — Verify", callback_data=SubCb(action="verify"))
    builder.adjust(1)
    return builder.as_markup()


def retry_verification() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Try again", callback_data=SubCb(action="verify"))
    return builder.as_markup()


def help_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔐 Privacy", callback_data=PrivacyCb(action="show"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def privacy_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Request data deletion", callback_data=PrivacyCb(action="delete"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def confirm_deletion() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Yes, request deletion", callback_data=PrivacyCb(action="delete_confirm"))
    builder.button(text="Cancel", callback_data=PrivacyCb(action="show"))
    builder.adjust(1)
    return builder.as_markup()
