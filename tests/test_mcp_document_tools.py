"""The real stdio protocol exposes bounded evidence without duplicate content or secrets."""

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from tests.test_artifact_service import pdf


@pytest.mark.asyncio
async def test_stdio_import_search_read_and_sanitized_invalid_arguments(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    pdf(root / "agreement.pdf")
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openreading.mcp_server.main",
            "--profile",
            "local-document-proof-v1",
            "--input-root",
            str(root),
            "--artifact-root",
            str(tmp_path.resolve() / "store"),
        ],
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "openreading_import",
            "openreading_search",
            "openreading_read",
        }
        assert all(tool.outputSchema is None for tool in tools)
        receipt_result = await session.call_tool("openreading_import", {"path": "agreement.pdf"})
        assert not receipt_result.isError
        assert receipt_result.structuredContent is None
        assert len(receipt_result.content) == 1
        receipt = json.loads(receipt_result.content[0].text)
        found = await session.call_tool(
            "openreading_search", {"artifact_id": receipt["artifact_id"], "query": "renewal"}
        )
        hit = json.loads(found.content[0].text)["hits"][0]
        read = await session.call_tool(
            "openreading_read",
            {"artifact_id": receipt["artifact_id"], "evidence_ids": [hit["evidence_id"]]},
        )
        assert "60 days" in json.loads(read.content[0].text)["passages"][0]["text"]
        denied = await session.call_tool("openreading_import", {"path": "../secret.pdf"})
        assert denied.isError
        assert "secret.pdf" not in denied.content[0].text
        with pytest.raises(McpError) as error:
            await session.call_tool(
                "openreading_import", {"path": "agreement.pdf", "password": "private-secret"}
            )
        assert "private-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_inprocess_protocol_checks_unknown_tool_bounds_and_error_redaction(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.tools import create_server

    root = tmp_path.resolve() / "input"
    root.mkdir()
    pdf(root / "sample.pdf")
    service = ArtifactService(ProfileConfig(root, tmp_path.resolve() / "store"))
    async with create_connected_server_and_client_session(create_server(service)) as session:
        assert len((await session.list_tools()).tools) == 3
        receipt = await session.call_tool("openreading_import", {"path": "sample.pdf"})
        identifier = json.loads(receipt.content[0].text)["artifact_id"]
        for name, args in [
            ("openreading_search", {"artifact_id": identifier, "query": "notice"}),
            (
                "openreading_read",
                {"artifact_id": identifier, "evidence_ids": ["p0001-b0000-s0000"]},
            ),
        ]:
            result = await session.call_tool(name, args)
            assert not result.isError
            assert result.structuredContent is None
        denied = await session.call_tool("openreading_import", {"path": "../secret.pdf"})
        assert denied.isError
        assert json.loads(denied.content[0].text)["error"]["code"] == "access_denied"
        for name, args in [
            ("missing", {}),
            ("openreading_search", {"artifact_id": identifier, "query": "secret" * 200}),
        ]:
            with pytest.raises(McpError) as error:
                await session.call_tool(name, args)
            assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_cancellation_waits_for_import_cleanup(tmp_path, monkeypatch):
    import asyncio
    import subprocess

    import anyio

    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.tools import _import

    root = tmp_path.resolve() / "input"
    root.mkdir()
    pdf(root / "sample.pdf")
    service = ArtifactService(ProfileConfig(root, tmp_path.resolve() / "store"))
    original = subprocess.Popen
    children = []

    def slow_child(command, **kwargs):
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", slow_child)
    task = asyncio.create_task(_import(service, "sample.pdf"))
    while not children:
        await anyio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].poll() is not None
    assert list((service.config.artifact_root / "staging").iterdir()) == []
    with service.store.import_lock():
        pass


def test_cli_configuration_failure_has_no_traceback(tmp_path, capsys):
    from openreading.cli.app import main

    assert (
        main(
            [
                "mcp",
                "--profile",
                "local-document-proof-v1",
                "--input-root",
                str(tmp_path / "missing"),
                "--artifact-root",
                str(tmp_path / "store"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_pre_cancelled_import_does_not_wait_for_a_thread_that_never_started(tmp_path):
    import subprocess

    root = tmp_path.resolve() / "input"
    root.mkdir()
    pdf(root / "sample.pdf")
    script = """
import anyio
import sys
from pathlib import Path
from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.mcp_server.tools import _import
async def check():
    service = ArtifactService(ProfileConfig(Path(sys.argv[1]), Path(sys.argv[2])))
    with anyio.CancelScope() as scope:
        scope.cancel()
        await _import(service, "sample.pdf")
    print("closed")
anyio.run(check)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(tmp_path.resolve() / "store")],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "closed"


def test_launch_canonicalizes_explicit_roots_and_preserves_domain_errors(
    tmp_path, monkeypatch, capsys
):
    import argparse

    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server import main as launcher

    root = tmp_path.resolve() / "input"
    root.mkdir()
    link = root.parent / "linked"
    link.symlink_to(root, target_is_directory=True)
    seen = []

    async def failing(config):
        seen.append(config)
        raise ArtifactError("engine_identity_unavailable")

    monkeypatch.setattr(launcher, "serve", failing)
    assert (
        launcher.launch(argparse.Namespace(input_root=link, artifact_root=root.parent / "store"))
        == 2
    )
    assert seen[0].input_root == root
    assert "Installed engine identity" in capsys.readouterr().err


def test_unsupported_platform_is_not_reported_as_missing_dependencies(monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from openreading.mcp_server import main as launcher

    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    assert launcher.launch(argparse.Namespace()) == 2
    assert "POSIX" in capsys.readouterr().err


def test_launch_sanitizes_root_resolution_errors(tmp_path, monkeypatch, capsys):
    import argparse
    from pathlib import Path

    from openreading.mcp_server import main as launcher

    def fail(self):
        raise OSError("secret-root-path")

    monkeypatch.setattr(Path, "resolve", fail)
    assert launcher.launch(argparse.Namespace(input_root=tmp_path, artifact_root=tmp_path)) == 2
    output = capsys.readouterr()
    assert "secret-root-path" not in output.err
    assert "Configure separate absolute" in output.err
