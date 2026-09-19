"""Batch execution preserves shared aggregation under the existing MCP authority and lifecycle."""

import importlib.util
import json

import pytest

from openreading.artifacts.results import RetainedResults
from openreading.mcp_server.execution import ExecutionRefused
from tests.test_execution_jobs import content, jobs, request, terminal
from tests.test_execution_jobs import store as store


def test_batch_executor_exists():
    assert importlib.util.find_spec("openreading.mcp_server.batch_execution") is not None


def test_batch_authorizes_every_request_before_source_acquisition(store, monkeypatch):
    manager = jobs(store)
    monkeypatch.setattr(
        store, "source", lambda *_: pytest.fail("acquired before all authorization")
    )
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        manager.start_batch({"requests": [request(), request("reducto")]})
    assert not list(manager.root.glob("ej1_*"))


def test_batch_shared_order_errors_and_provider_outcome(store, monkeypatch):
    from openreading.mcp_server import batch_execution as module

    requests = [request(), request(), request()]
    for item, name in zip(requests, ["first.pdf", "second.pdf", "third.pdf"], strict=True):
        item["document"]["filename"] = name
    observed = []

    def execute(attempt, value, **kwargs):
        observed.append(value)
        if len(observed) == 2:
            raise RuntimeError("secret document text")
        return content("failed" if len(observed) == 1 else "partial")

    monkeypatch.setattr(module.ExecutionAttempt, "run", execute)
    result = module.execute_batch(
        store,
        jobs(store).authority,
        {"requests": requests},
        environment={},
        check=lambda: None,
        on_attempt=lambda *_: None,
    )
    assert [item["document"]["filename"] for item in observed] == [
        "first.pdf",
        "second.pdf",
        "third.pdf",
    ]
    assert result.kind == "batch_result"
    assert result.payload["status"]["state"] == "partial"
    rows = result.payload["items"]
    assert [r["state"] for r in rows] == ["succeeded", "failed", "succeeded"]
    assert rows[0]["response"] == content("failed").payload
    assert rows[2]["response"] == content("partial").payload
    assert rows[1]["error"]["code"] == "execution_failed"
    assert "secret document text" not in json.dumps(result.wire())
    assert rows[1]["source"]["filename"] == "second.pdf"


def test_batch_cancellation_stops_next_item_and_has_no_retained_result(store, monkeypatch):
    from openreading.mcp_server import batch_execution as module
    from openreading.mcp_server.execution_process import ExecutionError

    ran = []

    def check():
        if ran:
            raise ExecutionError("cancelled")

    def execute(*args, **kwargs):
        ran.append(1)
        return content()

    monkeypatch.setattr(module.ExecutionAttempt, "run", execute)
    with pytest.raises(ExecutionError, match="cancelled"):
        module.execute_batch(
            store,
            jobs(store).authority,
            {"requests": [request(), request()]},
            environment={},
            check=check,
            on_attempt=lambda *_: None,
        )
    assert ran == [1]
    assert not list(RetainedResults(store).root.glob("*.json"))


@pytest.mark.parametrize("requests", [[], [request(), request("strategy:local")]])
def test_real_batch_survives_reconnect_and_retains_complete_result(store, requests):
    manager = jobs(store)
    initial = manager.start_batch({"requests": requests})
    assert initial.state == "queued"
    restarted = jobs(store)
    final = terminal(restarted, initial.job_id)
    assert final.state == "succeeded", final.wire()
    assert final.receipt.kind == "batch_result"
    loaded = RetainedResults(store).load(final.receipt.result_id)
    assert loaded.payload["summary"]["total"] == len(requests)
    assert final.response_state == ("succeeded" if requests else "failed")
    if requests:
        assert all(
            "OpenReading Test Document" in item["response"]["document"]["text"]
            for item in loaded.payload["items"]
        )
        assert len(loaded.provenance.source_sha256) == 2
    else:
        assert loaded.payload["warnings"][0]["code"] == "empty_batch"


@pytest.mark.parametrize("value", [{}, {"requests": {}}, {"requests": [], "extra": 1}])
def test_batch_rejects_malformed_wrapper(store, value):
    from openreading.mcp_server.batch_execution import authorize_batch

    with pytest.raises(ExecutionRefused, match="invalid_request"):
        authorize_batch(jobs(store).authority, value)


def test_source_disappearance_after_acceptance_is_one_failed_item(store):
    from openreading.mcp_server.execution_jobs import slot

    root = store.config.input_root
    (root / "vanishes.pdf").write_bytes((root / "sample.pdf").read_bytes())
    missing = request()
    missing["document"]["path"] = "vanishes.pdf"
    manager = jobs(store)
    with slot(manager.root, 0):
        first = manager.start_batch({"requests": [missing, request()]})
        (root / "vanishes.pdf").unlink()
    done = terminal(manager, first.job_id)
    assert done.state == "succeeded", done.wire()
    assert done.response_state == "partial"
    result = RetainedResults(store).load(done.receipt.result_id)
    assert [item["state"] for item in result.payload["items"]] == ["failed", "succeeded"]
    assert result.payload["items"][0]["error"]["code"] == "input_not_found"


def test_batch_recovery_uses_same_record_format_as_publication(store):
    from openreading.artifacts.models import json_bytes
    from openreading.mcp_server.execution_jobs import _write_bound
    from tests.test_execution_jobs import prepared
    from tests.test_mcp_batch_contracts import batch
    from tests.test_retained_results import provenance

    manager = jobs(store)
    root = prepared(manager)
    results = RetainedResults(store)
    receipt = results.publish("batch_result", batch(), provenance())
    _write_bound(root, "publication.json", store.grant, {"receipt": receipt.wire()})
    recovered = manager.get(root.name)
    assert recovered.state == "succeeded"
    assert recovered.receipt == receipt
    assert recovered.response_state == batch()["status"]["state"]
    assert (
        json.loads(results.path(receipt.result_id).read_bytes())["format"] == "retained-result.v0.3"
    )
    assert (
        json_bytes(results.record(results.load(receipt.result_id)).wire())
        == results.path(receipt.result_id).read_bytes()
    )


@pytest.mark.parametrize("code", ["cancelled", "timeout", "os_permission_denied"])
def test_fatal_worker_control_error_stops_batch_without_next_attempt(store, monkeypatch, code):
    from openreading.mcp_server import batch_execution as module
    from openreading.mcp_server.execution_process import ExecutionError

    observed = []

    def execute(*_args, **_kwargs):
        observed.append(1)
        raise ExecutionError(code)

    monkeypatch.setattr(module.ExecutionAttempt, "run", execute)
    with pytest.raises(ExecutionError, match=code):
        module.execute_batch(
            store,
            jobs(store).authority,
            {"requests": [request(), request()]},
            environment={},
            check=lambda: None,
            on_attempt=lambda *_: None,
        )
    assert observed == [1]


def test_batch_conflicting_adapter_versions_are_unmeasured(store, monkeypatch):
    from openreading.mcp_server import batch_execution as module

    responses = [content(), content()]
    responses[0].provenance.adapters["synthetic"] = "one"
    responses[1].provenance.adapters["synthetic"] = "two"
    responses[0].provenance.source_sha256 = ["a" * 64]
    responses[1].provenance.source_sha256 = ["b" * 64]
    iterator = iter(responses)
    monkeypatch.setattr(module.ExecutionAttempt, "run", lambda *_args, **_kwargs: next(iterator))
    result = module.execute_batch(
        store,
        jobs(store).authority,
        {"requests": [request(), request()]},
        environment={},
        check=lambda: None,
        on_attempt=lambda *_: None,
    )
    assert result.provenance.adapters == {"synthetic": None}
    assert result.provenance.source_sha256 == ["a" * 64, "b" * 64]
    assert [item["source"]["sha256"] for item in result.payload["items"]] == ["a" * 64, "b" * 64]


@pytest.mark.parametrize("when", ["before", "after"])
def test_batch_cancellation_publication_race(store, monkeypatch, when):
    from openreading.mcp_server import execution_jobs as module
    from tests.test_execution_jobs import prepared

    manager = jobs(store)
    root = prepared(manager)
    control = module._read_bound(root, "request.json", store.grant, None)
    control.update(operation="batch", request={"requests": [request(), request()]})
    module._write_bound(root, "request.json", store.grant, control)
    monkeypatch.setattr(module.ExecutionAttempt, "run", lambda *_a, **_kw: content())
    original_write, original_publish = module._write_bound, module.RetainedResults.publish

    def write(path, name, grant, payload):
        original_write(path, name, grant, payload)
        if when == "before" and name == "publication.json":
            original_write(root, "cancel.json", grant, {})

    def publish(*args, **kwargs):
        receipt = original_publish(*args, **kwargs)
        if when == "after":
            original_write(root, "cancel.json", store.grant, {})
        return receipt

    monkeypatch.setattr(module, "_write_bound", write)
    monkeypatch.setattr(module.RetainedResults, "publish", publish)
    module.run(root)
    final = manager.get(root.name)
    assert final.state == ("cancelled" if when == "before" else "succeeded")
    assert final.cancel_requested
    assert bool(final.receipt) == (when == "after")
    assert len(list(RetainedResults(store).root.glob("*.json"))) == (when == "after")
    if final.receipt:
        assert final.receipt.kind == "batch_result"
        assert final.response_state == "succeeded"


def test_batch_supervisor_reauthorizes_recorded_requests(store, monkeypatch):
    from openreading.mcp_server import execution_jobs as module
    from tests.test_execution_jobs import prepared

    manager = jobs(store)
    root = prepared(manager)
    control = module._read_bound(root, "request.json", store.grant, None)
    control.update(operation="batch", request={"requests": [request("reducto")]})
    module._write_bound(root, "request.json", store.grant, control)
    monkeypatch.setattr(
        module.ExecutionAttempt, "run", lambda *_a, **_kw: pytest.fail("unscoped child")
    )
    assert module.run(root) == 1
    final = manager.get(root.name)
    assert final.state == "failed"
    assert final.error.code == "scope_denied"
    assert final.receipt is None
