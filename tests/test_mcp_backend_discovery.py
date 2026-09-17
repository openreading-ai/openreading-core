"""Discovery reports the configured profile without executing or inspecting other backends."""

import json

import pytest
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from referencing import Registry, Resource

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.artifacts.limits import ArtifactError, DoclingLimits, ProfileConfig
from openreading.artifacts.service import ArtifactService, engine_identity
from openreading.credentials import EnvCredentialBroker
from openreading.mcp_server.tools import create_server
from openreading.schemas import backend_discovery_schema, descriptor_schema, validate_descriptor


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["pymupdf", "docling_local"])
async def test_discovery_is_scoped_and_does_not_probe_or_resolve_credentials(
    tmp_path, monkeypatch, backend
):
    root = tmp_path / "input"
    root.mkdir()
    identity = engine_identity()
    config = ProfileConfig(root, tmp_path / "store")
    if backend == "docling_local":
        config = ProfileConfig(
            root,
            tmp_path / "store",
            DoclingLimits(
                pages=None, deadline_seconds=None, worker_memory_bytes=None, worker_idle_seconds=60
            ),
            LocalDoclingConfig(artifacts_path=tmp_path / "absent-private-models", ocr=True),
        )
    service = ArtifactService(config, identity=identity)

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery executed or inspected ambient configuration")

    # These operations can load runtime dependencies, contact providers or read secrets.
    # Discovery must work even when none of them is available.
    for factory in BUILTIN_ADAPTERS.values():
        monkeypatch.setattr(factory, "health", forbidden)
        monkeypatch.setattr(factory, "submit", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve_config", forbidden)
    monkeypatch.setenv("REDUCTO_API_KEY", "do-not-return-this-secret")
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    try:
        async with create_connected_server_and_client_session(create_server(service)) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert "openreading_backends" in tools
            annotations = tools["openreading_backends"].annotations
            assert annotations.readOnlyHint and annotations.idempotentHint
            assert not annotations.openWorldHint and not annotations.destructiveHint
            first = await session.call_tool("openreading_backends", {})
            second = await session.call_tool("openreading_backends", {})
            assert not first.isError
            assert first.structuredContent is None and len(first.content) == 1
            assert first.content == second.content
            text = first.content[0].text
            body = json.loads(text)
            descriptor_contract = descriptor_schema()
            registry = Registry().with_resource(
                descriptor_contract["$id"], Resource.from_contents(descriptor_contract)
            )
            Draft202012Validator(backend_discovery_schema(), registry=registry).validate(body)
            assert body["scope"] == "configured_local_profile"
            assert body["readiness"] == "not_checked"
            assert body["schema_version"] == "0.1"
            assert [item["id"] for item in body["backends"]] == [backend]
            descriptor = body["backends"][0]
            validate_descriptor(descriptor)
            assert descriptor == make_adapter(backend).descriptor.to_schema_dict()
            assert "sources" in descriptor and "capabilities" in descriptor
            assert "do-not-return-this-secret" not in text
            assert str(tmp_path) not in text
            assert body["ocr_enabled"] is (backend == "docling_local")
            assert len(text.encode()) <= 65536
    finally:
        service.close()
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


@pytest.mark.asyncio
async def test_discovery_arguments_cannot_widen_scope_or_request_live_work(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    service = ArtifactService(ProfileConfig(root, tmp_path / "store"))
    try:
        async with create_connected_server_and_client_session(create_server(service)) as session:
            for arguments in (
                {"backend": "reducto-secret"},
                {"allowed_backends": ["reducto-secret"]},
                {"check": "reducto-secret"},
                {"config": "reducto-secret"},
            ):
                with pytest.raises(McpError) as error:
                    await session.call_tool("openreading_backends", arguments)
                assert "reducto-secret" not in str(error.value)
                assert error.value.error.code == -32602
    finally:
        service.close()


def test_oversized_descriptor_refuses_instead_of_truncating(tmp_path, monkeypatch):
    from openreading.mcp_server import discovery

    adapter = make_adapter("pymupdf")
    adapter.descriptor = adapter.descriptor.model_copy(deep=True)
    adapter.descriptor.capabilities = adapter.descriptor.capabilities.model_copy(
        update={"extra_description": "large claim " * 65536}
    )
    monkeypatch.setattr(discovery, "make_adapter", lambda name: adapter)
    with pytest.raises(ArtifactError, match="response_too_large"):
        discovery.describe_backends(ProfileConfig(tmp_path / "input", tmp_path / "store"))
