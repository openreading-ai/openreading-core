"""The `python -m openreading.schemas validate` command — wired into `make verify`, but the CLI
wrapper itself (schemas/__init__.py:main + _cli_validate, and schemas/__main__.py) was uncovered.
These pin its exit codes and that it validates the vendored schemas + any stored fixtures."""

from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openreading import schemas
from openreading.schemas import main as schemas_main


def test_validate_reports_ok_and_exits_0(capsys):
    rc = schemas_main(["validate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "schemas:" in out and "OK" in out
    assert "fixtures:" in out  # the fixture sweep ran (0 or more checked, 0 invalid)
    for path in Path(schemas.__file__).parent.glob("*.json"):
        assert f"{path.name} OK" in out


@pytest.mark.parametrize(
    "filename",
    [
        "backend-discovery.v0.1.json",
        "route-tool.v0.1.json",
        "response.v0.1.json",
        "future-contract.v0.1.json",
    ],
)
def test_validate_rejects_invalid_vendored_metadata(tmp_path, filename):
    """New and historical files must fail the CLI gate without a hand-maintained list."""
    copied = tmp_path / "schemas"
    shutil.copytree(Path(schemas.__file__).parent, copied)
    target = copied / filename
    doc = json.loads(target.read_text()) if target.exists() else {"type": "object"}
    doc["title"] = 42
    target.write_text(json.dumps(doc))
    # A separate interpreter avoids letting cached validators mask the corrupt resource.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; from openreading import schemas; "
            "original_files = schemas.resources.files; "
            "schemas.resources.files = lambda package: "
            "Path(sys.argv[1]) if package == 'openreading.schemas' else original_files(package); "
            "sys.exit(schemas.main(['validate']))",
            str(copied),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SchemaError" in result.stderr
    assert "42 is not of type 'string'" in result.stderr


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
