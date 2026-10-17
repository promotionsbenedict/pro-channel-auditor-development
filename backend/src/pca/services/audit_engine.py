"""Audit lifecycle: create (reserve credits), claim, scan page by page, finalise.

Credit policy (explicit and enforced here only):
- Credits are reserved when the audit is created.
- They are captured only when the scan COMPLETES and at least one requester was read.
- Partial, failed and cancelled audits release the full reservation.
Partial audits happen only for system-side reasons (time limit, lost access,
session revoked, repeated Telegram errors), so the customer is never charged
for an incomplete result. A cancelled audit's data is deleted immediately.

The scan is strictly read-only: it lists pending join requests and never
approves, declines or otherwise changes them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.config import Settings
from pca.db.models import AuditJob, AuditRequester, Channel, ChannelAuthorization, TelegramConnection
from pca.domain.enums import AuditJobStatus, ConnectionStatus, LastSeenCategory
from pca.security.crypto import Keyring
from pca.services import audience, ledger, pricing
from pca.services.connection import run_with_session
from pca.services.reports import required_scanned
from pca.telegram.errors import SessionInvalidError, TelegramGatewayError
from pca.telegram.types import AccountGateway, JoinRequestPage, ScanCursor

log = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    AuditJobStatus.PENDING.value,
    AuditJobStatus.QUEUED.value,
    AuditJobStatus.RUNNING.value,
    AuditJobStatus.WAITING_FOR_TELEGRAM.value,
)
FINAL_STATUSES = (
    AuditJobStatus.COMPLETED.value,
    AuditJobStatus.PARTIAL.value,
    AuditJobStatus.FAILED.value,
    AuditJobStatus.CANCELLED.value,
)
_ACCESS_LOST = {"channel_unavailable", "insufficient_rights"}


class AuditError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AuditNotifier(Protocol):
    async def progress(self, job: AuditJob) -> None: ...

    async def finished(self, job: AuditJob, channel_title: str) -> None: ...


class NullNotifier:
    async def progress(self, job: AuditJob) -> None:
        return None

    async def finished(self, job: AuditJob, channel_title: str) -> None:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def requester_ref(settings: Settings, job_id: int, user_id: int) -> str:
    """Job-scoped keyed hash: de-duplicates pages without storing the raw Telegram id."""
    key = hashlib.sha256(b"pca-requester-ref:" + settings.session_keys_raw.get_secret_value().encode()).digest()
    return hmac.new(key, f"{job_id}:{user_id}".encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- creation


@dataclass(frozen=True)
class AuditQuote:
    pending_requests: int
    scan_target: int
    credits: int
    capped: bool


async def quote_audit(session: AsyncSession, settings: Settings, auth: ChannelAuthorization) -> AuditQuote:
    pending = auth.pending_requests or 0
    if pending < 1:
        raise AuditError("nothing_to_audit")
    target = min(pending, settings.audit_max_requests)
    q = await pricing.quote(session, target)
    return AuditQuote(pending_requests=pending, scan_target=target, credits=q.credits, capped=target < pending)


async def create_audit(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    conn: TelegramConnection,
    auth: ChannelAuthorization,
    channel: Channel,
    chat_id: int | None,
    target_country: str | None = None,
) -> AuditJob:
    active = (
        await session.execute(
            select(func.count()).select_from(AuditJob).where(
                AuditJob.user_id == user_id, AuditJob.status.in_(ACTIVE_STATUSES)
            )
        )
    ).scalar_one()
    if active >= settings.audit_max_active_per_user:
        raise AuditError("too_many_active")

    q = await quote_audit(session, settings, auth)
    job = AuditJob(
        user_id=user_id,
        channel_id=channel.id,
        telegram_connection_id=conn.id,
        channel_authorization_id=auth.id,
        status=AuditJobStatus.QUEUED.value,
        expected_requests=q.pending_requests,
        quoted_credits=q.credits,
        reserved_credits=q.credits,
        scan_limit=q.scan_target,
        target_country=target_country if target_country in audience.COUNTRY_BY_CODE else None,
        progress_chat_id=chat_id,
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError as exc:
        raise AuditError("already_running") from exc
    if q.credits > 0:
        await ledger.reserve_for_audit(session, user_id=user_id, audit_job_id=job.id, credits=q.credits)
    await session.flush()
    log.info("audit queued", extra={"audit_job_id": job.id, "credits": q.credits, "target": q.scan_target})
    return job


async def get_user_job(session: AsyncSession, user_id: int, job_id: int, *, lock: bool = False) -> AuditJob | None:
    stmt = select(AuditJob).where(AuditJob.id == job_id, AuditJob.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def request_cancel(session: AsyncSession, settings: Settings, user_id: int, job_id: int) -> AuditJob | None:
    job = await get_user_job(session, user_id, job_id, lock=True)
    if job is None or job.status in FINAL_STATUSES:
        return job
    job.cancel_requested = True
    lease_live = job.lease_expires_at is not None and job.lease_expires_at > _now()
    if job.status != AuditJobStatus.RUNNING.value or not lease_live:
        await finalize(session, settings, job, AuditJobStatus.CANCELLED, "cancelled_by_user")
    await session.flush()
    return job


# --------------------------------------------------------------------------- claiming


async def claim_next(session: AsyncSession, settings: Settings, worker_id: str) -> AuditJob | None:
    """Claim one runnable job. SKIP LOCKED lets several workers poll safely; a job whose
    lease expired (crashed worker) is reclaimable and resumes from its checkpoint.
    Only one job per Telegram account runs at a time."""
    now = _now()
    busy = (
        select(AuditJob.telegram_connection_id)
        .where(
            AuditJob.status == AuditJobStatus.RUNNING.value,
            AuditJob.lease_expires_at > now,
            AuditJob.telegram_connection_id.is_not(None),
        )
        .scalar_subquery()
    )
    stmt = (
        select(AuditJob)
        .where(
            or_(
                and_(
                    AuditJob.status.in_((AuditJobStatus.QUEUED.value, AuditJobStatus.WAITING_FOR_TELEGRAM.value)),
                    or_(AuditJob.not_before.is_(None), AuditJob.not_before <= now),
                ),
                and_(AuditJob.status == AuditJobStatus.RUNNING.value, AuditJob.lease_expires_at <= now),
            ),
            or_(AuditJob.telegram_connection_id.is_(None), AuditJob.telegram_connection_id.not_in(busy)),
        )
        .order_by(AuditJob.not_before.asc().nulls_first(), AuditJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None
    # Time between the planned resume point (end of a park, or lease expiry after a crash) and
    # this claim is downtime, not scan time, so it must not eat the run-time budget.
    resumes_at = job.lease_expires_at if job.status == AuditJobStatus.RUNNING.value else job.not_before
    if job.started_at is not None and resumes_at is not None and resumes_at < now:
        job.paused_seconds += int((now - resumes_at).total_seconds())
    job.status = AuditJobStatus.RUNNING.value
    job.locked_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
    job.attempts += 1
    job.not_before = None
    if job.started_at is None:
        job.started_at = now
    await session.flush()
    return job


# --------------------------------------------------------------------------- finalisation


async def build_summary(session: AsyncSession, job: AuditJob) -> dict[str, Any]:
    r = AuditRequester
    cols = [func.count().label("total")]
    for cat in LastSeenCategory:
        cols.append(func.count().filter(r.last_seen == cat.value).label(f"seen_{cat.value}"))
    for flag in ("has_photo", "has_username", "is_premium", "is_deleted", "is_bot", "is_scam", "is_fake", "via_chatlist"):
        cols.append(func.count().filter(getattr(r, flag).is_(True)).label(flag))
    row = (await session.execute(select(*cols).where(r.audit_job_id == job.id))).one()._mapping
    total = int(row["total"])
    last_seen = {cat.value: int(row[f"seen_{cat.value}"]) for cat in LastSeenCategory}
    attrs = {
        k: int(row[k])
        for k in ("has_photo", "has_username", "is_premium", "is_deleted", "is_bot", "is_scam", "is_fake", "via_chatlist")
    }

    def share(n: int) -> float:
        return round(n / total, 4) if total else 0.0

    engaged = last_seen["online"] + last_seen["offline_24h"] + last_seen["recently"]
    risk = attrs["is_deleted"] + attrs["is_bot"] + attrs["is_scam"] + attrs["is_fake"]
    generated = _now()
    request_age, timeline = await _request_timing(session, job.id, generated)
    expected = job.expected_requests or 0
    live_total = (job.checkpoint or {}).get("live_total")
    withdrawn = max(expected - int(live_total), 0) if live_total else 0
    return {
        "version": 2,
        "request_age": request_age,
        "timeline": timeline,
        "scanned": total,
        "expected": expected,
        "live_total": int(live_total) if live_total else None,
        "withdrawn": withdrawn,
        "coverage": round(total / expected, 4) if expected else 0.0,
        "last_seen": last_seen,
        "attributes": attrs,
        "indicators": {
            "recent_activity_share": share(engaged),
            "confirmed_24h_share": share(last_seen["online"] + last_seen["offline_24h"]),
            "inactive_or_unknown_share": share(last_seen["older"] + last_seen["hidden"]),
            "risk_flag_share": share(risk),
            "no_photo_share": share(total - attrs["has_photo"]),
            "no_username_share": share(total - attrs["has_username"]),
        },
        "audience": await audience.build_audience(session, job),
        "generated_at": generated.isoformat(),
    }


AGE_BUCKETS: tuple[tuple[str, int], ...] = (("lt_1d", 1), ("d1_7", 7), ("d7_30", 30), ("d30_90", 90))
TIMELINE_DAYS = 90


async def _request_timing(
    session: AsyncSession, job_id: int, now: datetime
) -> tuple[dict[str, int], dict[str, Any]]:
    """Request age buckets and a daily arrival histogram (UTC), measured from ``now``."""
    r = AuditRequester
    known = r.requested_at.is_not(None)
    cols = [func.count().filter(r.requested_at.is_(None)).label("unknown")]
    lower = now
    for name, days in AGE_BUCKETS:
        edge = now - timedelta(days=days)
        cols.append(func.count().filter(known, r.requested_at >= edge, r.requested_at < lower).label(name))
        lower = edge
    cols.append(func.count().filter(known, r.requested_at < lower).label("gt_90"))
    # Requests stamped slightly in the future (clock skew) belong to the newest bucket.
    cols.append(func.count().filter(known, r.requested_at >= now).label("future"))
    row = (await session.execute(select(*cols).where(r.audit_job_id == job_id))).one()._mapping
    age = {name: int(row[name]) for name, _ in AGE_BUCKETS}
    age["lt_1d"] += int(row["future"])
    age["gt_90"] = int(row["gt_90"])
    age["unknown"] = int(row["unknown"])

    since = now - timedelta(days=TIMELINE_DAYS)
    day = func.date(func.timezone("UTC", r.requested_at))
    daily_rows = (
        await session.execute(
            select(day, func.count())
            .where(r.audit_job_id == job_id, known, r.requested_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    daily = {d.isoformat(): int(n) for d, n in daily_rows}
    earlier = (
        await session.execute(
            select(func.count()).where(r.audit_job_id == job_id, known, r.requested_at < since)
        )
    ).scalar_one()
    return age, {"daily": daily, "earlier": int(earlier), "unknown": age["unknown"], "days": TIMELINE_DAYS}


async def finalize(
    session: AsyncSession, settings: Settings, job: AuditJob, status: AuditJobStatus, error_code: str | None = None
) -> AuditJob:
    if job.status in FINAL_STATUSES:
        return job
    if status is AuditJobStatus.CANCELLED:
        await session.execute(delete(AuditRequester).where(AuditRequester.audit_job_id == job.id))
        job.summary = None
    else:
        job.summary = await build_summary(session, job)
        await session.execute(
            update(AuditRequester).where(AuditRequester.audit_job_id == job.id).values(requester_ref=None)
        )
    charge = job.reserved_credits if status is AuditJobStatus.COMPLETED and job.scanned_count > 0 else 0
    if job.reserved_credits > 0:
        try:
            if charge:
                await ledger.settle_audit(session, user_id=job.user_id, audit_job_id=job.id, charge=charge)
            else:
                await ledger.release_audit(session, user_id=job.user_id, audit_job_id=job.id)
        except ledger.AlreadyFinalizedError:
            log.info("audit reservation already finalised", extra={"audit_job_id": job.id})
    job.charged_credits = charge
    job.status = status.value
    job.error_code = error_code
    job.completed_at = _now()
    job.locked_by = None
    job.lease_expires_at = None
    job.checkpoint = None
    job.not_before = None
    await session.flush()
    log.info("audit finalised", extra={"audit_job_id": job.id, "status": status.value, "charged": charge})
    return job


def _end_status(job: AuditJob) -> AuditJobStatus:
    return AuditJobStatus.PARTIAL if job.scanned_count > 0 else AuditJobStatus.FAILED


def _active_seconds(job: AuditJob, now: datetime) -> float:
    """Run time that counts toward the duration limit: wall time minus time spent paused on
    Telegram flood waits, error backoff and worker downtime."""
    if job.started_at is None:
        return 0.0
    return max((now - job.started_at).total_seconds() - job.paused_seconds, 0.0)


def _checkpoint(
    cursor: ScanCursor, *, live_total: int | None = None, stalls: int = 0, floods: int = 0
) -> dict[str, int]:
    """Persisted resume point. Counters are included only when non-zero, so a page of real
    progress (which writes a plain checkpoint) resets them."""
    data = cursor.as_dict()
    if live_total:
        data["live_total"] = int(live_total)
    if stalls:
        data["stalls"] = stalls
    if floods:
        data["floods"] = floods
    return data


# --------------------------------------------------------------------------- scanning


async def _store_page(session: AsyncSession, settings: Settings, job: AuditJob, page: JoinRequestPage) -> int:
    if not page.items:
        return 0
    room = (job.scan_limit or settings.audit_max_requests) - job.scanned_count
    if room <= 0:
        return 0
    # Drop rows already stored (pages overlap) before applying the room cap, so the
    # repeated cursor row can never take the last free slot.
    refs = {it.user_id: requester_ref(settings, job.id, it.user_id) for it in page.items}
    existing = set(
        (
            await session.execute(
                select(AuditRequester.requester_ref).where(
                    AuditRequester.audit_job_id == job.id,
                    AuditRequester.requester_ref.in_(list(refs.values())),
                )
            )
        ).scalars()
    )
    seen: set[str] = set()
    fresh = []
    for it in page.items:
        ref = refs[it.user_id]
        if ref in existing or ref in seen:
            continue
        seen.add(ref)
        fresh.append(it)
    items = fresh[:room]
    if not items:
        return 0
    rows = [
        {
            "audit_job_id": job.id,
            "ordinal": job.scanned_count + i + 1,
            "requester_ref": refs[it.user_id],
            "requested_at": datetime.fromtimestamp(it.requested_at, UTC) if it.requested_at else None,
            "last_seen": it.last_seen,
            "has_photo": it.has_photo,
            "has_username": it.has_username,
            "is_premium": it.is_premium,
            "is_deleted": it.is_deleted,
            "is_bot": it.is_bot,
            "is_scam": it.is_scam,
            "is_fake": it.is_fake,
            "via_chatlist": it.via_chatlist,
            "name_script": it.name_script,
            "username_style": it.username_style,
            "name_style": it.name_style,
        }
        for i, it in enumerate(items)
    ]
    stmt = (
        pg_insert(AuditRequester)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=[AuditRequester.audit_job_id, AuditRequester.requester_ref],
            index_where=AuditRequester.requester_ref.is_not(None),
        )
        .returning(AuditRequester.id)
    )
    inserted = len((await session.execute(stmt)).all())
    job.scanned_count += inserted
    return inserted


@dataclass
class StepResult:
    done: bool
    sleep: float = 0.0


async def run_step(
    session: AsyncSession, settings: Settings, keyring: Keyring, gateway: AccountGateway, job_id: int, worker_id: str
) -> StepResult:
    """Fetch and persist one page. Returns done=True when the job left the running state."""
    job = await session.get(AuditJob, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.status != AuditJobStatus.RUNNING.value or job.locked_by != worker_id:
        return StepResult(done=True)
    if job.cancel_requested:
        await finalize(session, settings, job, AuditJobStatus.CANCELLED, "cancelled_by_user")
        return StepResult(done=True)
    started = _now()
    # The run-time limit counts active scanning only; Telegram-imposed waits are excluded.
    # A separate, much longer wall-clock ceiling stops a job holding credits indefinitely.
    if job.started_at and (
        _active_seconds(job, started) > settings.audit_max_duration_seconds
        or (started - job.started_at).total_seconds() > settings.audit_max_wall_seconds
    ):
        await finalize(session, settings, job, _end_status(job), "time_limit")
        return StepResult(done=True)

    conn = await session.get(TelegramConnection, job.telegram_connection_id) if job.telegram_connection_id else None
    auth = await session.get(ChannelAuthorization, job.channel_authorization_id) if job.channel_authorization_id else None
    channel = await session.get(Channel, job.channel_id)
    if conn is None or conn.connection_status != ConnectionStatus.ACTIVE.value:
        await finalize(session, settings, job, _end_status(job), "account_disconnected")
        return StepResult(done=True)
    if auth is None or channel is None:
        await finalize(session, settings, job, _end_status(job), "channel_unavailable")
        return StepResult(done=True)

    cursor = ScanCursor.from_dict(job.checkpoint)
    remaining = max((job.scan_limit or settings.audit_max_requests) - job.scanned_count, 0)
    # +1 because each page after the first repeats the cursor row. Re-asks after a stall use the
    # full page size: a shrunken limit would return a full page of already-seen rows and be
    # misread as a stuck cursor instead of confirming the end of the list.
    if int((job.checkpoint or {}).get("stalls", 0)) > 0:
        limit = settings.audit_page_size
    else:
        limit = min(settings.audit_page_size, remaining + 1)
    access_hash = auth.access_hash or 0
    try:
        page = await run_with_session(
            session,
            keyring,
            conn,
            lambda s: gateway.fetch_join_requests(s, channel.telegram_channel_id, access_hash, cursor, limit),
        )
    except SessionInvalidError:
        await finalize(session, settings, job, _end_status(job), "session_invalid")
        return StepResult(done=True)
    except TelegramGatewayError as exc:
        return await _handle_gateway_error(session, settings, job, exc)

    previous = job.checkpoint or {}
    stalls = int(previous.get("stalls", 0))
    # Latest positive count Telegram reported; withdrawn requests shrink it during a scan.
    live_total = page.total if page.total > 0 else int(previous.get("live_total") or 0)
    inserted = await _store_page(session, settings, job, page)
    if page.items:
        job.attempts = 0
    if page.total and job.expected_requests is not None and page.total > (job.expected_requests or 0):
        job.expected_requests = page.total
    now = _now()
    job.last_progress_at = now
    job.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
    scan_cap = job.scan_limit or settings.audit_max_requests

    # The disclosed scan limit was reached: complete under that scope.
    if job.scanned_count >= scan_cap:
        await finalize(session, settings, job, AuditJobStatus.COMPLETED)
        return StepResult(done=True)

    if inserted > 0 and page.next_cursor is not None:
        job.checkpoint = _checkpoint(page.next_cursor, live_total=live_total)
        await session.flush()
        return StepResult(done=False, sleep=settings.audit_page_delay_seconds)

    # Nothing new: an empty page, a short page, or only rows we already hold. That is not
    # proof of completion (Telegram can return transient empty/short pages), so re-ask a
    # few times with backoff (always at least one second) before treating the queue as done.
    stalls += 1
    resume_from = page.next_cursor if page.next_cursor is not None and page.next_cursor != cursor else cursor
    if stalls < settings.audit_stall_confirmations:
        job.checkpoint = _checkpoint(resume_from, live_total=live_total, stalls=stalls)
        backoff = max(settings.audit_stall_backoff_seconds, 1) * 2 ** (stalls - 1)
        job.lease_expires_at = now + timedelta(seconds=backoff + settings.worker_lease_seconds)
        await session.flush()
        log.info("audit scan stalled, retrying", extra={"audit_job_id": job.id, "stalls": stalls, "backoff": backoff})
        return StepResult(done=False, sleep=backoff)

    # A full page that adds nothing means the cursor is stuck, not that the list ended. The
    # end of the list is only confirmed by an empty or short page, whatever the percentage.
    if len(page.items) >= limit:
        log.warning("audit pagination stalled", extra={"audit_job_id": job.id, "scanned": job.scanned_count})
        await finalize(session, settings, job, _end_status(job), "pagination_stalled")
        return StepResult(done=True)

    # End confirmed. Requests withdrawn mid-scan shrink Telegram's live total, so the target
    # is the live total (falling back to the quoted one), held to the 99% floor.
    live = live_total or (job.expected_requests or 0)
    required = required_scanned(min(scan_cap, live), settings.audit_min_completion_ratio)
    if job.scanned_count >= required and job.scanned_count > 0:
        await finalize(session, settings, job, AuditJobStatus.COMPLETED)
    else:
        log.warning(
            "audit ended before reaching the expected count",
            extra={"audit_job_id": job.id, "scanned": job.scanned_count, "expected": live},
        )
        await finalize(session, settings, job, _end_status(job), "incomplete_scan")
    return StepResult(done=True)


async def _handle_gateway_error(
    session: AsyncSession, settings: Settings, job: AuditJob, exc: TelegramGatewayError
) -> StepResult:
    if exc.code in _ACCESS_LOST:
        await finalize(session, settings, job, _end_status(job), exc.code)
        return StepResult(done=True)
    if exc.code == "flood":
        wait = max(int(exc.retry_after or 30), 1)
        job.flood_waits += 1
        # Rate limits are Telegram's doing, not a failed attempt, and the checkpoint is left
        # untouched so the same page is retried after the wait.
        job.attempts = max(job.attempts - 1, 0)
        previous = job.checkpoint or {}
        floods = int(previous.get("floods", 0)) + 1
        if floods > settings.audit_max_consecutive_floods:
            await finalize(session, settings, job, _end_status(job), "flood_limit")
            return StepResult(done=True)
        resume = ScanCursor.from_dict(previous)
        job.checkpoint = _checkpoint(
            resume,
            live_total=int(previous.get("live_total") or 0),
            stalls=int(previous.get("stalls", 0)),
            floods=floods,
        )
        if wait <= settings.audit_flood_inline_max_seconds:
            job.paused_seconds += wait + 1
            job.lease_expires_at = _now() + timedelta(seconds=wait + settings.worker_lease_seconds)
            await session.flush()
            return StepResult(done=False, sleep=wait + 1)
        _park(job, wait + 5)
        await session.flush()
        log.info("audit parked on flood wait", extra={"audit_job_id": job.id, "seconds": wait})
        return StepResult(done=True)
    if job.attempts >= settings.audit_max_attempts:
        await finalize(session, settings, job, _end_status(job), exc.code)
        return StepResult(done=True)
    backoff = min(30 * 2 ** max(job.attempts - 1, 0), 1800)
    _park(job, backoff)
    job.error_code = exc.code
    await session.flush()
    log.warning("audit transient error", extra={"audit_job_id": job.id, "code": exc.code, "backoff": backoff})
    return StepResult(done=True)


def _park(job: AuditJob, seconds: int) -> None:
    job.paused_seconds += seconds
    job.status = AuditJobStatus.WAITING_FOR_TELEGRAM.value
    job.not_before = _now() + timedelta(seconds=seconds)
    job.locked_by = None
    job.lease_expires_at = None


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    keyring: Keyring,
    gateway: AccountGateway,
    job_id: int,
    worker_id: str,
    notifier: AuditNotifier,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    progress_every: float = 10.0,
) -> None:
    last_progress = 0.0
    loop = asyncio.get_running_loop()
    while True:
        async with session_factory() as session:
            try:
                result = await run_step(session, settings, keyring, gateway, job_id, worker_id)
                await session.commit()
            except Exception:
                await session.rollback()
                log.exception("audit step crashed", extra={"audit_job_id": job_id})
                async with session_factory() as s2:
                    job = await s2.get(AuditJob, job_id, with_for_update=True)
                    if job and job.status == AuditJobStatus.RUNNING.value and job.locked_by == worker_id:
                        if job.attempts >= settings.audit_max_attempts:
                            await finalize(s2, settings, job, _end_status(job), "internal_error")
                        else:
                            _park(job, 60)
                        await s2.commit()
                result = StepResult(done=True)
            job = await session.get(AuditJob, job_id, populate_existing=True)
            if job is not None:
                if job.status in FINAL_STATUSES:
                    channel = await session.get(Channel, job.channel_id)
                    await _safe(notifier.finished(job, channel.title if channel else ""))
                elif loop.time() - last_progress >= progress_every:
                    last_progress = loop.time()
                    await _safe(notifier.progress(job))
        if result.done:
            return
        if result.sleep:
            await sleep(result.sleep)


async def _safe(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception:
        log.warning("audit notification failed", exc_info=True)


async def purge_expired(session: AsyncSession, settings: Settings) -> int:
    """Delete per-requester rows past retention. Summaries (aggregates only) remain."""
    cutoff = _now() - timedelta(days=settings.audit_retention_days)
    old_jobs = select(AuditJob.id).where(AuditJob.status.in_(FINAL_STATUSES), AuditJob.completed_at < cutoff)
    result = await session.execute(delete(AuditRequester).where(AuditRequester.audit_job_id.in_(old_jobs)))
    return result.rowcount or 0
