"""Every execution path finalizes through `openreading.router.finalize` (D1, laws SX1, SX2, SX4).

A spy adapter records the requests it receives in `submit` and in `normalize`. Each test drives
one surface that turns an adapter's output into a response, and shows that the response carries
`schema_check` while the adapter never saw an `x-read-*` keyword. A source scan keeps any new
call site from skipping the helper.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from openreading import api, schemas
from openreading.adapters.base import BackendAdapter
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.credentials import EnvCredentialBroker
from openreading.derive.fields import has_annotations
from openreading.router import finalize
from openreading.router.clock import FakeClock
from openreading.router.executor import BoundedResultCache, execute_plan
from openreading.router.registry import Registry
from openreading.router.router import RoutePlan, RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import (
    BackendInfo,
    Document,
    Health,
    Job,
    JobState,
    NormalizedResponse,
    RawResult,
    ResponseState,
    Status,
    WaitMode,
)
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import BatchIntake
from openreading.types.request import OpenReadingRequest
from tests.fakes import make_backend
from tests.test_schema_check import LAB_STYLE_SCHEMAS

SRC = Path(__file__).resolve().parents[1] / "src" / "openreading"
PDF = build_sample_pdf()
PDF_B64 = base64.b64encode(PDF).decode()

SCHEMA = {
    "type": "object",
    "properties": {
        "total": {"type": "number", "x-read-criticality": "critical"},
        "payee": {"type": "string", "x-read-criticality": "optional"},
    },
}


class Spy(BackendAdapter):
    """An extraction-capable fake that records every request it is handed."""

    def __init__(
        self,
        backend_id: str = "spy-extract",
        *,
        fields: dict[str, Any] | None = None,
        wait: WaitMode = WaitMode.INLINE,
        native: bool = False,
    ) -> None:
        desc = make_backend(backend_id, custom_schema=True, input_formats=["pdf"]).descriptor
        update: dict[str, Any] = {"wait_modes": [wait]}
        if native:
            update["batch"] = BatchIntake(native="claimed")
        self.descriptor = desc.model_copy(update=update)
        self._wait = wait
        self.fields = {"payee": {"value": "ACME"}} if fields is None else fields
        self.submitted: list[OpenReadingRequest] = []
        self.normalized: list[OpenReadingRequest] = []

    def health(self) -> Health:
        return Health(ready=True)

    def submit(self, req: OpenReadingRequest, ctx) -> Job:
        self.submitted.append(req)
        if self._wait is WaitMode.POLL:
            return self.new_job(WaitMode.POLL, state=JobState.RUNNING, next_poll_at=0.0)
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(payload={})
        return job

    def poll(self, job: Job, ctx) -> Job:
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(payload={})
        return job

    def normalize(self, job: Job, ctx, req: OpenReadingRequest) -> NormalizedResponse:
        self.normalized.append(req)
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(text="spy"),
            typed_fields=self.fields or None,
        )

    def report_cost(self, job: Job) -> CostReport:
        return infra_only("page", 1.0)

    # native batch protocol, used only when the descriptor claims it
    def submit_many(self, reqs, ctx) -> Job:
        self.submitted.extend(reqs)
        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(payload={})
        return job

    def normalize_many(self, job, reqs, credentials=None):
        return [self.normalize(job, None, r) for r in reqs]

    def seen(self) -> list[OpenReadingRequest]:
        return self.submitted + self.normalized


def _no_annotations(reqs: list[OpenReadingRequest]) -> None:
    assert reqs, "the spy saw no request"
    for r in reqs:
        es = r.extraction_schema
        if es is not None and es.json_schema is not None:
            assert not has_annotations(es.json_schema)


def _req(schema=SCHEMA, backend="spy-extract", **over) -> OpenReadingRequest:
    body: dict[str, Any] = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": backend},
    }
    if schema is not None:
        body["extraction_schema"] = {"json_schema": schema}
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _assert_checked(resp: dict) -> None:
    schemas.validate_response(resp)
    check = resp["schema_check"]
    assert check["critical_ok"] is False
    assert {f["path"]: f["status"] for f in check["fields"]} == {
        "total": "missing",
        "payee": "present",
    }
    codes = [(w["code"], w.get("field")) for w in resp.get("warnings", [])]
    assert ("critical_field_missing", "total") in codes


@pytest.fixture
def spy(monkeypatch):
    adapter = Spy()
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: adapter)
    return adapter


# --------------------------------------------------------------------------- every D1 site
def test_run_request(spy):
    resp = api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": SCHEMA})
    _assert_checked(resp)
    _no_annotations(spy.seen())
    assert resp["status"]["state"] == "succeeded"  # SX2: a failed check changes no status


def test_execute_plan(spy):
    resp = execute_plan(RoutePlan(chosen=spy), _req(), broker=EnvCredentialBroker({}))
    _assert_checked(resp.to_schema_dict())
    _no_annotations(spy.seen())


def _strategy(cfg: dict, *backends: Spy):
    reg = Registry()
    for b in backends:
        reg.register(b)
    r = _req(backend="strategy:s")
    compiled = compile_strategy(r, "s", StrategyConfig.model_validate(cfg), reg, RouterConfig())
    return run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())


def test_strategy_leaf():
    leaf = Spy()
    res = _strategy({"version": 1, "strategies": {"s": {"steps": ["spy-extract"]}}}, leaf)
    _assert_checked(res.response.to_schema_dict())
    _no_annotations(leaf.seen())


def test_strategy_merge_rechecks_the_merged_result():
    a = Spy("spy-a", fields={"payee": {"value": "ACME"}})
    b = Spy("spy-b", fields={"total": {"value": 10}})
    cfg = {
        "version": 1,
        "strategies": {"s": {"parallel": ["spy-a", "spy-b"], "pick": "merge"}},
    }
    resp = _strategy(cfg, a, b).response
    assert set(resp.typed_fields) == {"payee", "total"}
    status = {f.path: f.status for f in resp.schema_check.fields}
    assert status == {"total": "present", "payee": "present"}
    assert resp.schema_check.critical_ok
    assert not any(w.code == "critical_field_missing" for w in resp.warnings or [])


def test_native_batch_checks_each_item(tmp_path, monkeypatch):
    native = Spy(native=True)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: native)
    for i in range(2):
        (tmp_path / f"doc{i}.pdf").write_bytes(PDF)
    env = api.run_batch(
        [str(tmp_path)], backend="spy-extract", extraction_schema={"json_schema": SCHEMA}
    )
    assert [i["transport"] for i in env["items"]] == ["native", "native"]
    for item in env["items"]:
        _assert_checked(item["response"])
    _no_annotations(native.seen())


def test_evals_run_case(monkeypatch):
    from openreading.evals.dataset import EvalCase
    from openreading.evals.runner import run_case

    checked: list[NormalizedResponse] = []
    real = finalize.apply_schema_check
    monkeypatch.setattr(
        finalize, "apply_schema_check", lambda r, q: checked.append(real(r, q)) or checked[-1]
    )
    leaf = Spy()
    case = EvalCase(
        name="c", request_body=_req().to_schema_dict(), expected={"text_contains": ["spy"]}
    )
    result = run_case(leaf, case)
    assert result.error is None
    _assert_checked(checked[0].to_schema_dict())
    _no_annotations(leaf.seen())


def test_calibrate(tmp_path, monkeypatch):
    from openreading.strategies.calibrate import calibrate_strategy

    checked: list[NormalizedResponse] = []
    real = finalize.apply_schema_check
    monkeypatch.setattr(
        finalize, "apply_schema_check", lambda r, q: checked.append(real(r, q)) or checked[-1]
    )
    case = tmp_path / "case_00"
    case.mkdir()
    (case / "case.json").write_text(
        json.dumps(
            {
                "name": "c0",
                "input": {"builtin_sample": True},
                "extraction_schema": {"json_schema": SCHEMA},
                "expected": {"text_contains": ["spy"]},
            }
        )
    )
    cheap, premium = Spy("cheap"), Spy("premium")
    reg = Registry()
    reg.register(cheap)
    reg.register(premium)
    cfg = StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "steps": [
                        {"backend": "cheap", "escalate_if": {"confidence_below": 0.5}},
                        "premium",
                    ]
                }
            },
        }
    )
    calibrate_strategy(str(tmp_path), cfg, "s", reg)
    _assert_checked(checked[0].to_schema_dict())
    _no_annotations(cheap.seen())


def test_server_parse(spy):
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    body = _req().to_schema_dict()
    r = TestClient(create_app()).post("/v1/parse", json=body)
    assert r.status_code == 200, r.text
    _assert_checked(r.json())
    _no_annotations(spy.seen())


def test_server_async_job_checks_against_the_stored_labels(monkeypatch):
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    poller = Spy(wait=WaitMode.POLL)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: poller)
    client = TestClient(create_app())
    created = client.post("/v1/jobs", json=_req().to_schema_dict())
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    rec = client.app.state.jobs[job_id]
    assert "x-read-criticality" in json.dumps(rec.req.to_schema_dict())  # labels kept
    done = client.get(f"/v1/jobs/{job_id}").json()
    _assert_checked(done["response"])
    _no_annotations(poller.seen())


def test_server_rejects_an_unknown_code_with_400_before_any_call(spy):
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    body = _req().to_schema_dict()
    body["extraction_schema"]["json_schema"]["properties"]["total"]["x-read-criticality"] = (
        "crtical"
    )
    r = TestClient(create_app()).post("/v1/parse", json=body)
    assert r.status_code == 400
    assert "crtical" in r.text
    assert spy.seen() == []


# --------------------------------------------------------------------------- SX6 on CLI and Python
def test_python_raises_validation_error_before_any_call(spy):
    from pydantic import ValidationError

    bad = {"properties": {"total": {"x-read-criticality": "crtical"}}}
    with pytest.raises(ValidationError):
        api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": bad})
    assert spy.seen() == []


def test_cli_exits_2_before_any_call(spy, tmp_path, capsys):
    from openreading.cli.app import main

    schema = tmp_path / "s.json"
    schema.write_text(json.dumps({"properties": {"total": {"x-read-criticality": "crtical"}}}))
    doc = tmp_path / "d.pdf"
    doc.write_bytes(PDF)
    assert main(["parse", str(doc), "--backend", "spy-extract", "--schema", str(schema)]) == 2
    assert "crtical" in capsys.readouterr().err
    assert spy.seen() == []


# --------------------------------------------------------------------------- SX1
def test_sx1_no_extraction_schema_means_no_check(spy):
    resp = api.run(PDF, backend="spy-extract", outputs={"typed_fields": True})
    assert "schema_check" not in resp
    assert all(r.extraction_schema is None for r in spy.seen())


def test_instructions_only_gets_the_generic_check(spy):
    # D12: instructions without a schema is generic-schema extraction at the ingress.
    resp = api.run(PDF, backend="spy-extract", extraction_schema={"instructions": "fields"})
    assert resp["schema_check"]["schema_ref"] == "bank:generic@v1"


def test_a_schema_without_labels_is_critical_ok(spy):
    plain = finalize.strip_request(_req()).extraction_schema.json_schema
    resp = api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": plain})
    assert resp["schema_check"]["critical_ok"] is True
    assert "warnings" not in resp or not any(
        w["code"].startswith("critical_field") for w in resp["warnings"]
    )


# --------------------------------------------------------------------------- SX4
@pytest.mark.parametrize("schema", LAB_STYLE_SCHEMAS)
def test_sx4_backends_never_see_x_read(spy, schema):
    resp = api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": schema})
    assert "schema_check" in resp
    _no_annotations(spy.seen())


# --------------------------------------------------------------------------- SX2 and the cache
def test_sx2_a_failed_check_never_moves_the_chain():
    first, second = Spy("spy-a"), Spy("spy-b")
    cache = BoundedResultCache()
    plan = RoutePlan(chosen=first, fallbacks=[second])
    resp = execute_plan(plan, _req(), broker=EnvCredentialBroker({}), cache=cache)
    assert resp.status.state is ResponseState.SUCCEEDED
    assert resp.schema_check.critical_ok is False
    assert second.seen() == []
    assert len(cache._d) == 1


def test_changing_only_a_label_hits_the_cache_and_recomputes_the_check():
    adapter = Spy(fields={"payee": {"value": "ACME"}, "total": {"value": 10}})
    cache = BoundedResultCache()
    broker = EnvCredentialBroker({})
    first = execute_plan(RoutePlan(chosen=adapter), _req(), broker=broker, cache=cache)
    assert {f.path: f.criticality for f in first.schema_check.fields}["payee"] == "optional"

    relabelled = json.loads(json.dumps(SCHEMA))
    relabelled["properties"]["payee"]["x-read-criticality"] = "critical"
    relabelled["properties"]["total"]["x-read-criticality"] = "critical"
    second = execute_plan(RoutePlan(chosen=adapter), _req(relabelled), broker=broker, cache=cache)
    assert len(adapter.submitted) == 1  # the paid result was reused
    assert any(w.code == "idempotent_replay" for w in second.warnings or [])
    assert {f.path: f.criticality for f in second.schema_check.fields}["payee"] == "critical"
    assert second.schema_check.schema_hash != first.schema_check.schema_hash
    assert [w.code for w in second.warnings or []].count("intent_all_critical") == 1


# --------------------------------------------------------------------------- every adapter path
def _fixture_adapters():
    """Every fixture-driven adapter that returns typed_fields, with a request that reaches it."""
    from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
    from openreading.adapters.google_gemini import GoogleGeminiAdapter
    from tests.test_anthropic_claude import FakeClaudeClient
    from tests.test_google_gemini import FakeGeminiClient

    return [
        ("anthropic-claude", lambda: AnthropicClaudeAdapter(client=FakeClaudeClient("extract"))),
        ("google-gemini", lambda: GoogleGeminiAdapter(client=FakeGeminiClient())),
        ("spy-extract", lambda: Spy()),
    ]


@pytest.mark.parametrize(
    "name,factory", _fixture_adapters(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_adapter_path_carries_a_check(name, factory):
    from openreading.types.runtime import RunContext

    adapter = factory()
    req = _req(backend=name)
    ctx = RunContext()
    resp = finalize.finalize_response(adapter, finalize.submit(adapter, req, ctx), ctx, req)
    assert resp.typed_fields
    assert resp.schema_check is not None
    assert resp.schema_check.schema_hash.startswith("sha256:")


def _module_runs():
    """(label, run) pairs: each run drives one adapter's own test helper over its fixture and
    returns (response, request). The check then needs nothing but those two."""
    import tests.test_chunkr as chunkr
    import tests.test_google_document_ai as docai
    import tests.test_mistral_ocr as mistral
    import tests.test_nuextract as nuextract
    import tests.test_qwen_vl as qwen
    import tests.test_reducto as reducto
    from openreading.adapters.chunkr import ChunkrAdapter
    from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
    from openreading.adapters.mistral_ocr import MistralOCRAdapter
    from openreading.adapters.nuextract import NuExtractAdapter
    from openreading.adapters.qwen_vl import QwenVLAdapter
    from openreading.adapters.reducto import ReductoAdapter

    def run_reducto():
        req = reducto._req(op="extract")
        return reducto._run_sync(ReductoAdapter(client=reducto.FakeReductoClient()), req), req

    def run_chunkr():
        req = chunkr._req(
            backend={"id": "chunkr", "operation": "extract"},
            extraction_schema={"json_schema": {"type": "object"}},
        )
        return chunkr._run(ChunkrAdapter(client=chunkr.FakeChunkrClient()), req), req

    def run_nuextract():
        req = nuextract._extract_req()
        return nuextract._run(NuExtractAdapter(client=nuextract.FakeNuExtractClient()), req), req

    def run_qwen():
        req = qwen._req(extraction_schema={"json_schema": {"type": "object"}})
        adapter = QwenVLAdapter(client=qwen.FakeQwenClient(fixture="extract_response"))
        return qwen._run(adapter, req), req

    def run_docai():
        req = docai._req()
        return docai._run(GoogleDocumentAIAdapter(client=docai.FakeDocAIClient()), req), req

    def run_mistral():
        req = mistral._req(extraction_schema={"json_schema": {"type": "object"}})
        resp, _ = mistral._run(MistralOCRAdapter(client=mistral.FakeMistralOCRClient()), req)
        return resp, req

    return [
        ("reducto", run_reducto),
        ("chunkr", run_chunkr),
        ("nuextract", run_nuextract),
        ("qwen-vl", run_qwen),
        ("google-document-ai", run_docai),
        ("mistral-ocr", run_mistral),
    ]


@pytest.mark.parametrize("name,run", _module_runs(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_fixture_adapter_reply_takes_the_same_check(name, run):
    """No branch on backend type: the one check reads any adapter's typed_fields."""
    resp, req = run()
    assert resp.typed_fields, name
    labelled = req.model_copy(update={"extraction_schema": _req().extraction_schema}, deep=True)
    finalize.apply_schema_check(resp, labelled)
    assert [f.path for f in resp.schema_check.fields] == ["total", "payee"]
    assert set(resp.schema_check.extra) <= set(resp.typed_fields)
    schemas.validate_response(resp.to_schema_dict())


# --------------------------------------------------------------------------- one finalize path
# Receivers that are not adapters: the module of helpers itself and a thread pool.
_NOT_AN_ADAPTER = {"finalize", "pool"}


def _adapter_method_uses(method_names: set[str], allowed: set[str]) -> list[str]:
    """Every `<x>.<method>` reference (called or passed on) in `src/` outside the allowed files."""
    import ast

    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("adapters/") or rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Attribute) and node.attr in method_names):
                continue
            receiver = node.value.id if isinstance(node.value, ast.Name) else None
            if receiver not in _NOT_AN_ADAPTER:
                found.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
    return found


def test_no_direct_normalize_call_outside_finalize():
    offenders = _adapter_method_uses(
        {"normalize"}, {"router/finalize.py", "testing/conformance.py"}
    )
    assert offenders == [], "call openreading.router.finalize instead:\n" + "\n".join(offenders)


def test_no_direct_submit_call_outside_finalize():
    offenders = _adapter_method_uses({"submit", "submit_many"}, {"router/finalize.py"})
    assert offenders == [], "call openreading.router.finalize.submit instead:\n" + "\n".join(
        offenders
    )


# --------------------------------------------------------------------------- images
_PNG_SCHEMA = {
    "type": "object",
    "properties": {
        "gross_pay": {"type": "string", "x-read-criticality": "critical"},
        "total": {"type": "number"},
    },
}


def _png_req(backend: str) -> OpenReadingRequest:
    from tests.test_openai_compatible import PNG_1PX

    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(PNG_1PX).decode(),
                "mime_type": "image/png",
            },
            "backend": {"id": backend},
            "extraction_schema": {"json_schema": _PNG_SCHEMA},
        }
    )


def test_a_png_through_anthropic_claude_carries_a_check():
    from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
    from openreading.types.runtime import RunContext
    from tests.test_anthropic_claude import FakeClaudeClient

    client = FakeClaudeClient("extract")
    adapter = AnthropicClaudeAdapter(client=client)
    req, ctx = _png_req("anthropic-claude"), RunContext()
    resp = finalize.finalize_response(adapter, finalize.submit(adapter, req, ctx), ctx, req)
    assert client.last_call["messages"][0]["content"][0]["type"] == "image"
    assert "x-read-" not in json.dumps(client.last_call["tools"])
    status = {f.path: f.status for f in resp.schema_check.fields}
    assert status["gross_pay"] == "present"
    assert resp.typed_fields["gross_pay"].type == "string"


def test_a_png_through_openai_compatible_carries_a_check():
    import httpx
    import respx

    from openreading.adapters.openai_compatible import OpenAICompatibleAdapter
    from tests.test_openai_compatible import CHAT, KEYED, _ctx, _reply

    with respx.mock:
        route = respx.post(CHAT).mock(
            return_value=httpx.Response(200, json=_reply(json.dumps({"total": "ten"})))
        )
        adapter = OpenAICompatibleAdapter(KEYED)
        req, ctx = _png_req(KEYED.name), _ctx()
        resp = finalize.finalize_response(adapter, finalize.submit(adapter, req, ctx), ctx, req)
        sent = json.loads(route.calls.last.request.content)
    assert sent["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png")
    assert "x-read-" not in json.dumps(sent["response_format"])
    assert resp.status.state is ResponseState.SUCCEEDED
    status = {f.path: f.status for f in resp.schema_check.fields}
    assert status == {"gross_pay": "missing", "total": "invalid"}
    assert not resp.schema_check.valid
