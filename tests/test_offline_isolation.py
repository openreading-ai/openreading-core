"""Exercise offline verification with synthetic operator configuration outside the checkout.

These subprocesses catch cwd discovery and dotenv leakage without opening a developer's files.
The selected tests still call the real CLI, router, ledger, and HTTP server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CONFIG = "version: 1\npolicy:\n  backends: [tesseract]\n"
_CASES = (
    "test_characterization.py::test_surface_matches_its_pin[route]",
    "test_characterization.py::test_resume_matches_its_pin",
    "test_cli_replay_calibrate.py::test_replay_roundtrip_is_deterministic_and_schema_valid",
    "test_cli_replay_calibrate.py::test_replay_without_config_exits_3",
    "test_explicit_backends.py::test_no_config_falls_back_to_pymupdf",
    "test_ledger_replay.py::test_resume_refuses_by_name_when_the_strategy_config_changed_since_the_original_run",
    "test_server.py::test_scoped_parse_catches_routing_refusals",
    "test_config.py",
)


@pytest.mark.parametrize("source", ["cwd", "environment", "dotenv"])
def test_offline_surfaces_ignore_operator_configuration(tmp_path, source):
    """Personal routing must not change verification, even when a CLI loads dotenv first."""
    caller = tmp_path / "caller"
    caller.mkdir()
    config = caller / ("openreading.yaml" if source == "cwd" else "operator.yaml")
    config.write_text(_CONFIG, encoding="utf-8")
    env = os.environ.copy()
    env.pop("OPENREADING_CONFIG", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("OPENREADING_REPIN", None)
    if source == "environment":
        env["OPENREADING_CONFIG"] = str(config)
    elif source == "dotenv":
        (caller / ".env").write_text(f"OPENREADING_CONFIG={config}\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in caller.iterdir()}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not live",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            *(str(ROOT / "tests" / case) for case in _CASES),
        ],
        cwd=caller,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {p.name: p.read_bytes() for p in caller.iterdir()} == before


@pytest.mark.parametrize("lane", ["not live", "live"])
def test_workspace_restores_test_environment_and_preserves_live_setup(tmp_path, lane):
    """Exercise the real fixture without contacting a provider or loading personal credentials."""
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(ROOT / "tests" / "conftest.py", suite / "conftest.py")
    (suite / "pytest.ini").write_text(
        "[pytest]\nmarkers = live: synthetic live-lane setup, without provider calls\n",
        encoding="utf-8",
    )
    (suite / ".env").write_text("OPENREADING_ISOLATION_LIVE=synthetic\n", encoding="utf-8")
    (suite / "test_probe.py").write_text(
        textwrap.dedent(
            """\
            import os
            from pathlib import Path
            import pytest
            from openreading import config
            from openreading.credentials import load_dotenv

            def test_configuration_created_by_the_test_is_still_loaded(monkeypatch):
                assert os.environ.get("OPENREADING_ISOLATION_LIVE") is None
                Path("openreading.yaml").write_text("version: 1\\npolicy:\\n  backends: [tesseract]\\n")
                assert config.load().policy == {"backends": ["tesseract"]}
                Path(".env").write_text("OPENREADING_ISOLATION_LEAK=synthetic\\n")
                assert load_dotenv() == 1
                assert os.environ["OPENREADING_ISOLATION_LEAK"] == "synthetic"
                # Teardown must also undo monkeypatch values captured after dotenv loading.
                monkeypatch.setenv("OPENREADING_ISOLATION_LEAK", "changed")

            def test_previous_test_environment_does_not_leak():
                assert os.environ.get("OPENREADING_ISOLATION_LEAK") is None
                assert os.environ.get("OPENREADING_ISOLATION_LIVE") is None
                assert config.load() is None

            @pytest.mark.live
            def test_live_lane_keeps_the_configured_directory_and_environment():
                assert Path.cwd() == Path(__file__).parent
                assert os.environ["OPENREADING_ISOLATION_LIVE"] == "synthetic"
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for key in ("PYTEST_ADDOPTS", "OPENREADING_ISOLATION_LEAK", "OPENREADING_ISOLATION_LIVE"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", lane, "-p", "no:cacheprovider", "--tb=short"],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("2 passed" if lane == "not live" else "1 passed") in result.stdout
