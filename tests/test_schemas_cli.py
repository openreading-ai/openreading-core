"""The `python -m openreading.schemas validate` command — wired into `make verify`, but the CLI
wrapper itself (schemas/__init__.py:main + _cli_validate, and schemas/__main__.py) was uncovered.
These pin its exit codes and that it validates the vendored schemas + any stored fixtures."""

from __future__ import annotations

import runpy
import sys

import pytest

from openreading.schemas import main as schemas_main


def test_validate_reports_ok_and_exits_0(capsys):
    # repo is green under `make verify` (which runs this), so all four schemas + every stored
    # fixture validate → exit 0 with an OK line naming each schema file.
    rc = schemas_main(["validate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "schemas:" in out and "OK" in out
    assert "fixtures:" in out  # the fixture sweep ran (0 or more checked, 0 invalid)


def test_no_subcommand_prints_usage_and_exits_2(capsys):
    rc = schemas_main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_unknown_subcommand_prints_usage_and_exits_2(capsys):
    rc = schemas_main(["frobnicate"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_module_entrypoint_runs_and_exits_cleanly(capsys, monkeypatch):
    # `python -m openreading.schemas validate` — covers schemas/__main__.py's SystemExit guard.
    monkeypatch.setattr(sys, "argv", ["openreading.schemas", "validate"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("openreading.schemas", run_name="__main__", alter_sys=True)
    assert exc.value.code == 0
    assert "schemas:" in capsys.readouterr().out
