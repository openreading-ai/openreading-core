"""CLI coverage for `openreading replay` and `openreading calibrate` — the two subcommands the
offline suite didn't exercise (cli/app.py was 67%). Both drive `main([...])` over the deterministic
sample PDF with a local (pymupdf) strategy, so they're fully offline and don't need network or
credentials. The deep replay/calibrate *semantics* are covered by test_strategy_decider /
test_strategy_calibrate; here we pin the CLI wrapper: arg handling, config loading, error exits,
and that stdout is schema-valid JSON."""

from __future__ import annotations

import json

import pytest

from openreading import schemas
from openreading.cli import main
from openreading.router.registry import Registry
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import RetryableError
from tests.fakes import ScriptedBackend

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _write_config(tmp_path, body: str) -> str:
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text(body)
    return str(cfg)


# a local-only cascade: rung 1 pymupdf (always eligible offline), gated so the config carries a
# calibratable predicate; rung 2 is only referenced (calibrate runs rung-1 only, replay of a clean
# doc never escalates).
_CONFIG = """\
version: 1
strategies:
  local_only:
    steps:
      - backend: pymupdf
        escalate_if:
          confidence_below: 0.85
      - tesseract
"""

# BL-112: a hosted rung-1 backend (reducto — hipaa_baa=tier_gated, no baa_tier_confirmed anywhere
# in this config) under a strategy file policy: block that requires a BAA. Never needs a real
# REDUCTO_API_KEY — the compliance refusal fires before any credential lookup or network call.
_POLICY_REQUIRES_BAA_CONFIG = """\
version: 1
policy:
  require_baa: true
strategies:
  hosted_only:
    steps:
      - backend: reducto
        escalate_if:
          confidence_below: 0.85
      - pymupdf
"""

# BL-123: the per-case sibling of _POLICY_REQUIRES_BAA_CONFIG above — no file-level policy: block
# at all, so a refusal can only come from an individual case.json's own compliance block. "cheap"
# is a fake, non-local, no-BAA hosted backend installed via _install_registry below.
_COMPLIANCE_PER_CASE_CONFIG = """\
version: 1
strategies:
  hosted_only:
    steps:
      - backend: cheap
        escalate_if:
          confidence_below: 0.85
      - premium
"""


# ---- replay -----------------------------------------------------------------------------------


def test_replay_roundtrip_is_deterministic_and_schema_valid(sample_pdf, tmp_path, capsys):
    cfg = _write_config(tmp_path, _CONFIG)

    # 1. run the strategy once to produce a real trace (response carries the orchestration block)
    rc = main(["parse", sample_pdf, "--strategy", "local_only", "--config", cfg])
    assert rc == 0
    original = json.loads(capsys.readouterr().out)
    assert original["orchestration"]["strategy"] == "local_only"
    chosen = original["orchestration"]["chosen_backend"]

    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(original))

    # 2. replay it: same logged decisions → same backend, still schema-valid JSON, exit 0
    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 0
    replayed = json.loads(capsys.readouterr().out)
    schemas.validate_response(replayed)
    assert replayed["orchestration"]["chosen_backend"] == chosen


def test_replay_refuses_on_a_mismatched_config_hash(sample_pdf, tmp_path, capsys):
    # BL-163: a whole-trace check at load time, before any decision point is consulted — a trace
    # whose recorded config_hash no longer matches the freshly compiled one (the configuration or
    # compliance posture changed since it was recorded) must be refused, naming config_hash, not
    # silently replayed against a different configuration than it was recorded under.
    cfg = _write_config(tmp_path, _CONFIG)
    rc = main(["parse", sample_pdf, "--strategy", "local_only", "--config", cfg])
    assert rc == 0
    original = json.loads(capsys.readouterr().out)
    original["orchestration"]["config_hash"] = "sha256:0000000000000000000000000000000000000000"

    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(original))

    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 3
    assert "config_hash" in capsys.readouterr().err


def test_replay_uses_strategy_name_from_the_trace(sample_pdf, tmp_path, capsys):
    # --strategy omitted → the name is read from the trace's orchestration block
    cfg = _write_config(tmp_path, _CONFIG)
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "local_only", "decisions": []}}))
    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["orchestration"]["strategy"] == "local_only"


def test_replay_without_strategy_name_anywhere_exits_2(sample_pdf, tmp_path, capsys):
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps({}))  # no orchestration, no strategy — and no --strategy given
    rc = main(["replay", sample_pdf, "--trace", str(trace)])
    assert rc == 2
    assert "no --strategy" in capsys.readouterr().err


def test_replay_without_config_exits_3(sample_pdf, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir → load_config finds no openreading.yaml
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "local_only", "decisions": []}}))
    rc = main(["replay", sample_pdf, "--trace", str(trace)])
    assert rc == 3
    assert "no openreading.yaml" in capsys.readouterr().err


# ---- calibrate --------------------------------------------------------------------------------


def _dataset(tmp_path, n: int) -> str:
    ds = tmp_path / "dataset"
    ds.mkdir()
    for i in range(n):
        d = ds / f"case_{i:02d}"
        d.mkdir()
        (d / "case.json").write_text(
            json.dumps({"name": f"c{i}", "input": {"builtin_sample": True}, "expected": {}})
        )
    return str(ds)


def test_calibrate_cli_emits_a_json_report(tmp_path, capsys):
    cfg = _write_config(tmp_path, _CONFIG)
    ds = _dataset(tmp_path, 3)
    rc = main(
        ["calibrate", ds, "--strategy", "local_only", "--config", cfg, "--target-escalation", "0.5"]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["strategy"] == "local_only"
    assert report["n_docs"] == 3
    assert report["rung1_backend"] == "pymupdf"
    # the gate's numeric predicate is what calibrate sweeps
    assert "confidence_below" in {s["predicate"] for s in report["sweeps"]}


# ---- BL-87: n_scored + the [calibrate] under-labeled advisory ----------------------------


def test_calibrate_cli_fully_unlabeled_dataset_reports_zero_scored_and_warns(tmp_path, capsys):
    # The exact fixture shape test_calibrate_cli_emits_a_json_report above already exercises
    # (every case's expected: {}) — n_scored must say plainly that nothing was actually scored,
    # and the CLI must warn on stderr rather than silently reporting a flat, precise-looking
    # scorer_agreement that measured nothing.
    cfg = _write_config(tmp_path, _CONFIG)
    ds = _dataset(tmp_path, 3)
    rc = main(
        ["calibrate", ds, "--strategy", "local_only", "--config", cfg, "--target-escalation", "0.5"]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["n_docs"] == 3
    assert report["n_scored"] == 0
    assert "[calibrate]" in err
    assert "0 of 3" in err


def test_calibrate_cli_partially_labeled_dataset_reports_n_scored_and_warns(tmp_path, capsys):
    # A mix: one case carries a recognized `expected` dimension, two don't — n_scored must land
    # strictly between 0 and n_docs, and the CLI must warn that scorer_agreement reflects only
    # the scored subset.
    cfg = _write_config(tmp_path, _CONFIG)
    ds = tmp_path / "dataset"
    ds.mkdir()
    cases = [
        {
            "name": "labeled",
            "input": {"builtin_sample": True},
            "expected": {"text_contains": ["x"]},
        },
        {"name": "unlabeled_a", "input": {"builtin_sample": True}, "expected": {}},
        {"name": "unlabeled_b", "input": {"builtin_sample": True}, "expected": {}},
    ]
    for i, case in enumerate(cases):
        d = ds / f"case_{i:02d}"
        d.mkdir()
        (d / "case.json").write_text(json.dumps(case))
    rc = main(["calibrate", str(ds), "--strategy", "local_only", "--config", cfg])
    assert rc == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["n_docs"] == 3
    assert report["n_scored"] == 1
    assert "[calibrate]" in err
    assert "1 of 3" in err


# ---- BL-107: per-case backend faults produce a coded, [calibrate]-tagged exit — never a bare
# traceback — and a mid-dataset fault doesn't silently discard the cases already scored before it


def _install_registry(monkeypatch, *backends) -> None:
    reg = Registry()
    for b in backends:
        reg.register(b)
    monkeypatch.setattr("openreading.cli.app.build_registry", lambda: reg)


class _FaultAtCase(ScriptedBackend):
    """Like ScriptedBackend, but only raises `error` from submit() on its `fail_at`-th (0-indexed)
    call — every case before it succeeds for real, so a test can prove those already-paid-for
    backend calls weren't silently thrown away by a mid-dataset fault (BL-107)."""

    def __init__(self, backend_id, *, fail_at, error, **kw):
        super().__init__(backend_id, **kw)
        self._fail_at = fail_at
        self._fault = error

    def submit(self, req, ctx):
        if len(self.requests) == self._fail_at:
            self.contexts.append(ctx)
            self.requests.append(req)
            raise self._fault
        return super().submit(req, ctx)


def test_calibrate_cli_retryable_error_from_submit_exits_cleanly_without_a_traceback(
    tmp_path, capsys, monkeypatch
):
    cfg = _write_config(tmp_path, _CONFIG)
    ds = _dataset(tmp_path, 1)
    fake = ScriptedBackend(
        "pymupdf", local=True, error=RetryableError("rate limited", backend_code="rate_limited")
    )
    _install_registry(monkeypatch, fake)

    rc = main(["calibrate", ds, "--strategy", "local_only", "--config", cfg])

    assert rc == 3
    out, err = capsys.readouterr()
    assert out == ""  # no partial/misleading report on the failure path
    assert "[calibrate]" in err
    assert "Traceback" not in err


def test_calibrate_cli_plain_crash_from_normalize_exits_cleanly_without_a_traceback(
    tmp_path, capsys, monkeypatch
):
    cfg = _write_config(tmp_path, _CONFIG)
    ds = _dataset(tmp_path, 1)
    fake = ScriptedBackend("pymupdf", local=True, normalize_error=KeyError("malformed_field"))
    _install_registry(monkeypatch, fake)

    rc = main(["calibrate", ds, "--strategy", "local_only", "--config", cfg])

    assert rc == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "[calibrate]" in err
    assert "Traceback" not in err


def test_calibrate_cli_mid_dataset_fault_does_not_silently_discard_already_scored_cases(
    tmp_path, capsys, monkeypatch
):
    # The live-reproduced shape from the backlog finding: case 3 of 4 faults. The two cases before
    # it made real, already-paid-for backend calls — the failure must say so, not read as if the
    # run failed on the very first case.
    cfg = _write_config(tmp_path, _CONFIG)
    ds = _dataset(tmp_path, 4)
    fake = _FaultAtCase(
        "pymupdf",
        local=True,
        fail_at=2,
        error=RetryableError("rate limited", backend_code="rate_limited"),
    )
    _install_registry(monkeypatch, fake)

    rc = main(["calibrate", ds, "--strategy", "local_only", "--config", cfg])

    assert rc == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "[calibrate]" in err
    assert "Traceback" not in err
    assert "2 case(s) already scored" in err
    # c0 and c1 genuinely ran; c2 was attempted and faulted; c3 was never reached.
    assert len(fake.requests) == 3


def test_calibrate_cli_per_case_compliance_refusal_names_the_case_and_already_scored_count(
    tmp_path, capsys, monkeypatch
):
    # BL-123: the CLI-level sibling of test_calibrate_cli_mid_dataset_fault_does_not_silently_
    # discard_already_scored_cases above, but the per-case fault is a ComplianceRefused from a
    # case.json-level `compliance` block on case index 1 (not index 0), rather than a
    # RetryableError — proving the case-name/"already scored" context reaches the CLI for a
    # per-case compliance refusal exactly the way it already does for every other AdapterError.
    # Deliberately NOT the file-level policy: block test_calibrate_cli_refuses_a_noncompliant_
    # rung1_backend_exits_3 below uses, which structurally always fires on case 0 and so has no
    # "already scored" count to add.
    cfg = _write_config(tmp_path, _COMPLIANCE_PER_CASE_CONFIG)
    fake = ScriptedBackend("cheap", local=False, hipaa_baa="no")
    _install_registry(monkeypatch, fake)

    ds = tmp_path / "dataset"
    ds.mkdir()
    ds.joinpath("case_00").mkdir()
    ds.joinpath("case_00", "case.json").write_text(
        json.dumps({"name": "c0", "input": {"builtin_sample": True}, "expected": {}})
    )
    ds.joinpath("case_01").mkdir()
    ds.joinpath("case_01", "case.json").write_text(
        json.dumps(
            {
                "name": "c1",
                "input": {"builtin_sample": True},
                "compliance": {"require_local": True},
                "expected": {},
            }
        )
    )

    rc = main(["calibrate", str(ds), "--strategy", "hosted_only", "--config", cfg])

    assert rc == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert "[calibrate]" in err
    assert "Traceback" not in err
    assert "1 case(s) already scored" in err  # c0 succeeded first
    # c0 (no compliance constraint) genuinely ran before c1's refusal stopped the loop.
    assert len(fake.requests) == 1


def test_calibrate_cli_without_config_exits_3(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no openreading.yaml on the path
    rc = main(["calibrate", str(tmp_path / "nope"), "--strategy", "local_only"])
    assert rc == 3
    assert "[calibrate]" in capsys.readouterr().err


def test_calibrate_cli_empty_dataset_exits_3(tmp_path, capsys):
    cfg = _write_config(tmp_path, _CONFIG)
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["calibrate", str(empty), "--strategy", "local_only", "--config", cfg])
    assert rc == 3
    assert "[calibrate]" in capsys.readouterr().err


def test_calibrate_cli_refuses_a_noncompliant_rung1_backend_exits_3(tmp_path, capsys):
    # BL-112: cmd_calibrate's `except (..., ComplianceRefused)` member was dead code — nothing on
    # calibrate_strategy's call graph could ever raise it. With the fix, the strategy file's own
    # policy: block (§1.3, folded the same way compile_strategy folds it) refuses a rung-1 backend
    # that can't satisfy it, and this now-reachable except member turns that into a clean exit 3 —
    # never a traceback, matching every other ComplianceRefused-producing command the openreading.cli docstring
    # already documents calibrate alongside.
    cfg = _write_config(tmp_path, _POLICY_REQUIRES_BAA_CONFIG)
    ds = _dataset(tmp_path, 2)
    rc = main(["calibrate", ds, "--strategy", "hosted_only", "--config", cfg])
    assert rc == 3
    assert "[calibrate]" in capsys.readouterr().err


# ---- malformed config surfaces as a located error, not a crash ---------------------------------

# steps must be a list — this is schema-valid YAML but an invalid strategy config, so load_config
# raises ConfigError and each command reports it with its own tag and exit 3.
_BAD_CONFIG = "version: 1\nstrategies:\n  local_only:\n    steps: 5\n"


def test_replay_malformed_config_exits_3(sample_pdf, tmp_path, capsys):
    cfg = _write_config(tmp_path, _BAD_CONFIG)
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "local_only", "decisions": []}}))
    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 3
    assert "[replay]" in capsys.readouterr().err


def test_calibrate_malformed_config_exits_3(tmp_path, capsys):
    cfg = _write_config(tmp_path, _BAD_CONFIG)
    rc = main(["calibrate", str(tmp_path / "ds"), "--strategy", "local_only", "--config", cfg])
    assert rc == 3
    assert "[calibrate]" in capsys.readouterr().err


# ---- a `policy:` block that is not a policy fails soft, exactly like a grammar error -----------
#
# One tagged stderr line and exit 3. main() returning at all — rather than propagating the error
# out of the command — is what pins "no traceback".

_BAD_POLICY = "version: 1\npolicy: {require_locall: true}\nstrategies:\n  local_only: [pymupdf]\n"


def test_replay_malformed_policy_block_exits_3_without_a_traceback(sample_pdf, tmp_path, capsys):
    cfg = _write_config(tmp_path, _BAD_POLICY)
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "local_only", "decisions": []}}))
    rc = main(["replay", sample_pdf, "--trace", str(trace), "--config", cfg])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[replay] ")
    assert "require_locall" in err
    assert len(err.splitlines()) == 1


def test_calibrate_malformed_policy_block_exits_3_without_a_traceback(tmp_path, capsys):
    cfg = _write_config(tmp_path, _BAD_POLICY)
    ds = _dataset(tmp_path, 1)
    rc = main(["calibrate", ds, "--strategy", "local_only", "--config", cfg])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[calibrate] ")
    assert "require_locall" in err
    assert len(err.splitlines()) == 1


def test_calibrate_cli_refuses_a_parallel_first_rung_without_a_traceback(tmp_path, capsys):
    """A `compare:` or `race:` step compiles to a node keyed `parallel`, which carries no single
    backend. Reading `rung1["backend"]` raised a bare KeyError at exit 1, with a traceback and no
    `[calibrate]` tag, where every other unsupported shape here is one line and exit 3."""
    cfg = _write_config(
        tmp_path,
        "version: 1\nstrategies:\n  side_by_side:\n"
        "    compare: [pymupdf, tesseract]\n    then: docling\n",
    )
    dataset = tmp_path / "cases" / "one"
    dataset.mkdir(parents=True)
    (dataset / "case.json").write_text('{"expected": {"text_contains": ["x"]}}')
    rc = main(["calibrate", str(tmp_path / "cases"), "--strategy", "side_by_side", "--config", cfg])
    assert rc == 3
    err = capsys.readouterr().err
    assert "[calibrate]" in err
    assert "Traceback" not in err
    assert "side_by_side" in err
