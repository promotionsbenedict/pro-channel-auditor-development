"""Audit launch, progress, history, reports, exports and comparisons. Gated by AccessGateMiddleware."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from pca.bot import keyboards, texts
from pca.bot.keyboards import AudCb, PageCb
from pca.bot.ui import respond
from pca.config import Settings
from pca.db.models import User
from pca.domain.enums import TERMINAL_AUDIT_STATUSES as FINAL_AUDIT_STATUSES
from pca.services import audit_engine, ledger, report_pdf, reports
from pca.services import channels as channel_service
from pca.services.audits import list_user_audits
from pca.services.users import get_active_connection

log = logging.getLogger(__name__)

router = Router(name="reports")
router.message.filter(F.chat.type == "private")

SENSITIVE = {"sensitive": True}
HISTORY_LIMIT = 200


def _safe_name(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_")[:40] or "channel"


async def _alert(query: CallbackQuery, text: str) -> None:
    await query.answer(text, show_alert=True)


# --------------------------------------------------------------------------- launch


@router.callback_query(AudCb.filter(F.action == "quote"), flags=SENSITIVE)
async def show_quote(
    query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    connection = await get_active_connection(session, db_user.id)
    found = await channel_service.get_authorization(session, connection, callback_data.id) if connection else None
    if found is None:
        await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
        return
    auth, channel = found
    try:
        quote = await audit_engine.quote_audit(session, settings, auth)
    except audit_engine.AuditError as exc:
        await respond(query, texts.audit_error(exc.code), keyboards.back_to_menu())
        return
    except Exception:  # pricing misconfiguration
        log.exception("audit quote failed")
        await respond(query, texts.GENERIC_ERROR, keyboards.back_to_menu())
        return
    wallet = await ledger.get_wallet(session, db_user.id)
    available = wallet.available_credits if wallet else 0
    await respond(
        query,
        texts.audit_quote_text(channel, quote, available),
        keyboards.audit_confirm(auth.id, available >= quote.credits),
    )


@router.callback_query(AudCb.filter(F.action == "confirm"), flags=SENSITIVE)
async def confirm_audit(
    query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    connection = await get_active_connection(session, db_user.id)
    found = await channel_service.get_authorization(session, connection, callback_data.id) if connection else None
    if found is None or not found[0].can_manage_join_requests:
        await respond(query, texts.CHANNEL_GONE, keyboards.back_to_menu())
        return
    auth, channel = found
    try:
        job = await audit_engine.create_audit(
            session, settings, user_id=db_user.id, conn=connection, auth=auth, channel=channel, chat_id=query.from_user.id
        )
    except audit_engine.AuditError as exc:
        await session.rollback()
        await respond(query, texts.audit_error(exc.code), keyboards.back_to_menu())
        return
    except ledger.InsufficientCreditsError:
        await session.rollback()
        await respond(query, texts.audit_error("insufficient_credits"), keyboards.audit_confirm(auth.id, False))
        return
    await session.commit()
    await respond(query, texts.audit_started_text(job, channel.title), keyboards.audit_running(job.id))


# --------------------------------------------------------------------------- open / cancel


async def _show_audit(event: Message | CallbackQuery, session: AsyncSession, user_id: int, job_id: int) -> None:
    view = await reports.load_report(session, user_id, job_id)
    if view is None:
        await respond(event, texts.AUDIT_NOT_FOUND, keyboards.back_to_menu())
        return
    if view.status not in FINAL_AUDIT_STATUSES:
        await respond(event, texts.audit_progress_text(view), keyboards.audit_running(view.job_id))
        return
    await respond(
        event,
        reports.render_text(view),
        keyboards.audit_report(view.job_id, has_csv=view.reportable) if view.reportable else keyboards.audit_closed(),
    )


@router.callback_query(AudCb.filter(F.action == "open"), flags=SENSITIVE)
async def open_audit(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    await _show_audit(query, session, db_user.id, callback_data.id)


@router.message(F.text.regexp(r"^/report_(\d{1,12})(@\w+)?$").as_("match"), flags=SENSITIVE)
async def report_command(message: Message, match: re.Match, session: AsyncSession, db_user: User) -> None:
    await _show_audit(message, session, db_user.id, int(match.group(1)))


@router.message(Command("report"), flags=SENSITIVE)
async def report_arg_command(message: Message, command: CommandObject, session: AsyncSession, db_user: User) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer(texts.REPORT_USAGE)
        return
    await _show_audit(message, session, db_user.id, int(arg))


@router.callback_query(AudCb.filter(F.action == "cancel_ask"), flags=SENSITIVE)
async def cancel_ask(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    job = await audit_engine.get_user_job(session, db_user.id, callback_data.id)
    if job is None:
        await respond(query, texts.AUDIT_NOT_FOUND, keyboards.back_to_menu())
        return
    if job.status in FINAL_AUDIT_STATUSES:
        await _show_audit(query, session, db_user.id, job.id)
        return
    await respond(query, texts.AUDIT_CANCEL_CONFIRM, keyboards.audit_cancel_confirm(job.id))


@router.callback_query(AudCb.filter(F.action == "cancel"), flags=SENSITIVE)
async def cancel_audit(
    query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    job = await audit_engine.request_cancel(session, settings, db_user.id, callback_data.id)
    await session.commit()
    if job is None:
        await respond(query, texts.AUDIT_NOT_FOUND, keyboards.back_to_menu())
        return
    await _show_audit(query, session, db_user.id, job.id)


# --------------------------------------------------------------------------- exports


@router.callback_query(AudCb.filter(F.action == "pdf"), flags=SENSITIVE)
async def send_pdf(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    view = await reports.load_report(session, db_user.id, callback_data.id)
    if view is None or not view.reportable:
        await _alert(query, texts.REPORT_UNAVAILABLE)
        return
    await query.answer("Preparing PDF…")
    data = report_pdf.render_report_pdf(view)
    name = f"audit_{view.job_id}_{_safe_name(view.channel_title)}.pdf"
    await query.bot.send_document(
        query.from_user.id, BufferedInputFile(data, filename=name), caption=f"Audit #{view.job_id} report"
    )


@router.callback_query(AudCb.filter(F.action == "csv"), flags=SENSITIVE)
async def send_csv(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    view = await reports.load_report(session, db_user.id, callback_data.id)
    data = await reports.export_csv(session, view) if view is not None else None
    if data is None:
        await _alert(query, texts.CSV_UNAVAILABLE)
        return
    await query.answer()
    name = f"audit_{view.job_id}_{_safe_name(view.channel_title)}.csv"
    await query.bot.send_document(
        query.from_user.id, BufferedInputFile(data, filename=name), caption=texts.CSV_CAPTION
    )


# --------------------------------------------------------------------------- history


def _labels(items) -> list[tuple[int, str]]:
    return [
        (i.job_id, f"#{i.job_id} · {i.channel_title} · {reports.STATUS_LABEL.get(i.status, i.status)} · {i.created_at:%d %b}")
        for i in items
    ]


async def _history(event, session: AsyncSession, user_id: int, page: int) -> None:
    items = await list_user_audits(session, user_id, HISTORY_LIMIT)
    if not items:
        await respond(event, texts.audits_text([]), keyboards.back_to_menu())
        return
    await respond(event, texts.audit_history_text(len(items)), keyboards.audit_list(_labels(items), page))


@router.message(Command("my_audits"), flags=SENSITIVE)
@router.callback_query(keyboards.MenuCb.filter(F.action == "audits"), flags=SENSITIVE)
async def show_audits(event: Message | CallbackQuery, session: AsyncSession, db_user: User) -> None:
    await _history(event, session, db_user.id, 0)


# --------------------------------------------------------------------------- comparison


async def _compare_pick_a(event, session: AsyncSession, user_id: int, page: int) -> None:
    items = await list_user_audits(session, user_id, HISTORY_LIMIT, completed_only=True)
    if len(items) < 2:
        await respond(event, texts.COMPARE_EMPTY, keyboards.back_to_menu())
        return
    await respond(event, texts.COMPARE_PICK_A, keyboards.audit_list(_labels(items), page, "cmp_a"))


async def _compare_pick_b(event, session: AsyncSession, user_id: int, base_id: int, page: int) -> None:
    base = await reports.load_report(session, user_id, base_id)
    if base is None or base.status != "completed":
        await respond(event, texts.COMPARE_BASE_NOT_COMPLETED, keyboards.back_to_menu())
        return
    items = await list_user_audits(session, user_id, HISTORY_LIMIT, completed_only=True, exclude_job_id=base_id)
    if not items:
        await respond(event, texts.COMPARE_EMPTY, keyboards.back_to_menu())
        return
    await respond(event, texts.compare_pick_b(base.channel_title, base.job_id), keyboards.audit_list(_labels(items), page, "cmp_b", base_id))


@router.message(Command("compare"), flags=SENSITIVE)
@router.callback_query(keyboards.MenuCb.filter(F.action == "compare"), flags=SENSITIVE)
async def start_compare(event: Message | CallbackQuery, session: AsyncSession, db_user: User) -> None:
    await _compare_pick_a(event, session, db_user.id, 0)


@router.callback_query(AudCb.filter(F.action == "compare"), flags=SENSITIVE)
async def run_compare(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    a = await reports.load_report(session, db_user.id, callback_data.ref)
    b = await reports.load_report(session, db_user.id, callback_data.id)
    if a is None or b is None:
        await respond(query, texts.AUDIT_NOT_FOUND, keyboards.back_to_menu())
        return
    try:
        cmp = reports.build_comparison(a, b)
    except reports.ComparisonError as exc:
        await respond(query, texts.compare_error(exc.code), keyboards.back_to_menu())
        return
    await respond(query, reports.render_comparison_text(cmp), keyboards.compare_result(a.job_id, b.job_id))


@router.callback_query(AudCb.filter(F.action == "cmp_pdf"), flags=SENSITIVE)
async def send_compare_pdf(query: CallbackQuery, callback_data: AudCb, session: AsyncSession, db_user: User) -> None:
    a = await reports.load_report(session, db_user.id, callback_data.ref)
    b = await reports.load_report(session, db_user.id, callback_data.id)
    try:
        cmp = reports.build_comparison(a, b) if a and b else None
    except reports.ComparisonError:
        cmp = None
    if cmp is None:
        await _alert(query, texts.REPORT_UNAVAILABLE)
        return
    await query.answer("Preparing PDF…")
    await query.bot.send_document(
        query.from_user.id,
        BufferedInputFile(report_pdf.render_comparison_pdf(cmp), filename=f"comparison_{a.job_id}_vs_{b.job_id}.pdf"),
        caption=f"Comparison of audits #{a.job_id} and #{b.job_id}",
    )


# --------------------------------------------------------------------------- pagination


@router.callback_query(PageCb.filter(), flags=SENSITIVE)
async def paginate(query: CallbackQuery, callback_data: PageCb, session: AsyncSession, db_user: User) -> None:
    name, page = callback_data.list, callback_data.page
    if name == "audits":
        await _history(query, session, db_user.id, page)
    elif name == "cmp_a":
        await _compare_pick_a(query, session, db_user.id, page)
    elif name == "cmp_b":
        await _compare_pick_b(query, session, db_user.id, callback_data.ref, page)
    elif name.startswith("ch_"):
        action = name[3:]
        connection = await get_active_connection(session, db_user.id)
        if connection is None:
            await respond(query, texts.CHANNELS_REQUIRES_CONNECTION, keyboards.account_menu(False))
            return
        rows = await channel_service.list_channels(session, connection)
        if action == "audit":
            items = [(a.id, c.title, True) for a, c in rows if a.can_manage_join_requests]
            text = texts.AUDIT_PICK
        else:
            items = [(a.id, c.title, a.can_manage_join_requests) for a, c in rows]
            text = texts.channels_text(len(items), sum(1 for i in items if i[2]))
        await respond(query, text, keyboards.channel_list(items, action, page))
    else:
        await query.answer()
