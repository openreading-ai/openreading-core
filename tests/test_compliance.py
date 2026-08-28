"""Stage-1 data-residency matching (routing_and_compliance.md §4.4).

Region codes are opaque identifiers, not a hierarchy: `eastus2` is a distinct Azure region from
`eastus`, and `us` is not a parent of `us-east-1`. Matching them by string prefix let one region
code silently satisfy a request for a different one, admitting an undeclared region into a
data-residency filter — so the match is exact (case-insensitive) and nothing else.
"""

from __future__ import annotations

import pytest

from openreading.router.compliance import _region_covered, evaluate, parse_retention_hours
from openreading.types.request import Compliance
from tests.fakes import make_backend

# the shipped azure-document-intelligence region list — the descriptor the bug was found against
_AZURE = ["eastus", "westus", "westeurope", "northeurope", "us", "eu"]


@pytest.mark.parametrize(
    "want, regions",
    [
        ("eu", ["us", "eu"]),
        ("eastus", _AZURE),
        ("europe-west2", ["us", "eu", "europe-west2"]),
        ("EU", ["us", "eu"]),  # request casing normalized
        ("eu", ["US", "EU"]),  # descriptor casing normalized
    ],
)
def test_exact_region_is_covered(want: str, regions: list[str]) -> None:
    assert _region_covered(want, regions)


@pytest.mark.parametrize(
    "want, regions",
    [
        ("eastus2", _AZURE),  # a real, distinct, UNDECLARED Azure region
        ("eastus2", ["eastus"]),
        ("eastus", ["eastus2"]),  # reverse direction
        ("us", ["us-east-1", "us-west-2"]),  # 'us' is not a parent of an AWS region code
        ("eu", ["europe-west2"]),
        ("eu", []),
    ],
)
def test_distinct_region_codes_never_cover_each_other(want: str, regions: list[str]) -> None:
    assert not _region_covered(want, regions)


def test_evaluate_drops_a_neighbouring_undeclared_region() -> None:
    desc = make_backend("azure-like", hipaa_baa="yes", regions=_AZURE).descriptor
    drop = evaluate(Compliance(data_region="eastus2"), desc)
    assert drop is not None
    assert (drop.stage, drop.code) == (1, "region_mismatch")
    assert evaluate(Compliance(data_region="eastus"), desc) is None


@pytest.mark.parametrize("value", ["zero", "zdr", "none", "0", "0h"])
def test_parse_retention_hours_zero_shorthand(value: str) -> None:
    """'zdr' is the module's own docstring example for the literal-shorthand form; every
    existing max_retention test only drives the numeric-with-optional-'h' branch instead."""
    assert parse_retention_hours(value) == 0.0
