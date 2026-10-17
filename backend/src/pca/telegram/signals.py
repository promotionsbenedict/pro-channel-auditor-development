"""Non-identifying signals derived from a profile's display name and username.

The text is inspected in memory and reduced to coarse categories. Names and
usernames are never stored, logged or returned: only the category labels leave
this module. All heuristics are pattern-based and can misjudge real people, so
callers must present the results as estimates.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_SCRIPT_BY_PREFIX = {
    "LATIN": "latin",
    "DEVANAGARI": "devanagari",
    "BENGALI": "bengali",
    "TAMIL": "tamil",
    "TELUGU": "telugu",
    "GUJARATI": "gujarati",
    "GURMUKHI": "gurmukhi",
    "KANNADA": "kannada",
    "MALAYALAM": "malayalam",
    "ARABIC": "arabic",
    "CYRILLIC": "cyrillic",
    "HEBREW": "hebrew",
    "GREEK": "greek",
    "THAI": "thai",
    "ETHIOPIC": "ethiopic",
    "ARMENIAN": "armenian",
    "GEORGIAN": "georgian",
    "HANGUL": "hangul",
    "CJK": "cjk",
    "HIRAGANA": "cjk",
    "KATAKANA": "cjk",
}

# A name counts as one writing system only when it dominates the letters; otherwise it is "mixed".
DOMINANT_SCRIPT_SHARE = 0.7

_VOWELS = "aeiouy"
_CONSONANT_RUN = re.compile(r"[^aeiouy\d_]{6,}")
_ALTERNATING = re.compile(r"(?:[a-z]\d){3,}")
_PROMO = re.compile(r"(https?://|www\.|t\.me/|@\w{3,}|\.(?:com|ru|io|me|net|org|xyz)\b|\d{8,})", re.IGNORECASE)


def _clean(*parts: str | None) -> str:
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    return unicodedata.normalize("NFKC", joined)


def name_script(*parts: str | None) -> str:
    """Dominant writing system of a display name, ``mixed``, or ``none`` when it has no letters."""
    counts: Counter[str] = Counter()
    for ch in _clean(*parts):
        if not ch.isalpha():
            continue
        prefix = unicodedata.name(ch, "").split(" ", 1)[0]
        counts[_SCRIPT_BY_PREFIX.get(prefix, "other")] += 1
    total = sum(counts.values())
    if not total:
        return "none"
    script, top = counts.most_common(1)[0]
    return script if top / total >= DOMINANT_SCRIPT_SHARE else "mixed"


def username_style(username: str | None) -> str:
    """``none``, ``plain``, ``digit_heavy`` or ``random_like``."""
    if not username:
        return "none"
    handle = username.lower()
    digits = sum(c.isdigit() for c in handle)
    if digits >= 4 and digits / len(handle) >= 0.4:
        return "digit_heavy"
    letters = [c for c in handle if c.isalpha()]
    if len(letters) >= 7:
        vowel_ratio = sum(c in _VOWELS for c in letters) / len(letters)
        if vowel_ratio < 0.2 or _CONSONANT_RUN.search(handle):
            return "random_like"
    if _ALTERNATING.search(handle):
        return "random_like"
    return "plain"


def name_style(*parts: str | None) -> str:
    """``empty``, ``promo`` (link, handle or long number in the name), ``emoji_heavy`` or ``normal``."""
    text = _clean(*parts)
    if not text:
        return "empty"
    if _PROMO.search(text):
        return "promo"
    symbols = sum(1 for c in text if unicodedata.category(c) in ("So", "Sk"))
    if symbols >= 3 and symbols / max(1, len(text.replace(" ", ""))) >= 0.3:
        return "emoji_heavy"
    return "normal"
