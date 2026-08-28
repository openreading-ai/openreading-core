"""CLI — drives main() over the sample PDF. `parse` runs PyMuPDF for real and emits schema-valid
JSON; a hosted backend without credentials exits cleanly; `route` prints a compliance-first plan.
The last section pins the one exit code every `ComplianceRefused` catch site must return (3)."""

from __future__ import annotations

import json
import sys

import pytest

from openreading import schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.cli import main
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import Job, JobState, WaitMode
from openreading.types.errors import ComplianceRefused, RetryableError, UnsupportedFeatureError
from openreading.types.liveness import (  # noqa: E402
    LivenessReport,
    LivenessStatus,
    ProbeKind,
)
from tests.fakes import ScriptedBackend
from tests.test_batch_native import _FakeNative

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def test_parse_pymupdf_emits_schema_valid_json(sample_pdf, capsys):
    rc = main(["parse", sample_pdf, "--backend", "pymupdf", "--pages", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    schemas.validate_response(out)  # the CLI already validated; double-check here
    assert out["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in out["document"]["text"]


def test_parse_hosted_without_credentials_names_the_missing_vars(sample_pdf, capsys, monkeypatch):
    for var in (
        "GCP_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROCESSOR_ID",
        "OPENREADING_GOOGLE_DOCUMENT_AI_PROJECT_ID",
        "OPENREADING_GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    rc = main(["parse", sample_pdf, "--backend", "google-document-ai"])
    assert rc == 3  # clean preflight exit, not a crash
    err = capsys.readouterr().err
    # the error a user sees IS the setup doc: exact env vars + signup URL
    assert "missing required" in err
    assert "GCP_PROJECT_ID" in err and "GCP_PROCESSOR_ID" in err
    assert "cloud.google.com/document-ai" in err  # signup_url


def test_parse_unknown_backend_errors(sample_pdf, capsys):
    with pytest.raises(SystemExit):  # argparse choices reject it
        main(["parse", sample_pdf, "--backend", "nope"])


def test_parse_extract_on_structural_backend_reports_unsupported(sample_pdf, capsys):
    # pymupdf can't do schema-driven extraction; asking for it via --extract must surface the 4th
    # taxonomy member as a clean advisory, not silently return geometry-only.
    rc = main(["parse", sample_pdf, "--backend", "pymupdf", "--extract"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "unsupported feature" in err and "custom_schema_extraction" in err


def test_route_phi_policy_chooses_compliant_and_drops_noncompliant(sample_pdf, tmp_path, capsys):
    policy = tmp_path / "phi.json"
    policy.write_text(
        json.dumps({"require_baa": True, "no_train_on_data": True, "optimize_for": "accuracy"})
    )
    rc = main(["route", sample_pdf, "--policy", str(policy)])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["chosen"] is not None
    # aws-textract trains=opt_out and the opt-out is not confirmed here -> dropped at stage 1
    assert plan["dropped"]["aws-textract"]["stage"] == 1
    assert plan["dropped"]["aws-textract"]["code"] == "trains_on_data"
    # the chosen + every fallback is compliant (local, or a no-train cloud with a BAA in force —
    # a tier-gated BAA is not one, and this policy confirmed nothing)
    from openreading.adapters.registry import make_adapter

    assert plan["dropped"]["reducto"]["code"] == "no_baa"
    for bid in [plan["chosen"], *plan["fallbacks"]]:
        c = make_adapter(bid).descriptor.compliance
        assert c.runs_fully_local or c.hipaa_baa == "yes"
        assert c.runs_fully_local or c.trains_on_customer_data in ("no", "na_local")


def test_route_confirmed_baa_tier_readmits_reducto(sample_pdf, tmp_path, capsys):
    policy = tmp_path / "phi.json"
    policy.write_text(json.dumps({"require_baa": True, "baa_tier_confirmed": ["reducto"]}))
    main(["route", sample_pdf, "--policy", str(policy)])
    plan = json.loads(capsys.readouterr().out)
    assert "reducto" in [plan["chosen"], *plan["fallbacks"]]


def test_route_confirmed_optout_readmits_textract(sample_pdf, tmp_path, capsys):
    policy = tmp_path / "phi.json"
    policy.write_text(
        json.dumps(
            {
                "require_baa": True,
                "no_train_on_data": True,
                "train_optout_confirmed": ["aws-textract"],
            }
        )
    )
    main(["route", sample_pdf, "--policy", str(policy)])
    plan = json.loads(capsys.readouterr().out)
    assert "aws-textract" in [plan["chosen"], *plan["fallbacks"]]


def test_route_require_local_drops_all_hosted_and_can_run(sample_pdf, tmp_path, capsys):
    policy = tmp_path / "local.json"
    policy.write_text(json.dumps({"require_local": True, "optimize_for": "offline"}))
    rc = main(["route", sample_pdf, "--policy", str(policy), "--run"])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0
    from openreading.adapters.registry import make_adapter

    for bid in [plan["chosen"], *plan["fallbacks"]]:
        assert make_adapter(bid).descriptor.compliance.runs_fully_local
    # --run executed the chosen local backend and attached a schema-valid result
    assert "result" in plan
    schemas.validate_response(plan["result"])


def test_route_run_normalize_crash_is_a_clean_error_not_a_traceback(
    sample_pdf, tmp_path, capsys, monkeypatch
):
    # BL-99 Finding 2 (Trent, folded into this item): before execute_plan itself caught a plain
    # normalize() crash, it propagated straight out of execute_plan, past cmd_route's own
    # `except PlanExhaustedError` clause (the only one it has around `--run`), all the way out of
    # main() (which has no catch-all either) — a raw Python traceback, file paths and line numbers,
    # no `[route]` tag. Once execute_plan's own fix lands (falls back, or cleanly exhausts to
    # PlanExhaustedError), cmd_route's EXISTING handling already produces a clean line: a trail entry
    # never carries the raw failure message (readiness.py's own documented design — "Trails drop the
    # failure message"), so there is no separate CLI-side redaction fix to make; this proves the
    # property end to end through the real CLI entry point, not just execute_plan in isolation. Also
    # asserts the secret never reaches stderr, per the openreading.credentials docstring's CLI-inclusive promise.
    from openreading.adapters.pymupdf.adapter import PyMuPDFAdapter

    secret = "sk-cli-leak-0009-must-never-appear"
    calls: list[int] = []

    def _boom(self, job, ctx, req):
        calls.append(1)
        raise ValueError(f"malformed page structure, key={secret}")

    monkeypatch.setattr(PyMuPDFAdapter, "normalize", _boom)

    policy = tmp_path / "empty.json"
    policy.write_text("{}")  # no constraints: the local set (pymupdf among it) is eligible
    rc = main(["route", sample_pdf, "--policy", str(policy), "--run"])

    assert calls, "pymupdf.normalize was never attempted — the crash path was not exercised"
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert secret not in err
    if rc == 3:  # the chain exhausted cleanly
        assert err.startswith("[route")
    else:  # a healthy fallback backend still completed the run
        assert rc == 0


# ---- BL-144: _describe_read_error's own docstring-claimed PermissionError/IsADirectoryError -----
# ---- branches, direct and through the CLI ---------------------------------------------------------
#
# BL-141 (this same past sprint) added _describe_read_error and claimed, in its own docstring, that
# `.strerror` is correct "for a real FileNotFoundError/PermissionError" — but every regression test it
# added or extended only ever constructed a missing file (FileNotFoundError/SourceNotFoundError) or
# malformed JSON (json.JSONDecodeError). Nothing in the suite ever triggered a real OS-level
# PermissionError, and the same-shaped IsADirectoryError gap (a caller pointing --policy/--trace/
# --response at a directory) was never covered either. The line itself is coverage-covered by the
# tested cases (shared branch), so --cov-fail-under=91 can't see this gap — these tests close it
# directly, without relying on line coverage as a proxy for fault-class coverage.


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 is a POSIX permission model")
def test_describe_read_error_permission_error_returns_bare_strerror(tmp_path):
    from openreading.cli.app import _describe_read_error

    blocked = tmp_path / "blocked.json"
    blocked.write_text("{}")
    blocked.chmod(0o000)
    try:
        with pytest.raises(PermissionError) as exc_info:
            blocked.read_text()
    finally:
        blocked.chmod(0o644)  # restore so tmp_path's own teardown can remove it
    e = exc_info.value
    # .strerror, not a hardcoded OS message string — the exact wording is platform-dependent.
    assert _describe_read_error(e) == e.strerror


def test_describe_read_error_is_a_directory_error_returns_bare_strerror(tmp_path):
    from openreading.cli.app import _describe_read_error

    with pytest.raises(IsADirectoryError) as exc_info:
        tmp_path.read_text()
    e = exc_info.value
    assert _describe_read_error(e) == e.strerror


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 is a POSIX permission model")
def test_route_unreadable_policy_exits_3_without_a_traceback(sample_pdf, tmp_path, capsys):
    # A missing path, malformed JSON, and an unreadable (chmod 000) file are all user errors, so they
    # land in the same soft-failure bucket --config already uses: one tagged stderr line, exit 3.
    # main() returning at all (rather than propagating FileNotFoundError/JSONDecodeError/
    # PermissionError) is what pins "no traceback". BL-144: the chmod'd case is the one
    # through-the-CLI proof that _describe_read_error's PermissionError branch — docstring-claimed
    # since BL-141, never previously exercised anywhere — behaves like its FileNotFoundError/
    # JSONDecodeError siblings below, on `_load_policy`, the one non-SourceNotFoundError call site.
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text(json.dumps({"require_local": True}))
    unreadable.chmod(0o000)
    try:
        for policy in (tmp_path / "missing.json", malformed, unreadable):
            rc = main(["route", sample_pdf, "--policy", str(policy)])
            assert rc == 3
            err = capsys.readouterr().err
            assert err.startswith("[route] cannot read policy")
            assert len(err.splitlines()) == 1
            # BL-141: the path (a missing-file FileNotFoundError, a JSONDecodeError, and now a
            # PermissionError alike) is named exactly once — not once in the "cannot read policy
            # <path>:" prefix and again inside {e}'s own string, the way a real OSError's str()
            # already embeds ": '<path>'".
            assert err.count(str(policy)) == 1
    finally:
        unreadable.chmod(0o644)  # restore so tmp_path's own teardown can remove it


def test_backends_command_reports_ready_and_missing(capsys, monkeypatch):
    monkeypatch.setenv("REDUCTO_API_KEY", "sk_present")
    for var in ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc = main(["backends"])
    assert rc == 0
    rows = {
        line.split()[0]: line
        for line in capsys.readouterr().out.splitlines()
        if line and not line.startswith("BACKEND")
    }
    assert " yes " in f" {rows['reducto'].split(maxsplit=2)[2]} " or "yes" in rows["reducto"]
    assert "yes" in rows["pymupdf"]  # local, always ready
    assert "no" in rows["azure-document-intelligence"]
    assert "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT" in rows["azure-document-intelligence"]


def test_auth_rejected_message_names_env_and_never_echoes_key():
    from openreading.readiness import auth_rejected_hint

    msg = auth_rejected_hint("reducto")
    assert "REDUCTO_API_KEY" in msg
    assert "platform.reducto.ai" in msg
    assert "rejected" in msg


# ---- ComplianceRefused is exit 3 on every command that catches it -------------------------------
#
# Exit 4 belongs to `route`'s "no compliant backend for the policy" verdict (and a partial batch);
# a refusal reported by any other command is a can't-run, i.e. 3. Three of the six catch sites
# (batch parse, calibrate, compare fan-out) cannot reach a real refusal today — nothing plumbs a
# policy that far — so those inject the exception at the seam the command actually calls.

_HOSTED_ONLY = "strategies:\n  hosted_only:\n    steps:\n      - backend: reducto\n"


@pytest.fixture
def refusing_config(tmp_path):
    """A strategy whose only backend is hosted — refused outright under `require_local`."""
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text("version: 1\n" + _HOSTED_ONLY)
    return str(cfg)


@pytest.fixture
def local_policy(tmp_path):
    p = tmp_path / "local.json"
    p.write_text(json.dumps({"require_local": True}))
    return str(p)


def _raise_refused(*args, **kwargs):
    raise ComplianceRefused("nothing is compliant here", constraint="no_compliant_backend")


def test_parse_compliance_refused_exits_3(sample_pdf, tmp_path, capsys):
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text("version: 1\npolicy:\n  require_local: true\n" + _HOSTED_ONLY)
    rc = main(["parse", sample_pdf, "--strategy", "hosted_only", "--config", str(cfg)])
    assert rc == 3
    assert "no compliant backend" in capsys.readouterr().err


def test_parse_batch_compliance_refused_exits_3(sample_pdf, capsys, monkeypatch):
    from openreading.cli import app as cli_app

    monkeypatch.setattr(cli_app.api, "run_batch", _raise_refused)
    rc = main(["parse", sample_pdf, sample_pdf, "--backend", "pymupdf"])  # >=2 args → batch
    assert rc == 3
    assert "nothing is compliant here" in capsys.readouterr().err


def test_strategy_plan_compliance_refused_exits_3(
    sample_pdf, refusing_config, local_policy, capsys
):
    rc = main(
        [
            "strategy",
            "plan",
            sample_pdf,
            "--strategy",
            "hosted_only",
            "--config",
            refusing_config,
            "--policy",
            local_policy,
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "[strategy plan]" in err and "no compliant backend" in err


def test_replay_compliance_refused_exits_3(
    sample_pdf, refusing_config, local_policy, tmp_path, capsys
):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "hosted_only", "decisions": []}}))
    rc = main(
        [
            "replay",
            sample_pdf,
            "--trace",
            str(trace),
            "--config",
            refusing_config,
            "--policy",
            local_policy,
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "[replay]" in err and "no compliant backend" in err


def test_calibrate_compliance_refused_exits_3(refusing_config, tmp_path, capsys, monkeypatch):
    import openreading.strategies.calibrate as calibrate_mod

    monkeypatch.setattr(calibrate_mod, "calibrate_strategy", _raise_refused)
    rc = main(
        [
            "calibrate",
            str(tmp_path / "ds"),
            "--strategy",
            "hosted_only",
            "--config",
            refusing_config,
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "[calibrate]" in err and "nothing is compliant here" in err


def test_compare_fanout_compliance_refused_exits_3(sample_pdf, capsys, monkeypatch):
    from openreading.cli import app as cli_app

    monkeypatch.setattr(cli_app.api, "run", _raise_refused)
    rc = main(["compare", sample_pdf, "--backends", "pymupdf,tesseract"])
    assert rc == 3
    assert "nothing is compliant here" in capsys.readouterr().err


# ---- BL-122: RetryableError reaching a directly-named backend is a clean exit 3, never the -----
# ---- generic exit-1 crash handler -------------------------------------------------------------
#
# The realistic trigger is router.driver's poll loop raising RetryableError past its deadline /
# MAX_CONSECUTIVE_FAULTS (or a submit-time rate-limit). Two fixture traps apply here (not to the
# ComplianceRefused tests above, which patch api.run/calibrate_strategy directly): (1) `--backend`
# validates against `choices=sorted(BUILTIN_ADAPTERS)` at argparse time, so the fixture must use a
# real slug — `pymupdf`, never a made-up one; (2) `cmd_parse`'s and `cmd_compare`'s named-backend
# resolution goes through `make_adapter()` (BUILTIN_ADAPTERS directly), never `build_registry` —
# patching `cli.app.build_registry` (test_cli_replay_calibrate.py's `_install_registry` pattern)
# is a silent no-op here and would let the real PyMuPDF adapter run instead of the fake.


def test_parse_named_backend_retryable_error_exits_3_clean(sample_pdf, capsys, monkeypatch):
    fake = ScriptedBackend("pymupdf", error=RetryableError("simulated deadline exceeded"))
    monkeypatch.setitem(BUILTIN_ADAPTERS, "pymupdf", lambda: fake)
    rc = main(["parse", sample_pdf, "--backend", "pymupdf"])
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[pymupdf] simulated deadline exceeded"]  # single clean [label] line
    assert "RetryableError" not in err  # no class-name prefix (unlike the exit-1 handler)
    assert "error:" not in err  # the exit-1 handler's own "error: <ClassName>: " marker


# ---- BL-128: RetryableError from a native-batch backend's submit_many/run_to_completion is a ---
# ---- clean exit 3 through `_cmd_parse_batch` too, never the generic exit-1 crash handler --------
#
# BL-122 (this same sprint) fixed this exact defect shape for `cmd_parse`'s single-document path
# and `cmd_compare`'s fan-out, but never reached `_cmd_parse_batch` — the third sibling call site,
# reached via `api.run_batch()`'s native-batch dispatch (`_run_native`) rather than the single-
# document `run_request`. Two independent triggers, both through the real, unmodified `main()`
# entry point and a real `_FakeNative` (tests/test_batch_native.py) registered under a real
# `BUILTIN_ADAPTERS` slug — never a made-up one, since `--backend` validates against
# `choices=sorted(BUILTIN_ADAPTERS)` at argparse time.


def test_parse_batch_native_submit_error_exits_3_clean(sample_pdf, capsys, monkeypatch):
    # Trigger A: a native-batch backend's submit_many raises RetryableError outright (e.g.
    # submit-time rate-limit exhaustion). Before this fix, this fell through _cmd_parse_batch's
    # old, hand-rolled `(TerminalError, ComplianceRefused)` except tuple into the generic `except
    # Exception` handler: exit 1, `[batch] error: RetryableError: ...` — the raw, class-name-
    # prefixed shape BL-122 eliminated at the other two sites.
    fake = _FakeNative(submit_many_error=RetryableError("simulated submit-time rate limit"))
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-native", lambda: fake)
    rc = main(["parse", sample_pdf, sample_pdf, "--backend", "fake-native"])  # >=2 args -> batch
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[fake-native] simulated submit-time rate limit"]  # single clean [label] line
    assert "RetryableError" not in err  # no class-name prefix (unlike the exit-1 handler)
    assert "error:" not in err  # the exit-1 handler's own "error: <ClassName>: " marker


def test_compare_fanout_retryable_error_exits_3_clean(sample_pdf, capsys, monkeypatch):
    fake = ScriptedBackend("pymupdf", error=RetryableError("simulated rate-limit exhaustion"))
    monkeypatch.setitem(BUILTIN_ADAPTERS, "pymupdf", lambda: fake)
    rc = main(["compare", sample_pdf, "--backends", "pymupdf,tesseract"])
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[pymupdf] simulated rate-limit exhaustion"]  # single clean [bid] line
    assert "RetryableError" not in err
    assert "error:" not in err


def test_compare_deadline_flag_reaches_the_fanout_and_exceeded_exits_3_clean(
    sample_pdf, capsys, monkeypatch
):
    # BL-169 round-1 review (trent, Medium): compare's fan-out (api.run(doc, backend=bid)) shared
    # parse's exact original Bug B exposure — no --deadline at all. Folded in the same shape as
    # parse's own single-document sibling: a job that never finishes and never faults, driven past
    # a real (FakeClock) deadline through the real, unmodified CLI entry point.
    from openreading.cli import app as cli_app
    from openreading.router.clock import FakeClock
    from tests.fakes import PollFake

    stuck = PollFake(polls_needed=10**9, flaky=0)  # never finishes, never faults
    monkeypatch.setattr(cli_app.api, "RealClock", FakeClock)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "stuck-poll-fake", lambda: stuck)
    rc = main(["compare", sample_pdf, "--backends", "stuck-poll-fake,tesseract", "--deadline", "1"])
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[stuck-poll-fake] deadline exceeded"]


def test_compare_deadline_flag_threads_seconds_to_every_fanned_out_backend_as_ms(
    sample_pdf, capsys, monkeypatch
):
    # BL-169 round-2 review (ben AND trent, both independently, High, mutation-confirmed): the
    # end-to-end timeout test above cannot tell "the --deadline value took effect" apart from "the
    # pre-existing 120s default eventually fired anyway" — an infinite-poll fake under FakeClock
    # trips ANY finite deadline identically. Both reviewers proved this by reverting just
    # cli/app.py's `deadline_ms=deadline_ms` in cmd_compare's fan-out call and getting a fully
    # green suite. This is compare's sibling of parse's own sound spy test
    # (test_parse_single_document_deadline_flag_threads_seconds_to_named_backend_as_ms): asserts
    # the actual numeric value reaches build_run_context for EVERY fanned-out backend, not just
    # that *a* timeout eventually occurs somewhere in the loop.
    from openreading.cli import app as cli_app
    from tests.fakes import ScriptedBackend

    seen: list = []
    real_build_run_context = cli_app.api.build_run_context

    def spy(req, descriptor, *, broker=None, deadline_ms=None):
        seen.append(deadline_ms)
        return real_build_run_context(req, descriptor, broker=broker, deadline_ms=deadline_ms)

    monkeypatch.setattr(cli_app.api, "build_run_context", spy)
    monkeypatch.setitem(
        BUILTIN_ADAPTERS, "fake-compare-a", lambda: ScriptedBackend("fake-compare-a")
    )
    monkeypatch.setitem(
        BUILTIN_ADAPTERS, "fake-compare-b", lambda: ScriptedBackend("fake-compare-b")
    )
    rc = main(
        ["compare", sample_pdf, "--backends", "fake-compare-a,fake-compare-b", "--deadline", "90"]
    )
    assert rc == 0
    assert seen == [90_000, 90_000]


def test_parse_batch_native_deadline_exceeded_exits_3_clean(sample_pdf, capsys, monkeypatch):
    # Trigger B: the ordinary, no-fault-injected path — a native-batch job simply still RUNNING
    # when DEFAULT_DEADLINE_MS (120_000ms, unconditional on every path per build_run_context) has
    # elapsed. TRAP: _FakeNative's own default submit_many returns an already-SUCCEEDED job, so a
    # naive test that doesn't override submit_many would exercise no polling loop at all and pass
    # without ever reaching _run_native's deadline branch — submit_many must be explicitly
    # overridden here to return a job that stays RUNNING forever (poll() just echoes it back), so
    # only the deadline check — not any injected fault — ends the loop. A real 120s wall-clock wait
    # is avoided by monkeypatching `api.RealClock` to `FakeClock` (router/clock.py's own existing
    # injectable-clock test double: `sleep()` advances virtual time instead of blocking), so the
    # deadline is crossed via fast virtual time through the real, unmodified `_run_native`.
    from openreading.cli import app as cli_app
    from openreading.router.clock import FakeClock

    class _StuckRunningNative(_FakeNative):
        def submit_many(self, reqs, ctx):
            return Job(
                id="jb-stuck",
                backend_id="fake-native",
                wait_mode=WaitMode.POLL,
                state=JobState.RUNNING,
            )

        def poll(self, job, ctx):
            return job  # never advances past RUNNING — only the deadline check ends the loop

    monkeypatch.setattr(cli_app.api, "RealClock", FakeClock)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-native", lambda: _StuckRunningNative())
    rc = main(["parse", sample_pdf, sample_pdf, "--backend", "fake-native"])
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[fake-native] deadline exceeded"]  # router/driver.py's own literal message
    assert "RetryableError" not in err
    assert "error:" not in err


# ---- BL-129: UnsupportedFeatureError reaching cmd_compare's fan-out is a clean exit 3, never ----
# ---- the generic exit-1 crash handler — the fourth _CLEAN_EXIT3_ERRORS member, added right ------
# ---- after BL-122 built the tuple with only three ------------------------------------------------
#
# Same two fixture traps as the RetryableError pair above (real BUILTIN_ADAPTERS slug, patched via
# monkeypatch.setitem so make_adapter() resolves the fake). cmd_parse is deliberately NOT re-tested
# here: its own dedicated `except UnsupportedFeatureError` clause already catches this before
# `_CLEAN_EXIT3_ERRORS` is ever reached (covered by test_parse_extract_on_structural_backend_
# reports_unsupported above) — this gap was cmd_compare's fan-out only.


def test_compare_fanout_unsupported_feature_error_exits_3_clean(sample_pdf, capsys, monkeypatch):
    fake = ScriptedBackend(
        "pymupdf", error=UnsupportedFeatureError("simulated unsupported feature", feature="ocr")
    )
    monkeypatch.setitem(BUILTIN_ADAPTERS, "pymupdf", lambda: fake)
    rc = main(["compare", sample_pdf, "--backends", "pymupdf,tesseract"])
    assert rc == 3
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert lines == ["[pymupdf] simulated unsupported feature"]  # single clean [bid] line
    assert "UnsupportedFeatureError" not in err  # no class-name prefix (unlike the exit-1 handler)
    assert "error:" not in err  # the exit-1 handler's own "error: <ClassName>: " marker


# ---- BL-133: a missing document/trace/response path is a clean, coded exit — never a traceback --
#
# Same defect class BL-32 (sprint 6) fixed for --policy/--config on these same commands
# (test_route_unreadable_policy_exits_3_without_a_traceback above is the shape every test below
# mirrors); BL-32 never touched the positional document/--trace/--response argument each of these
# commands also reads first. `cmd_parse`'s single-document branch gets exit 2 (SourceNotFoundError,
# matching `_cmd_parse_batch`'s own sibling handling of the identical condition); the rest get exit
# 3, `the openreading.cli docstring`'s established "can't run" bucket for an unreadable input file.
#
# BL-141: every test below also pins the path appearing EXACTLY ONCE in the printed line — the
# duplicate-path defect this item fixed (`_describe_read_error` swaps a real OSError's `.strerror`,
# or an `str(e)` that never had the path to begin with, in for `str(e)` on the exception directly,
# so the explicit `cannot read {path}:` prefix each site already prints is never echoed a second
# time by the exception's own message).


def test_parse_missing_document_exits_2_without_a_traceback(tmp_path, capsys):
    missing = tmp_path / "missing.pdf"
    rc = main(["parse", str(missing), "--backend", "pymupdf"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("[pymupdf]")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    # cmd_parse already let SourceNotFoundError's own string carry the path (no separate prefix) —
    # BL-141's errno-style reconstruction of SourceNotFoundError must not regress that single
    # mention into two.
    assert err.count(str(missing)) == 1
    # BL-143: BL-141 constructed SourceNotFoundError with a placeholder `errno=None`, and cmd_parse
    # interpolates str(e) directly (it's one of the two sites BL-141 deliberately left outside its
    # own `_describe_read_error` helper) — CPython's OSError.__str__ rendered that as a literal
    # "[Errno None] ..." prefix. A real errno.ENOENT must make this indistinguishable from a genuine
    # OS-raised FileNotFoundError.
    assert "None" not in err
    assert err == f"[pymupdf] [Errno 2] no such file or directory: '{missing}'\n"


def test_parse_batch_glob_no_matches_exits_2_without_a_traceback(tmp_path, capsys):
    # _cmd_parse_batch's glob branch: a single glob-pattern argument (no directory, no second
    # positional) is enough to route through looks_batch into batch mode (M2), where _expand_arg's
    # `matches` guard raises SourceNotFoundError("glob matched no files", ...) before any backend
    # is ever touched.
    pattern = str(tmp_path / "*.nope")
    rc = main(["parse", pattern, "--backend", "pymupdf"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("[batch]")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert err.count(pattern) == 1
    # BL-143: same errno=None regression as cmd_parse's single-document branch above, but through
    # _cmd_parse_batch's own (SourceLimitError, SourceNotFoundError, JobsLimitError) except clause.
    assert "None" not in err
    assert err == f"[batch] [Errno 2] glob matched no files: '{pattern}'\n"


def test_parse_batch_missing_positional_path_exits_2_without_a_traceback(
    sample_pdf, tmp_path, capsys
):
    # _cmd_parse_batch's positional-path branch: >=2 positional args always routes through
    # looks_batch into batch mode (M2) regardless of whether either one is a glob/directory. The
    # first arg resolves fine; the second hits _expand_arg's trailing "no such file or directory"
    # raise for a plain path that is neither a glob, a directory, nor an existing file.
    missing = tmp_path / "missing.pdf"
    rc = main(["parse", sample_pdf, str(missing), "--backend", "pymupdf"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("[batch]")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert err.count(str(missing)) == 1
    # BL-143: same errno=None regression as cmd_parse's single-document branch above, but through
    # _cmd_parse_batch's own (SourceLimitError, SourceNotFoundError, JobsLimitError) except clause.
    assert "None" not in err
    assert err == f"[batch] [Errno 2] no such file or directory: '{missing}'\n"


def test_route_missing_document_exits_3_without_a_traceback(tmp_path, capsys):
    policy = tmp_path / "phi.json"
    policy.write_text(json.dumps({"require_baa": True}))
    missing = tmp_path / "missing.pdf"
    rc = main(["route", str(missing), "--policy", str(policy)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[route] cannot read")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert err.count(str(missing)) == 1


def test_strategy_plan_missing_document_exits_3_without_a_traceback(tmp_path, capsys):
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text("version: 1\nstrategies:\n  main:\n    steps:\n      - backend: pymupdf\n")
    missing = tmp_path / "missing.pdf"
    rc = main(["strategy", "plan", str(missing), "--strategy", "main", "--config", str(cfg)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[strategy plan] cannot read")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert err.count(str(missing)) == 1


def test_replay_unreadable_trace_exits_3_without_a_traceback(sample_pdf, tmp_path, capsys):
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    for trace in (tmp_path / "missing.json", malformed):
        rc = main(["replay", sample_pdf, "--trace", str(trace)])
        assert rc == 3
        err = capsys.readouterr().err
        assert err.startswith("[replay] cannot read")
        assert "Traceback" not in err
        assert len(err.splitlines()) == 1
        assert err.count(str(trace)) == 1


def test_replay_missing_document_exits_3_without_a_traceback(tmp_path, capsys):
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text("version: 1\nstrategies:\n  main:\n    steps:\n      - backend: pymupdf\n")
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"orchestration": {"strategy": "main", "decisions": []}}))
    missing = tmp_path / "missing.pdf"
    rc = main(["replay", str(missing), "--trace", str(trace), "--config", str(cfg)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[replay] cannot read")
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert err.count(str(missing)) == 1


def test_explain_unreadable_response_exits_3_without_a_traceback(tmp_path, capsys):
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    for response in (tmp_path / "missing.json", malformed):
        rc = main(["explain", str(response)])
        assert rc == 3
        err = capsys.readouterr().err
        assert err.startswith("[explain] cannot read")
        assert "Traceback" not in err
        assert len(err.splitlines()) == 1
        assert err.count(str(response)) == 1


def test_compare_from_unreadable_response_exits_5_without_duplicate_path(tmp_path, capsys):
    # BL-141: cmd_compare's --from site — never covered by a dedicated test before this item,
    # unlike its seven siblings above. Missing path (real FileNotFoundError, path baked into
    # str(e)) and malformed JSON (JSONDecodeError, path never in str(e)) both land on one tagged
    # exit-5 line with the path named exactly once.
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    for response in (tmp_path / "missing.json", malformed):
        rc = main(["compare", "--from", str(response)])
        assert rc == 5
        err = capsys.readouterr().err
        assert err.startswith("[compare] cannot read")
        assert "Traceback" not in err
        assert len(err.splitlines()) == 1
        assert err.count(str(response)) == 1


def test_compare_fanout_unreadable_input_exits_5_without_duplicate_path(tmp_path, capsys):
    # BL-141: cmd_compare's plain fan-in read site (>=2 response-file positional args, no --from/
    # --backends/--all-ready) — the second, likewise never-before-covered site in the same command.
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"document": {}, "backend": {}, "status": {}}))
    for response in (tmp_path / "missing.json", malformed):
        rc = main(["compare", str(response), str(other)])
        assert rc == 5
        err = capsys.readouterr().err
        assert err.startswith("[compare] cannot read")
        assert "Traceback" not in err
        assert len(err.splitlines()) == 1
        assert err.count(str(response)) == 1


def test_parse_single_document_deadline_flag_threads_seconds_to_named_backend_as_ms(
    sample_pdf, capsys, monkeypatch
):
    # BL-169: --deadline used to only reach native-batch dispatch (BL-135, below). A single
    # explicit file (not a batch — `looks_batch` is false for one file) with --backend NAME now
    # gets the identical seconds-to-ms conversion, proven through the real, unmodified CLI entry
    # point and a real BUILTIN_ADAPTERS-registered fake, spying on api.build_run_context the same
    # way the batch test below does.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.cli import app as cli_app
    from tests.fakes import ScriptedBackend

    seen: dict = {}
    real_build_run_context = cli_app.api.build_run_context

    def spy(req, descriptor, *, broker=None, deadline_ms=None):
        seen["deadline_ms"] = deadline_ms
        return real_build_run_context(req, descriptor, broker=broker, deadline_ms=deadline_ms)

    monkeypatch.setattr(cli_app.api, "build_run_context", spy)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-single", lambda: ScriptedBackend("fake-single"))
    rc = main(["parse", sample_pdf, "--backend", "fake-single", "--deadline", "90"])
    assert rc == 0
    assert seen["deadline_ms"] == 90_000


def test_parse_single_document_deadline_exceeded_exits_3_clean(sample_pdf, capsys, monkeypatch):
    # BL-169 round-1 review (trent, High): the two tests above only prove --deadline's
    # seconds-to-ms conversion reaches build_run_context (plumbing) — neither uses a POLL-mode
    # fake, so neither can exercise a job actually running past its deadline. This is the
    # single-document sibling of test_parse_batch_native_deadline_exceeded_exits_3_clean below,
    # end to end through the real, unmodified CLI entry point: a job that never finishes and
    # never faults, driven past a real (FakeClock) deadline, exiting 3 clean.
    from openreading.cli import app as cli_app
    from openreading.router.clock import FakeClock
    from tests.fakes import PollFake

    stuck = PollFake(polls_needed=10**9, flaky=0)  # never finishes, never faults
    monkeypatch.setattr(cli_app.api, "RealClock", FakeClock)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "stuck-poll-fake", lambda: stuck)
    rc = main(["parse", sample_pdf, "--backend", "stuck-poll-fake", "--deadline", "1"])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.strip() == "[stuck-poll-fake] deadline exceeded"


def test_parse_single_document_without_deadline_flag_passes_none(sample_pdf, capsys, monkeypatch):
    # The common case (no --deadline) must keep resolving to build_run_context's own
    # DEFAULT_DEADLINE_MS fallback — BL-169 must not force every single-document parse to start
    # passing an explicit value where None used to reach build_run_context.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.cli import app as cli_app
    from tests.fakes import ScriptedBackend

    seen: dict = {}
    real_build_run_context = cli_app.api.build_run_context

    def spy(req, descriptor, *, broker=None, deadline_ms=None):
        seen["deadline_ms"] = deadline_ms
        return real_build_run_context(req, descriptor, broker=broker, deadline_ms=deadline_ms)

    monkeypatch.setattr(cli_app.api, "build_run_context", spy)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-single2", lambda: ScriptedBackend("fake-single2"))
    rc = main(["parse", sample_pdf, "--backend", "fake-single2"])
    assert rc == 0
    assert seen["deadline_ms"] is None


def test_parse_batch_deadline_flag_threads_seconds_to_native_dispatch_as_ms(
    sample_pdf, capsys, monkeypatch
):
    # BL-135: --deadline is CLI-friendly SECONDS; api.run_batch's own deadline_ms= parameter (and
    # every internal deadline field it feeds — RunContext.deadline_ms,
    # DEFAULT_NATIVE_BATCH_DEADLINE_MS) is milliseconds. Proven through the real, unmodified CLI
    # entry point and a real BUILTIN_ADAPTERS-registered fake (never a made-up backend name —
    # `--backend` validates against `choices=sorted(BUILTIN_ADAPTERS)` at argparse time), spying on
    # api.build_run_context — the one factory _run_native calls — the same way
    # tests/test_batch_native.py's own BL-135 wiring tests do at the Python-API layer.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.cli import app as cli_app
    from tests.test_batch_native import _FakeNative

    seen: dict = {}
    real_build_run_context = cli_app.api.build_run_context

    def spy(req, descriptor, *, broker=None, deadline_ms=None):
        seen["deadline_ms"] = deadline_ms
        return real_build_run_context(req, descriptor, broker=broker, deadline_ms=deadline_ms)

    monkeypatch.setattr(cli_app.api, "build_run_context", spy)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-native", lambda: _FakeNative(native="claimed"))
    rc = main(["parse", sample_pdf, sample_pdf, "--backend", "fake-native", "--deadline", "90"])
    assert rc == 0
    assert seen["deadline_ms"] == 90_000


# --- backends --check (liveness; internal/design/liveness.md §9) -----------------------------


def _rows_of(out: str) -> dict[str, str]:
    return {
        line.split()[0]: line
        for line in out.splitlines()
        if line and not line.startswith("BACKEND")
    }


def test_backends_default_listing_says_configured_not_ready(capsys):
    """ "READY" promised reachability this listing has never checked. The header word changes with
    the meaning; the command itself stays offline and free."""
    assert main(["backends"]) == 0
    out = capsys.readouterr().out
    assert "CONFIGURED" in out.splitlines()[0]
    assert "READY" not in out.splitlines()[0]


def test_backends_without_check_never_probes(capsys, monkeypatch):
    """A bare `openreading backends` must remain byte-for-byte offline — probing is opt-in only."""
    import openreading.cli.app as app_mod

    def _boom(*a, **kw):  # pragma: no cover - the point is that it is never called
        raise AssertionError("bare `backends` must never call check_liveness")

    monkeypatch.setattr(app_mod, "check_liveness", _boom)
    assert main(["backends"]) == 0


def test_backends_check_probes_only_the_named_backend(capsys, monkeypatch):
    import openreading.cli.app as app_mod

    seen = []

    def _fake(adapter, **kw):
        seen.append(adapter.descriptor.id)
        return LivenessReport(
            backend=adapter.descriptor.id,
            status=LivenessStatus.LIVE,
            measured=True,
            probe=ProbeKind.LOCAL,
            checked_at="2026-08-17T09:41:07Z",
            latency_ms=1.0,
            detail="responding",
        )

    monkeypatch.setattr(app_mod, "check_liveness", _fake)
    assert main(["backends", "--check", "pymupdf"]) == 0
    assert seen == ["pymupdf"]
    out = capsys.readouterr().out
    assert "STATUS" in out and "live" in out and "responding" in out


def test_backends_check_all_probes_only_backends_that_declare_a_probe(capsys, monkeypatch):
    """`all` means "every backend that can actually be measured" — probing the rest would spend the
    user's patience re-printing an inference the plain listing already gave them for free."""
    import openreading.cli.app as app_mod

    seen = []

    def _fake(adapter, **kw):
        seen.append(adapter.descriptor.id)
        return LivenessReport(
            backend=adapter.descriptor.id,
            status=LivenessStatus.LIVE,
            measured=True,
            checked_at="2026-08-17T09:41:07Z",
        )

    monkeypatch.setattr(app_mod, "check_liveness", _fake)
    assert main(["backends", "--check", "all"]) == 0
    assert sorted(seen) == ["anthropic-claude", "docling", "pymupdf", "qwen-vl", "tesseract"]


def test_backends_check_unknown_backend_exits_3_with_a_clean_message(capsys):
    assert main(["backends", "--check", "not-a-backend"]) == 3
    out, err = capsys.readouterr()
    assert out == ""  # no half-printed table on the failure path
    assert "[backends]" in err and "not-a-backend" in err
    assert "Traceback" not in err


def test_backends_check_reports_the_measured_flag_and_latency(capsys, monkeypatch):
    """The measurement/inference split has to survive onto the terminal too, not only the API."""
    import openreading.cli.app as app_mod

    monkeypatch.setattr(
        app_mod,
        "check_liveness",
        lambda adapter, **kw: LivenessReport(
            backend=adapter.descriptor.id,
            status=LivenessStatus.CONFIGURED_UNVERIFIED,
            measured=False,
            checked_at="2026-08-17T09:41:07Z",
            detail="configured, but not verified",
        ),
    )
    assert main(["backends", "--check", "chunkr"]) == 0
    row = _rows_of(capsys.readouterr().out)["chunkr"]
    assert "configured_unverified" in row
    assert " no " in row  # measured=no
    assert " - " in row  # no latency is claimed for an inference
