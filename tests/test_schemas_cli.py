"""The `python -m openreading.schemas validate` command — wired into `make verify`, but the CLI
wrapper itself (schemas/__init__.py:main + _cli_validate, and schemas/__main__.py) was uncovered.
These pin its exit codes and that it validates the vendored schemas + any stored fixtures."""

from __future__ import annotations

import json
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openreading.schemas import main as schemas_main


def test_validate_reports_ok_and_exits_0(capsys):
    import openreading.schemas as schemas

    rc = schemas_main(["validate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "schemas:" in out and "OK" in out
    assert "fixtures:" in out  # the fixture sweep ran (0 or more checked, 0 invalid)
    for path in Path(schemas.__file__).parent.glob("*.json"):
        assert f"{path.name} OK" in out


@pytest.mark.parametrize("name", ["request.v0.1.json", "new-family.v0.1.json"])
def test_validate_rejects_invalid_historical_and_new_schema_metadata(
    tmp_path, monkeypatch, capsys, name
):
    import openreading.schemas as schemas

    root = tmp_path / "schemas"
    shutil.copytree(Path(schemas.__file__).parent, root)
    path = root / name
    value = json.loads(path.read_text()) if path.exists() else {"type": "object"}
    value["title"] = 42
    path.write_text(json.dumps(value))
    monkeypatch.setattr(schemas, "resources", SimpleNamespace(files=lambda package: root))
    assert schemas_main(["validate"]) == 1
    assert name in capsys.readouterr().err


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
