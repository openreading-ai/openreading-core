"""The `openreading` console entrypoint (cli/__main__.py) and the `explain` command's branches —
the last uncovered corners of the CLI. `backends` is fully offline (no file, no network), so it's
the safe command to drive the module entrypoint through."""

from __future__ import annotations

import json
import runpy
import sys

import pytest

from openreading.cli import main


def test_cli_module_entrypoint_runs_backends(capsys, monkeypatch):
    # `python -m openreading.cli backends` — covers cli/__main__.py's SystemExit guard.
    monkeypatch.setattr(sys, "argv", ["openreading", "backends"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("openreading.cli", run_name="__main__", alter_sys=True)
    assert exc.value.code == 0
    assert "BACKEND" in capsys.readouterr().out


def test_explain_reports_missing_orchestration(tmp_path, capsys):
    resp = tmp_path / "plain.json"
    resp.write_text(json.dumps({"status": {"state": "succeeded"}}))  # no orchestration block
    rc = main(["explain", str(resp)])
    assert rc == 3
    assert "no orchestration block" in capsys.readouterr().err


def test_explain_renders_orchestration_story(tmp_path, capsys):
    # a minimal-but-real orchestration block: one attempt with a gate, one dropped backend.
    trace = tmp_path / "orch.json"
    trace.write_text(
        json.dumps(
            {
                "orchestration": {
                    "strategy": "s",
                    "chosen_backend": "pymupdf",
                    "outcome": "succeeded",
                    "attempts": [
                        {
                            "node": "rung1",
                            "backend": "pymupdf",
                            "category": "ok",
                            "duration_ms": 12,
                            "cost_usd": 0.0,
                            "gates": [
                                {
                                    "predicate": "confidence_below",
                                    "observed": 0.9,
                                    "threshold": 0.8,
                                    "fired": False,
                                }
                            ],
                        }
                    ],
                    "dropped": [{"backend": "aws-textract", "stage": 1, "code": "trains_on_data"}],
                }
            }
        )
    )
    rc = main(["explain", str(trace)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "strategy s" in out and "pymupdf" in out
    assert "confidence_below" in out  # the gate-rendering inner loop ran
    assert "dropped aws-textract" in out
