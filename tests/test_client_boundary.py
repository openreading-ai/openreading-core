"""Client retention and MCP imports must work without loading the parsing engine."""

import subprocess
import sys


def test_client_imports_without_engine():
    script = """
import importlib.abc
import sys
class NoEngine(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = (
            "openreading.adapters", "openreading.api", "openreading.router",
            "openreading.server", "openreading.strategies", "openreading.derive",
            "openreading.artifacts.worker", "openreading.artifacts.service",
            "pypdf", "puremagic", "yaml",
        )
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise AssertionError("Client loaded engine module: " + fullname)
sys.meta_path.insert(0, NoEngine())
from openreading.artifacts.retained import RetainedService
from openreading.artifacts.retention import retain_response
from openreading.artifacts.jobs import ImportExecution
from openreading.mcp_server.session import serve
from openreading.mcp_server.tools import create_server
from openreading.types.response import NormalizedResponse
assert RetainedService and retain_response and ImportExecution and serve and create_server
assert NormalizedResponse
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_client_build_contains_only_canonical_client_modules(tmp_path):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = root / "packages/agent-client/profile.py"
    spec = importlib.util.spec_from_file_location("client_profile", path)
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    target = tmp_path / "openreading"
    profile.stage_package(target)
    assert not (target / "adapters").exists()
    assert not (target / "server").exists()
    assert not (target / "api.py").exists()
    for module in profile.MODULES:
        relative = module.replace(".", "/") + ".py"
        assert (target / relative).read_bytes() == (
            root / "src/openreading" / relative
        ).read_bytes()
    assert set(p.name for p in (target / "schemas").glob("*.json")) == set(profile.SCHEMAS)
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import openreading
assert not hasattr(openreading, "run")
from openreading.artifacts.retained import RetainedService
from openreading.artifacts.retention import retain_response
from openreading.artifacts.jobs import ImportExecution
from openreading.mcp_server.session import serve
from openreading.mcp_server.tools import create_server
from openreading.schemas import response_schema
assert response_schema()["properties"]["schema_version"]["const"] == "0.3"
assert not any(n.startswith("openreading.adapters") for n in sys.modules)
from pathlib import Path
from openreading.artifacts.jobs import run, main
job = Path(sys.argv[1]) / "job"
job.mkdir()
(job / "request.json").write_text("{}")
try:
    run(job)
except ValueError as error:
    assert str(error) == "Local jobs require the parser-enabled Core package"
else:
    raise AssertionError("Client-only package accepted local parser dispatch")
assert main([str(job)]) == 2
assert not any(n.startswith("openreading.adapters") for n in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_lazy_public_exports_keep_engine_api_compatible():
    import openreading
    import openreading.types as types

    assert openreading.run is __import__("openreading.api", fromlist=["run"]).run
    assert "run" in dir(openreading)
    assert "NormalizedResponse" in dir(types)
    assert (
        types.NormalizedResponse
        is __import__(
            "openreading.types.response", fromlist=["NormalizedResponse"]
        ).NormalizedResponse
    )
    for module in (openreading, types):
        try:
            _ = module.not_a_public_symbol
        except AttributeError:
            pass
        else:
            raise AssertionError("Unknown exports must raise AttributeError")
