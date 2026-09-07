"""A descriptor's vendor claims are documentation. Core must never branch on one.

The removal set deleted three things that read a per-vendor table and decided with it: the
compliance filter, the capability gate, and the cost scorer. Each was wrong in the same way. The
table was a claim about a company this project does not control, published on a page that changes
without notice, with nothing here able to detect drift. Being wrong did not fail loudly.

The claims themselves stay on the descriptor, because a maintainer's dated reading of a vendor's
own documentation is useful to a person choosing a backend. What must not come back is code that
BEHAVES on one. This test is that guarantee, mechanized: a field naming something a vendor can do
must be read at zero sites outside the model that declares it and the adapter that fills it in.

The distinction this file draws, and the reason the load-bearing set is untouched by it:

- **A claim about a vendor** (`ocr`, `languages`, `max_pages_per_request`, `cancel_supported`) is
  someone's reading of a docs page. Core cannot check it and cannot notice it going stale.
- **A fact about this machine or this process** (`id`, `type`, `credentials_spec`, `wait_modes`,
  `protocol_version`, `signup_url`) is verifiable here, every run. Those stay load-bearing, and
  this test says nothing about them.

Two of the pinned names are worth calling out, because they read like guardrails and are not.
`idempotency_supported` and `cancel_supported` name behaviour the runtime genuinely has
(`ctx.idempotency_key` is always set; the job store has a DELETE), so a reader reasonably assumes
they gate those paths. They do not. A field in a published schema implying a check no code
performs is the same defect the compliance table was.

If a future change genuinely needs one of these, deleting its name here is the argument, and the
question it has to answer is: what happens when the vendor changes it and nobody notices?
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "openreading"

# Every descriptor field that states what a VENDOR can do, how fast, or how much. Verified read at
# zero sites when pinned. Grouped as the descriptor groups them, so a new claim lands beside its
# neighbours rather than at the end.
VENDOR_CLAIMS = frozenset(
    {
        # capabilities: what the backend says it can extract
        "ocr",
        "handwriting",
        "printed_tables",
        "complex_tables",
        "forms_key_value",
        "reading_order",
        "multi_column",
        "figures_charts",
        "signatures",
        "classification",
        "splitting",
        "vlm_based",
        "human_in_the_loop",
        "languages",
        "input_formats",
        "max_pages_per_request",
        "max_file_size",
        # runtime: what the vendor or the model says it needs
        "cold_start_s",
        "vram_class",
        "hardware",
        "system_deps",
        "offline_capable",
        "serving",
        "sandbox",
        # protocol and routing hints
        "adapter_impl",
        "idempotency_supported",
        "cancel_supported",
        "normalization_difficulty",
    }
)


def _consuming_modules() -> list[Path]:
    """Every module that could branch on a descriptor.

    `types/descriptor.py` declares the fields and one adapter package per backend fills them in.
    Neither is a consumer, and both must keep naming them.
    """
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "adapter.py":
            continue  # an adapter BUILDS its descriptor; that is not reading a claim
        if path.name == "descriptor.py" and path.parent.name in {"types", "ledger"}:
            continue
        out.append(path)
    return out


@pytest.mark.parametrize("field", sorted(VENDOR_CLAIMS))
def test_no_module_reads_a_vendor_claim_off_a_descriptor(field: str) -> None:
    readers = []
    for path in _consuming_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == field:
                readers.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert readers == [], (
        f"{field!r} is a claim about a vendor, and core now reads it at {readers}. A fact core "
        "cannot verify must not change what core does: the vendor can revise it without notice "
        "and nothing here would detect the drift. Keep the field as documentation, or delete it "
        "from VENDOR_CLAIMS and say in the same commit what happens when it goes stale."
    )


def test_the_pinned_set_still_matches_the_descriptor_model() -> None:
    """A claim renamed in the model but not here would leave this file guarding nothing.

    Guards the guard: every pinned name must still be a field the descriptor declares.
    """
    model = SRC / "types" / "descriptor.py"
    declared = {
        stmt.target.id
        for node in ast.walk(ast.parse(model.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }

    assert declared >= VENDOR_CLAIMS, "pinned names no longer on the descriptor: " + ", ".join(
        sorted(VENDOR_CLAIMS - declared)
    )
