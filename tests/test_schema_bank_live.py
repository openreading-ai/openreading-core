"""Keyed live lane: every bank entry goes once to every backend that claims schema extraction.

This is the proof the inclusion rule asks for before an entry is released (`openreading.
schema_bank`). The offline suite proves an entry validates its expected record. It cannot show
that a strict backend accepts the schema itself, such as a provider that refuses an object with
no `additionalProperties: false`. A backend without a key skips cleanly. Marked `live`, so
`make verify` never runs it; `make verify-live` does, and each real call is billed to your key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openreading import run, schema_bank
from openreading.adapters.registry import known_backends, make_adapter

pytestmark = pytest.mark.live

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# The proof document for each entry. `generic` reads the same statement as `bank_statement`.
_DOCUMENT = {
    "bank_statement": _EXAMPLES / "john_smith_1000_2026_01.pdf",
    "generic": _EXAMPLES / "john_smith_1000_2026_01.pdf",
    "rfq": _EXAMPLES / "bank" / "rfq_ardent_letter_quote.pdf",
    "drawing_title_block": _EXAMPLES / "bank" / "drawing_mounting_bracket_asme.pdf",
    "clinical_trial_agreement": _EXAMPLES / "bank" / "cta_pellucid_short.pdf",
}


def _schema_capable() -> list[str]:
    """Backends whose descriptor claims `custom_schema_extraction`, in slug order."""
    slugs = []
    for slug in sorted(known_backends()):
        if make_adapter(slug).descriptor.capabilities.custom_schema_extraction:
            slugs.append(slug)
    return slugs


@pytest.mark.parametrize("entry", schema_bank.entries(), ids=lambda e: e.ref)
@pytest.mark.parametrize("backend", _schema_capable())
def test_a_bank_entry_runs_on_every_schema_capable_backend(backend, entry):  # pragma: no cover
    from tests.live_helpers import skip_unless_creds

    skip_unless_creds(backend)
    document = _DOCUMENT[entry.name]
    resp = run(document, backend=backend, extraction_schema={"json_schema": entry.schema})
    assert resp["status"]["state"] in ("succeeded", "partial"), resp["status"]
    check = resp["schema_check"]
    assert check["schema_ref"] == entry.ref
    assert resp.get("typed_fields"), f"{backend} returned no fields for {entry.ref}"
