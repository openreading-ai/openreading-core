"""Planning uses trusted backend scope without reading sources or executing providers."""

import json
import sys

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.credentials import EnvCredentialBroker
from openreading.mcp_server.tools import create_server


@pytest.mark.asyncio
async def test_local_route_ignores_ambient_config_and_never_runs_adapters(tmp_path, monkeypatch):
    root = tmp_path / "input"
    root.mkdir()
    service = ArtifactService(ProfileConfig(root, tmp_path / "store"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Route planning touched execution or credentials")

    for factory in BUILTIN_ADAPTERS.values():
        monkeypatch.setattr(factory, "submit", forbidden)
        monkeypatch.setattr(factory, "health", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve", forbidden)
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "missing-secret-config.yaml"))
    try:
        async with create_connected_server_and_client_session(create_server(service)) as session:
            catalog = {t.name: t for t in (await session.list_tools()).tools}
            assert "openreading_route" in catalog
            hints = catalog["openreading_route"].annotations
            assert hints.readOnlyHint and hints.idempotentHint and not hints.openWorldHint
            reply = await session.call_tool("openreading_route", {})
            assert not reply.isError
            body = json.loads(reply.content[0].text)
            assert body == {
                "schema_version": "0.1",
                "execution": "not_started",
                "allowed_backends": ["pymupdf"],
                "chain": ["pymupdf"],
                "dropped": [],
                "terminal_reason": None,
            }
            denied = await session.call_tool("openreading_route", {"backend": "reducto"})
            assert denied.isError
            assert json.loads(denied.content[0].text)["terminal_reason"] == "scope_denied"
            for args in (
                {"document": {"path": "secret"}},
                {"allowed_backends": ["reducto-secret"]},
                {"config": "secret"},
                {"backend": "strategy:secret"},
            ):
                with pytest.raises(McpError) as error:
                    await session.call_tool("openreading_route", args)
                assert error.value.error.code == -32602
                assert "secret" not in str(error.value)
    finally:
        service.close()


def test_operator_snapshot_and_router_semantics(tmp_path, monkeypatch):
    from openreading import api
    from openreading.mcp_server.routing import RoutingConfig, plan_route
    from openreading.schemas import route_tool_schema

    raw = {"version": 1, "policy": {"backends": ["reducto", "tesseract", "pymupdf"]}}
    config = RoutingConfig.from_operator(
        "pymupdf", config=raw, allowed_backends=["tesseract", "pymupdf"]
    )
    ungranted = RoutingConfig.from_operator(
        "pymupdf", config={"version": 1, "policy": {"backends": ["reducto"]}}
    )
    assert ungranted.allowed_backends == frozenset({"pymupdf"})
    assert plan_route(ungranted).terminal_reason == "scope_denied"
    raw["policy"]["backends"].clear()
    result = plan_route(config, fallback=["pymupdf", "reducto", "not-installed"])
    assert result.chain == ["pymupdf", "tesseract"]
    assert result.dropped == ["reducto"]
    assert result.terminal_reason is None
    Draft202012Validator(route_tool_schema()).validate(result.wire())
    expected = api.route(
        b"no parsing necessary",
        config={"version": 1, "policy": {"backends": ["pymupdf", "reducto", "tesseract"]}},
    ).restrict_to(config.allowed_backends)
    assert result.chain == expected.eligible_ids
    assert plan_route(config, backend="tesseract").chain == ["tesseract"]
    empty = RoutingConfig((), frozenset({"pymupdf"}))
    assert plan_route(empty).terminal_reason == "no_backend_in_scope"
    denied = RoutingConfig(("pymupdf",), frozenset())
    assert plan_route(denied).terminal_reason == "scope_denied"

    def forbidden():
        raise AssertionError("Denied named backend reached adapter registry")

    monkeypatch.setattr("openreading.mcp_server.routing.build_registry", forbidden)
    assert plan_route(config, backend="unregistered").terminal_reason == "scope_denied"


def test_explicit_file_snapshot_default_and_strict_setup(tmp_path, monkeypatch):
    from openreading.mcp_server.routing import RoutingConfig, plan_route

    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\npolicy:\n  backends: [tesseract, pymupdf]\n")
    config = RoutingConfig.from_operator(
        "pymupdf", config=path, allowed_backends=["pymupdf", "tesseract"]
    )
    path.write_text("version: 1\npolicy:\n  backends: [reducto]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_CONFIG", str(path))
    assert plan_route(config).chain == ["tesseract", "pymupdf"]
    assert RoutingConfig.from_operator("docling_local").default_backends == ("docling_local",)
    assert plan_route(RoutingConfig.from_operator("docling_local")).chain == ["docling_local"]
    for defaults, allowed in (
        (["pymupdf"], frozenset()),
        ((1,), frozenset()),
        ((), {"pymupdf"}),
        ((), frozenset({"missing"})),
    ):
        with pytest.raises(ValueError):
            RoutingConfig(defaults, allowed)


def test_plan_budget_refuses_instead_of_truncating(monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server import routing

    monkeypatch.setattr(routing, "MAX_ROUTE_BYTES", 10)
    with pytest.raises(ArtifactError, match="response_too_large"):
        routing.plan_route(routing.RoutingConfig.from_operator("pymupdf"))


def test_launcher_passes_explicit_route_scope_and_snapshotted_policy(tmp_path, monkeypatch):
    from openreading.mcp_server import main as launcher
    from openreading.mcp_server.routing import plan_route

    source = tmp_path / "input"
    source.mkdir()
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\npolicy:\n  backends: [reducto, tesseract]\n")
    seen = []

    async def capture(config, **kwargs):
        path.unlink()
        seen.append(plan_route(kwargs["routing_config"]).wire())

    monkeypatch.setattr(launcher, "serve", capture)
    assert (
        launcher.main(
            [
                "--profile",
                "local-document-proof-v1",
                "--input-root",
                str(source),
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--routing-config",
                str(path),
                "--allow-backend",
                "tesseract",
            ]
        )
        == 0
    )
    assert seen[0]["chain"] == ["tesseract"]
    assert seen[0]["dropped"] == ["reducto"]


@pytest.mark.parametrize(
    "extra",
    [["--allow-backend", "not-a-backend"], ["--routing-config", "/missing-config-secret.yaml"]],
)
def test_bad_operator_setup_refuses_before_transport(tmp_path, monkeypatch, capsys, extra):
    from openreading.mcp_server import main as launcher

    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid setup started transport")

    monkeypatch.setattr(launcher, "serve", forbidden)
    assert (
        launcher.main(
            [
                "--profile",
                "local-document-proof-v1",
                "--input-root",
                str(tmp_path / "input"),
                "--artifact-root",
                str(tmp_path / "artifacts"),
                *extra,
            ]
        )
        == 2
    )
    assert "secret" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_stdio_launcher_plans_under_operator_scope(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\npolicy:\n  backends: [reducto, tesseract, pymupdf]\n")
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
            "--routing-config",
            str(path),
            "--allow-backend",
            "pymupdf",
            "--allow-backend",
            "tesseract",
        ],
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        for _ in range(2):
            reply = await session.call_tool(
                "openreading_route", {"fallback": ["pymupdf", "reducto"]}
            )
            assert not reply.isError
            payload = json.loads(reply.content[0].text)
            assert payload["chain"] == ["pymupdf", "tesseract"]
            assert payload["dropped"] == ["reducto"]
            assert payload["execution"] == "not_started"
            path.write_text("invalid: changed after startup\n")
        denied = await session.call_tool("openreading_route", {"backend": "reducto"})
        assert denied.isError
        assert json.loads(denied.content[0].text)["terminal_reason"] == "scope_denied"
