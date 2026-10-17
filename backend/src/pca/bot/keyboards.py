from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pca.services import audience


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


PAGE_SIZE = 8


class PageCb(CallbackData, prefix="pg"):
    """Page navigation for any list. ``list`` names the list; ``ref`` carries context (e.g. comparison base)."""

    list: str
    page: int
    ref: int = 0


def page_bounds(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int, int]:
    """Clamp ``page`` into range and return (page, start, end)."""
    pages = max(1, -(-total // size))
    page = min(max(page, 0), pages - 1)
    return page, page * size, min(total, (page + 1) * size)


def _nav_row(builder: InlineKeyboardBuilder, list_name: str, page: int, total: int, ref: int = 0) -> int:
    pages = max(1, -(-total // PAGE_SIZE))
    if pages <= 1:
        return 0
    count = 0
    if page > 0:
        builder.button(text="‹ Prev", callback_data=PageCb(list=list_name, page=page - 1, ref=ref))
        count += 1
    builder.button(text=f"{page + 1}/{pages}", callback_data=PageCb(list=list_name, page=page, ref=ref))
    count += 1
    if page < pages - 1:
        builder.button(text="Next ›", callback_data=PageCb(list=list_name, page=page + 1, ref=ref))
        count += 1
    return count


def channel_list(items: list[tuple[int, str, bool]], action: str, page: int = 0) -> InlineKeyboardMarkup:
    """items: (authorization id, title, can audit). Paginated so large admin lists stay usable."""
    page, start, end = page_bounds(len(items), page)
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    for auth_id, title, can_audit in items[start:end]:
        mark = "✅" if can_audit else "🚫"
        builder.button(text=f"{mark} {title[:48]}", callback_data=ChanCb(action=action, id=auth_id))
        rows.append(1)
    nav = _nav_row(builder, f"ch_{action}", page, len(items))
    if nav:
        rows.append(nav)
    if action == "view":
        builder.button(text="🔄 Refresh from Telegram", callback_data=MenuCb(action="channels"))
        rows.append(1)
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


class AudCb(CallbackData, prefix="au"):
    """Audit actions. ``id`` is an authorization id for confirm, otherwise an audit job id."""

    action: str
    id: int
    ref: int = 0


def audit_confirm(auth_id: int, affordable: bool, country: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = audience.country_label(country)
    builder.button(text=f"🌍 Target country: {label}", callback_data=AudCb(action="pick", id=auth_id, ref=country))
    if affordable:
        builder.button(text="✅ Confirm and start audit", callback_data=AudCb(action="confirm", id=auth_id, ref=country))
    else:
        builder.button(text="💳 Buy Credits", callback_data=MenuCb(action="buy"))
    builder.button(text="✖️ Cancel", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def audit_countries(auth_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, country in enumerate(audience.COUNTRIES, start=1):
        builder.button(text=country.name, callback_data=AudCb(action="quote", id=auth_id, ref=index))
    builder.button(text="No target country", callback_data=AudCb(action="quote", id=auth_id, ref=0))
    builder.adjust(2)
    return builder.as_markup()


def audit_running(job_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh progress", callback_data=AudCb(action="open", id=job_id))
    builder.button(text="⏹ Cancel audit", callback_data=AudCb(action="cancel_ask", id=job_id))
    builder.adjust(1)
    return builder.as_markup()


def audit_cancel_confirm(job_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Yes, cancel the audit", callback_data=AudCb(action="cancel", id=job_id))
    builder.button(text="Keep running", callback_data=AudCb(action="open", id=job_id))
    builder.adjust(1)
    return builder.as_markup()


def audit_report(job_id: int, *, has_csv: bool, has_audience: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 PDF report", callback_data=AudCb(action="pdf", id=job_id))
    if has_csv:
        builder.button(text="📑 CSV export", callback_data=AudCb(action="csv", id=job_id))
    if has_audience:
        builder.button(text="🌍 Audience CSV", callback_data=AudCb(action="aud_csv", id=job_id))
    builder.button(text="⚖️ Compare with…", callback_data=PageCb(list="cmp_b", page=0, ref=job_id))
    builder.button(text="📊 My Audits", callback_data=MenuCb(action="audits"))
    builder.adjust(2, 1, 1, 1)
    return builder.as_markup()


def audit_closed() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 New Audit", callback_data=MenuCb(action="audit"))
    builder.button(text="📊 My Audits", callback_data=MenuCb(action="audits"))
    builder.adjust(1)
    return builder.as_markup()


def compare_result(a_id: int, b_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 Comparison PDF", callback_data=AudCb(action="cmp_pdf", id=b_id, ref=a_id))
    builder.button(text=f"Report #{a_id}", callback_data=AudCb(action="open", id=a_id))
    builder.button(text=f"Report #{b_id}", callback_data=AudCb(action="open", id=b_id))
    builder.button(text="⚖️ New comparison", callback_data=MenuCb(action="compare"))
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def audit_list(items: list[tuple[int, str]], page: int, list_name: str = "audits", ref: int = 0) -> InlineKeyboardMarkup:
    """items: (audit job id, label). ``cmp_a``/``cmp_b`` lists pick comparison sides."""
    page, start, end = page_bounds(len(items), page)
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    for job_id, label in items[start:end]:
        if list_name == "cmp_a":
            data = PageCb(list="cmp_b", page=0, ref=job_id)
        elif list_name == "cmp_b":
            data = AudCb(action="compare", id=job_id, ref=ref)
        else:
            data = AudCb(action="open", id=job_id)
        builder.button(text=label[:60], callback_data=data)
        rows.append(1)
    nav = _nav_row(builder, list_name, page, len(items), ref)
    if nav:
        rows.append(nav)
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


class BuyCb(CallbackData, prefix="buy"):
    action: str
    id: int = 0


def package_list(packages: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for package_id, label in packages:
        builder.button(text=label[:60], callback_data=BuyCb(action="pkg", id=package_id))
    builder.button(text="🧾 Payment history", callback_data=BuyCb(action="history"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def pay_link(url: str, payment_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Pay securely with Paystack", url=url)
    builder.button(text="🔄 I've paid — check status", callback_data=BuyCb(action="check", id=payment_id))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def wallet_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Buy Credits", callback_data=MenuCb(action="buy"))
    builder.button(text="🧾 Payment history", callback_data=BuyCb(action="history"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(2, 1)
    return builder.as_markup()


class SupCb(CallbackData, prefix="sp"):
    action: str


def support_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Message support", callback_data=SupCb(action="write"))
    builder.button(text="⬅️ Main menu", callback_data=MenuCb(action="home"))
    builder.adjust(1)
    return builder.as_markup()


def cancel_input() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data=SupCb(action="cancel"))
    return builder.as_markup()


def admin_dashboard(url: str | None) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🛠 Open admin dashboard", web_app=WebAppInfo(url=url))
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
