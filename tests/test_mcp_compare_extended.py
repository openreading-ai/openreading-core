"""Truth and corpus comparisons preserve shared semantics and grant-bound retained inputs."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError
from referencing import Registry, Resource

from openreading import schemas
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import ResultContent, ResultError, ResultRecord
from openreading.comparison import compare
from openreading.comparison.corpus import corpus_pairs, corpus_report_dict
from openreading.mcp_server.comparison import compare_results
from openreading.mcp_server.results import deliver_result
from openreading.types.compare_tool import CompareError, CompareRequest
from tests.test_mcp_batch_contracts import batch
from tests.test_mcp_results import reconstruct
from tests.test_mcp_results import result_service as result_service
from tests.test_retained_results import provenance, response


def retain_inputs(store, corpus=False):
    payloads = [batch(), batch()] if corpus else [response(text="alpha"), response(text="beta")]
    kind = "batch_result" if corpus else "normalized_response"
    ids = [
        store.publish(kind, payload, provenance(source_sha256=[digest * 64])).result_id
        for payload, digest in zip(payloads, ["c", "d"], strict=True)
    ]
    return ids, payloads


def run(store, ids, **options):
    reply = compare_results(
        store, CompareRequest(result_ids=ids, **options), budget=4096, request_id=1
    )
    receipt = json.loads(reply.content[0].text)
    return receipt, store.load(receipt["result_id"])


def validator(schema):
    registry = Registry().with_resources(
        (doc["$id"], Resource.from_contents(doc))
        for doc in [
            schemas.response_schema(),
            schemas.comparison_report_schema(),
            schemas.corpus_report_schema(),
            schemas.batch_result_schema(),
        ]
    )
    return Draft202012Validator(schema, registry=registry)


@pytest.mark.parametrize("truth", [{"text": "alpha"}, {}, {"unrecognized": None}])
def test_truth_preserves_expected_values_and_shared_scores(result_service, truth):
    _, store = result_service
    ids, payloads = retain_inputs(store)
    before = {p: p.read_bytes() for p in store.root.iterdir()}
    receipt, content = run(store, ids, truth=truth, baseline=ids[0])
    assert content.payload == compare(payloads, baseline="synthetic", truth=truth)
    assert content.wire()["provenance"]["truth"] == truth
    scores = content.payload["truth"]["by_subject"]
    assert scores["synthetic"]["overall"] == (1.0 if "text" in truth else None)
    if "text" in truth:
        assert scores["synthetic#2"]["overall"] < 1
    record = json.loads(store.path(receipt["result_id"]).read_bytes())
    assert record["format"] == "retained-result.v0.4"
    validator(schemas.retained_result_schema()).validate(record)
    assert run(store, ids, truth=truth, baseline=ids[0])[0] == receipt
    assert run(store, ids, baseline=ids[0])[0]["result_id"] != receipt["result_id"]
    assert all(p.read_bytes() == data for p, data in before.items())


def test_truth_changes_identity_even_when_scores_match(result_service):
    _, store = result_service
    ids, _ = retain_inputs(store)
    a, content_a = run(store, ids, truth={"text": "ALPHA"})
    b, content_b = run(store, ids, truth={"text": "alpha"})
    assert content_a.payload == content_b.payload
    assert a["result_id"] != b["result_id"]
    assert content_a.provenance.request_sha256 != content_b.provenance.request_sha256


def test_corpus_preserves_shared_pairing_and_retained_subjects(result_service):
    _, store = result_service
    first, second = batch(), batch()
    # Earlier duplicates lose to the last succeeded row in the shared engine.
    duplicate = copy.deepcopy(first["items"][0])
    duplicate["response"] = response(text="discarded earlier duplicate")
    first["items"].insert(0, duplicate)
    extra = copy.deepcopy(second["items"][0])
    extra["source"]["filename"] = "third.pdf"
    second["items"].append(extra)
    first["summary"].update(total=3, succeeded=2)
    second["summary"].update(total=3, succeeded=2)
    payloads = [first, second, first]
    ids = [store.publish("batch_result", p, provenance()).result_id for p in payloads]
    before = {p: p.read_bytes() for p in store.root.iterdir()}
    receipt, content = run(store, ids)
    labels = ["run_1", "run_2", "run_3"]
    assert receipt["kind"] == "corpus_report"
    assert content.payload == corpus_report_dict(corpus_pairs(payloads, labels), payloads, labels)
    assert content.provenance.subjects == dict(zip(labels, ids, strict=True))
    assert content.provenance.adapters == dict.fromkeys(labels)
    assert content.provenance.source_sha256 == ["c" * 64] * 3
    assert all(
        s.verification == "producer_asserted" for s in content.provenance.subject_sources.values()
    )
    documents = content.payload["documents"]
    assert [d["source"]["filename"] for d in documents] == ["first.pdf", "third.pdf"]
    assert documents[0]["verdict"] == "equivalent"
    assert documents[1]["verdict"] == "unpaired"
    assert content.payload["rollup"]["documents"] == 2
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("case", ["truth", "empty_truth", "baseline", "mixed", "report"])
def test_corpus_refuses_invalid_combinations_before_engine_or_publication(
    result_service,
    monkeypatch,
    case,
):
    from openreading.mcp_server import comparison

    _, store = result_service
    ids, _ = retain_inputs(store, corpus=True)
    options = {}
    if case in {"truth", "empty_truth"}:
        options["truth"] = {"text": "secret"} if case == "truth" else {}
    elif case == "baseline":
        options["baseline"] = ids[0]
    elif case == "mixed":
        ids[1] = store.publish("normalized_response", response(), provenance()).result_id
    else:
        ids[1] = run(store, ids)[0]["result_id"]
    before = {p: p.read_bytes() for p in store.root.iterdir()}
    monkeypatch.setattr(
        comparison, "corpus_pairs", lambda *_: pytest.fail("invalid inputs reached engine")
    )
    with pytest.raises(CompareError, match="invalid_comparison"):
        run(store, ids, **options)
    assert {p: p.read_bytes() for p in store.root.iterdir()} == before


@pytest.mark.parametrize("corpus", [False, True])
def test_new_comparisons_budget_before_publication(result_service, corpus):
    _, store = result_service
    ids, _ = retain_inputs(store, corpus)
    options = {} if corpus else {"truth": {"text": "alpha"}}
    before = set(store.root.iterdir())
    with pytest.raises(ArtifactError, match="response_too_large"):
        compare_results(
            store, CompareRequest(result_ids=ids, **options), budget=4096, request_id='"界\\' * 3000
        )
    assert set(store.root.iterdir()) == before


@pytest.mark.parametrize("corpus", [False, True])
def test_new_reports_deliver_losslessly_and_reject_format_downgrade(result_service, corpus):
    _, store = result_service
    ids, _ = retain_inputs(store, corpus)
    options = {} if corpus else {"truth": {"text": "café界" * 1800}}
    receipt, content = run(store, ids, **options)
    record = json.loads(store.path(receipt["result_id"]).read_bytes())
    validator(schemas.retained_result_schema()).validate(record)
    for version in ["0.1", "0.2", "0.3"]:
        wrong = {**record, "format": "retained-result.v" + version}
        with pytest.raises(ValidationError):
            ResultRecord.model_validate(wrong)
        assert not validator(schemas.retained_result_schema()).is_valid(wrong)
    for mode, budget in [("auto", 1_000_000), ("file", 4096), ("fragments", 4096)]:
        cursor, pages = None, []
        while True:
            reply = deliver_result(
                store,
                receipt["result_id"],
                mode=mode,
                budget=budget,
                root=None,
                request_id=1,
                cursor=cursor,
            )
            page = json.loads(reply.content[0].text)
            validator(schemas.result_tool_schema()).validate(page)
            pages.append(page)
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        if mode == "auto":
            actual = pages[0]["content"]
        elif mode == "file":
            raw = Path(pages[0]["local_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == receipt["content_sha256"]
            assert len(raw) == receipt["content_bytes"]
            actual = json.loads(raw)
        else:
            actual = reconstruct(pages)
        assert actual == content.wire()


def test_corpus_publication_refuses_valid_local_artifact_subject(result_service):
    from openreading.artifacts.result_models import AttributedResultProvenance
    from tests.test_artifact_service import pdf

    service, store = result_service
    pdf(service.config.input_root / "sample.pdf")
    local = service.import_document("sample.pdf")
    manifest, _, _ = service.store.load_document(local.artifact_id)
    ids, _ = retain_inputs(store, corpus=True)
    _, content = run(store, ids)
    origin = content.provenance.model_dump(mode="json")
    origin["subjects"]["run_1"] = local.artifact_id
    origin["subject_sources"]["run_1"] = {
        "source_sha256": [manifest.document_sha256],
        "verification": "verified_source_bytes",
    }
    origin["source_sha256"][0] = manifest.document_sha256
    attributed = AttributedResultProvenance.model_validate(origin)
    # A real readable artifact and matching hashes leave only the corpus kind rule to refuse.
    before = {p: p.read_bytes() for p in service.config.artifact_root.rglob("*") if p.is_file()}
    with pytest.raises(ResultError, match="invalid_result"):
        store.publish("corpus_report", content.payload, attributed)
    assert {
        p: p.read_bytes() for p in service.config.artifact_root.rglob("*") if p.is_file()
    } == before


def test_corpus_publication_refuses_wrong_input_kind_and_hashes(result_service):
    _, store = result_service
    ids, _ = retain_inputs(store, True)
    _, content = run(store, ids)
    single = store.publish("normalized_response", response(), provenance()).result_id
    for damage in ["kind", "hash", "local", "unattributed", "payload", "nested"]:
        wire = content.wire()
        if damage == "kind":
            wire["provenance"]["subjects"]["run_1"] = single
        elif damage == "hash":
            wire["provenance"]["subject_sources"]["run_1"]["source_sha256"] = ["f" * 64]
            wire["provenance"]["source_sha256"][0] = "f" * 64
        elif damage == "local":
            wire["provenance"]["subjects"]["run_1"] = "or1_" + "a" * 64
        elif damage == "unattributed":
            del wire["provenance"]["subject_sources"]
        elif damage == "nested":
            wire["payload"]["documents"][0]["report"]["schema_version"] = "9.9"
        else:
            wire["payload"]["rollup"]["documents"] = -1
        before = set(store.root.iterdir())
        with pytest.raises((ResultError, ValueError, SchemaError)):
            value = ResultContent.model_validate(wire)
            store.publish(value.kind, value.payload, value.provenance)
        assert set(store.root.iterdir()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["local", "general"])
async def test_protocol_truth_and_corpus_work_in_both_profiles(result_service, profile):
    from mcp.shared.memory import create_connected_server_and_client_session

    from openreading.mcp_server.general import create_server as general_server
    from openreading.mcp_server.tools import create_server as local_server
    from tests.test_execution_jobs import jobs

    service, store = result_service
    server = local_server(service) if profile == "local" else general_server(jobs(service.store))
    async with create_connected_server_and_client_session(server) as session:
        catalog = {t.name: t for t in (await session.list_tools()).tools}
        assert len(catalog) == (13 if profile == "local" else 12)
        for corpus in [False, True]:
            ids, _ = retain_inputs(store, corpus)
            args = {"result_ids": ids, **({} if corpus else {"truth": {"text": "alpha"}})}
            Draft202012Validator(catalog["openreading_compare"].inputSchema).validate(args)
            reply = await session.call_tool("openreading_compare", args)
            assert not reply.isError
            receipt = json.loads(reply.content[0].text)
            Draft202012Validator(schemas.compare_tool_schema()).validate(receipt)
            content = store.load(receipt["result_id"])
            assert content.kind == ("corpus_report" if corpus else "comparison_report")
            fetched = await session.call_tool(
                "openreading_get_result", {"result_id": receipt["result_id"]}
            )
            assert json.loads(fetched.content[0].text)["content"] == content.wire()


@pytest.mark.parametrize("corpus", [False, True])
def test_engine_exceptions_never_echo_expected_values_or_publish(
    result_service, monkeypatch, corpus
):
    from openreading.mcp_server import comparison

    _, store = result_service
    ids, _ = retain_inputs(store, corpus)
    before = set(store.root.iterdir())

    def fail(*args, **kwargs):
        raise RuntimeError("secret expected values")

    monkeypatch.setattr(comparison, "corpus_report_dict" if corpus else "build_report", fail)
    with pytest.raises(CompareError, match="comparison_failed") as error:
        run(store, ids, **({} if corpus else {"truth": {"text": "secret expected values"}}))
    assert "secret" not in json.dumps(error.value.wire())
    assert set(store.root.iterdir()) == before


def test_scored_provenance_refuses_missing_truth_section_or_wrong_dimensions(result_service):
    _, store = result_service
    ids, _ = retain_inputs(store)
    _, content = run(store, ids, truth={"text": "alpha"})
    for truth in [None, {"dimensions": []}]:
        wire = content.wire()
        if truth is None:
            del wire["payload"]["truth"]
        else:
            wire["payload"]["truth"].update(truth)
        with pytest.raises(ValueError):
            ResultContent.model_validate(wire)


@pytest.mark.asyncio
async def test_stdio_new_comparisons_survive_restart(result_service):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    service, store = result_service
    singles, _ = retain_inputs(store)
    batches, _ = retain_inputs(store, True)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openreading.mcp_server.main",
            "--profile",
            "general-execution-v1",
            "--input-root",
            str(service.config.input_root),
            "--artifact-root",
            str(service.config.artifact_root),
            "--execute-backend",
            "pymupdf",
        ],
    )
    receipts = []
    for iteration in range(2):
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            current = []
            for ids, options in [(singles, {"truth": {"text": "café界"}}), (batches, {})]:
                reply = await session.call_tool(
                    "openreading_compare", {"result_ids": ids, **options}
                )
                assert not reply.isError
                receipt = json.loads(reply.content[0].text)
                current.append(receipt)
                fetched = await session.call_tool(
                    "openreading_get_result",
                    {"result_id": receipt["result_id"], "delivery": "file"},
                )
                export = json.loads(fetched.content[0].text)
                assert Path(export["local_path"]).read_bytes() == json_bytes(
                    store.load(receipt["result_id"]).wire()
                )
            if iteration:
                assert current == receipts
            receipts = current


@pytest.mark.parametrize("corpus", [False, True])
def test_new_comparisons_reject_foreign_and_corrupted_inputs_before_engine(
    result_service, tmp_path, monkeypatch, corpus
):
    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.results import RetainedResults
    from openreading.artifacts.store import Store
    from openreading.mcp_server import comparison

    _, store = result_service
    ids, _ = retain_inputs(store, corpus)
    root = tmp_path / "foreign"
    root.mkdir()
    foreign_store = Store(ProfileConfig(root, store.store.config.artifact_root))
    options = {} if corpus else {"truth": {"text": "alpha"}}
    monkeypatch.setattr(comparison, "corpus_pairs", lambda *_: pytest.fail("unverified corpus"))
    monkeypatch.setattr(
        comparison, "build_report", lambda *_a, **_kw: pytest.fail("unverified truth")
    )
    try:
        foreign = RetainedResults(foreign_store)
        with pytest.raises(ResultError, match="result_not_found"):
            run(foreign, ids, **options)
        foreign.path(ids[0]).write_bytes(store.path(ids[0]).read_bytes())
        with pytest.raises(ResultError, match="result_corrupt"):
            run(foreign, ids, **options)
        assert list(foreign.root.iterdir()) == [foreign.path(ids[0])]
    finally:
        foreign_store.close()
    store.path(ids[1]).write_bytes(b"secret corrupted bytes")
    before = set(store.root.iterdir())
    with pytest.raises(ResultError, match="result_corrupt"):
        run(store, ids, **options)
    assert set(store.root.iterdir()) == before
