"""Require explicit MCP dispositions when public processing surfaces gain or lose operations.

The mapping binds source symbols to rows in the public capability table.
It catches unaccounted operations and removed rows, not semantic accuracy of prose.
For example, a new HTTP route needs a disposition even when it remains operator-only.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

import openreading
from openreading.mcp_server.general import INPUTS

ROOT = Path(__file__).resolve().parents[1]
# Row prefixes keep each source symbol attached to its public disposition, including exclusions.
COVERAGE = {
    "api": {
        "run": "Python `run`,",
        "route": "Python `route`,",
        "run_batch": "Python `run_batch`,",
        "resume": "Python `resume`,",
        "compare": "Python `compare`,",
    },
    "cli": {
        "parse": "Python `run`,",
        "route": "Python `route`,",
        "resume": "Python `resume`,",
        "compare": "Python `compare`,",
        "backends": "Backend discovery and diagnostics",
        "strategy_list": "Strategy list,",
        "strategy_show": "Strategy list,",
        "strategy_normalize": "Strategy list,",
        "strategy_validate": "Strategy list,",
        "strategy_plan": "Strategy list,",
        "explain": "Complete output,",
        "replay": "CLI `replay`",
        "calibrate": "Calibration, benchmark,",
        "leaderboard": "Calibration, benchmark,",
        "benchmark_list": "Calibration, benchmark,",
        "benchmark_show": "Calibration, benchmark,",
        "benchmark_estimate": "Calibration, benchmark,",
        "benchmark_prepare": "Calibration, benchmark,",
        "benchmark_run": "Calibration, benchmark,",
        "benchmark_report": "Calibration, benchmark,",
        "rules": "Calibration, benchmark,",
        "serve": "Config authoring,",
        "mcp": "Config authoring,",
    },
    "http": {
        "/healthz": "Config authoring,",
        "/v1/backends": "Backend discovery and diagnostics",
        "/v1/backends/{backend_id}/liveness": "Backend discovery and diagnostics",
        "/v1/parse": "Python `run`,",
        "/v1/route": "Python `route`,",
        "/v1/compare": "Python `compare`,",
        "/v1/batch": "Python `run_batch`,",
        "/v1/jobs": "Job status,",
        "/v1/jobs/{job_id}": "Job status,",
        "/v1/webhooks/{backend_id}": "Config authoring,",
    },
}


def _source_operations(surface):
    if surface == "api":
        return {
            name for name in openreading.__all__ if inspect.isfunction(getattr(openreading, name))
        }
    module = "cli" if surface == "cli" else "server"
    tree = ast.parse((ROOT / f"src/openreading/{module}/app.py").read_text())
    if surface == "cli":
        return {
            node.name.removeprefix("cmd_")
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("cmd_")
        }
    return {
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "app"
        and node.func.attr in {"get", "post", "delete", "put", "patch", "head", "options"}
    }


def _rows():
    guide = (ROOT / "src/openreading/mcp_server/README.md").read_text()
    section = guide.split("### General MCP capability coverage\n", 1)[1].split("\n## ", 1)[0]
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ")
    ][2:]


@pytest.mark.parametrize("surface", ["api", "cli", "http"])
def test_every_public_operation_has_a_documented_mcp_disposition(surface):
    assert _source_operations(surface) == set(COVERAGE[surface])
    rows = _rows()
    for symbol, prefix in COVERAGE[surface].items():
        matches = [row for row in rows if row[0].startswith(prefix)]
        assert len(matches) == 1, (surface, symbol, prefix)
        assert len(matches[0]) == 3 and all(matches[0]), (surface, symbol)


def test_capability_dispositions_reference_the_complete_general_catalog():
    names = {name for row in _rows() for name in re.findall(r"\bopenreading_[a-z_]+\b", row[1])}
    assert names == set(INPUTS)
