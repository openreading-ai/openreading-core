"""P4 — gate-record provenance (`source`) + `explain` grouping (§9, harness §15 T6).

A Plain strategy's gate records carry the four-word source they came from; `explain` groups gate
rows under that word. Advanced strategies carry no source and render exactly as today.
"""

from __future__ import annotations

import base64
import json
import types

from openreading.cli.app import cmd_explain
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.plain import desugar_config
from openreading.strategies.trace import GateRecord
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ " * 4
CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


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
    """Mirror the loader: desugar, then compile with the resulting plain_info, then run."""
    desugared, plain_info = desugar_config(cfg)
    model = StrategyConfig.model_validate(desugared)
    r = _req()
    compiled = compile_strategy(r, "s", model, reg, RouterConfig(), plain_info=plain_info)
    return run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())


def _gates(res, backend):
    for a in res.orchestration["attempts"]:
        if a["backend"] == backend:
            return a.get("gates", [])
    return []


def test_plain_gate_records_carry_source_word():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),  # trips looks_bad (garbled)
        ScriptedBackend("tesseract", local=True, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {"s": {"try": ["pymupdf", "tesseract"], "escalate_when": "looks_bad"}},
    }
    gates = _gates(_run(cfg, reg), "pymupdf")
    assert gates  # pymupdf gated and escalated
    assert all(g.get("source") == "looks_bad" for g in gates)  # every looks_bad member is tagged
    assert any(g["predicate"] == "garbled" and g["fired"] for g in gates)


def test_plain_source_maps_each_criterion_word():
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, typed_fields={}),  # missing fires
        ScriptedBackend("anthropic-claude", cost_low=0.01, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "try": ["reducto", "anthropic-claude"],
                "escalate_when": {"missing": ["total"], "low_confidence": 0.9},
            }
        },
    }
    gates = _gates(_run(cfg, reg), "reducto")
    by_pred = {g["predicate"]: g.get("source") for g in gates}
    assert by_pred.get("fields_required") == "missing"
    assert by_pred.get("confidence_below") == "low_confidence"


def test_advanced_gate_records_have_no_source():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"steps": [{"backend": "pymupdf", "escalate_if": {"garbled": True}}, "reducto"]}
        },
    }
    gates = _gates(_run(cfg, reg), "pymupdf")
    assert gates
    assert all("source" not in g for g in gates)  # advanced: no source key at all (renders flat)


def test_gate_record_source_is_additive():
    assert "source" not in GateRecord("garbled", True, False, False).as_dict()
    d = GateRecord("garbled", True, True, True, source="looks_bad").as_dict()
    assert d["source"] == "looks_bad"


def _explain(response, tmp_path, capsys):
    f = tmp_path / "resp.json"
    f.write_text(json.dumps(response))
    cmd_explain(types.SimpleNamespace(response=str(f)))
    return capsys.readouterr().out


def _attempt(gates):
    return {
        "orchestration": {
            "strategy": "s",
            "chosen_backend": "tesseract",
            "outcome": "ok",
            "attempts": [
                {
                    "node": "steps[0]",
                    "backend": "pymupdf",
                    "category": "quality_escalated",
                    "duration_ms": 410,
                    "cost_usd": 0,
                    "gates": gates,
                }
            ],
        }
    }


def test_explain_groups_plain_gates_by_source(tmp_path, capsys):
    resp = _attempt(
        [
            {
                "predicate": "scanned_pages_detected",
                "threshold": True,
                "observed": True,
                "fired": True,
                "source": "looks_bad",
            },
            {
                "predicate": "empty_pages_over",
                "threshold": 0.2,
                "observed": 1.0,
                "fired": True,
                "source": "looks_bad",
            },
            {
                "predicate": "garbled",
                "threshold": True,
                "observed": 0.1,
                "fired": False,
                "source": "looks_bad",
            },
            {
                "predicate": "confidence_below",
                "threshold": 0.6,
                "observed": None,
                "fired": False,
                "skipped": "signal_unavailable",
                "source": "low_confidence",
            },
        ]
    )
    out = _explain(resp, tmp_path, capsys)
    assert out.count("looks_bad") == 1  # grouped: one header, not repeated per predicate
    assert out.count("low_confidence") == 1
    assert "scanned_pages_detected" in out and "garbled" in out  # predicates still shown
    # the group header precedes its predicates
    assert out.index("looks_bad") < out.index("scanned_pages_detected")


def test_explain_advanced_gates_render_flat(tmp_path, capsys):
    resp = _attempt(
        [{"predicate": "garbled", "threshold": True, "observed": True, "fired": True}]  # no source
    )
    out = _explain(resp, tmp_path, capsys)
    assert "garbled" in out
    assert "looks_bad" not in out  # advanced traces are not grouped


def _decisions(decisions):
    return {
        "orchestration": {
            "strategy": "s",
            "chosen_backend": "pymupdf",
            "outcome": "ok",
            "attempts": [],
            "decisions": decisions,
        }
    }


def test_explain_renders_decisions_downgraded_only_when_set(tmp_path, capsys):
    """BL-60: `decisions[]` is rendered; a genuine downgrade's reason string is surfaced, and a
    decision that resolved to its own configured choice carries no downgrade annotation."""
    resp = _decisions(
        [
            {
                "decision_id": "abc123",
                "node_path": "steps[0].decide",
                "label": "d1",
                "point": "decide",
                "eligible": ["pymupdf", "tesseract"],
                "chosen": "otherwise",
                "decider": "engine",
                "config_hash": "h",
                "strategy": "s",
                "downgraded": "env_disabled",
            },
            {
                "decision_id": "def456",
                "node_path": "steps[1].decide",
                "label": "d2",
                "point": "decide",
                "eligible": ["reducto"],
                "chosen": "reducto",
                "decider": "llm",
                "config_hash": "h",
                "strategy": "s",
                "downgraded": None,
            },
        ]
    )
    out = _explain(resp, tmp_path, capsys)
    lines = out.splitlines()
    d1_line = next(line for line in lines if "steps[0].decide" in line)
    d2_line = next(line for line in lines if "steps[1].decide" in line)
    assert "env_disabled" in d1_line  # the downgrade reason string is surfaced
    assert "otherwise" in d1_line  # ...alongside the decision's chosen value
    assert "reducto" in d2_line  # a non-downgraded decision's chosen value is shown
    assert "downgraded" not in d2_line  # ...with no downgrade annotation
