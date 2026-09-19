"""Comparison tools consume authorized retained inputs without executing extraction."""

import copy
import hashlib
import json
import re

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.artifacts.models import json_bytes
from openreading.comparison import compare
from openreading.mcp_server.tools import create_server
from tests.test_mcp_results import result_service as result_service
from tests.test_retained_results import provenance, response


def retain(store, payload, **changes):
    return store.publish("normalized_response", payload, provenance(**changes)).result_id


@pytest.mark.asyncio
async def test_compare_retains_engine_report_and_maps_repeated_backends(
    result_service, monkeypatch
):
    service, store = result_service
    payloads = [
        response(text="alpha"),
        response("partial", text="beta"),
        response("failed", text=""),
    ]
    payloads[1]["backend"]["version"] = "2"
    payloads[2]["backend"]["version"] = "2"
    ids = [retain(store, p) for p in payloads]
    before = {p: p.read_bytes() for p in store.root.glob("*.json")}
    expected = compare(copy.deepcopy(payloads))

    def forbidden(*args, **kwargs):
        raise AssertionError("Comparison executed a backend or resolved credentials")

    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.credentials import EnvCredentialBroker

    for factory in BUILTIN_ADAPTERS.values():
        monkeypatch.setattr(factory, "submit", forbidden)
        monkeypatch.setattr(factory, "health", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve", forbidden)
    monkeypatch.setenv("OPENREADING_CONFIG", "/private-secret/config.yaml")
    async with create_connected_server_and_client_session(create_server(service)) as session:
        catalog = {t.name: t for t in (await session.list_tools()).tools}
        from openreading import cli

        assert "openreading_compare" in catalog
        assert all(name in cli.__doc__ for name in catalog)
        count = re.search(r"Serve (\d+) tools over stdio", cli.__doc__)
        assert count and int(count[1]) == len(catalog)
        assert "unchanged inputs and implementation" in catalog["openreading_compare"].description
        assert "inputs and implementation must also remain unchanged" in cli.__doc__
        hints = catalog["openreading_compare"].annotations
        assert not hints.readOnlyHint and hints.idempotentHint and not hints.openWorldHint
        reply = await session.call_tool("openreading_compare", {"result_ids": ids})
        assert not reply.isError
        receipt = json.loads(reply.content[0].text)
        assert receipt["kind"] == "comparison_report"
        stored = store.load(receipt["result_id"])
        assert stored.payload == expected
        assert stored.provenance.subjects == dict(
            zip(["synthetic", "synthetic (2)", "synthetic#3"], ids, strict=True)
        )
        assert stored.provenance.adapters == {
            "synthetic": None,
            "synthetic (2)": "2",
            "synthetic#3": "2",
        }
        assert stored.provenance.source_sha256 == ["c" * 64] * 3
        assert receipt["content_sha256"] == hashlib.sha256(json_bytes(stored.wire())).hexdigest()
        second = await session.call_tool("openreading_compare", {"result_ids": ids})
        assert json.loads(second.content[0].text) == receipt
        fetched = await session.call_tool(
            "openreading_get_result", {"result_id": receipt["result_id"]}
        )
        assert json.loads(fetched.content[0].text)["content"] == stored.wire()
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.asyncio
async def test_compare_rejects_paths_unknown_arguments_and_non_response_inputs(result_service):
    service, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    async with create_connected_server_and_client_session(create_server(service)) as session:
        assert "openreading_compare" in {t.name for t in (await session.list_tools()).tools}
        for args in [
            {"result_ids": ["/private-secret/a.json", ids[0]]},
            {"result_ids": ids, "truth": "/private-secret/truth.json"},
            {"result_ids": ids, "backend": "private-secret"},
            {"result_ids": ids[:1]},
            {"result_ids": ids, "baseline": "/private-secret/base.json"},
        ]:
            with pytest.raises(McpError) as error:
                await session.call_tool("openreading_compare", args)
            assert error.value.error.code == -32602
            assert "private-secret" not in str(error.value)
        missing = await session.call_tool(
            "openreading_compare", {"result_ids": [ids[0], "orr1_" + "f" * 64]}
        )
        assert missing.isError
        assert json.loads(missing.content[0].text)["error"]["code"] == "result_not_found"
        good = await session.call_tool("openreading_compare", {"result_ids": ids})
        report_id = json.loads(good.content[0].text)["result_id"]
        before = set(store.root.glob("*.json"))
        bad = await session.call_tool("openreading_compare", {"result_ids": [ids[0], report_id]})
        assert bad.isError
        assert json.loads(bad.content[0].text)["error"]["code"] == "invalid_comparison"
        assert set(store.root.glob("*.json")) == before


def test_baseline_reuses_or_appends_authorized_input_and_binds_options(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest

    _, store = result_service
    payloads = [response(text=t) for t in ["alpha", "beta", "gamma"]]
    ids = [retain(store, p) for p in payloads]
    receipts = []
    for baseline, expected_ids in [(None, ids[:2]), (ids[1], ids[:2]), (ids[2], ids)]:
        request = CompareRequest(result_ids=ids[:2], baseline=baseline)
        receipt = compare_results(store, request, budget=4096, request_id=1)
        content = store.load(json.loads(receipt.content[0].text)["result_id"])
        expected = compare(
            payloads[: len(expected_ids)],
            baseline=None if baseline is None else f"synthetic#{expected_ids.index(baseline) + 1}",
        )
        assert content.payload == expected
        assert list(content.provenance.subjects.values()) == expected_ids
        assert (
            content.provenance.request_sha256
            == hashlib.sha256(json_bytes(request.model_dump(mode="json"))).hexdigest()
        )
        receipts.append(content)
    assert len({r.provenance.request_sha256 for r in receipts}) == 3
    assert len({r.provenance.config_sha256 for r in receipts}) == 3
    # Repeated input IDs are separate subjects, and a selected baseline uses the first occurrence.
    receipt = compare_results(
        store,
        CompareRequest(result_ids=[ids[0], ids[0]], baseline=ids[0]),
        budget=4096,
        request_id=1,
    )
    content = store.load(json.loads(receipt.content[0].text)["result_id"])
    assert content.payload["baseline"]["baseline"] == "synthetic"
    assert content.provenance.subjects == {"synthetic": ids[0], "synthetic#2": ids[0]}


def test_compare_mixes_local_artifacts_and_general_results_without_changing_inputs(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest
    from tests.test_artifact_service import pdf

    service, store = result_service
    pdf(service.config.input_root / "sample.pdf")
    local = service.import_document("sample.pdf")
    manifest, _, original = service.store.load_document(local.artifact_id)
    before = {p: p.read_bytes() for p in service.store.documents.rglob("*") if p.is_file()}
    other = response(text="different")
    retained = retain(store, other)
    result = compare_results(
        store, CompareRequest(result_ids=[local.artifact_id, retained]), budget=4096, request_id=1
    )
    content = store.load(json.loads(result.content[0].text)["result_id"])
    assert content.payload == compare([original, other])
    assert list(content.provenance.subjects.values()) == [local.artifact_id, retained]
    assert content.provenance.source_sha256 == [manifest.document_sha256, "c" * 64]
    assert content.wire()["provenance"]["subject_sources"] == {
        content.payload["subjects"][0]["label"]: {
            "source_sha256": [manifest.document_sha256],
            "verification": "verified_source_bytes",
        },
        content.payload["subjects"][1]["label"]: {
            "source_sha256": ["c" * 64],
            "verification": "producer_asserted",
        },
    }
    assert all(p.read_bytes() == data for p, data in before.items())


def test_compare_refuses_foreign_and_corrupt_inputs_before_publication(result_service, tmp_path):
    import shutil

    from openreading.artifacts.limits import ArtifactError, ProfileConfig
    from openreading.artifacts.result_models import ResultError
    from openreading.artifacts.results import RetainedResults
    from openreading.artifacts.store import Store
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest

    _, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_store = Store(ProfileConfig(foreign_root, store.store.config.artifact_root))
    try:
        foreign = RetainedResults(foreign_store)
        with pytest.raises(ResultError, match="result_not_found"):
            compare_results(foreign, CompareRequest(result_ids=ids), budget=4096, request_id=1)
        shutil.copyfile(store.path(ids[0]), foreign.path(ids[0]))
        with pytest.raises(ResultError, match="result_corrupt"):
            compare_results(foreign, CompareRequest(result_ids=ids), budget=4096, request_id=1)
    finally:
        foreign_store.close()
    for broken, error in [(ids[1], ResultError), ("or1_" + "f" * 64, ArtifactError)]:
        store.path(ids[1]).write_bytes(b"corrupt private-secret")
        before = set(store.root.glob("*.json"))
        with pytest.raises(error):
            compare_results(
                store, CompareRequest(result_ids=[ids[0], broken]), budget=4096, request_id=1
            )
        assert set(store.root.glob("*.json")) == before


def test_compare_receipt_budget_refuses_before_publication(result_service):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest
    from tests.test_mcp_delivery import rpc_bytes

    _, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    request = CompareRequest(result_ids=ids)
    before = set(store.root.glob("*.json"))
    with pytest.raises(ArtifactError, match="response_too_large"):
        compare_results(store, request, budget=4096, request_id='"\\界' * 3000)
    assert set(store.root.glob("*.json")) == before
    receipt = compare_results(store, request, budget=4096, request_id=1)
    assert len(rpc_bytes(receipt, 1)) <= 4096


@pytest.mark.asyncio
async def test_engine_failure_is_sanitized_and_publishes_nothing(result_service, monkeypatch):
    from openreading.mcp_server import comparison

    service, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    before = set(store.root.glob("*.json"))

    def fail(*args, **kwargs):
        raise RuntimeError("private-secret extracted content")

    monkeypatch.setattr(comparison, "build_report", fail)
    async with create_connected_server_and_client_session(create_server(service)) as session:
        reply = await session.call_tool("openreading_compare", {"result_ids": ids})
        assert reply.isError
        body = json.loads(reply.content[0].text)
        assert body["error"]["code"] == "comparison_failed"
        assert "private-secret" not in reply.content[0].text
    assert set(store.root.glob("*.json")) == before


def test_ambiguous_engine_labels_fail_closed(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareError, CompareRequest

    _, store = result_service
    collision = response()
    collision["backend"]["id"] = "synthetic#2"
    ids = [
        retain(store, response(text="first")),
        retain(store, response(text="second")),
        retain(store, collision),
    ]
    before = set(store.root.glob("*.json"))
    with pytest.raises(CompareError, match="invalid_comparison"):
        compare_results(store, CompareRequest(result_ids=ids), budget=4096, request_id=1)
    assert set(store.root.glob("*.json")) == before


def test_invalid_schema_response_in_local_artifact_is_refused(result_service, monkeypatch):
    from types import SimpleNamespace

    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareError, CompareRequest

    _, store = result_service
    identifier = retain(store, response())
    # Exercise the comparator's validation even if an older artifact loader admits a payload.
    monkeypatch.setattr(
        store.store,
        "load_document",
        lambda _: (
            SimpleNamespace(document_sha256="c" * 64),
            [],
            {"private-secret": "not a response"},
        ),
    )
    with pytest.raises(CompareError, match="invalid_comparison"):
        compare_results(
            store,
            CompareRequest(result_ids=[identifier, "or1_" + "d" * 64]),
            budget=4096,
            request_id=1,
        )


def test_baseline_failure_does_not_invoke_engine(result_service, monkeypatch):
    from openreading.artifacts.result_models import ResultError
    from openreading.mcp_server import comparison
    from openreading.types.compare_tool import CompareRequest

    _, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    calls = []
    monkeypatch.setattr(comparison, "build_report", lambda *a, **kw: calls.append(True))
    with pytest.raises(ResultError, match="result_not_found"):
        comparison.compare_results(
            store,
            CompareRequest(result_ids=ids, baseline="orr1_" + "f" * 64),
            budget=4096,
            request_id=1,
        )
    assert calls == []


def test_compare_schema_matches_model_and_validates_replies(result_service):
    from jsonschema import Draft202012Validator
    from pydantic import TypeAdapter

    from openreading.artifacts.limits import ArtifactError
    from openreading.artifacts.result_models import ResultError
    from openreading.mcp_server.comparison import compare_results
    from openreading.mcp_server.tools import INPUTS
    from openreading.schemas import compare_tool_schema
    from openreading.types.compare_tool import CompareError, ComparePayload, CompareRequest

    _, store = result_service
    schema = compare_tool_schema()
    generated = TypeAdapter(ComparePayload).json_schema()
    assert {k: v for k, v in schema.items() if k not in {"$id", "$schema"}} == generated
    assert INPUTS["openreading_compare"] == {
        **schema["$defs"]["CompareRequest"],
        "$defs": {"JsonValue": schema["$defs"]["JsonValue"]},
    }
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    ids = [retain(store, response(text=t)) for t in ["alpha", "beta"]]
    request = CompareRequest(result_ids=ids)
    validator.validate(request.wire())
    reply = compare_results(store, request, budget=4096, request_id=1)
    validator.validate(json.loads(reply.content[0].text))
    for error in [
        CompareError("invalid_comparison"),
        CompareError("comparison_failed"),
        ResultError("result_not_found"),
    ]:
        validator.validate(error.wire())
    validator.validate(ArtifactError("response_too_large").envelope().wire())


@pytest.mark.asyncio
async def test_real_stdio_compare_retrieval_and_restart(result_service):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    service, store = result_service
    ids = [retain(store, response(text=t)) for t in ["alpha café", "beta 界"]]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openreading.mcp_server.main",
            "--profile",
            "local-document-proof-v1",
            "--input-root",
            str(service.config.input_root),
            "--artifact-root",
            str(service.config.artifact_root),
        ],
    )
    receipts = []
    for _ in range(2):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            reply = await session.call_tool(
                "openreading_compare", {"result_ids": ids, "baseline": ids[0]}
            )
            assert not reply.isError
            receipts.append(json.loads(reply.content[0].text))
            fetched = await session.call_tool(
                "openreading_get_result", {"result_id": receipts[-1]["result_id"]}
            )
            assert not fetched.isError
            content = json.loads(fetched.content[0].text)["content"]
            assert content["payload"]["baseline"]["baseline"] == "synthetic"
            assert content["provenance"]["subjects"] == dict(
                zip(["synthetic", "synthetic#2"], ids, strict=True)
            )
    assert receipts[0] == receipts[1]


def test_large_comparison_receipt_and_lossless_delivery(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.mcp_server.results import deliver_result
    from openreading.types.compare_tool import CompareRequest
    from tests.test_mcp_results import reconstruct

    _, store = result_service
    payloads = [response(text="a"), response(text="b")]
    for p, suffix in zip(payloads, ["first", "second"], strict=True):
        p["typed_fields"] = {f"field-{n}": {"value": suffix + "界" * 80} for n in range(80)}
    ids = [retain(store, p) for p in payloads]
    response_message = compare_results(
        store, CompareRequest(result_ids=ids), budget=4096, request_id=1
    )
    receipt = json.loads(response_message.content[0].text)
    assert receipt["content_bytes"] > 4096
    expected = store.load(receipt["result_id"]).wire()
    assert expected["payload"] == compare([store.load(i).payload for i in ids])
    pages, cursor = [], None
    while True:
        reply = deliver_result(
            store,
            receipt["result_id"],
            mode="fragments",
            budget=4096,
            root=None,
            request_id=1,
            cursor=cursor,
        )
        page = json.loads(reply.content[0].text)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) > 1
    assert reconstruct(pages) == expected


def test_comparison_keeps_input_order_even_when_mapping_keys_sort(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest

    _, store = result_service
    payloads = [response(text="last alphabetically"), response(text="first alphabetically")]
    for item, backend in zip(payloads, ["z-backend", "a-backend"], strict=True):
        item["backend"]["id"] = backend
    ids = [retain(store, p) for p in payloads]
    reply = compare_results(store, CompareRequest(result_ids=ids), budget=4096, request_id=1)
    content = store.load(json.loads(reply.content[0].text)["result_id"])
    assert [s["label"] for s in content.payload["subjects"]] == ["z-backend", "a-backend"]
    assert content.provenance.subjects == {"z-backend": ids[0], "a-backend": ids[1]}
    assert content.payload == compare([store.load(i).payload for i in ids])


def test_source_attribution_preserves_multiple_hashes_and_empty_claims(result_service):
    from openreading.mcp_server.comparison import compare_results
    from openreading.types.compare_tool import CompareRequest

    _, store = result_service
    claims = [["a" * 64, "b" * 64], [], ["a" * 64]]
    ids = [
        retain(store, response(text=str(index)), source_sha256=hashes)
        for index, hashes in enumerate(claims)
    ]
    reply = compare_results(
        store, CompareRequest(result_ids=ids[:2], baseline=ids[2]), budget=4096, request_id=1
    )
    identifier = json.loads(reply.content[0].text)["result_id"]
    content = store.load(identifier)
    assert json.loads(store.path(identifier).read_bytes())["format"] == "retained-result.v0.2"
    assert content.provenance.source_sha256 == ["a" * 64, "b" * 64, "a" * 64]
    assert content.wire()["provenance"]["subject_sources"] == {
        label: {"source_sha256": hashes, "verification": "producer_asserted"}
        for label, hashes in zip(["synthetic", "synthetic#2", "synthetic#3"], claims, strict=True)
    }
