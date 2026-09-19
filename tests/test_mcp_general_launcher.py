"""General startup keeps execution authority and credentials separate from local planning."""

import argparse
import json
import os
import select
import signal
import subprocess
import sys
from types import ModuleType

import pytest

from openreading.artifacts.limits import ArtifactError
from openreading.mcp_server import main as launcher


def arguments(tmp_path, profile="general-execution-v1"):
    return [
        "--profile",
        profile,
        "--input-root",
        str(tmp_path / "input"),
        "--artifact-root",
        str(tmp_path / "store"),
    ]


@pytest.fixture
def captured_server(monkeypatch):
    captured = {}

    async def serve(config, **kwargs):
        captured.update(config=config, **kwargs)

    module = ModuleType("openreading.mcp_server.general")
    module.serve = serve
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return captured


def test_general_selects_uncapped_profile_without_local_engine(
    tmp_path, monkeypatch, captured_server
):
    from openreading.artifacts import service

    def forbidden(*args, **kwargs):
        raise AssertionError("General startup constructed a local parser")

    monkeypatch.setattr(service, "ArtifactService", forbidden)
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "missing-secret.yaml"))
    monkeypatch.setenv("REDUCTO_API_KEY", "ambient-secret")
    assert launcher.main(arguments(tmp_path)) == 0
    config = captured_server["config"]
    assert config.docling is None
    for field in ("source_bytes", "pages", "extraction_bytes", "store_bytes", "deadline_seconds"):
        assert getattr(config.limits, field) is None
    assert config.limits.document_bytes == 65536
    assert captured_server["authority"].allowed_backends == frozenset()
    assert captured_server["environment"] == {}
    assert captured_server["deadline_seconds"] is None
    assert captured_server["concurrency"] == 1


def test_general_forwards_explicit_authority_and_only_selected_environment(
    tmp_path, monkeypatch, captured_server, capsys
):
    path = tmp_path / "operator.yaml"
    path.write_text("version: 1\npolicy:\n  backends: [reducto, pymupdf]\n")
    monkeypatch.setenv("REDUCTO_API_KEY", "chosen-secret")
    monkeypatch.setenv("UNSELECTED_KEY", "ambient-secret")
    assert (
        launcher.main(
            arguments(tmp_path)
            + [
                "--execution-config",
                str(path),
                "--execute-backend",
                "pymupdf",
                "--execute-backend",
                "reducto",
                "--execute-strategy",
                "fast",
                "--execution-env",
                "REDUCTO_API_KEY",
                "--execution-deadline-seconds",
                "90.5",
                "--execution-concurrency",
                "2",
                "--document-response-bytes",
                "4096",
                "--document-export-root",
                str(tmp_path / "exports"),
            ]
        )
        == 0
    )
    authority = captured_server["authority"]
    assert authority.allowed_backends == frozenset({"pymupdf", "reducto"})
    assert authority.allowed_strategies == frozenset({"fast"})
    path.unlink()
    assert authority.loaded.config.policy.backends == ["reducto", "pymupdf"]
    assert captured_server["environment"] == {"REDUCTO_API_KEY": "chosen-secret"}
    assert captured_server["deadline_seconds"] == 90.5
    assert captured_server["concurrency"] == 2
    assert captured_server["document_response_bytes"] == 4096
    assert captured_server["document_export_root"] == tmp_path / "exports"
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.parametrize(
    "extra",
    [
        ["--routing-config", "/secret.yaml"],
        ["--allow-backend", "pymupdf"],
        ["--profile-config", "/secret.json"],
        ["--execute-backend", "unknown-secret"],
        ["--execute-strategy", "unknown-secret"],
        ["--execution-config", "/missing-secret.yaml"],
        ["--execution-env", "MISSING_SECRET"],
        ["--execution-env", ""],
        ["--execution-env", "secret=value"],
        ["--execution-env", "secret-name"],
        ["--execution-env", "9secret"],
        ["--execution-env", "secret\0name"],
        ["--execution-deadline-seconds", "0"],
        ["--execution-deadline-seconds", "-1"],
        ["--execution-deadline-seconds", "nan"],
        ["--execution-deadline-seconds", "inf"],
        ["--execution-concurrency", "0"],
        ["--execution-concurrency", "-1"],
    ],
)
def test_general_rejects_invalid_setup_without_echoing_values(
    tmp_path, extra, captured_server, capsys, monkeypatch
):
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    assert launcher.main(arguments(tmp_path) + extra) == 2
    assert not captured_server
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    assert "secret" not in output.err.lower()


@pytest.mark.parametrize(
    "extra",
    [
        ["--execution-config", "/secret.yaml"],
        ["--execute-backend", "pymupdf"],
        ["--execute-strategy", "fast"],
        ["--execution-env", "KEY"],
        ["--execution-deadline-seconds", "45"],
        ["--execution-concurrency", "1"],
    ],
)
@pytest.mark.parametrize("profile", ["local-document-proof-v1", "local-document-proof-v2"])
def test_local_profiles_refuse_execution_flags(tmp_path, extra, profile, monkeypatch, capsys):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Local startup ignored an execution option")

    monkeypatch.setattr(launcher, "serve", forbidden)
    assert launcher.main(arguments(tmp_path, profile) + extra) == 2
    assert "secret" not in capsys.readouterr().err.lower()


def test_direct_general_profile_config_refuses_docling_configuration(tmp_path):
    args = argparse.Namespace(
        profile="general-execution-v1",
        input_root=tmp_path / "input",
        artifact_root=tmp_path / "store",
        profile_config=None,
    )
    config = launcher.profile_config(args)
    assert config.docling is None and config.limits.pages is None
    args.profile_config = tmp_path / "secret.json"
    with pytest.raises(ArtifactError, match="configuration_required"):
        launcher.profile_config(args)


def test_direct_unknown_profile_is_refused(tmp_path):
    args = argparse.Namespace(
        profile="unknown",
        input_root=tmp_path / "input",
        artifact_root=tmp_path / "store",
        profile_config=None,
    )
    with pytest.raises(ArtifactError, match="configuration_required"):
        launcher.profile_config(args)


def test_local_default_still_uses_local_server(tmp_path, monkeypatch):
    captured = []

    async def serve(config):
        captured.append(config)

    monkeypatch.setattr(launcher, "serve", serve)
    assert launcher.main(arguments(tmp_path, "local-document-proof-v1")) == 0
    assert captured[0].limits.pages == 100


@pytest.mark.parametrize(
    "name", ["HOME", "TMPDIR", "XDG_CACHE_HOME", "OPENREADING_CONFIG", "OPENREADING_LEDGER"]
)
def test_real_general_startup_refuses_reserved_worker_environment(
    tmp_path, monkeypatch, capsys, name
):
    (tmp_path / "input").mkdir()
    monkeypatch.setenv(name, "private-secret-value")
    assert launcher.main(arguments(tmp_path) + ["--execution-env", name]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Invalid MCP execution configuration.\n"
    assert not (tmp_path / "store" / "execution-jobs").exists()


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_general_real_stdio_needs_no_local_parser_and_stops_with_open_input(tmp_path, stop):
    (tmp_path / "input").mkdir()
    code = """
import builtins
import sys
from openreading.artifacts import service
from openreading.mcp_server.main import main

def forbidden(*args, **kwargs):
    raise AssertionError("General startup selected a local parser")

service.ArtifactService = service.engine_identity = forbidden
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in {"fitz", "pymupdf", "docling", "docling_core"}:
        forbidden()
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
sys.exit(main(sys.argv[1:]))
"""
    with subprocess.Popen(
        [sys.executable, "-c", code, *arguments(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:

            def send(value):
                process.stdin.write(json.dumps(value).encode() + b"\n")
                process.stdin.flush()

            def receive():
                assert select.select([process.stdout], [], [], 10)[0], "MCP reply timed out"
                line = process.stdout.readline()
                assert line, "MCP process exited before replying"
                return json.loads(line)

            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            )
            assert receive()["id"] == 1
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = receive()["result"]["tools"]
            assert {tool["name"] for tool in tools} == {
                "openreading_strategy",
                "openreading_backends",
                "openreading_readiness",
                "openreading_liveness",
                "openreading_parse",
                "openreading_batch",
                "openreading_resume",
                "openreading_get_job",
                "openreading_list_jobs",
                "openreading_cancel_job",
                "openreading_get_result",
                "openreading_compare",
                "openreading_route",
            }
            os.kill(process.pid, stop)
            assert process.wait(timeout=5) == 130
            assert b"Traceback" not in process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
