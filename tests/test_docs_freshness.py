"""BL-24 / BL-49 — the numbers the docs quote about the gate must agree with the gate.

Four places cite `make verify`'s coverage floor (two root docs, two module docstrings). Those citations drifted apart (and away from the
Makefile) for four sprints because nothing checked them. The floor has a single source of truth —
`--cov-fail-under` in the Makefile — so it is checked against it here.

The offline test count is different: it has no stable value to check against (it grows every
sprint that adds a test), so BL-24 originally only checked the four docs for INTERNAL agreement —
every doc must quote the same number. That number itself still drifted for eight sprints running,
growing further out of date every sprint, because a hard-coded count is the wrong mechanism: there
is nothing to keep it current. BL-49 replaced it with the opposite discipline — state the floor
(stable, checked below), drop the count, and point at `pytest -m "not live" --collect-only -q` as
the live source of truth. So this file now checks the reverse of what it used to: no doc may
reintroduce a hard-coded offline test count, and the docs that used to carry one must point at the
live collection command instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_MAKEFILE = _ROOT / "Makefile"
# Every doc that cites the gate's numbers. A rename here must fail loudly, not silently pass.
_DOCS = (
    "AGENTS.md",
    "README.md",
    "tests/conftest.py",
    "src/openreading/adapters/__init__.py",
)
# The subset of _DOCS that BL-49 rewrote away from a hard-coded offline test count, in favor of
# citing the live collection command as the source of truth instead.
_FORMERLY_COUNTED_DOCS = (
    "AGENTS.md",
    "README.md",
    "tests/conftest.py",
)

# "90% coverage floor", "90% floor", "--cov-fail-under=90"
_FLOOR_RE = re.compile(r"(\d{1,3})%\s+(?:coverage\s+)?floor|--cov-fail-under=(\d{1,3})")
# "~1385 tests", "~1385 offline tests" — the tilde is required so prose like "the 8 methods" or a
# section count can never be mistaken for a suite size. BL-49: this must now match NOTHING in any
# doc — a hard-coded count is the exact drift this file exists to catch.
_COUNT_RE = re.compile(r"~([\d,]{3,})\+?\s+(?:offline\s+)?tests")
# The live source of truth BL-49 points readers at instead of a hard-coded count.
_LIVE_COUNT_RE = re.compile(r'pytest\s+-m\s+"not live"\s+--collect-only')


def _read(rel: str) -> str:
    p = _ROOT / rel
    assert p.is_file(), f"{rel} is missing — update _DOCS in this test"
    return p.read_text(encoding="utf-8")


def _floors(text: str) -> list[int]:
    return [int(a or b) for a, b in _FLOOR_RE.findall(text)]


def _counts(text: str) -> list[int]:
    return [int(m.replace(",", "")) for m in _COUNT_RE.findall(text)]


def test_makefile_declares_a_coverage_floor():
    floors = _floors(_MAKEFILE.read_text(encoding="utf-8"))
    assert len(floors) == 1, f"expected exactly one --cov-fail-under in the Makefile, got {floors}"


@pytest.mark.parametrize("rel", _DOCS)
def test_doc_cites_the_makefile_coverage_floor(rel):
    (expected,) = _floors(_MAKEFILE.read_text(encoding="utf-8"))
    cited = _floors(_read(rel))
    assert cited, f"{rel} cites no coverage floor — did the wording change?"
    assert set(cited) == {expected}, (
        f"{rel} cites {sorted(set(cited))}, Makefile gate is {expected}"
    )


@pytest.mark.parametrize("rel", _DOCS)
def test_no_doc_cites_a_hardcoded_offline_test_count(rel):
    counts = _counts(_read(rel))
    assert not counts, (
        f"{rel} cites a hard-coded offline test count {counts} — BL-49 replaced this with the "
        'coverage floor plus a pointer to the live `pytest -m "not live" --collect-only -q` '
        "count; don't reintroduce a value that will drift again."
    )


@pytest.mark.parametrize("rel", _FORMERLY_COUNTED_DOCS)
def test_formerly_counted_doc_points_at_the_live_collection_command(rel):
    text = _read(rel)
    assert _LIVE_COUNT_RE.search(text), (
        f"{rel} no longer cites a hard-coded test count but doesn't point at "
        '`pytest -m "not live" --collect-only` as the live source of truth (BL-49)'
    )
