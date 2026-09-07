"""Milestone 15.1 — pick: merge (typed-fields vote ensemble) + pages[].source_backend (spec §2.4).

Merge composes ONE response field-by-field from the completed candidates: majority value per field,
provenance recorded, confidence never fabricated. Document text/markdown/pages are the best-scoring
branch's (the merge_base), wholesale. Fully offline via ScriptedBackend fakes producing typed_fields.
"""

from __future__ import annotations

import base64

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ " * 4


def _req():
    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "strategy:s"},
        }
    )


def _run(cfg, reg):
    r = _req()
    compiled = compile_strategy(r, "s", StrategyConfig.model_validate(cfg), reg, RouterConfig())
    return run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())


def _cats(res):
    return [(a["backend"], a["category"]) for a in res.orchestration["attempts"]]


def _merge_cfg(branches):
    return {
        "version": 1,
        "strategies": {
            "s": {"parallel": branches, "pick": "merge", "budget": {"max_cost_usd": 1.0}}
        },
    }


def test_merge_takes_majority_field_value():
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, typed_fields={"amount": {"value": "100"}}),
        ScriptedBackend("aws-textract", text=GARBLED, typed_fields={"amount": {"value": "100"}}),
        ScriptedBackend("azure-di", text=GARBLED, typed_fields={"amount": {"value": "200"}}),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract", "azure-di"]), reg)
    assert res.response.typed_fields["amount"].value == "100"  # 2 votes beat 1


def test_merge_tie_breaks_by_confidence():
    # one vote each → tie → the higher REPORTED confidence wins (never invented)
    reg = scripted_registry(
        ScriptedBackend(
            "reducto",
            text=CLEAN,
            typed_fields={"amount": {"value": "100", "confidence": 0.95}},
        ),
        ScriptedBackend(
            "aws-textract",
            text=CLEAN,
            typed_fields={"amount": {"value": "200", "confidence": 0.40}},
        ),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
    assert res.response.typed_fields["amount"].value == "100"
    assert res.response.typed_fields["amount"].confidence == 0.95


def test_merge_never_fabricates_confidence():
    # the chosen source reported no confidence → the merged field's confidence is None, not invented
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, typed_fields={"ref": {"value": "AB"}}),
        ScriptedBackend("aws-textract", text=GARBLED, typed_fields={"ref": {"value": "AB"}}),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
    assert res.response.typed_fields["ref"].value == "AB"
    assert res.response.typed_fields["ref"].confidence is None


def test_merge_absent_field_stays_absent():
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, typed_fields={"a": {"value": "1"}}),
        ScriptedBackend("aws-textract", text=GARBLED, typed_fields={"b": {"value": "2"}}),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
    tf = res.response.typed_fields
    assert set(tf) == {"a", "b"}  # union of what branches produced; nothing fabricated
    assert "c" not in tf


def test_merge_records_base_and_source_with_provenance():
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, typed_fields={"amount": {"value": "100"}}),
        ScriptedBackend("aws-textract", text=GARBLED, typed_fields={"amount": {"value": "100"}}),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
    cats = dict(_cats(res))
    assert (
        cats["reducto"] == "merge_base"
    )  # CLEAN → best-scoring base (document channels wholesale)
    assert cats["aws-textract"] == "merge_source"
    prov = {p["field"]: p["backend"] for p in res.orchestration["merge"]}
    assert prov["amount"] in ("reducto", "aws-textract")  # per-field provenance recorded


def test_merge_base_supplies_text_and_source_backend():
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, typed_fields={"amount": {"value": "100"}}),
        ScriptedBackend("aws-textract", text=GARBLED, typed_fields={"amount": {"value": "100"}}),
    )
    res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
    assert res.response.document.text == CLEAN  # the base's text, wholesale
    # every page carries per-page provenance
    assert all(p.source_backend == "reducto" for p in res.response.document.pages or [])


def test_merge_determinism():
    def once():
        reg = scripted_registry(
            ScriptedBackend("reducto", text=CLEAN, typed_fields={"amount": {"value": "100"}}),
            ScriptedBackend(
                "aws-textract",
                text=GARBLED,
                typed_fields={"amount": {"value": "200"}},
            ),
        )
        res = _run(_merge_cfg(["reducto", "aws-textract"]), reg)
        return res.response.typed_fields["amount"].value, res.orchestration["merge"]

    assert once() == once()
