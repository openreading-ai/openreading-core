"""H3 — the value-equivalence ladder, table-driven with positive AND negative cases per tier
(DESIGN §4B). The ladder must never assert an equivalence it cannot defend — the day/month swap
and cross-currency cases are the traps."""

from __future__ import annotations

import pytest

from openreading.comparison.equivalence import equivalence_tier

POSITIVE = [
    # (a, b, expected tier)
    ("Loan Application", "Loan Application", "exact"),
    ("Total ", "total", "normalized"),
    ("ACME  Corp", "acme corp", "normalized"),
    ("$4,400.00", "4400.00 USD", "money"),
    ("$4,400.00", "$4400", "money"),
    ("USD 4400", "4400 USD", "money"),
    ("$4,400.00", "4400", "money"),  # BL-36: currency-signalled vs signal-less bare number
    ("4400 USD", "4400", "money"),  # BL-36: same bridge, signal on the trailing-code side
    ("£100", "100 GBP", "money"),  # symbol normalizes to the correct ISO code (BL-45/Trent)
    ("1,234", "1234", "number"),
    ("1234.0", "1234", "number"),
    ("-1,000", "-1000", "number"),
    ("2024-01-02", "Jan 2, 2024", "date"),
    ("2024-01-02", "2 January 2024", "date"),
    ("2024/01/02", "January 2 2024", "date"),
]

NEGATIVE = [
    ("$4,400.00", "$4,600.00"),  # money amounts differ
    ("4400 USD", "4400 EUR"),  # money currencies differ
    ("$100", "€100"),  # money currencies differ — both symbol-represented (BL-45/Trent)
    ("$100", "100 EUR"),  # money currencies differ — symbol vs. named code (BL-45/Trent)
    ("100 USD", "€100"),  # money currencies differ — named code vs. symbol (BL-45/Trent)
    ("$100", "£100"),  # money currencies differ — a second symbol pair (BL-45/Trent)
    ("12", "13"),  # numbers differ
    ("2024-01-02", "2024-02-01"),  # day/month swap — must NOT be equivalent
    ("01/02/2024", "02/01/2024"),  # ambiguous numeric dates — deliberately unparsed
    ("hello", "world"),  # unrelated text
    ("Total", "Subtotal"),  # different labels
]

# BL-150: a bare 3-letter unit abbreviation is the identical regex shape as a currency code
# (`[A-Za-z]{3}`) but is not a real ISO-4217 code, and must never be treated as a currency signal.
# Each of these is a same-valued (a, b) pair — same number, one side carrying a non-currency unit
# suffix/prefix — that must resolve to `number` or `None`, and specifically must never be `money`.
NON_CURRENCY_UNIT_SUFFIX = [
    ("30 min", "30"),  # minutes
    ("100 lbs", "100"),  # pounds
    ("12 pcs", "12"),  # pieces
    ("5 hrs", "5"),  # hours
    ("10 kgs", "10"),  # kilograms
    ("3 gal", "3"),  # gallons
    ("2 doz", "2"),  # dozen
]

NON_CURRENCY_UNIT_PREFIX = [
    ("min 30", "30"),
    ("lbs 100", "100"),
    ("pcs 12", "12"),
]


@pytest.mark.parametrize(("a", "b", "tier"), POSITIVE)
def test_ladder_positive(a: str, b: str, tier: str) -> None:
    assert equivalence_tier(a, b) == tier


@pytest.mark.parametrize(("a", "b"), NEGATIVE)
def test_ladder_negative(a: str, b: str) -> None:
    assert equivalence_tier(a, b) is None


@pytest.mark.parametrize(("a", "b"), NON_CURRENCY_UNIT_SUFFIX)
def test_bl150_non_currency_unit_suffix_never_reaches_money_tier(a: str, b: str) -> None:
    # A real unit abbreviation must never fabricate a currency signal — the acceptance criterion
    # is "number or None, never money", not a specific single outcome.
    tier = equivalence_tier(a, b)
    assert tier != "money"
    assert tier in ("number", None)


@pytest.mark.parametrize(("a", "b"), NON_CURRENCY_UNIT_PREFIX)
def test_bl150_non_currency_unit_prefix_never_reaches_money_tier(a: str, b: str) -> None:
    tier = equivalence_tier(a, b)
    assert tier != "money"
    assert tier in ("number", None)


def test_bl150_finding_repro_no_longer_returns_money() -> None:
    # The exact repro from the finding: none of these involve money, so none may resolve at the
    # `money` tier. Given the current ladder (letters break the pure-numeric `number` tier too),
    # the correct, fully-defended answer is None — "these differ, a human should look" — exactly
    # the outcome the ladder's own stated conservatism promises.
    assert equivalence_tier("30 min", "30") is None
    assert equivalence_tier("100 lbs", "100") is None
    assert equivalence_tier("12 pcs", "12") is None


def test_bl150_fake_named_code_does_not_bridge_to_bare_number() -> None:
    # A fabricated, non-ISO "code" must behave identically to no suffix at all: it cannot bridge
    # to a signal-less bare number the way a real code (BL-36) can.
    assert equivalence_tier("100 xyz", "100") is None


def test_exact_beats_number() -> None:
    # identical numeric strings resolve at `exact`, not `number`
    assert equivalence_tier("4400", "4400") == "exact"


def test_plain_number_not_money() -> None:
    # no currency signal → the `number` tier handles it, not `money`
    assert equivalence_tier("1,234", "1234") == "number"


def test_money_bridges_signal_vs_signal_less() -> None:
    # BL-36: a currency-signalled value must not be stranded against a signal-less bare number
    # carrying the identical amount — the ladder now bridges money/number when only one side
    # carries a signal, instead of returning None at every tier.
    assert equivalence_tier("$4,400.00", "4400") in ("money", "number")


def test_money_signal_vs_signal_less_still_respects_amount_mismatch() -> None:
    # the BL-36 bridge compares amounts, not just signal presence — a genuine amount mismatch
    # must still fail every tier.
    assert equivalence_tier("$4,400.00", "4600") is None


def test_none_is_incomparable() -> None:
    assert equivalence_tier(None, "x") is None
    assert equivalence_tier("x", None) is None


def test_symmetry() -> None:
    for a, b, _ in POSITIVE:
        assert equivalence_tier(a, b) == equivalence_tier(b, a)
