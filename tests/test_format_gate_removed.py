"""The router no longer decides what a backend can read, and batch dispatches everything.

part 1. The stage-2 format branch dropped a backend when the
request's MIME type fell outside its descriptor's `input_formats`. That was a per-vendor
capability table, and vendors change what they accept without telling us.

It was also barely functioning. Measured before this change: `.docx` dropped nine backends and
`.svg` dropped none, because an unknown extension became `application/pdf` before the router saw
it. The gate was a projection of core's own nine-entry table rather than knowledge of any vendor.

Nothing is lost, because `executor.py` already implements what the gate was standing in for:
every exception from a backend, taxonomy or not, is appended to the trail and the chain
continues. The gate bought one saved round trip. It did not buy safety.
"""

from __future__ import annotations

import pytest

from openreading import api
from openreading.adapters.registry import make_adapter


# An explicit mime_type, not a filename. Writing PDF bytes into `a.docx` would resolve to
# application/pdf now that content beats filename, and the test would pass without ever reaching
# the gate it exists to check.
@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/svg+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/tiff",
        "text/html",
    ],
)
def test_no_backend_is_dropped_for_its_declared_formats(tmp_path, mime):
    """A `.docx` used to drop nine backends. A format a backend cannot read is now the backend's
    own refusal, which the fallback chain already handles."""
    doc = tmp_path / "doc.bin"
    doc.write_bytes(b"%PDF-1.4\n")
    plan = api.route(str(doc), mime_type=mime)
    assert not [i for i, d in plan.dropped.items() if d.code == "unsupported_format"]


def test_input_formats_is_documentation_and_nothing_branches_on_it():
    """The field stays on the descriptor for `openreading backends` to show. Keeping it as a
    ROUTING input is what this change removes, so the router must not import the comparison
    helper any more."""
    from openreading.router import router as router_module

    assert not hasattr(router_module, "_format_token")
    assert make_adapter("pymupdf").descriptor.capabilities.input_formats


def test_a_backend_still_refuses_a_format_it_cannot_read(tmp_path):
    """The mechanism the gate is being replaced by. pymupdf raises `unsupported_format` on its own
    behalf, first-hand, which is a fact it knows and the router did not."""
    from openreading.types.errors import TerminalError

    doc = tmp_path / "not-a-document.qqq"
    doc.write_bytes(b"\x00\x01\x02nonsense")
    with pytest.raises(TerminalError):
        api.run(str(doc), backend="pymupdf")


# ---- batch: accept everything the caller named, report per item ---------------------------------


def test_a_visible_unknown_extension_is_dispatched_not_skipped(tmp_path):
    """The behaviour that actually changes. `corpus/notes.rtf` used to be skipped without ever
    reaching a backend; it is now sent, and the item reports whatever the backend said."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.pdf").write_bytes(
        __import__("openreading.testing.sample_pdf", fromlist=["x"]).build_sample_pdf()
    )
    (corpus / "notes.rtf").write_bytes(b"{\\rtf1 hello}")

    env = api.run_batch([str(corpus)], backend="pymupdf")

    states = {i["source"]["relpath"]: i["state"] for i in env["items"]}
    assert states["real.pdf"] == "succeeded"
    assert states["notes.rtf"] == "failed", "an unknown format is dispatched and reports its result"
    assert "skipped" not in set(states.values())


def test_hidden_files_are_still_excluded(tmp_path):
    """A separate rule that survives untouched: `_expand_dir` prunes dotfiles because they are
    hidden, not because of their format. `.DS_Store` never reaches intake."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.pdf").write_bytes(
        __import__("openreading.testing.sample_pdf", fromlist=["x"]).build_sample_pdf()
    )
    (corpus / ".DS_Store").write_bytes(b"\x00\x01")

    env = api.run_batch([str(corpus)], backend="pymupdf")

    assert [i["source"]["relpath"] for i in env["items"]] == ["real.pdf"]


def test_the_batch_envelope_has_no_skip_vocabulary():
    """`skip_reason`, the `skipped` item state and `summary.skipped` all leave the schema, so a
    reader cannot branch on a state nothing produces."""
    import json
    from pathlib import Path

    from openreading import schemas

    path = Path(schemas.__file__).parent / schemas.BATCH_RESULT_SCHEMA_FILE
    doc = json.loads(path.read_text())
    item = doc["$defs"]["BatchItem"]
    assert "skip_reason" not in item["properties"]
    assert "skipped" not in item["properties"]["state"]["enum"]
    summary = doc["$defs"]["BatchSummary"]
    assert "skipped" not in summary["properties"]
    assert "skipped" not in summary.get("required", [])
