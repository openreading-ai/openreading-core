"""Selection is opt-in, argument-free, transactional, and bounded independently of import."""

import asyncio
import json
from contextlib import asynccontextmanager

import anyio
import jsonschema
import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.mcp_server.tools import create_server


@pytest.fixture
def service(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    result = ArtifactService(ProfileConfig(root, tmp_path.resolve() / "artifacts"))
    yield result
    result.close()


async def call(server, arguments=None):
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="openreading_select_document", arguments={} if arguments is None else arguments
        )
    )
    result = (await server.request_handlers[types.CallToolRequest](request)).root
    assert len(result.content) == 1
    assert result.structuredContent is None
    payload = json.loads(result.content[0].text)
    from openreading.schemas import selection_tool_schema

    jsonschema.validate(payload, selection_tool_schema())
    return payload, result.isError


class Provider:
    def __init__(self, root, reference="chosen.pdf", wait=False):
        self.root, self.reference, self.wait = root, reference, wait
        self.entered = anyio.Event()
        self.rolled_back = False
        self.exited = False

    @asynccontextmanager
    async def select(self):
        self.entered.set()
        try:
            if self.wait:
                await anyio.sleep_forever()
            yield self.reference
        except BaseException:
            self.rolled_back = True
            raise
        finally:
            self.exited = True


@pytest.mark.asyncio
async def test_absent_provider_is_explicit_and_argument_validation_precedes_ui(service):
    server = create_server(service)
    payload, failed = await call(server)
    assert failed and payload["error"]["code"] == "selection_unavailable"
    provider = Provider(service.config.input_root)
    server = create_server(service, selection_provider=provider)
    for key in ("path", "title", "initial_directory", "filename", "filter", "callback"):
        with pytest.raises(McpError) as error:
            await call(server, {key: "private-secret"})
        assert "private-secret" not in str(error.value)
    assert not provider.entered.is_set()


@pytest.mark.asyncio
async def test_selected_receipt_is_validated_without_parsing_or_original_path(service):
    (service.config.input_root / "chosen.pdf").write_bytes(b"%PDF-1.7\nselected")
    provider = Provider(service.config.input_root)
    payload, failed = await call(create_server(service, selection_provider=provider))
    assert not failed
    assert payload == {
        "schema_version": "0.1",
        "path": "chosen.pdf",
        "display_name": "chosen.pdf",
        "source_bytes": 17,
    }
    assert provider.exited and not provider.rolled_back
    assert str(service.config.input_root) not in json.dumps(payload)
    assert list((service.config.artifact_root / "artifacts").glob("or1_*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["../secret.pdf", "/secret.pdf", "missing.pdf", "link.pdf", "directory", "", 42]
)
async def test_invalid_provider_reference_rolls_back_and_is_sanitized(service, reference):
    root = service.config.input_root
    (root / "directory").mkdir()
    (root / "link.pdf").symlink_to(root / "directory")
    provider = Provider(root, reference)
    payload, failed = await call(create_server(service, selection_provider=provider))
    assert failed and payload["error"]["code"] == "selection_failed"
    assert "secret" not in json.dumps(payload)
    assert provider.rolled_back and provider.exited


@pytest.mark.asyncio
async def test_user_cancel_is_distinct_from_failure(service):
    provider = Provider(service.config.input_root, None)
    payload, failed = await call(create_server(service, selection_provider=provider))
    assert failed and payload["error"]["code"] == "selection_cancelled"
    assert provider.exited


@pytest.mark.asyncio
async def test_concurrent_selection_refuses_without_entering_another_dialog(service):
    provider = Provider(service.config.input_root, wait=True)
    server = create_server(service, selection_provider=provider)
    task = asyncio.create_task(call(server))
    await provider.entered.wait()
    payload, failed = await call(server)
    assert failed and payload["error"]["code"] == "busy"
    assert not provider.exited
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.exited and provider.rolled_back
    provider.wait, provider.reference = False, None
    assert (await call(server))[0]["error"]["code"] == "selection_cancelled"


@pytest.mark.asyncio
async def test_deadline_rolls_back_before_releasing_selection_slot(service):
    provider = Provider(service.config.input_root, wait=True)
    server = create_server(service, selection_provider=provider, selection_timeout_seconds=0.01)
    payload, failed = await call(server)
    assert failed and payload["error"]["code"] == "selection_timeout"
    assert provider.exited and provider.rolled_back
    provider.wait, provider.reference = False, None
    assert (await call(server))[0]["error"]["code"] == "selection_cancelled"


@pytest.mark.asyncio
async def test_provider_failure_cannot_leak_local_path(service):
    class Broken:
        @asynccontextmanager
        async def select(self):
            raise RuntimeError("private-secret")
            yield

    payload, failed = await call(create_server(service, selection_provider=Broken()))
    assert failed and payload["error"]["code"] == "selection_failed"
    assert "private-secret" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_catalog_shape_matches_availability_and_selection_is_not_readonly(service):
    snapshots = []
    for provider in (None, Provider(service.config.input_root)):
        server = create_server(service, selection_provider=provider)
        result = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
        tools = {tool.name: tool for tool in result.root.tools}
        tool = tools["openreading_select_document"]
        assert tool.inputSchema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        assert not tool.annotations.readOnlyHint
        assert not tool.annotations.idempotentHint
        assert not tool.annotations.openWorldHint
        snapshots.append({name: (t.inputSchema, t.annotations) for name, t in tools.items()})
        assert ("unavailable" in tool.description) == (provider is None)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, 181])
def test_selection_deadline_is_finite_and_bounded(service, timeout):
    with pytest.raises(ValueError):
        create_server(service, selection_timeout_seconds=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 25 * 1024 * 1024 + 1])
async def test_selected_size_is_checked_before_receipt(service, size):
    with (service.config.input_root / "chosen.pdf").open("wb") as file:
        file.truncate(size)
    provider = Provider(service.config.input_root)
    payload, failed = await call(create_server(service, selection_provider=provider))
    assert failed and payload["error"]["code"] == "selection_failed"
    assert provider.rolled_back


def test_provider_receipt_and_failure_models_reject_unknown_fields():
    from pydantic import ValidationError

    from openreading.types.selection import SelectionFailure, SelectionReceipt

    for model, payload in (
        (SelectionReceipt, {"path": "a.pdf", "display_name": "a.pdf", "source_bytes": 1}),
        (SelectionFailure, SelectionFailure.from_code("busy").wire()),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "original_path": "/private-secret"})


@pytest.mark.parametrize("end", ["eof", "cancel", "signal"])
def test_stdio_pending_selection_cleans_up_on_disconnect_cancel_and_signal(tmp_path, end):
    import os
    import select
    import signal
    import subprocess
    import sys
    import time

    root = tmp_path.resolve() / "input"
    root.mkdir()
    started, cleaned = root.parent / "started", root.parent / "cleaned"
    script = """
import sys, anyio
from pathlib import Path
from contextlib import asynccontextmanager
from openreading.mcp_server.main import main
class Provider:
    @asynccontextmanager
    async def select(self):
        Path(sys.argv[1]).write_text('started')
        try:
            await anyio.sleep_forever()
            yield None
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.05)
                Path(sys.argv[2]).write_text('cleaned')
raise SystemExit(main(sys.argv[3:], selection_provider=Provider()))
"""
    args = [
        sys.executable,
        "-c",
        script,
        str(started),
        str(cleaned),
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(root.parent / "artifacts"),
    ]
    with subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:

            def send(payload):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}).encode() + b"\n")
                process.stdin.flush()

            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            )
            assert select.select([process.stdout], [], [], 10)[0]
            assert json.loads(process.stdout.readline())["id"] == 1
            send({"method": "notifications/initialized"})
            send(
                {
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "openreading_select_document", "arguments": {}},
                }
            )
            limit = time.monotonic() + 10
            while not started.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            assert started.exists()
            if end == "signal":
                os.kill(process.pid, signal.SIGTERM)
            elif end == "cancel":
                send({"method": "notifications/cancelled", "params": {"requestId": 2}})
            else:
                process.stdin.close()
            limit = time.monotonic() + 3
            while not cleaned.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            assert cleaned.exists(), "pending selection survived transport cancellation"
            if end == "cancel":
                process.stdin.close()
            assert process.wait(timeout=5) == (130 if end == "signal" else 0)
            assert b"Traceback" not in process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_async_cleanup_keeps_admission_until_finished(service, ending):
    entered, started, release, finished = (anyio.Event() for _ in range(4))

    class AsyncProvider:
        @asynccontextmanager
        async def select(self):
            entered.set()
            try:
                await anyio.sleep_forever()
                yield None
            finally:
                with anyio.CancelScope(shield=True):
                    started.set()
                    await release.wait()
                    finished.set()

    server = create_server(
        service, selection_provider=AsyncProvider(), selection_timeout_seconds=0.02
    )
    scope = anyio.CancelScope()
    results = []

    async def first():
        with scope:
            results.append(await call(server))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first)
        if ending == "cancel":
            await entered.wait()
            scope.cancel()
        await started.wait()
        try:
            assert (await call(server))[0]["error"]["code"] == "busy"
            assert not finished.is_set()
        finally:
            release.set()
    assert finished.is_set()
    if ending == "deadline":
        assert results[0][0]["error"]["code"] == "selection_timeout"


@pytest.mark.parametrize("timeout", [0, -1, 999, float("nan"), float("inf"), True])
def test_main_refuses_bad_timeout_before_configuration_io(tmp_path, monkeypatch, capsys, timeout):
    from openreading.mcp_server import main as entry

    def no_io(*args):
        pytest.fail("invalid timeout reached profile or service setup")

    monkeypatch.setattr(entry, "profile_config", no_io)
    result = entry.main(
        [
            "--profile",
            "local-document-proof-v1",
            "--input-root",
            str(tmp_path / "input"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        selection_timeout_seconds=timeout,
    )
    assert result == 2
    output = capsys.readouterr()
    assert not output.out
    assert "Traceback" not in output.err
    assert "timeout" in output.err.lower()
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_serve_refuses_bad_timeout_before_service_creation(tmp_path, monkeypatch):
    import openreading.artifacts.service as service_module
    from openreading.mcp_server.main import serve

    def no_service(*args):
        pytest.fail("invalid timeout opened the artifact service")

    monkeypatch.setattr(service_module, "ArtifactService", no_service)
    with pytest.raises(ValueError, match="timeout"):
        await serve(
            ProfileConfig(tmp_path / "input", tmp_path / "artifacts"), selection_timeout_seconds=0
        )


def test_selection_models_and_wire_schema_agree_on_boundaries():
    from pydantic import ValidationError

    from openreading.schemas import selection_tool_schema
    from openreading.types.selection import MESSAGES, SelectionFailure, SelectionReceipt

    validator = jsonschema.Draft202012Validator(selection_tool_schema())
    receipt = {"schema_version": "0.1", "path": "a.pdf", "display_name": "a.pdf", "source_bytes": 1}
    cases = [(SelectionReceipt, receipt, True)]
    for key in ("path", "display_name"):
        for value, valid in (("", False), ("a" * 1024, True), ("a" * 1025, False), (None, False)):
            cases.append((SelectionReceipt, {**receipt, key: value}, valid))
    for value in (0, -1, True, "1", None):
        cases.append((SelectionReceipt, {**receipt, "source_bytes": value}, False))
    for key in receipt:
        if key != "schema_version":  # Models insert the version; wire payloads must carry it.
            cases.append((SelectionReceipt, {k: v for k, v in receipt.items() if k != key}, False))
    for code in MESSAGES:
        failure = SelectionFailure.from_code(code).wire()
        cases.append((SelectionFailure, failure, True))
        for key, value in (("code", "unknown"), ("retryable", "false"), ("message", 1)):
            cases.append(
                (SelectionFailure, {**failure, "error": {**failure["error"], key: value}}, False)
            )
        cases.append(
            (SelectionFailure, {**failure, "error": {**failure["error"], "path": "secret"}}, False)
        )
    for model, payload, valid in cases:
        assert validator.is_valid(payload) == valid, payload
        if valid:
            assert model.model_validate(payload).wire() == payload
        else:
            with pytest.raises(ValidationError):
                model.model_validate(payload)
    for model, payload in (
        (SelectionReceipt, receipt),
        (SelectionFailure, SelectionFailure.from_code("busy").wire()),
    ):
        for version in ("0.2", 0.1, None):
            wrong_version = {**payload, "schema_version": version}
            assert not validator.is_valid(wrong_version)
            with pytest.raises(ValidationError):
                model.model_validate(wrong_version)
        missing_version = {key: value for key, value in payload.items() if key != "schema_version"}
        assert not validator.is_valid(missing_version)
        validator.validate(model.model_validate(missing_version).wire())
