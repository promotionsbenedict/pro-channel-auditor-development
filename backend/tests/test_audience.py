from __future__ import annotations

import csv
import dataclasses
import io

import pytest
from sqlalchemy import update

from pca.db.models import AuditJob, AuditRequester
from pca.domain.enums import AuditJobStatus
from pca.services import audience, reports
from pca.services.report_pdf import render_report_pdf
from pca.telegram import signals
from pca.telegram.types import JoinRequestPage
from tests.test_audit_engine import FakeScan, _claim, _run, _setup, keyring, settings  # noqa: F401

# --------------------------------------------------------------------------- signals


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("Chinedu", "Okafor", "latin"),
        ("राहुल", "शर्मा", "devanagari"),
        ("محمد", None, "arabic"),
        ("Иван", "Петров", "cyrillic"),
        ("Rahul", "राहुल", "mixed"),
        ("🔥🔥", "123", "none"),
        (None, None, "none"),
    ],
)
def test_name_script(first, last, expected):
    assert signals.name_script(first, last) == expected


def test_username_style_categories():
    assert signals.username_style(None) == "none"
    assert signals.username_style("") == "none"
    assert signals.username_style("john_smith") == "plain"
    assert signals.username_style("user84629153") == "digit_heavy"
    assert signals.username_style("xkqzwrtplv") == "random_like"


def test_name_style_categories():
    assert signals.name_style(None, None) == "empty"
    assert signals.name_style("Ada", "Obi") == "normal"
    assert signals.name_style("Win big t.me/promo", None) == "promo"
    assert signals.name_style("🔥🔥🔥🔥", "💰") == "emoji_heavy"


def test_signals_return_only_category_labels():
    out = {signals.name_script("Chinedu"), signals.username_style("chinedu_o"), signals.name_style("Chinedu")}
    assert out <= {"latin", "plain", "normal"}


# --------------------------------------------------------------------------- analysis


def _target(code, scripts, analyzed=None):
    return audience.analyze_target(code, scripts, analyzed if analyzed is not None else sum(scripts.values()))


def test_no_target_never_claims_suitability():
    out = _target(None, {"latin": 80, "devanagari": 20})
    assert out["country"] is None
    assert out["suitability"] == "no_target"
    assert out["matching"] is None and out["match_share"] is None


def test_too_few_classified_names_gives_no_share():
    out = _target("IN", {"devanagari": 10, "unknown": 500}, analyzed=510)
    assert out["suitability"] == "insufficient_data"
    assert out["confidence"] == "low"
    assert out["match_share"] is None, "no percentage may be reported from a tiny sample"


def test_latin_match_is_never_high_confidence_for_latin_countries():
    out = _target("NG", {"latin": 2000})
    assert out["suitability"] == "consistent"
    assert out["confidence"] == "low"
    assert "does not narrow" in out["verdict"]


def test_non_latin_match_is_informative():
    out = _target("RU", {"cyrillic": 900, "latin": 50})
    assert out["suitability"] == "consistent"
    assert out["confidence"] == "high"


def test_atypical_scripts_are_inconsistent_and_hedged():
    out = _target("NG", {"cyrillic": 90, "latin": 10})
    assert out["suitability"] == "inconsistent"
    assert "cannot be confirmed" in out["verdict"]


def test_partial_match_is_mixed():
    out = _target("NG", {"latin": 30, "devanagari": 10})
    assert out["suitability"] == "mixed"
    assert out["match_share"] == 0.75


def test_confidence_drops_when_most_names_are_unclassified():
    full = _target("RU", {"cyrillic": 600})
    thin = _target("RU", {"cyrillic": 600, "unknown": 0, "none": 1500}, analyzed=2100)
    assert audience.LEVELS.index(thin["confidence"]) < audience.LEVELS.index(full["confidence"])


def test_latin_and_mixed_scripts_are_not_regional_clues():
    out = _target("NG", {"latin": 200, "mixed": 50, "devanagari": 100})
    assert [c["script"] for c in out["clues"]] == ["devanagari"]


def test_small_scripts_are_not_clues():
    out = _target("NG", {"latin": 500, "devanagari": 3})
    assert out["clues"] == []


def test_small_counts_are_suppressed():
    assert audience.cell(3) == "under 5"
    assert audience.cell(0) == "0"
    assert audience.cell(5) == "5"
    assert audience.share_text(3, 100) == ""
    assert audience.share_text(50, 100) == "50.0%"
    assert audience.share_text(5, 0) == ""


def test_country_index_round_trips_and_rejects_bad_values():
    for i, c in enumerate(audience.COUNTRIES, start=1):
        assert audience.country_by_index(i) is c
        assert audience.country_index(c.code) == i
    assert audience.country_by_index(0) is None
    assert audience.country_by_index(len(audience.COUNTRIES) + 1) is None
    assert audience.country_label(0) == "Not set"
    assert audience.country_index("ZZ") == 0
    codes = {c.code for c in audience.COUNTRIES}
    assert {"NG", "IN", "US"} <= codes


# --------------------------------------------------------------------------- export


def _manual_audience(scripts, target="IN"):
    analyzed = sum(scripts.values())
    indicators = {key: 0 for key, _ in audience.INDICATOR_LABELS} | {"suspicious": 3}
    return {
        "version": 1,
        "basis": "estimate",
        "location_verified": False,
        "analyzed": analyzed,
        "scripts": scripts,
        "indicators": indicators,
        "suspicious_share": round(3 / analyzed, 4),
        "suspicion_level": "low",
        "quality_confidence": audience.sample_level(analyzed),
        "target": audience.analyze_target(target, scripts, analyzed),
    }


def test_export_csv_is_aggregate_estimate_only():
    aud = _manual_audience({"latin": 70, "devanagari": 4, "arabic": 26})
    data = audience.export_csv(aud, "IN")
    assert data is not None and data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == ["section", "metric", "count", "share", "note"]
    sections = {r[0] for r in rows[1:]}
    assert sections <= {"summary", "writing_system", "indicator", "regional_clue"}
    text = data.decode("utf-8-sig").lower()
    assert "estimate" in text and "not verified" in text
    assert "user_id" not in text and "@" not in text and "t.me/" not in text
    deva = next(r for r in rows if r[0] == "writing_system" and r[1] == "Devanagari")
    assert deva[2] == "<5" and deva[3] == "", "counts under 5 are suppressed with no share"
    suspicious = next(r for r in rows if r[1] == "Any suspicion indicator")
    assert suspicious[2] == "<5"


def test_export_csv_none_without_analysis():
    assert audience.export_csv(None) is None
    assert audience.export_csv({}) is None


def test_text_lines_labelled_as_estimate_and_empty_without_data():
    assert audience.text_lines(None) == []
    lines = audience.text_lines(_manual_audience({"latin": 60, "devanagari": 40}))
    joined = "\n".join(lines)
    assert "estimate" in joined.lower()
    assert audience.ESTIMATE_NOTICE in joined


def test_estimate_notice_says_locations_are_not_verified():
    assert "verif" in audience.ESTIMATE_NOTICE.lower()


def test_requester_table_stores_no_names_or_usernames():
    forbidden = {"first_name", "last_name", "name", "username", "display_name", "phone", "telegram_user_id", "user_id"}
    assert not forbidden & {c.name for c in AuditRequester.__table__.columns}


# --------------------------------------------------------------------------- end to end


class ScriptedScan(FakeScan):
    """FakeScan whose requesters carry the derived profile categories."""

    async def fetch_join_requests(self, *args, **kwargs):
        page = await super().fetch_join_requests(*args, **kwargs)
        items = [
            dataclasses.replace(
                it,
                name_script="devanagari" if it.user_id % 4 == 0 else "latin",
                username_style="random_like" if it.user_id % 7 == 0 else "plain",
                name_style="normal",
            )
            for it in page.items
        ]
        return JoinRequestPage(total=page.total, items=items, next_cursor=page.next_cursor)


async def _scan(sm, settings, keyring, tg_id, country):
    user_id, job_id = await _setup(sm, settings, keyring, tg_id, pending=40)
    if country:
        async with sm() as s, s.begin():
            await s.execute(update(AuditJob).where(AuditJob.id == job_id).values(target_country=country))
    await _claim(sm, settings)
    await _run(sm, settings, keyring, ScriptedScan(40), job_id)
    return user_id, job_id


async def test_audit_stores_aggregate_audience_for_selected_country(sessionmaker, settings, keyring):
    user_id, job_id = await _scan(sessionmaker, settings, keyring, 9301, "NG")
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        aud = job.summary["audience"]
    assert aud["basis"] == "estimate" and aud["location_verified"] is False
    assert aud["analyzed"] == 40
    assert aud["scripts"] == {"latin": 30, "devanagari": 10}
    tgt = aud["target"]
    assert tgt["country"] == "NG"
    assert (tgt["classified"], tgt["matching"], tgt["match_share"]) == (40, 30, 0.75)
    assert tgt["suitability"] == "mixed"
    assert aud["indicators"]["random_username"] == 5
    assert aud["indicators"]["suspicious"] == 9, "4 deleted accounts plus 5 random-looking usernames"
    assert aud["suspicious_share"] == 0.225
    assert aud["suspicion_level"] == "moderate", "22.5% is above the 10% moderate bar and below the 25% high bar"
    flat = repr(aud)
    assert "user_id" not in flat and "9301" not in flat


async def test_audit_without_country_still_reports_clues_but_no_suitability(sessionmaker, settings, keyring):
    _, job_id = await _scan(sessionmaker, settings, keyring, 9302, None)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        tgt = job.summary["audience"]["target"]
    assert tgt["country"] is None and tgt["suitability"] == "no_target"
    assert tgt["match_share"] is None


async def test_audit_without_profile_signals_has_no_audience_section(sessionmaker, settings, keyring):
    user_id, job_id = await _setup(sessionmaker, settings, keyring, 9303, pending=35)
    await _claim(sessionmaker, settings)
    await _run(sessionmaker, settings, keyring, FakeScan(35), job_id)
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert job.status == AuditJobStatus.COMPLETED.value
        assert job.summary["audience"] is None
        assert job.charged_credits == 5, "the existing audit and payment flow is unchanged"
        view = await reports.load_report(s, user_id, job_id)
    assert view.audience is None
    assert audience.export_csv(view.audience) is None
    assert "Audience intelligence" not in reports.render_text(view)
    assert render_report_pdf(view).startswith(b"%PDF")


async def test_report_text_pdf_and_csv_include_audience(sessionmaker, settings, keyring):
    user_id, job_id = await _scan(sessionmaker, settings, keyring, 9304, "IN")
    async with sessionmaker() as s:
        view = await reports.load_report(s, user_id, job_id)
        main_csv = await reports.export_csv(s, view)
    assert view.target_country == "IN"
    assert view.audience is not None

    text = reports.render_text(view)
    assert "Audience intelligence (estimate)" in text

    pdf = render_report_pdf(view)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000

    aud_csv = audience.export_csv(view.audience, view.target_country)
    assert aud_csv is not None
    body = aud_csv.decode("utf-8-sig")
    assert "India" in body and "estimate" in body.lower()
    assert str(user_id) not in {r[1] for r in csv.reader(io.StringIO(body))}

    assert main_csv is not None, "the existing per-audit CSV export still works"


async def test_charges_and_scan_completeness_unchanged_with_audience(sessionmaker, settings, keyring):
    from pca.services import ledger

    user_id, job_id = await _scan(sessionmaker, settings, keyring, 9305, "US")
    async with sessionmaker() as s:
        job = await s.get(AuditJob, job_id)
        assert (job.scanned_count, job.charged_credits) == (40, 5)
        w = await ledger.get_wallet(s, user_id)
    assert (w.available_credits, w.reserved_credits) == (95, 0)
