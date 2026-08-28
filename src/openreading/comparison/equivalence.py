"""The value-equivalence ladder (DESIGN §4B). Deterministic, closed, no LLM, no deps beyond
stdlib. `equivalence_tier(a, b)` returns the name of the first tier at which two values are
equivalent, or None if they differ. Tiers, in order: exact → normalized → money → number → date.

The ladder is intentionally conservative: it never asserts equivalence it cannot defend. Ambiguous
numeric dates (01/02/2024 — is that Jan 2 or Feb 1?) are deliberately NOT parsed, so a day/month
swap can never be mistaken for a match.
"""

from __future__ import annotations

import re
from datetime import datetime

TIERS = ("exact", "normalized", "money", "number", "date")


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", str(s).strip()).casefold()


def _exact(a: object, b: object) -> bool:
    return str(a) == str(b)


def _normalized(a: object, b: object) -> bool:
    return _norm(a) == _norm(b)


# --- money: requires a currency SIGNAL (symbol or ISO code) on at least one side, so two
# signal-less amounts still fall to `number` — but a signalled amount now bridges to a bare
# number on the other side instead of being stranded between the two tiers (BL-36). A symbol maps
# to its real ISO code (not None) so the cross-currency guard below can actually tell two
# different symbol-represented currencies apart instead of treating either as a wildcard (BL-45).
_CUR_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_MONEY_RE = re.compile(
    r"^\s*(?P<pre>[A-Za-z]{3}|[$€£¥])?\s*(?P<num>-?[\d,]+(?:\.\d+)?)\s*(?P<post>[A-Za-z]{3})?\s*$"
)

# BL-150: `_MONEY_RE`'s `pre`/`post` groups match ANY three Latin letters, not just real currency
# codes — the identical shape as an ordinary unit abbreviation ("min", "lbs", "pcs", "hrs", "kgs",
# "gal", "doz", ...). A named 3-letter token only counts as a currency SIGNAL if it's a real
# ISO-4217 code; anything else must fall through to `number`/`None`, exactly as if the
# currency-shaped suffix/prefix hadn't been there at all. Deliberately a short, explicit allow-list
# (not full ISO-4217, and no new dependency — see the module docstring's "no deps beyond stdlib")
# covering the majors this product's document classes actually see; always a superset of
# `_CUR_SYMBOL`'s own values so a symbol never maps to a code this allow-list would itself reject.
_ISO_4217_CODES = frozenset(_CUR_SYMBOL.values()) | {
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "HKD",
    "INR",
    "KRW",
    "MXN",
    "NZD",
    "SEK",
    "NOK",
    "DKK",
    "SGD",
    "ZAR",
    "BRL",
    "AED",
}


def _parse_money(v: object) -> tuple[float, str | None, bool] | None:
    """Parse `v` as an amount plus any currency signal. Returns `(amount, iso_code, had_signal)`;
    `iso_code` is set only for a named, real ISO-4217 code (a bare symbol like `$` signals
    currency without naming one). None only when `v` isn't number-shaped at all — a signal-less
    amount is still returned (`had_signal=False`) so `_money` can bridge it against a signalled
    value. A 3-letter token that matches the regex shape but isn't a real currency code (BL-150,
    e.g. "min", "lbs", "pcs") is not a signal at all: it's ignored, exactly as if it hadn't
    matched, so it can neither fabricate a signal nor silently override a real one."""
    m = _MONEY_RE.match(str(v))
    if not m:
        return None
    pre, post = m.group("pre"), m.group("post")
    cur = None
    had_signal = False
    for tok in (pre, post):
        if not tok:
            continue
        if tok in _CUR_SYMBOL:
            cur = _CUR_SYMBOL[tok]
            had_signal = True
        elif tok.upper() in _ISO_4217_CODES:
            cur = tok.upper()
            had_signal = True
        # else: shape-only match (e.g. "min", "lbs") — not a real code, not a signal (BL-150).
    try:
        amount = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    return amount, cur, had_signal


def _money(a: object, b: object) -> bool:
    pa, pb = _parse_money(a), _parse_money(b)
    if pa is None or pb is None:
        return False
    (amt_a, cur_a, sig_a), (amt_b, cur_b, sig_b) = pa, pb
    if not (sig_a or sig_b):
        return False  # neither side signals currency → not a money-tier match, defer to `number`
    if amt_a != amt_b:
        return False
    return not (cur_a and cur_b and cur_a != cur_b)  # both named & differ → not equivalent


# --- number: whole string is numeric, thousands separators tolerated ---
_NUM_RE = re.compile(r"^\s*-?[\d,]+(?:\.\d+)?\s*$")


def _parse_number(v: object) -> float | None:
    s = str(v)
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _number(a: object, b: object) -> bool:
    na, nb = _parse_number(a), _parse_number(b)
    return na is not None and nb is not None and na == nb


# --- date: only UNAMBIGUOUS formats (ISO + month-name); numeric M/D/Y is excluded on purpose ---
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def _parse_date(v: object) -> tuple[int, int, int] | None:
    s = str(v).strip()
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)  # noqa: DTZ007 — date-only, no tz semantics
            return (d.year, d.month, d.day)
        except ValueError:
            continue
    return None


def _date(a: object, b: object) -> bool:
    da, db = _parse_date(a), _parse_date(b)
    return da is not None and db is not None and da == db


_LADDER = (
    ("exact", _exact),
    ("normalized", _normalized),
    ("money", _money),
    ("number", _number),
    ("date", _date),
)


def equivalence_tier(a: object, b: object) -> str | None:
    """The first tier at which `a` and `b` are equivalent, or None if they differ. `None` inputs
    are incomparable (return None)."""
    if a is None or b is None:
        return None
    for name, fn in _LADDER:
        if fn(a, b):
            return name
    return None
