"""Milestone 12.1 — route nodes + facts (execution.md §4, spec §2.5).

Covers: fact computation + evaluate_when (doc_type/mime/pages/size/filename/compliance/
sample_percent, any_of, fact_unavailable, unknown-key fallback), and route dispatch through the
engine (first-match, default fallthrough, all-rules-traced, nested route→cascade,
decide→otherwise).
"""

from __future__ import annotations

import base64

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import (
    StrategyConfig,
    compile_strategy,
    compute_facts,
    evaluate_when,
    run_strategy,
)
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


def _req(
    *,
    doc_type=None,
    mime="application/pdf",
    compliance=None,
    filename=None,
    pdf=False,
    size_bytes=None,
):
    doc: dict = {"mime_type": mime}
    if pdf:
        doc["bytes_base64"] = base64.b64encode(build_sample_pdf()).decode()
    elif size_bytes:
        doc["bytes_base64"] = base64.b64encode(b"x" * size_bytes).decode()
    else:
        doc["path"] = "/x.pdf"
    if filename:
        doc["filename"] = filename
    body: dict = {"document": doc, "backend": {"id": "strategy:s"}}
    if doc_type:
        body["routing"] = {"doc_type_hint": doc_type}
    if compliance:
        body["compliance"] = compliance
    return OpenReadingRequest.model_validate(body)


def _run(cfg, name, reg, req):
    c = StrategyConfig.model_validate(cfg)
    compiled = compile_strategy(req, name, c, reg, RouterConfig())
    return run_strategy(
        compiled, req, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock()
    )


# ---- fact computation + evaluate_when ---------------------------------------------------------


def test_compute_facts_from_pdf():
    facts = compute_facts(_req(doc_type="invoice", filename="loan.pdf", pdf=True))
    assert facts.doc_type == "invoice"
    assert facts.mime == "application/pdf"
    assert facts.page_count == 2  # the sample PDF has 2 pages
    assert facts.filename == "loan.pdf"
    assert facts.content_hash is not None


def test_evaluate_when_doc_type_list_and_and():
    facts = compute_facts(_req(doc_type="invoice", pdf=True))
    assert evaluate_when({"doc_type": ["invoice", "receipt"]}, facts).matched
    assert not evaluate_when({"doc_type": ["paystub"]}, facts).matched
    # keys AND: doc_type matches but pages_over fails
    assert not evaluate_when({"doc_type": ["invoice"], "pages_over": 100}, facts).matched


def test_evaluate_when_mime():
    facts = compute_facts(_req(mime="application/pdf", pdf=True))
    assert evaluate_when({"mime": "application/pdf"}, facts).matched  # string form
    assert evaluate_when({"mime": ["text/plain", "application/pdf"]}, facts).matched  # list form
    assert not evaluate_when({"mime": "text/plain"}, facts).matched
    assert not evaluate_when({"mime": ["text/plain", "image/png"]}, facts).matched


def test_evaluate_when_any_of_ors():
    facts = compute_facts(_req(doc_type="paystub", pdf=True))
    gate = {"any_of": [{"doc_type": ["invoice"]}, {"doc_type": ["paystub"]}]}
    assert evaluate_when(gate, facts).matched


def test_evaluate_when_fact_unavailable_does_not_match():
    # no bytes → page_count uncomputable → pages_over unavailable → rule doesn't match
    facts = compute_facts(_req())  # path source, no bytes
    res = evaluate_when({"pages_over": 5}, facts)
    assert not res.matched
    assert res.predicates[0].status == "unavailable"


def test_evaluate_when_pages_under():
    facts = compute_facts(_req(pdf=True))  # the sample PDF has 2 pages
    assert evaluate_when({"pages_under": 5}, facts).matched
    assert not evaluate_when({"pages_under": 1}, facts).matched


def test_evaluate_when_size():
    facts = compute_facts(_req(size_bytes=3 * 1024 * 1024))  # 3 MB
    assert evaluate_when({"size_over_mb": 2}, facts).matched
    assert not evaluate_when({"size_over_mb": 5}, facts).matched


def test_evaluate_when_size_under_mb():
    facts = compute_facts(_req(size_bytes=3 * 1024 * 1024))  # 3 MB
    assert evaluate_when({"size_under_mb": 5}, facts).matched
    assert not evaluate_when({"size_under_mb": 2}, facts).matched


def test_evaluate_when_filename_matches():
    facts = compute_facts(_req(filename="loan_2024.pdf", pdf=True))
    assert evaluate_when({"filename_matches": r"loan_\d+\.pdf"}, facts).matched
    assert not evaluate_when({"filename_matches": r"^invoice"}, facts).matched

    unset = compute_facts(_req(pdf=True))  # no filename → unavailable, never matches
    res = evaluate_when({"filename_matches": r"loan"}, unset)
    assert not res.matched
    assert res.predicates[0].status == "unavailable"


def test_sample_percent_deterministic():
    req = _req(pdf=True)  # same request/bytes → the content-hash bucket is stable
    assert compute_facts(req).sample_bucket() == compute_facts(req).sample_bucket()
    assert 0 <= compute_facts(req).sample_bucket() < 100


def test_evaluate_when_sample_percent():
    facts = compute_facts(_req(pdf=True))
    bucket = facts.sample_bucket()  # deterministic given fixed content; straddle it either side
    assert evaluate_when({"sample_percent": bucket + 1}, facts).matched
    assert not evaluate_when({"sample_percent": bucket - 1}, facts).matched


def test_evaluate_when_unknown_fact_key_is_unavailable():
    facts = compute_facts(_req(pdf=True))
    res = evaluate_when({"nonexistent_fact": 1}, facts)
    assert not res.matched
    assert res.predicates[0].status == "unavailable"


# ---- route dispatch through the engine --------------------------------------------------------


def _reg():
    return scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", text=CLEAN),
    )


CFG = {
    "version": 1,
    "strategies": {
        "s": {
            "route": {
                "rules": [
                    {"when": {"doc_type": ["invoice"]}, "use": "reducto"},
                    {"when": {"pages_over": 1000}, "use": "reducto"},
                ],
                "default": "pymupdf",
            }
        }
    },
}


def test_route_first_match_dispatches():
    res = _run(CFG, "s", _reg(), _req(doc_type="invoice", pdf=True))
    assert res.response.backend.id == "reducto"
    dec = res.orchestration["decisions"][0]
    assert dec["point"] == "route" and dec["chosen"] == 0


def test_route_default_fallthrough():
    res = _run(CFG, "s", _reg(), _req(doc_type="generic", pdf=True))  # matches no rule
    assert res.response.backend.id == "pymupdf"
    assert res.orchestration["decisions"][0]["chosen"] == "default"


def test_route_all_rules_traced():
    res = _run(CFG, "s", _reg(), _req(doc_type="invoice", pdf=True))
    rules = res.orchestration["decisions"][0]["rules"]
    assert len(rules) == 2  # both rules recorded even though rule 0 matched
    assert rules[0]["matched"] and not rules[1]["matched"]


def test_route_nested_into_cascade():
    reg = _reg()
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "route": {
                    "rules": [{"when": {"doc_type": ["invoice"]}, "use": "strategy:cheap"}],
                    "default": "pymupdf",
                }
            },
            "cheap": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"},
        },
    }
    res = _run(cfg, "s", reg, _req(doc_type="invoice", pdf=True))
    assert res.response.backend.id in ("pymupdf", "reducto")  # ran the cascade
    assert res.orchestration["decisions"][0]["chosen"] == 0


def test_decide_node_takes_otherwise_in_engine_mode():
    reg = _reg()
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"decide": {"among": ["strategy:a", "pymupdf"], "otherwise": "pymupdf"}},
            "a": ["reducto"],
        },
    }
    res = _run(cfg, "s", reg, _req(pdf=True))
    assert res.response.backend.id == "pymupdf"  # engine mode → otherwise
    assert res.orchestration["decisions"][0]["point"] == "decide"
