"""PDF rendering for audit reports and comparisons (fpdf2).

Uses DejaVu Sans when installed (the Docker image ships ``fonts-dejavu-core``) so
channel titles in any script render. Without it, falls back to the core Helvetica
font and transliterates text to Latin-1 rather than failing.
"""

from __future__ import annotations

import os
import unicodedata
from datetime import UTC, datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from pca.services import audience
from pca.services.reports import (
    AGE_LABELS,
    LAST_SEEN_LABELS,
    LIMITATIONS,
    Comparison,
    ReportView,
    _fmt_count,
    _fmt_diff,
    _fmt_pct,
    has_timing,
    quality_summary,
    status_explanation,
    warnings,
    weekly_timeline,
)

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF")
INK = (31, 41, 55)
MUTED = (107, 114, 128)
ACCENT = (37, 99, 235)
RULE = (229, 231, 235)
WARN = (180, 83, 9)

_ASCII_MAP = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "•": "-", "Δ": "d", "·": "-"})


def _find_font() -> tuple[str, str] | None:
    override = os.environ.get("PCA_PDF_FONT_DIR")
    for d in ((override,) if override else ()) + FONT_DIRS:
        regular, bold = os.path.join(d, "DejaVuSans.ttf"), os.path.join(d, "DejaVuSans-Bold.ttf")
        if os.path.isfile(regular) and os.path.isfile(bold):
            return regular, bold
    return None


class _Doc(FPDF):
    def __init__(self, title: str) -> None:
        super().__init__(format="A4")
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(True, margin=18)
        self.doc_title = title
        fonts = _find_font()
        if fonts:
            self.add_font("DejaVu", "", fonts[0])
            self.add_font("DejaVu", "B", fonts[1])
            self.family_name, self.unicode = "DejaVu", True
        else:
            self.family_name, self.unicode = "Helvetica", False
        self.set_title(self.t(title))
        self.set_creator("Pro Channel Auditor")

    def t(self, text: object) -> str:
        s = str(text)
        if self.unicode:
            return s
        s = s.translate(_ASCII_MAP)
        s = unicodedata.normalize("NFKD", s)
        return s.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=INK) -> None:
        self.set_font(self.family_name, "B" if bold else "", size)
        self.set_text_color(*color)

    def footer(self) -> None:
        self.set_y(-12)
        self.font(8, color=MUTED)
        self.cell(0, 6, self.t(f"Pro Channel Auditor · {self.doc_title} · page {self.page_no()}"), align="C")

    def para(self, text: str, size: float = 10, color=INK, bold: bool = False, h: float = 5) -> None:
        self.font(size, bold, color)
        self.multi_cell(0, h, self.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def heading(self, text: str) -> None:
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(3)
        self.font(12, True, ACCENT)
        self.cell(0, 7, self.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*RULE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def bullets(self, items, color=INK) -> None:
        for item in items:
            self.para(f"• {item}", 9.5, color)

    def table(self, header: list[str], rows: list[list[str]], widths: list[float]) -> None:
        self.font(9, True, MUTED)
        for text, w in zip(header, widths, strict=True):
            self.cell(w, 6, self.t(text), border="B", align="L" if w == widths[0] else "R")
        self.ln()
        self.font(9.5)
        for row in rows:
            if self.get_y() > self.h - 24:
                self.add_page()
            for i, (text, w) in enumerate(zip(row, widths, strict=True)):
                self.cell(w, 6, self.t(text), align="L" if i == 0 else "R")
            self.ln()

    def bars(self, items: list[tuple[str, float, str]], label_w: float = 62) -> None:
        """Horizontal bars; value is a 0–100 percentage."""
        track = self.w - self.l_margin - self.r_margin - label_w - 34
        for label, pct, note in items:
            if self.get_y() > self.h - 24:
                self.add_page()
            y = self.get_y()
            self.font(9.5)
            self.cell(label_w, 6, self.t(label))
            self.set_fill_color(*RULE)
            self.rect(self.get_x(), y + 1.5, track, 3, style="F")
            self.set_fill_color(*ACCENT)
            self.rect(self.get_x(), y + 1.5, max(0.4, track * min(pct, 100) / 100), 3, style="F")
            self.set_x(self.get_x() + track + 2)
            self.font(9, color=MUTED)
            self.cell(32, 6, self.t(note), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _title_block(doc: _Doc, title: str, subtitle: str) -> None:
    doc.add_page()
    doc.font(18, True)
    doc.multi_cell(0, 9, doc.t(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    doc.para(subtitle, 9.5, MUTED)
    doc.para(f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", 9, MUTED)


def _audience_section(doc: _Doc, aud: dict) -> None:
    tgt = aud["target"]
    classified = tgt["classified"]
    doc.heading("Audience intelligence (estimate)")
    doc.para(audience.ESTIMATE_NOTICE, 9, WARN)
    doc.ln(1)
    doc.table(
        ["", "Value"],
        [
            ["Target country", tgt["country_name"] or "not selected"],
            ["Target-audience suitability", audience.SUITABILITY_LABELS[tgt["suitability"]]],
            ["Confidence", tgt["confidence"].capitalize()],
            ["Accounts analysed", _fmt_count(aud["analyzed"])],
            ["Names with a recognisable writing system", _fmt_count(classified)],
            ["Locations verified", "No"],
        ],
        [100, 78],
    )
    doc.para(tgt["verdict"], 9.5)

    scripts = audience.script_rows(aud)
    if scripts:
        doc.heading("Name writing systems")
        doc.bars(
            [
                (
                    audience.SCRIPT_LABELS.get(s, s),
                    n * 100 / classified if classified else 0,
                    f"{audience.cell(n)} · {audience.share_text(n, classified)}".rstrip(" ·"),
                )
                for s, n in scripts
            ]
        )
        doc.para("Shares are of names with a recognisable writing system, not of countries.", 9, MUTED)
    if tgt["clues"]:
        doc.heading("Possible regional clues")
        doc.bullets([c["region"] for c in tgt["clues"]], MUTED)

    ind = aud["indicators"]
    doc.heading("Suspicious-account indicators")
    doc.table(
        ["Indicator", "Count", "Share"],
        [
            [label, audience.cell(ind[key]), audience.share_text(ind[key], aud["analyzed"]) or "-"]
            for key, label in audience.INDICATOR_LABELS
        ]
        + [["Any indicator (incl. Telegram flags)", audience.cell(ind["suspicious"]), audience.share_text(ind["suspicious"], aud["analyzed"]) or "-"]],
        [110, 34, 34],
    )
    doc.para(
        f"Overall suspicion level: {aud['suspicion_level']} (confidence {aud['quality_confidence']}). "
        "Counts under 5 are suppressed. Indicators are patterns, not proof that an account is fake.",
        9,
        MUTED,
    )


def render_report_pdf(view: ReportView) -> bytes:
    doc = _Doc(f"Audit #{view.job_id}")
    username = f" (@{view.channel_username})" if view.channel_username else ""
    when = (view.completed_at or view.created_at).strftime("%Y-%m-%d %H:%M UTC")
    _title_block(doc, f"Audit report: {view.channel_title}{username}", f"Audit #{view.job_id} · {view.status_label} · {when}")
    doc.ln(2)
    doc.para(status_explanation(view), 10)

    if not view.reportable:
        return bytes(doc.output())

    scanned = view.denominator
    doc.heading("Coverage")
    coverage = _fmt_pct(scanned * 100 / view.expected if view.expected else None)
    doc.table(
        ["", "Value"],
        [
            ["Pending requests at start", _fmt_count(view.expected)],
            ["Requests scanned", _fmt_count(scanned)],
            ["Audit coverage", coverage],
            ["Scan limit", _fmt_count(view.scan_limit) if view.scan_limit else "none"],
            ["Credits charged", _fmt_count(view.charged_credits) if view.charged_credits is not None else "0"],
        ],
        [120, 58],
    )
    doc.para(f"Percentages in this report are of the {scanned:,} scanned requests.", 9, MUTED)

    doc.heading("Last-seen distribution")
    doc.bars(
        [
            (label, view.pct(n := view.count("last_seen", key)), f"{n:,} · {view.pct(n):.1f}%")
            for key, label in LAST_SEEN_LABELS
        ]
    )

    doc.heading("Profile indicators")
    doc.bars(
        [
            (label, view.pct(n := view.count("attributes", key)), f"{n:,} · {view.pct(n):.1f}%")
            for key, label in (("has_photo", "Has profile photo"), ("has_username", "Has username"), ("is_premium", "Telegram Premium"))
        ]
    )

    doc.heading("Telegram-reported flags")
    doc.table(
        ["Flag", "Count", "Share"],
        [
            [label, _fmt_count(n := view.count("attributes", key)), _fmt_pct(view.pct(n))]
            for key, label in (
                ("is_deleted", "Deleted accounts"),
                ("is_bot", "Bots"),
                ("is_scam", "Scam flag"),
                ("is_fake", "Fake flag"),
                ("via_chatlist", "Joined via a chat folder link"),
            )
        ],
        [110, 34, 34],
    )

    if has_timing(view):
        doc.heading("Request age")
        doc.bars(
            [
                (label, view.pct(n := view.count("request_age", key)), f"{n:,} · {view.pct(n):.1f}%")
                for key, label in AGE_LABELS
            ]
        )
        weeks = weekly_timeline(view, 12)
        peak = max((n for _, n in weeks), default=0) or 1
        doc.heading("Arrival timeline (last 12 weeks)")
        doc.bars([(f"Week of {start:%d %b %Y}", n * 100 / peak, f"{n:,}") for start, n in weeks])
        earlier = view.count("timeline", "earlier")
        if earlier:
            doc.para(f"{earlier:,} request(s) arrived more than 90 days before the audit.", 9, MUTED)

    doc.heading("Quality indicators")
    doc.bullets(quality_summary(view))

    if view.audience:
        _audience_section(doc, view.audience)

    warn = warnings(view)
    if warn:
        doc.heading("Warnings")
        doc.bullets(warn, WARN)

    doc.heading("Limitations")
    doc.bullets(LIMITATIONS, MUTED)
    doc.para(
        "This report contains aggregate statistics only. It does not list requester identities, and "
        "the audit never approved, rejected or contacted any requester.",
        9,
        MUTED,
    )
    return bytes(doc.output())


def render_comparison_pdf(cmp: Comparison) -> bytes:
    a, b = cmp.a, cmp.b
    doc = _Doc(f"Comparison #{a.job_id} vs #{b.job_id}")
    _title_block(doc, "Channel comparison", f"A: {a.channel_title} (#{a.job_id})  ·  B: {b.channel_title} (#{b.job_id})")
    doc.para("Percentages are of each audit's scanned requests. Differences are B minus A; pp = percentage points.", 9, MUTED)
    doc.heading("Metrics")
    rows = []
    for row in cmp.rows:
        if row.a_pct is None and row.a_count is None:
            av, bv = _fmt_pct(row.a_pct), _fmt_pct(row.b_pct)
        elif row.a_pct is None:
            av, bv = _fmt_count(row.a_count), _fmt_count(row.b_count)
        else:
            av = f"{_fmt_count(row.a_count)} ({_fmt_pct(row.a_pct)})"
            bv = f"{_fmt_count(row.b_count)} ({_fmt_pct(row.b_pct)})"
        rows.append([row.label, av, bv, _fmt_diff(row)])
    doc.table(["Metric", "A", "B", "Difference"], rows, [70, 38, 38, 32])
    doc.heading("Conclusion")
    doc.bullets(cmp.conclusion)
    doc.heading("Limitations")
    doc.bullets(LIMITATIONS, MUTED)
    return bytes(doc.output())
