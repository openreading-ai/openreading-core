"""M4 — the strategy bridge: --keep-candidates retention (D-v4-6/D-v4-14), `compare --from`, and
the DIRECT-path byte-identity guard (H6b). Retention is exercised via ScriptedBackend in a
`pick: best` race, where the loser completes and is retained."""

from __future__ import annotations

import base64
import json

from openreading import api, schemas
from openreading.cli import main
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ " * 4

_BEST = {
    "version": 1,
    "strategies": {
        "s": {
            "parallel": ["reducto", "aws-textract"],
            "pick": "best",
            "budget": {"max_cost_usd": 1.0},
        }
    },
}

# the same `pick: best` node, wrapped as the single step of a cascade and carrying a step-position
# gate (§6.3) — the `compare:`/`then:` desugar target. Retention must survive the nested walk.
_CASCADE_GATED = {
    "version": 1,
    "strategies": {
        "s": {
            "budget": {"max_cost_usd": 1.0},
            "steps": [
                {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": {"garbled": True},
                }
            ],
        }
    },
}


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


def _run(reg, *, keep_candidates=False, cfg=_BEST):
    r = _req()
    compiled = compile_strategy(r, "s", StrategyConfig.model_validate(cfg), reg, RouterConfig())
    return run_strategy(
        compiled,
        r,
        registry=reg,
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        keep_candidates=keep_candidates,
    )


def _reg():
    from tests.fakes import ScriptedBackend, scripted_registry

    return scripted_registry(
        ScriptedBackend("reducto", text=GARBLED),  # low quality → judged_lost
        ScriptedBackend("aws-textract", text=CLEAN),  # winner
    )


# --- retention --------------------------------------------------------------------------


def test_candidates_retained_on_race() -> None:
    res = _run(_reg(), keep_candidates=True)
    assert res.response.backend.id == "aws-textract"
    cands = res.orchestration["candidates"]
    assert [c["backend"] for c in cands] == ["reducto"]  # the completed loser
    assert cands[0]["category"] == "judged_lost"
    schemas.validate_response(cands[0]["response"])  # a full, schema-valid envelope


def test_candidates_absent_by_default() -> None:
    res = _run(_reg())  # keep_candidates defaults False
    assert "candidates" not in res.orchestration


def test_candidates_retained_under_a_cascade_wrapped_gated_parallel_step() -> None:
    # a nested composite step walks a child context (its own clamped deadline); retention state must
    # travel with it, or the documented `compare --from` bridge silently returns zero candidates.
    res = _run(_reg(), keep_candidates=True, cfg=_CASCADE_GATED)
    assert res.response.backend.id == "aws-textract"  # clean winner passes the garbled step gate
    cands = res.orchestration["candidates"]
    assert [(c["backend"], c["category"]) for c in cands] == [("reducto", "judged_lost")]
    schemas.validate_response(cands[0]["response"])


def test_candidates_absent_by_default_under_a_cascade_wrapped_step() -> None:
    res = _run(_reg(), cfg=_CASCADE_GATED)
    assert "candidates" not in res.orchestration


# --- compare --from ---------------------------------------------------------------------


def test_compare_from_extracts_winner_and_candidates(tmp_path, capsys) -> None:
    res = _run(_reg(), keep_candidates=True)
    res.response.orchestration = res.orchestration
    doc = res.response.to_schema_dict()
    path = tmp_path / "run.json"
    path.write_text(json.dumps(doc))
    rc = main(["compare", "--from", str(path)])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["subjects"]) == 2  # winner + one retained candidate
    assert all(s["source"] == "candidate" for s in report["subjects"])
    assert {s["backend"]["id"] for s in report["subjects"]} == {"aws-textract", "reducto"}


def test_compare_from_without_candidates_exit_5(tmp_path, capsys) -> None:
    from tests.fakes import make_envelope

    path = tmp_path / "plain.json"
    path.write_text(json.dumps(make_envelope("pymupdf", text="x")))  # no orchestration
    assert main(["compare", "--from", str(path)]) == 5


# --- DIRECT path byte-identity (H6b) ----------------------------------------------------


def test_direct_path_byte_identical_with_flag() -> None:
    # --keep-candidates is a no-op on a direct backend run: the envelope is byte-identical.
    pdf = build_sample_pdf()
    a = api.run(pdf, backend="pymupdf", mime_type="application/pdf")
    b = api.run(pdf, backend="pymupdf", mime_type="application/pdf", keep_candidates=True)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "orchestration" not in a  # direct runs never carry an orchestration block
