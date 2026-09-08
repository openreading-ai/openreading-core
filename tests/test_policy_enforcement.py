"""Policy enforcement at the boundaries a loader does not cover.

The file is validated by `strategy-config` v0.3 when it is read. Nothing validates a dict a
Python caller hands straight to `config.apply`, `config.router_config` or `compile_strategy`, and
nothing used to stop a request from relaxing what the file required. Both are enforcement holes
rather than authoring mistakes, so they are pinned here rather than in `test_policy_validation`.

Law PF1: neither source can weaken the other. Booleans OR, retention keeps the lower ceiling, and
two different regions are a refusal rather than a winner, because regions have no ordering and a
request cannot express two at once.

Law PF2: a public call is safe on its own. `compile_strategy` and `config.apply` enforce the
policy they are given without relying on an earlier loader call.
"""

from __future__ import annotations

import pytest

from openreading import api
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _req(**body) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "", "mime_type": "application/pdf"},
            "backend": {"id": None},
            **body,
        }
    )


# --- PF1: retention keeps the lower ceiling ------------------------------------------------------


# --- PF1: two regions are a refusal, not a winner ------------------------------------------------


# --- PF1: booleans and attestations --------------------------------------------------------------


# --- PF2: a raw dict cannot buy permission -------------------------------------------------------


# --- PF2: compile_strategy enforces the policy it is handed --------------------------------------


# --- PF6: evaluation honours the same file every other surface does ------------------------------


def _one_case_dataset(tmp_path):
    import json

    case = tmp_path / "c1"
    case.mkdir()
    (case / "case.json").write_text(
        json.dumps(
            {
                "name": "c1",
                "input": {"builtin_sample": True},
                "expected": {"text_contains": ["OpenReading Test Document"]},
            }
        )
    )
    return str(tmp_path)


# --- PF4: one batch, one snapshot ----------------------------------------------------------------


# --- PF6: publisher identity follows content, not a path -----------------------------------------


# --- PF3: resume compares policy provenance, not only its effect --------------------------------


def _armed_run(tmp_path, monkeypatch, policy_block: str) -> str:
    import contextlib
    import io

    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.pdf").write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text(
        f"version: 1\n{policy_block}strategies:\n  s: [pymupdf]\n"
    )
    seen: dict[str, str] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        api.run("s.pdf", strategy="s", on_run_armed=lambda r: seen.setdefault("id", r))
    return seen["id"]


# --- PF2, the remaining reimplementation ---------------------------------------------------------


# --- PF2, the boundary itself --------------------------------------------------------------------


@pytest.mark.parametrize("bridge", ["parsebench", "extractbench"])
def test_no_publisher_bridge_passes_the_raw_config_path_to_execution(bridge):
    """The structural half, which is what would have caught the asymmetry without either
    publisher installed. A bridge that hands `config` rather than `snapshot` to `execute_target`
    reads the file a second time, and the second read can differ from the one its name was built
    from."""
    import inspect

    from openreading.evals import extractbench, parsebench

    source = inspect.getsource({"parsebench": parsebench, "extractbench": extractbench}[bridge])
    assert "config=snapshot," in source, f"{bridge} does not execute its snapshot"
    assert "config=config," not in source, f"{bridge} still passes the raw path to execution"
