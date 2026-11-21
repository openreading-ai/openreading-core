"""The MCP readiness tool and the launcher's `--keychain` and `--config` flags.

The launcher's real `serve` is swapped for an in-memory MCP session over the same
`create_server`, so each test drives startup and a tool call through `launch` without stdio.
"""

import argparse
import json
import os
import textwrap

import pytest
from mcp.shared.exceptions import McpError

from openreading import credentials
from tests.test_artifact_service import pdf

KEY = "sk-ant-mcp-keychain-value-0123456789"
SETUP = "Run `openreading setup` in a terminal to configure a backend."
VALUE_WORDS = ("key", "secret", "token", "credential", "password", "value")


def _clear_anthropic(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENREADING_ANTHROPIC_CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _launch(monkeypatch, tmp_path, *extra: str) -> tuple[int, dict]:
    """Run `launch` through argument parsing and record one `openreading_backends` call."""
    import openreading.mcp_server.main as mcp_main

    seen: dict = {}

    async def serve(config, **_kwargs):
        from mcp.shared.memory import create_connected_server_and_client_session

        from openreading.artifacts.service import ArtifactService
        from openreading.mcp_server.backends import backends_report
        from openreading.mcp_server.tools import create_server

        service = ArtifactService(config)
        try:
            server = create_server(service, backends=backends_report)
            async with create_connected_server_and_client_session(server) as session:
                result = await session.call_tool("openreading_backends", {})
                seen["error"] = result.isError
                seen["text"] = result.content[0].text
                seen["in_use"] = credentials.keychain_in_use()
        finally:
            service.close()

    monkeypatch.setattr(mcp_main, "serve", serve)
    root = tmp_path.resolve() / "input"
    root.mkdir(exist_ok=True)
    argv = [
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(tmp_path.resolve() / "store"),
        *extra,
    ]
    return mcp_main.main(argv), seen


def _row(report: dict, slug: str) -> dict:
    return next(r for r in report["backends"] if r["backend"] == slug)


def test_without_the_flag_the_launcher_and_tool_never_call_the_keychain(
    monkeypatch, tmp_path, fake_keychain
):
    _clear_anthropic(monkeypatch)
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    code, seen = _launch(monkeypatch, tmp_path)
    assert code == 0 and seen["error"] is False
    report = json.loads(seen["text"])
    assert report["keychain"] is False
    assert report["next_step"] == SETUP
    row = _row(report, "anthropic-claude")
    assert row["ready"] is False and "ANTHROPIC_API_KEY" in row["missing"]
    assert fake_keychain.calls == []
    assert seen["in_use"] is False


def test_with_the_flag_a_saved_key_resolves_by_name_and_never_by_value(
    monkeypatch, tmp_path, fake_keychain
):
    _clear_anthropic(monkeypatch)
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    code, seen = _launch(monkeypatch, tmp_path, "--keychain")
    assert code == 0 and seen["in_use"] is True
    report = json.loads(seen["text"])
    assert report["keychain"] is True
    row = _row(report, "anthropic-claude")
    assert row["ready"] is True
    assert {"var": "ANTHROPIC_API_KEY", "source": "keychain"} in row["found"]
    assert KEY not in seen["text"]
    # The saved value stays out of the environment that a worker process would inherit.
    assert "ANTHROPIC_API_KEY" not in os.environ
    # The opt-in ends with the launcher, so a later caller in this process starts clean.
    assert credentials.keychain_in_use() is False


def test_an_exported_variable_reports_env_and_beats_the_keychain(
    monkeypatch, tmp_path, fake_keychain
):
    _clear_anthropic(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exported")
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    code, seen = _launch(monkeypatch, tmp_path, "--keychain")
    assert code == 0
    row = _row(json.loads(seen["text"]), "anthropic-claude")
    assert row["found"] == [{"var": "ANTHROPIC_API_KEY", "source": "env"}]
    assert "sk-ant-exported" not in seen["text"]


def test_the_launcher_reads_only_an_explicit_config(monkeypatch, tmp_path):
    yaml = textwrap.dedent(
        """\
        version: 1
        providers:
          example-llm:
            kind: openai-compatible
            base_url: https://llm.example.test/v1
            model: vision-1
            key_env: EXAMPLE_LLM_API_KEY
        """
    )
    # The client's working directory is arbitrary, so a file there must change nothing.
    (tmp_path / "openreading.yaml").write_text(yaml)
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "openreading.yaml"))
    code, seen = _launch(monkeypatch, tmp_path)
    assert code == 0
    assert "example-llm" not in {r["backend"] for r in json.loads(seen["text"])["backends"]}

    explicit = tmp_path / "project.yaml"
    explicit.write_text(yaml)
    code, seen = _launch(monkeypatch, tmp_path, "--config", str(explicit))
    assert code == 0
    row = _row(json.loads(seen["text"]), "example-llm")
    assert row["ready"] is False and row["missing"] == ["EXAMPLE_LLM_API_KEY"]


def test_a_bad_explicit_config_exits_2_before_serving(monkeypatch, tmp_path, capsys):
    code, seen = _launch(monkeypatch, tmp_path, "--config", str(tmp_path / "absent.yaml"))
    assert code == 2 and seen == {}
    assert "config file not found" in capsys.readouterr().err


def test_the_cli_accepts_the_flags_and_still_skips_ambient_files(monkeypatch, tmp_path):
    import openreading.mcp_server.main as mcp_main
    from openreading.cli import app as cli

    seen: dict = {}

    def launch(args: argparse.Namespace) -> int:
        seen["keychain"], seen["config"] = args.keychain, args.config
        seen["in_use"] = credentials.keychain_in_use()
        return 0

    monkeypatch.setattr(mcp_main, "launch", launch)
    root = str(tmp_path.resolve())
    argv = ["mcp", "--profile", "local-document-proof-v1", "--input-root", root]
    argv += ["--artifact-root", root + "/a", "--keychain"]
    assert cli.main(argv) == 0
    # The CLI hands the flag to the launcher, which opts in for its own lifetime.
    assert seen == {"keychain": True, "config": None, "in_use": False}


@pytest.mark.asyncio
async def test_no_tool_accepts_a_key_and_the_readiness_tool_is_read_only(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.backends import backends_report
    from openreading.mcp_server.tools import BACKENDS_INPUT, INPUTS, create_server

    def names(schema) -> set[str]:
        found: set[str] = set()
        if isinstance(schema, dict):
            found |= set(schema.get("properties", {}))
            for value in schema.values():
                found |= names(value)
        elif isinstance(schema, list):
            for value in schema:
                found |= names(value)
        return found

    every = set().union(*(names(schema) for schema in [*INPUTS.values(), BACKENDS_INPUT]))
    assert not {n for n in every if any(word in n.lower() for word in VALUE_WORDS)}

    root = tmp_path.resolve() / "input"
    root.mkdir()
    pdf(root / "sample.pdf")
    service = ArtifactService(ProfileConfig(root, tmp_path.resolve() / "store"))
    try:
        # Without a reporter, as in the client-only wheel, the tool does not exist.
        async with create_connected_server_and_client_session(create_server(service)) as session:
            assert "openreading_backends" not in {
                t.name for t in (await session.list_tools()).tools
            }
            with pytest.raises(McpError):
                await session.call_tool("openreading_backends", {})
        server = create_server(service, backends=backends_report)
        async with create_connected_server_and_client_session(server) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            tool = tools["openreading_backends"]
            assert tool.inputSchema == {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            }
            assert tool.annotations is not None
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.openWorldHint is False
            assert SETUP in (tool.description or "")
            with pytest.raises(McpError) as error:
                await session.call_tool("openreading_backends", {"api_key": KEY})
            assert KEY not in str(error.value)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_the_engine_launcher_adds_the_tool_only_for_local_processing(monkeypatch, remote):
    import openreading.mcp_server.session as session_module
    from openreading.artifacts.jobs import ImportExecution
    from openreading.mcp_server import main as mcp_main
    from openreading.mcp_server.backends import backends_report

    seen = {}

    async def fake_serve(config, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(session_module, "serve", fake_serve)
    execution = ImportExecution(("/trusted/worker",), {"destination": "test"}) if remote else None
    await mcp_main.serve(None, execution=execution)  # type: ignore[arg-type]
    # A Core server that receives the documents holds the keys, so its own GET /v1/backends answers.
    assert seen["backends"] is (None if remote else backends_report)
