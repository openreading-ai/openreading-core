"""Milestone 11.6 — surface wiring: the `strategy:` prefix through api/CLI/server, the response
schema bump, and the byte-identical no-config equivalence golden (harness H4b).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from openreading import api, schemas
from openreading.cli.app import main
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import UnknownStrategyError


@pytest.fixture
def _clean_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.pdf").write_bytes(build_sample_pdf())
    return tmp_path


# ---- H4b: byte-identical no-config equivalence -----------------------------------------------


def test_no_config_path_is_byte_identical(_clean_cwd):
    """The legacy named-backend path must produce identical output whether or not an
    openreading.yaml is present (the strategy layer never touches a named-backend request)."""
    doc = str(_clean_cwd / "s.pdf")
    without = api.run(doc, backend="pymupdf")

    (_clean_cwd / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  cheap: [pymupdf, tesseract]\n"
    )
    with_config = api.run(doc, backend="pymupdf")

    assert json.dumps(without, sort_keys=True) == json.dumps(with_config, sort_keys=True)
    assert "orchestration" not in without  # legacy path never adds the block


def test_named_backend_run_never_imports_the_strategy_package(tmp_path):
    """Guardrail T10 (§5.2): no file / no strategy ⇒ the strategy module is not imported. Checked in
    a fresh subprocess (this process' sys.modules is polluted by the strategy test suite)."""
    doc = tmp_path / "s.pdf"
    doc.write_bytes(build_sample_pdf())
    script = textwrap.dedent(f"""
        import sys, contextlib, io
        import openreading.api as api
        assert not any(m.startswith("openreading.strategies") for m in sys.modules), "imported on `import api`"
        with contextlib.redirect_stdout(io.StringIO()):  # swallow the pymupdf advisory
            api.run({str(doc)!r}, backend="pymupdf")
        leaked = [m for m in sys.modules if m.startswith("openreading.strategies")]
        assert not leaked, f"a named-backend run imported: {{leaked}}"
        print("OK")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr


def test_a_policy_only_file_gates_a_named_backend_without_importing_the_engine(tmp_path):
    """Guardrail T10, second case (law P6). The file's `policy:` block now gates a named-backend
    run, and reading it must still leave the strategy package out of `sys.modules`. Before the
    file was read on every path the refusal below never happened: `parse --backend reducto` ran
    under a `require_local` file as though no file existed."""
    doc = tmp_path / "s.pdf"
    doc.write_bytes(build_sample_pdf())
    (tmp_path / "openreading.yaml").write_text("version: 1\npolicy: {require_baa: true}\n")
    script = textwrap.dedent(f"""
        import sys, os, contextlib, io
        os.chdir({str(tmp_path)!r})
        import openreading.api as api
        from openreading.types.errors import ComplianceRefused
        with contextlib.redirect_stdout(io.StringIO()):  # swallow the pymupdf advisory
            api.run({str(doc)!r}, backend="pymupdf")     # local: the policy admits it
        open("openreading.yaml", "w").write("version: 1\\npolicy: {{require_local: true}}\\n")
        try:
            api.run({str(doc)!r}, backend="reducto")
        except ComplianceRefused as e:
            assert "require_local" in str(e), e
        else:
            raise AssertionError("a require_local file did not gate a named hosted backend")
        leaked = [m for m in sys.modules if m.startswith("openreading.strategies")]
        assert not leaked, f"reading the policy block imported: {{leaked}}"
        print("OK")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr


def test_auto_without_defaults_strategy_unchanged(_clean_cwd):
    doc = str(_clean_cwd / "s.pdf")
    without = api.run(doc, backend="auto")
    (_clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    with_config = api.run(doc, backend="auto")  # no defaults.strategy → auto behaves the same
    assert json.dumps(without, sort_keys=True) == json.dumps(with_config, sort_keys=True)


# ---- strategy engagement via the api ----------------------------------------------------------


def test_strategy_run_embeds_orchestration_and_is_schema_valid(_clean_cwd):
    (_clean_cwd / "openreading.yaml").write_text(
        "version: 1\nstrategies:\n  cheap: [pymupdf, tesseract]\n"
    )
    out = api.run(str(_clean_cwd / "s.pdf"), strategy="cheap")
    assert out["backend"]["id"] == "pymupdf"
    assert out["orchestration"]["outcome"] == "ok"
    schemas.validate_response(out)  # v0.2 schema accepts the orchestration block


def test_defaults_strategy_engages_on_auto(_clean_cwd):
    (_clean_cwd / "openreading.yaml").write_text(
        "version: 1\ndefaults: { strategy: cheap }\nstrategies:\n  cheap: [pymupdf]\n"
    )
    out = api.run(str(_clean_cwd / "s.pdf"), backend="auto")
    assert "orchestration" in out and out["orchestration"]["strategy"] == "cheap"


def test_strategy_none_forces_legacy_auto(_clean_cwd):
    (_clean_cwd / "openreading.yaml").write_text(
        "version: 1\ndefaults: { strategy: cheap }\nstrategies:\n  cheap: [pymupdf]\n"
    )
    out = api.run(str(_clean_cwd / "s.pdf"), backend="strategy:none")
    assert "orchestration" not in out  # escape hatch: plain router, defaults.strategy ignored


def test_unknown_strategy_raises(_clean_cwd):
    (_clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    with pytest.raises(UnknownStrategyError):
        api.run(str(_clean_cwd / "s.pdf"), strategy="ghost")


def test_preset_strategy_runs_without_config(_clean_cwd):
    """The four built-in presets are part of the library even with no openreading.yaml loaded —
    the Run picker, the Strategies page, and `strategy list` all offer them configless, so the
    execution path must accept them too (spec §5: "no config found — presets still work")."""
    out = api.run(str(_clean_cwd / "s.pdf"), strategy="cost_saver")
    assert out["orchestration"]["strategy"] == "cost_saver"
    assert out["orchestration"]["outcome"] == "ok"
    schemas.validate_response(out)


def test_unknown_strategy_without_config_names_the_presets(_clean_cwd):
    """With no config file the presets ARE the defined set — the error must list them rather than
    claim nothing is loaded."""
    with pytest.raises(UnknownStrategyError) as exc:
        api.run(str(_clean_cwd / "s.pdf"), strategy="ghost")
    assert "cost_saver" in str(exc.value)


def test_build_request_does_not_make_adapter_on_strategy_prefix():
    # T1: a strategy: id must not be passed to make_adapter (KeyError). build_request just holds it.
    req = api.build_request(b"%PDF-1.4 minimal", "strategy:cheap", mime_type="application/pdf")
    assert req.backend.id == "strategy:cheap" and req.backend.type is None


# ---- CLI surface ------------------------------------------------------------------------------


def test_cli_parse_strategy(_clean_cwd, capsys):
    (_clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    rc = main(["parse", str(_clean_cwd / "s.pdf"), "--strategy", "cheap"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(out)
    assert doc["orchestration"]["strategy"] == "cheap"


def test_cli_parse_unknown_strategy_exit2(_clean_cwd, capsys):
    (_clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    rc = main(["parse", str(_clean_cwd / "s.pdf"), "--strategy", "ghost"])
    assert rc == 2
    assert "unknown strategy" in capsys.readouterr().err


def test_cli_parse_requires_exactly_one_target(_clean_cwd, capsys):
    rc = main(["parse", str(_clean_cwd / "s.pdf"), "--backend", "pymupdf", "--strategy", "cheap"])
    assert rc == 2
    assert "exactly one" in capsys.readouterr().err


def test_cli_parse_backend_unchanged(_clean_cwd, capsys):
    rc = main(["parse", str(_clean_cwd / "s.pdf"), "--backend", "pymupdf"])
    assert rc == 0
    assert "orchestration" not in json.loads(capsys.readouterr().out)


def test_cli_strategy_plan(_clean_cwd, capsys):
    cfg = _clean_cwd / "openreading.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf, reducto]\n")
    rc = main(
        ["strategy", "plan", str(_clean_cwd / "s.pdf"), "--strategy", "cheap", "--config", str(cfg)]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["strategy"] == "cheap" and "tree" in out and out["config_hash"].startswith("sha256:")


def test_cli_strategy_plan_malformed_policy_block_exits_3_without_a_traceback(_clean_cwd, capsys):
    # A `policy:` block that is not a policy fails the same way a grammar error does: one tagged
    # stderr line, exit 3. main() returning at all is what pins "no traceback".
    cfg = _clean_cwd / "openreading.yaml"
    cfg.write_text("version: 1\npolicy: {require_locall: true}\nstrategies:\n  cheap: [pymupdf]\n")
    rc = main(
        [
            "strategy",
            "plan",
            str(_clean_cwd / "s.pdf"),
            "--strategy",
            "cheap",
            "--config",
            str(cfg),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[strategy plan] ")
    assert "did you mean 'require_local'" in err
    assert len(err.splitlines()) == 1


def test_cli_explain(_clean_cwd, capsys, tmp_path):
    (_clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    # produce a real response with an orchestration block, save it, explain it
    out = api.run(str(_clean_cwd / "s.pdf"), strategy="cheap")
    resp_file = _clean_cwd / "resp.json"
    resp_file.write_text(json.dumps(out))
    rc = main(["explain", str(resp_file)])
    text = capsys.readouterr().out
    assert rc == 0
    assert "strategy cheap" in text and "pymupdf" in text
