"""Audience intelligence: regional clues, target-country suitability and quality indicators.

Telegram does not reveal where a requester lives, so nothing here is a verified
location. The analysis compares the writing systems of requesters' display names
with those typical for the target country and reports a suitability band with a
confidence level. It never produces per-country percentages: the only shares shown
are observed shares of scanned requests (for example "12% of classified names use
Devanagari"), which are facts about names, not about countries.

Everything is aggregate. Counts below ``MIN_CELL`` are suppressed in every output.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import AuditJob, AuditRequester
from pca.domain.enums import LastSeenCategory

MIN_CELL = 5
MIN_CLASSIFIED = 30
SUSPICION_MODERATE = 0.10
SUSPICION_HIGH = 0.25
LEVELS = ("low", "medium", "high")

# Scripts that say nothing about writing system (no letters, not analysed) or are not specific.
UNCLASSIFIED = frozenset({"none", "unknown"})
NON_CLUE_SCRIPTS = frozenset({"latin", "mixed", "other"})


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    scripts: frozenset[str]
    # True when the country's main writing system is not Latin, so a match is informative.
    informative: bool


def _c(code: str, name: str, scripts: str, informative: bool = False) -> Country:
    return Country(code, name, frozenset(scripts.split()), informative)


# Append-only: the position of a country is encoded in Telegram callback data (1-based).
COUNTRIES: tuple[Country, ...] = (
    _c("NG", "Nigeria", "latin"),
    _c("IN", "India", "latin devanagari bengali tamil telugu gujarati gurmukhi kannada malayalam arabic"),
    _c("US", "United States", "latin"),
    _c("GB", "United Kingdom", "latin"),
    _c("PK", "Pakistan", "latin arabic"),
    _c("BD", "Bangladesh", "latin bengali"),
    _c("ID", "Indonesia", "latin"),
    _c("PH", "Philippines", "latin"),
    _c("BR", "Brazil", "latin"),
    _c("RU", "Russia", "cyrillic", True),
    _c("UA", "Ukraine", "cyrillic", True),
    _c("EG", "Egypt", "arabic", True),
    _c("SA", "Saudi Arabia", "arabic", True),
    _c("AE", "United Arab Emirates", "arabic latin"),
    _c("IR", "Iran", "arabic", True),
    _c("TR", "Turkey", "latin"),
    _c("KE", "Kenya", "latin"),
    _c("ZA", "South Africa", "latin"),
    _c("DE", "Germany", "latin"),
    _c("FR", "France", "latin"),
    _c("ES", "Spain", "latin"),
    _c("NP", "Nepal", "latin devanagari"),
)
COUNTRY_BY_CODE = {c.code: c for c in COUNTRIES}

SCRIPT_LABELS = {
    "latin": "Latin",
    "devanagari": "Devanagari",
    "bengali": "Bengali",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "gujarati": "Gujarati",
    "gurmukhi": "Gurmukhi",
    "kannada": "Kannada",
    "malayalam": "Malayalam",
    "arabic": "Arabic",
    "cyrillic": "Cyrillic",
    "hebrew": "Hebrew",
    "greek": "Greek",
    "thai": "Thai",
    "ethiopic": "Ethiopic",
    "armenian": "Armenian",
    "georgian": "Georgian",
    "hangul": "Hangul",
    "cjk": "Chinese / Japanese",
    "mixed": "Mixed scripts",
    "other": "Other",
}

SCRIPT_REGIONS = {
    "devanagari": "Devanagari names are common in India and Nepal (Hindi, Marathi, Nepali).",
    "bengali": "Bengali names are common in Bangladesh and eastern India.",
    "tamil": "Tamil names are common in southern India and Sri Lanka.",
    "telugu": "Telugu names are common in southern India.",
    "gujarati": "Gujarati names are common in western India.",
    "gurmukhi": "Gurmukhi names are common in Punjab, India.",
    "kannada": "Kannada names are common in southern India.",
    "malayalam": "Malayalam names are common in Kerala, India.",
    "arabic": "Arabic-script names are used across the Middle East, North Africa, Pakistan and Iran.",
    "cyrillic": "Cyrillic names are common in Russia, Ukraine and neighbouring countries.",
    "hebrew": "Hebrew names are most common in Israel.",
    "greek": "Greek names are most common in Greece and Cyprus.",
    "thai": "Thai names are most common in Thailand.",
    "ethiopic": "Ethiopic names are common in Ethiopia and Eritrea.",
    "armenian": "Armenian names are common in Armenia and its diaspora.",
    "georgian": "Georgian names are common in Georgia.",
    "hangul": "Hangul names are common in South and North Korea.",
    "cjk": "Chinese or Japanese characters suggest East Asian language communities.",
}

SUITABILITY_LABELS = {
    "consistent": "Consistent signals",
    "mixed": "Mixed signals",
    "inconsistent": "Inconsistent signals",
    "insufficient_data": "Not enough data",
    "no_target": "No target country selected",
}

INDICATOR_LABELS = (
    ("random_username", "Random-looking usernames"),
    ("digit_heavy_username", "Digit-heavy usernames"),
    ("promotional_name", "Names containing links, handles or long numbers"),
    ("emoji_heavy_name", "Emoji-heavy names"),
    ("low_signal_profile", "No photo, no username and inactive or hidden"),
)

ESTIMATE_NOTICE = (
    "These are estimates from writing systems and username patterns. Telegram does not reveal member "
    "locations, so no location here is verified and no country percentages are given."
)


def country_by_index(index: int) -> Country | None:
    return COUNTRIES[index - 1] if 1 <= index <= len(COUNTRIES) else None


def country_index(code: str | None) -> int:
    for i, c in enumerate(COUNTRIES, start=1):
        if c.code == code:
            return i
    return 0


def country_label(index: int) -> str:
    c = country_by_index(index)
    return c.name if c else "Not set"


def sample_level(n: int) -> str:
    if n < MIN_CLASSIFIED:
        return "insufficient"
    if n < 100:
        return "low"
    if n < 500:
        return "medium"
    return "high"


def _cap(level: str, ceiling: str) -> str:
    return LEVELS[min(LEVELS.index(level), LEVELS.index(ceiling))]


def cell(n: int, marker: str = "under 5") -> str:
    return marker if 0 < n < MIN_CELL else f"{n:,}"


def share_text(n: int, total: int) -> str:
    if not total or 0 < n < MIN_CELL:
        return ""
    return f"{n * 100 / total:.1f}%"


# --------------------------------------------------------------------------- analysis


def _clues(classified_counts: dict[str, int], classified: int) -> list[dict[str, str]]:
    found = []
    for script, n in sorted(classified_counts.items(), key=lambda kv: -kv[1]):
        if script in NON_CLUE_SCRIPTS or n < MIN_CELL or n * 100 < classified * 5:
            continue
        found.append({"script": script, "label": SCRIPT_LABELS.get(script, script), "region": SCRIPT_REGIONS[script]})
    return found


def analyze_target(country_code: str | None, script_counts: dict[str, int], analyzed: int) -> dict[str, Any]:
    """Suitability of the audience for ``country_code`` from name scripts alone. Always an estimate."""
    classified_counts = {s: n for s, n in script_counts.items() if s not in UNCLASSIFIED}
    classified = sum(classified_counts.values())
    country = COUNTRY_BY_CODE.get(country_code) if country_code else None
    out: dict[str, Any] = {
        "country": country.code if country else None,
        "country_name": country.name if country else None,
        "classified": classified,
        "matching": None,
        "match_share": None,
        "clues": _clues(classified_counts, classified),
    }
    if country is None:
        return out | {
            "suitability": "no_target",
            "confidence": _cap(sample_level(classified), "high") if classified >= MIN_CLASSIFIED else "low",
            "verdict": "No target country was selected, so regional clues are shown for reference only.",
        }
    if classified < MIN_CLASSIFIED:
        return out | {
            "suitability": "insufficient_data",
            "confidence": "low",
            "verdict": (
                f"Too few names carry a recognisable writing system ({classified:,}) to judge suitability for "
                f"{country.name}."
            ),
        }

    matching = sum(n for s, n in classified_counts.items() if s in country.scripts)
    share = matching / classified
    suitability = "consistent" if share >= 0.85 else "mixed" if share >= 0.5 else "inconsistent"
    confidence = sample_level(classified)
    if not country.informative:
        confidence = _cap(confidence, "low" if suitability == "consistent" else "medium")
    if classified / max(analyzed, 1) < 0.5:
        confidence = LEVELS[max(LEVELS.index(confidence) - 1, 0)]

    if suitability == "consistent":
        verdict = f"Writing systems are consistent with {country.name}."
        if not country.informative:
            verdict += f" Latin script is shared by many countries, so this does not narrow the audience to {country.name}."
    elif suitability == "mixed":
        verdict = f"Some names use writing systems that are atypical for {country.name}."
    else:
        verdict = (
            f"Most names use writing systems atypical for {country.name}. The audience may be elsewhere "
            "or multilingual; locations cannot be confirmed."
        )
    return out | {"matching": matching, "match_share": round(share, 4), "suitability": suitability,
                  "confidence": confidence, "verdict": verdict}


async def build_audience(session: AsyncSession, job: AuditJob) -> dict[str, Any] | None:
    """Aggregate audience analysis for a finished scan, or ``None`` when no rows carry signals."""
    r = AuditRequester
    analysed = r.name_script != "unknown"
    scripts = {
        s: int(n)
        for s, n in (
            await session.execute(
                select(r.name_script, func.count()).where(r.audit_job_id == job.id, analysed).group_by(r.name_script)
            )
        ).all()
    }
    analyzed = sum(scripts.values())
    if not analyzed:
        return None

    low_signal = and_(
        r.has_photo.is_(False),
        r.has_username.is_(False),
        r.last_seen.in_((LastSeenCategory.OLDER.value, LastSeenCategory.HIDDEN.value)),
    )
    telegram_flag = or_(r.is_deleted.is_(True), r.is_bot.is_(True), r.is_scam.is_(True), r.is_fake.is_(True))
    pattern_flag = or_(r.username_style == "random_like", r.name_style == "promo")
    row = (
        await session.execute(
            select(
                func.count().filter(r.username_style == "random_like").label("random_username"),
                func.count().filter(r.username_style == "digit_heavy").label("digit_heavy_username"),
                func.count().filter(r.name_style == "promo").label("promotional_name"),
                func.count().filter(r.name_style == "emoji_heavy").label("emoji_heavy_name"),
                func.count().filter(low_signal).label("low_signal_profile"),
                func.count().filter(or_(telegram_flag, pattern_flag, low_signal)).label("suspicious"),
            ).where(r.audit_job_id == job.id, analysed)
        )
    ).one()._mapping
    indicators = {k: int(row[k]) for k, _ in INDICATOR_LABELS} | {"suspicious": int(row["suspicious"])}
    suspicious_share = round(indicators["suspicious"] / analyzed, 4)
    level = "high" if suspicious_share >= SUSPICION_HIGH else "moderate" if suspicious_share >= SUSPICION_MODERATE else "low"
    quality_confidence = sample_level(analyzed)
    return {
        "version": 1,
        "basis": "estimate",
        "location_verified": False,
        "analyzed": analyzed,
        "scripts": scripts,
        "indicators": indicators,
        "suspicious_share": suspicious_share,
        "suspicion_level": level,
        "quality_confidence": "low" if quality_confidence == "insufficient" else quality_confidence,
        "target": analyze_target(job.target_country, scripts, analyzed),
    }


# --------------------------------------------------------------------------- rendering


def script_rows(aud: dict[str, Any]) -> list[tuple[str, int]]:
    """Classified scripts, largest first. The caller decides how to display small counts."""
    items = [(s, n) for s, n in (aud.get("scripts") or {}).items() if s not in UNCLASSIFIED]
    return sorted(items, key=lambda kv: (-kv[1], kv[0]))


def text_lines(aud: dict[str, Any] | None) -> list[str]:
    """Compact Telegram HTML block. Static strings only, so nothing here needs escaping."""
    if not aud:
        return []
    tgt = aud["target"]
    classified = tgt["classified"]
    lines = ["<b>Audience intelligence (estimate)</b>"]
    if tgt["country"]:
        lines.append(
            f"Target: {tgt['country_name']} · {SUITABILITY_LABELS[tgt['suitability']]} · "
            f"confidence {tgt['confidence']}"
        )
    else:
        lines.append(SUITABILITY_LABELS["no_target"])
    lines.append(tgt["verdict"])
    top = [f"{SCRIPT_LABELS.get(s, s)} {cell(n)}" + (f" ({share_text(n, classified)})" if share_text(n, classified) else "")
           for s, n in script_rows(aud)[:4]]
    if top:
        lines.append("Name scripts (of classified names): " + ", ".join(top))
    for clue in tgt["clues"][:2]:
        lines.append(f"• {clue['region']}")
    ind = aud["indicators"]
    lines.append(
        f"Suspicion indicators: {cell(ind['suspicious'])} of {aud['analyzed']:,} "
        f"({aud['suspicion_level']}, confidence {aud['quality_confidence']})"
    )
    lines.append(f"<i>{ESTIMATE_NOTICE}</i>")
    return lines


def csv_rows(aud: dict[str, Any], target_country: str | None) -> list[list[str]]:
    tgt = aud["target"]
    classified = tgt["classified"]
    marker = "<5"
    rows: list[list[str]] = [
        ["summary", "analysis_basis", "", "", "estimate; not verified locations"],
        ["summary", "accounts_analyzed", f"{aud['analyzed']:,}", "", ""],
        ["summary", "target_country", target_country or "", "", tgt["country_name"] or "none selected"],
        ["summary", "suitability", SUITABILITY_LABELS[tgt["suitability"]], "", tgt["verdict"]],
        ["summary", "confidence", tgt["confidence"], "", "based on sample size and signal strength"],
        ["summary", "suspicion_level", aud["suspicion_level"], "", f"confidence {aud['quality_confidence']}"],
    ]
    for script, n in script_rows(aud):
        rows.append(["writing_system", SCRIPT_LABELS.get(script, script), cell(n, marker), share_text(n, classified), "share of classified names"])
    for key, label in INDICATOR_LABELS:
        n = aud["indicators"][key]
        rows.append(["indicator", label, cell(n, marker), share_text(n, aud["analyzed"]), "share of analysed accounts"])
    n = aud["indicators"]["suspicious"]
    rows.append(["indicator", "Any suspicion indicator", cell(n, marker), share_text(n, aud["analyzed"]), "includes Telegram scam/fake/bot/deleted flags"])
    for clue in tgt["clues"]:
        rows.append(["regional_clue", clue["label"], "", "", clue["region"]])
    return rows


def export_csv(aud: dict[str, Any] | None, target_country: str | None = None) -> bytes | None:
    """Aggregate-only CSV: no rows per person, no identifiers, small counts suppressed."""
    if not aud:
        return None
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "metric", "count", "share", "note"])
    writer.writerows(csv_rows(aud, target_country or aud["target"]["country"]))
    return buf.getvalue().encode("utf-8-sig")
