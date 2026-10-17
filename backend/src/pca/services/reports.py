"""Audit reports: owner-scoped loading, Telegram text, minimised CSV and comparisons.

Everything here is derived from the stored aggregate summary (plus minimised
per-requester rows for the CSV). Wording is deliberately cautious: last-seen data
is a visibility signal, not proof that an account is a genuine human, engaged
with the channel, or located anywhere in particular.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import AuditJob, AuditRequester, Channel
from pca.domain.enums import AuditJobStatus

MIN_COVERAGE = 0.95

STATUS_LABEL = {
    "completed": "Completed",
    "partial": "Partial",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "queued": "Queued",
    "pending": "Queued",
    "running": "Running",
    "waiting_for_telegram": "Waiting for Telegram",
}

REASON_TEXT = {
    "time_limit": "the audit reached its maximum run time",
    "channel_unavailable": "the connected account lost access to the channel",
    "insufficient_rights": "the connected account lost the right to manage join requests",
    "session_invalid": "the connected Telegram session was logged out or revoked",
    "account_disconnected": "the Telegram account was disconnected",
    "internal_error": "repeated errors while talking to Telegram",
    "incomplete_scan": "Telegram stopped returning requests before the expected number had been read",
    "cancelled_by_user": "you cancelled it",
}

LAST_SEEN_LABELS: tuple[tuple[str, str], ...] = (
    ("online", "Online at scan time"),
    ("offline_24h", "Confirmed seen within 24 hours"),
    ("recently", "“Recently” (exact time unknown)"),
    ("last_week", "Within the last week"),
    ("last_month", "Within the last month"),
    ("older", "Older or unavailable"),
    ("hidden", "Hidden or unknown"),
)

AGE_LABELS: tuple[tuple[str, str], ...] = (
    ("lt_1d", "Under 1 day"),
    ("d1_7", "1–7 days"),
    ("d7_30", "7–30 days"),
    ("d30_90", "30–90 days"),
    ("gt_90", "Over 90 days"),
    ("unknown", "Date unavailable"),
)

LIMITATIONS: tuple[str, ...] = (
    "Telegram shows last-seen data only as each user's privacy settings allow; “recently” can mean anything up to a few days.",
    "Last-seen status does not prove an account is a real person, will engage with the channel, or is not automated.",
    "Location and nationality cannot be determined. Telegram provides no reliable location data and we do not infer it from names, usernames or emoji.",
    "Bot, scam and fake labels are Telegram's own flags and only cover accounts Telegram has marked.",
)


# --------------------------------------------------------------------------- view model


@dataclass(frozen=True)
class ReportView:
    job_id: int
    channel_title: str
    channel_username: str | None
    status: str
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    expected: int
    scan_limit: int | None
    scanned: int
    flood_waits: int
    charged_credits: int | None
    summary: dict[str, Any] | None

    @property
    def reportable(self) -> bool:
        return self.summary is not None and self.status in ("completed", "partial")

    @property
    def target(self) -> int:
        """The disclosed scope: pending requests at start, capped by the scan limit."""
        if self.scan_limit and self.expected:
            return min(self.scan_limit, self.expected)
        return self.scan_limit or self.expected

    @property
    def under_covered(self) -> bool:
        return bool(self.target) and self.scanned < self.target * MIN_COVERAGE

    @property
    def incomplete_but_marked_complete(self) -> bool:
        """Audits finalised as completed before completion was verified (e.g. a short page
        treated as the end of the list). Never presented as a complete audit."""
        return self.status == AuditJobStatus.COMPLETED.value and self.under_covered

    @property
    def status_label(self) -> str:
        if self.incomplete_but_marked_complete:
            return "Incomplete scan"
        return STATUS_LABEL.get(self.status, self.status)

    def count(self, *path: str) -> int:
        node: Any = self.summary or {}
        for key in path:
            if not isinstance(node, dict):
                return 0
            node = node.get(key, 0)
        return int(node or 0)

    @property
    def denominator(self) -> int:
        return self.count("scanned")

    def pct(self, n: int) -> float:
        d = self.denominator
        return n * 100 / d if d else 0.0


async def load_report(session: AsyncSession, user_id: int, job_id: int) -> ReportView | None:
    """Owner-scoped: returns None for jobs that don't exist or belong to someone else."""
    row = (
        await session.execute(
            select(AuditJob, Channel)
            .join(Channel, Channel.id == AuditJob.channel_id)
            .where(AuditJob.id == job_id, AuditJob.user_id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    job, channel = row
    return ReportView(
        job_id=job.id,
        channel_title=channel.title,
        channel_username=channel.username,
        status=job.status,
        error_code=job.error_code,
        created_at=job.created_at,
        completed_at=job.completed_at,
        expected=job.expected_requests or 0,
        scan_limit=job.scan_limit,
        scanned=job.scanned_count,
        flood_waits=job.flood_waits,
        charged_credits=job.charged_credits,
        summary=job.summary,
    )


def status_explanation(view: ReportView) -> str:
    reason = REASON_TEXT.get(view.error_code or "", "an unexpected problem")
    counts = f"{view.scanned:,} of {view.target:,} expected requests were scanned"
    if view.incomplete_but_marked_complete:
        note = " Contact support to have the credits restored." if view.charged_credits else ""
        return (
            f"Incomplete: only {counts}. This is not a complete audit and the figures cover "
            f"just the scanned requests.{note}"
        )
    if view.status == AuditJobStatus.COMPLETED.value:
        if view.scan_limit and view.expected and view.scan_limit < view.expected:
            return f"Completed within the disclosed scan limit: {view.scanned:,} of {view.expected:,} pending requests."
        return f"Completed: {view.scanned:,} of {view.expected:,} pending requests scanned."
    if view.status == AuditJobStatus.PARTIAL.value:
        return (
            f"Partial: the scan stopped early because {reason} ({counts}). Figures cover only the "
            "scanned requests. No credits were charged."
        )
    if view.status == AuditJobStatus.FAILED.value:
        return f"Failed: the audit could not run because {reason}. No credits were charged and no report was produced."
    if view.status == AuditJobStatus.CANCELLED.value:
        return "Cancelled: the audit was stopped, its data was deleted and no credits were charged."
    return "This audit is still in progress."


def warnings(view: ReportView) -> list[str]:
    out: list[str] = []
    scanned = view.denominator
    if view.scan_limit and view.expected and view.scan_limit < view.expected:
        out.append(f"The scan was capped at {view.scan_limit:,} of {view.expected:,} pending requests.")
    if view.expected and scanned < view.expected * 0.95:
        out.append(
            f"Coverage is {scanned * 100 / view.expected:.1f}%: some requests were not scanned "
            "(they may have been approved, withdrawn or expired during the scan)."
        )
    if view.expected and scanned > view.expected:
        out.append("More requests arrived during the scan than were pending when it started.")
    if view.flood_waits:
        out.append(f"Telegram asked us to slow down {view.flood_waits} time(s); the scan paused and resumed.")
    hidden = view.count("last_seen", "hidden")
    if scanned and hidden * 2 > scanned:
        out.append("More than half of requesters hide their last-seen status, so activity figures are limited.")
    if scanned and scanned < 50:
        out.append("Small sample: percentages based on fewer than 50 requests are not very reliable.")
    return out


def quality_summary(view: ReportView) -> list[str]:
    """Plain-language indicator notes. Never claims to identify real or fake people."""
    if not view.denominator:
        return ["No requests were scanned, so there is nothing to summarise."]
    p = view.pct
    confirmed = view.count("last_seen", "online") + view.count("last_seen", "offline_24h")
    recently = view.count("last_seen", "recently")
    stale = view.count("last_seen", "older") + view.count("last_seen", "hidden")
    flags = sum(view.count("attributes", k) for k in ("is_deleted", "is_bot", "is_scam", "is_fake"))
    no_photo = view.denominator - view.count("attributes", "has_photo")
    lines = [
        f"{p(confirmed):.1f}% were confirmed active within 24 hours and a further {p(recently):.1f}% "
        "showed “recently” (exact time unknown).",
        f"{p(stale):.1f}% had an old, unavailable or hidden last-seen status.",
        f"{p(no_photo):.1f}% have no visible profile photo.",
        f"{p(flags):.1f}% are deleted accounts or carry a Telegram bot/scam/fake flag.",
    ]
    if p(flags) >= 10:
        lines.append("The share of deleted or flagged accounts is high and worth investigating before relying on this audience.")
    elif p(stale) >= 60:
        lines.append("Most requesters show little visible recent activity. This can reflect privacy settings as well as inactivity.")
    else:
        lines.append("No single indicator stands out strongly. Treat these as signals to weigh, not a verdict.")
    return lines


def weekly_timeline(view: ReportView, weeks: int = 8) -> list[tuple[date, int]]:
    """Arrivals per 7-day window, newest last, ending on the summary's generation date."""
    timeline = (view.summary or {}).get("timeline") or {}
    daily: dict[str, int] = timeline.get("daily") or {}
    generated = (view.summary or {}).get("generated_at")
    end = datetime.fromisoformat(generated).astimezone(UTC).date() if generated else date.today()
    out: list[tuple[date, int]] = []
    for w in range(weeks - 1, -1, -1):
        start = end - timedelta(days=7 * w + 6)
        total = sum(daily.get((start + timedelta(days=i)).isoformat(), 0) for i in range(7))
        out.append((start, total))
    return out


def has_timing(view: ReportView) -> bool:
    return bool((view.summary or {}).get("request_age"))


# --------------------------------------------------------------------------- Telegram text


def _e(value: object) -> str:
    return escape(str(value), quote=False)


def _line(view: ReportView, label: str, n: int) -> str:
    return f"• {label}: {n:,} ({view.pct(n):.1f}%)"


def render_text(view: ReportView) -> str:
    """HTML for Telegram; kept well under the 4096-character limit."""
    title = _e(view.channel_title)
    when = (view.completed_at or view.created_at).strftime("%Y-%m-%d %H:%M UTC")
    head = [f"<b>📋 Audit #{view.job_id} · {title}</b>", f"{_e(view.status_label)} · {when}", "", _e(status_explanation(view))]
    if not view.reportable:
        return "\n".join(head)

    scanned = view.denominator
    coverage = f"{scanned * 100 / view.expected:.1f}%" if view.expected else "n/a"
    body = [
        "",
        "<b>Coverage</b>",
        f"Pending at start: {view.expected:,} · Scanned: {scanned:,} · Coverage: {coverage}",
        f"<i>Percentages below are of the {scanned:,} scanned requests.</i>",
        "",
        "<b>Last seen</b>",
        *(_line(view, label, view.count("last_seen", key)) for key, label in LAST_SEEN_LABELS),
        "",
        "<b>Profile</b>",
        _line(view, "Has profile photo", view.count("attributes", "has_photo")),
        _line(view, "Has username", view.count("attributes", "has_username")),
        _line(view, "Telegram Premium", view.count("attributes", "is_premium")),
        "",
        "<b>Telegram-reported flags</b>",
        _line(view, "Deleted accounts", view.count("attributes", "is_deleted")),
        _line(view, "Bots", view.count("attributes", "is_bot")),
        _line(view, "Scam flag", view.count("attributes", "is_scam")),
        _line(view, "Fake flag", view.count("attributes", "is_fake")),
    ]
    if has_timing(view):
        body += ["", "<b>Request age</b>"]
        body += [_line(view, label, view.count("request_age", key)) for key, label in AGE_LABELS]
        weeks = weekly_timeline(view, 4)
        body += ["", "<b>Arrivals, last 4 weeks</b>"]
        body += [f"• Week of {start:%d %b}: {n:,}" for start, n in weeks]
    body += ["", "<b>Quality indicators</b>", *(f"• {_e(s)}" for s in quality_summary(view))]
    warn = warnings(view)
    if warn:
        body += ["", "<b>⚠️ Warnings</b>", *(f"• {_e(w)}" for w in warn)]
    body += [
        "",
        "<i>Location cannot be determined. Last-seen data reflects privacy settings and is not proof "
        "of real or engaged people. The PDF has full details and limitations.</i>",
    ]
    return "\n".join(head + body)


# --------------------------------------------------------------------------- CSV


CSV_COLUMNS = (
    "row",
    "request_date",
    "request_age_days",
    "last_seen",
    "has_photo",
    "has_username",
    "is_premium",
    "is_deleted",
    "is_bot",
    "is_scam",
    "is_fake",
    "via_chatlist",
)


async def export_csv(session: AsyncSession, view: ReportView) -> bytes | None:
    """Minimised export: no ids, names, usernames or exact times. Request time is day-granular."""
    if not view.reportable:
        return None
    rows = (
        await session.execute(
            select(AuditRequester).where(AuditRequester.audit_job_id == view.job_id).order_by(AuditRequester.ordinal)
        )
    ).scalars()
    reference = (view.completed_at or datetime.now(UTC)).astimezone(UTC).date()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    written = 0
    for i, r in enumerate(rows, start=1):
        req_day = r.requested_at.astimezone(UTC).date() if r.requested_at else None
        writer.writerow(
            (
                i,
                req_day.isoformat() if req_day else "",
                (reference - req_day).days if req_day else "",
                r.last_seen,
                int(r.has_photo),
                int(r.has_username),
                int(r.is_premium),
                int(r.is_deleted),
                int(r.is_bot),
                int(r.is_scam),
                int(r.is_fake),
                int(r.via_chatlist),
            )
        )
        written += 1
    if not written:
        return None
    return buf.getvalue().encode("utf-8-sig")


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True)
class MetricRow:
    label: str
    a_count: int | None
    b_count: int | None
    a_pct: float | None
    b_pct: float | None

    @property
    def diff_count(self) -> int | None:
        if self.a_count is None or self.b_count is None:
            return None
        return self.b_count - self.a_count

    @property
    def diff_pp(self) -> float | None:
        if self.a_pct is None or self.b_pct is None:
            return None
        return self.b_pct - self.a_pct


@dataclass(frozen=True)
class Comparison:
    a: ReportView
    b: ReportView
    rows: list[MetricRow] = field(default_factory=list)
    conclusion: list[str] = field(default_factory=list)


class ComparisonError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _confirmed(v: ReportView) -> int:
    return v.count("last_seen", "online") + v.count("last_seen", "offline_24h")


def _flags(v: ReportView) -> int:
    return sum(v.count("attributes", k) for k in ("is_bot", "is_scam", "is_fake"))


def _metric(label: str, a: ReportView, b: ReportView, getter) -> MetricRow:
    na, nb = getter(a), getter(b)
    return MetricRow(label, na, nb, a.pct(na), b.pct(nb))


def build_comparison(a: ReportView, b: ReportView) -> Comparison:
    if a.job_id == b.job_id:
        raise ComparisonError("same_audit")
    if not (a.reportable and b.reportable) or a.status != "completed" or b.status != "completed":
        raise ComparisonError("not_completed")

    def coverage(v: ReportView) -> float | None:
        return v.denominator * 100 / v.expected if v.expected else None

    rows = [
        MetricRow("Pending requests", a.expected, b.expected, None, None),
        MetricRow("Scanned requests", a.denominator, b.denominator, None, None),
        MetricRow("Audit coverage", None, None, coverage(a), coverage(b)),
        _metric("Confirmed active within 24h", a, b, _confirmed),
        _metric("“Recently” (time unknown)", a, b, lambda v: v.count("last_seen", "recently")),
        _metric("Within last week / month", a, b, lambda v: v.count("last_seen", "last_week") + v.count("last_seen", "last_month")),
        _metric("Hidden or unknown status", a, b, lambda v: v.count("last_seen", "hidden")),
        _metric("Older or unavailable", a, b, lambda v: v.count("last_seen", "older")),
        _metric("Has profile photo", a, b, lambda v: v.count("attributes", "has_photo")),
        _metric("Has username", a, b, lambda v: v.count("attributes", "has_username")),
        _metric("Telegram Premium", a, b, lambda v: v.count("attributes", "is_premium")),
        _metric("Deleted accounts", a, b, lambda v: v.count("attributes", "is_deleted")),
        _metric("Telegram bot/scam/fake flags", a, b, _flags),
    ]
    if has_timing(a) and has_timing(b):
        rows += [_metric(f"Requests aged {label.lower()}", a, b, lambda v, k=key: v.count("request_age", k)) for key, label in AGE_LABELS]
    return Comparison(a=a, b=b, rows=rows, conclusion=_conclude(a, b))


def _conclude(a: ReportView, b: ReportView) -> list[str]:
    def score(v: ReportView) -> float:
        # Higher is "more visible recent activity, fewer warning signs". A heuristic only.
        return (
            v.pct(_confirmed(v))
            + 0.5 * v.pct(v.count("last_seen", "recently"))
            + 0.25 * v.pct(v.count("attributes", "has_photo"))
            - v.pct(v.count("attributes", "is_deleted") + _flags(v)) * 2
        )

    lines: list[str] = []
    small = min(a.denominator, b.denominator) < 50
    gap = score(b) - score(a)
    an, bn = a.channel_title, b.channel_title
    if abs(gap) < 5:
        lines.append(f"The two audiences look broadly similar on the available indicators ({an} vs {bn}).")
    else:
        better, worse = (bn, an) if gap > 0 else (an, bn)
        lines.append(
            f"{better} shows more visible recent activity and/or fewer warning signs than {worse} on the available indicators."
        )
    hidden_gap = abs(b.pct(b.count("last_seen", "hidden")) - a.pct(a.count("last_seen", "hidden")))
    if hidden_gap >= 15:
        lines.append(
            "The share of hidden last-seen statuses differs a lot between the two, which limits how directly activity can be compared."
        )
    if small:
        lines.append("At least one audit scanned fewer than 50 requests, so differences may be due to chance.")
    lines.append(
        "This comparison measures visibility signals only. It cannot confirm that either audience consists of real or engaged people, "
        "and location cannot be determined for either channel."
    )
    return lines


def _fmt_count(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def _fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:.1f}%"


def _fmt_diff(row: MetricRow) -> str:
    parts = []
    if row.diff_count is not None and row.a_pct is None:
        parts.append(f"{row.diff_count:+,}")
    if row.diff_pp is not None:
        parts.append(f"{row.diff_pp:+.1f} pp")
    return " / ".join(parts) or "—"


def render_comparison_text(cmp: Comparison) -> str:
    a, b = cmp.a, cmp.b
    lines = [
        "<b>⚖️ Channel comparison</b>",
        f"A: {_e(a.channel_title)} (audit #{a.job_id}, {(a.completed_at or a.created_at):%Y-%m-%d})",
        f"B: {_e(b.channel_title)} (audit #{b.job_id}, {(b.completed_at or b.created_at):%Y-%m-%d})",
        "<i>Percentages are of each audit's scanned requests. Differences are B minus A; pp = percentage points.</i>",
        "",
    ]
    for row in cmp.rows:
        if row.label.startswith("Requests aged"):
            continue
        if row.a_pct is None and row.a_count is None:
            a_txt, b_txt = _fmt_pct(row.a_pct), _fmt_pct(row.b_pct)
        elif row.a_pct is None:
            a_txt, b_txt = _fmt_count(row.a_count), _fmt_count(row.b_count)
        else:
            a_txt = f"{_fmt_count(row.a_count)} ({_fmt_pct(row.a_pct)})"
            b_txt = f"{_fmt_count(row.b_count)} ({_fmt_pct(row.b_pct)})"
        lines.append(f"<b>{_e(row.label)}</b>\nA {a_txt} · B {b_txt} · Δ {_fmt_diff(row)}")
    lines += ["", "<b>Conclusion</b>", *(f"• {_e(s)}" for s in cmp.conclusion)]
    return "\n".join(lines)
