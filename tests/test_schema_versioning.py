"""C12 — version-identity consistency across every schema family (DESIGN §7).

The version an artifact CLAIMS must be internally consistent. For each schema *family* the
NEWEST vendored file must agree on its version across (a) its filename, (b) its ``$id`` URL,
(c) its in-band ``schema_version`` const where it carries one, and (d) the pydantic default
the producer stamps. This is the meta-test that makes the response.v0.1/0.2 drift class
(filename said v0.2, const said "0.1") impossible to ship again.

Scoped per §7: every file asserts filename-version == ``$id``-version; the newest file per
family additionally asserts const and producer-default agreement. Older files' consts are NOT
asserted, which deliberately exempts ``response.v0.2.json``'s known-bad "0.1" const (a
released, immutable file — see the pinned regression test below).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from openreading import schemas
from openreading.types import NormalizedResponse, OpenReadingRequest
from openreading.types.batch import BatchResult, CorpusReport
from openreading.types.leaderboard import BenchmarkReport
from openreading.types.liveness import LivenessReport

SCHEMA_DIR = Path(schemas.__file__).parent

# Both the filename and the ``$id`` URL end in "vX.Y.json". The separator before the version
# is "/" (slash form: .../schemas/response/v0.3.json) or "." (dot form:
# .../schemas/comparison-report.v0.1.json). One documented regex handles both historical URL
# shapes (DESIGN §7 C12).
_VER_RE = re.compile(r"[/.]v(\d+\.\d+)\.json$")
_FAMILY_RE = re.compile(r"^(.*)\.v\d+\.\d+\.json$")

# family -> callable returning the producer's stamped default version. Only families with a
# pydantic mirror appear here; comparison-report is a plain dict guarded at runtime by
# ``validate_comparison_report`` inside ``build_report``; descriptor/strategy-config carry no
# in-band string version at all.
_PRODUCER_DEFAULT = {
    "response": lambda: NormalizedResponse.model_fields["schema_version"].default,
    "request": lambda: OpenReadingRequest.model_fields["schema_version"].default,
    "batch-result": lambda: BatchResult.model_fields["schema_version"].default,
    "corpus-report": lambda: CorpusReport.model_fields["schema_version"].default,
    "leaderboard-report": lambda: BenchmarkReport.model_fields["schema_version"].default,
    "liveness-report": lambda: LivenessReport.model_fields["schema_version"].default,
}


def _ver_from(s: str) -> str:
    m = _VER_RE.search(s)
    assert m, f"no vX.Y.json version found in {s!r}"
    return m.group(1)


def _schema_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.v*.json"))


def _by_family() -> dict[str, list[Path]]:
    fams: dict[str, list[Path]] = {}
    for p in _schema_files():
        m = _FAMILY_RE.match(p.name)
        assert m, f"unparseable schema filename {p.name!r}"
        fams.setdefault(m.group(1), []).append(p)
    return fams


def _const_of(schema: dict) -> str | None:
    return ((schema.get("properties") or {}).get("schema_version") or {}).get("const")


def test_c12_filename_version_matches_id_version_for_every_file():
    for p in _schema_files():
        doc = json.loads(p.read_text())
        assert _ver_from(p.name) == _ver_from(doc["$id"]), (
            f"{p.name}: filename version != $id version ({doc['$id']})"
        )


def test_c12_newest_file_per_family_is_version_consistent():
    for family, paths in _by_family().items():
        newest = max(paths, key=lambda p: tuple(int(x) for x in _ver_from(p.name).split(".")))
        fver = _ver_from(newest.name)
        doc = json.loads(newest.read_text())

        const = _const_of(doc)
        if const is not None:
            assert const == fver, (
                f"{newest.name}: in-band const {const!r} != filename version {fver!r}"
            )

        producer = _PRODUCER_DEFAULT.get(family)
        if producer is not None:
            assert producer() == fver, (
                f"{family}: pydantic default {producer()!r} != newest schema version {fver!r}"
            )


def test_c12_response_v0_2_const_is_known_bad_and_exempted():
    """Pinned regression: response.v0.2.json is a RELEASED (immutable) file whose const is the
    historical "0.1" drift bug. C12 must NOT assert older-file consts, so this file is exempt.
    Editing v0.2 in place would be caught by the non-additive schema-diff gate (§7)."""
    doc = json.loads((SCHEMA_DIR / "response.v0.2.json").read_text())
    assert _const_of(doc) == "0.1"  # the known bug, deliberately frozen


# ---- open vs closed sets, the MINOR-allowlist's own distinction (A9) ---------------------------


def _newest_response() -> dict:
    newest = max(SCHEMA_DIR.glob("response.v*.json"), key=lambda p: p.name)
    return json.loads(newest.read_text())


def test_the_open_sets_are_declared_open_in_the_schema():
    """`pydoc openreading.schemas` lets a MINOR add values to the OPEN sets. An open set must
    therefore be unconstrained in the schema — otherwise the "compatible" addition is a validation
    failure for anyone pinned to the older file."""
    resp = _newest_response()
    code = resp["properties"]["warnings"]["items"]["properties"]["code"]
    assert code == {"type": "string"}, "warnings[].code is documented OPEN; it must not be an enum"
    assert resp["properties"]["backend"]["properties"]["id"] == {"type": "string"}, (
        "backend.id is documented OPEN; it must not be an enum"
    )


def test_block_type_is_a_closed_enum_matching_the_python_vocabulary():
    """A9: the MINOR allowlist once listed "block types" beside the open sets while the schema
    declared a 22-value enum, so the contract page and the policy contradicted each other. The
    enum is the truth — a caller may switch on it exhaustively — and an unmapped native concept
    reaches them as `other` + `native_type` instead of a new value. Adding one is a MAJOR."""
    from openreading.types.enums import BlockType

    block_type = _newest_response()["$defs"]["Block"]["properties"]["type"]
    assert "enum" in block_type, "Block.type is CLOSED; opening it is a MAJOR, not a policy edit"
    assert block_type["enum"] == [b.value for b in BlockType]
    assert "other" in block_type["enum"], (
        "the `other` bucket is what makes a closed vocabulary affordable — without it a new "
        "native concept would have nowhere honest to land"
    )
