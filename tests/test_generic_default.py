"""The generic default (design D12): where `apply_default` runs, where it never runs, and what
each backend receives for `--extract` without `--schema`.

The ingress tests drive the CLI, Python and HTTP entry points against a spy adapter. The reload
tests show a stored request is taken literally. The per-adapter tests reuse each adapter's own
test helpers and show the outgoing body carries the `generic` schema.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
import respx

from openreading import api, schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.cli.app import main
from openreading.router import finalize
from openreading.schema_bank import apply_default, resolve
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import WaitMode
from openreading.types.request import ExtractionSchema, OpenReadingRequest
from tests.test_finalize_paths import SCHEMA, Spy

PDF = build_sample_pdf()
PDF_B64 = base64.b64encode(PDF).decode()
GENERIC = resolve("generic").schema
REF = "bank:generic@v1"
INSTR = {"instructions": "read the totals"}


def _has_generic(obj: Any) -> bool:
    """True when `obj` carries every top-level property of the generic schema."""
    text = json.dumps(obj)
    return all(f'"{name}"' in text for name in GENERIC["properties"])


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "spy-extract"},
        **extra,
    }


@pytest.fixture
def spy(monkeypatch):
    adapter = Spy()
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: adapter)
    return adapter


@pytest.fixture
def client(spy):
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    return TestClient(create_app())


# --------------------------------------------------------------------------- apply_default
def test_apply_default_fills_only_an_instructions_only_request():
    req = OpenReadingRequest.model_validate(_body(extraction_schema=INSTR))
    out = apply_default(req)
    assert out.extraction_schema.json_schema == GENERIC
    assert out.extraction_schema.instructions == "read the totals"
    assert req.extraction_schema.json_schema is None  # never mutates


def test_apply_default_leaves_every_other_request_alone():
    plain = OpenReadingRequest.model_validate(_body(outputs={"typed_fields": True}))
    assert apply_default(plain) is plain
    own = OpenReadingRequest.model_validate(_body(extraction_schema={"json_schema": SCHEMA}))
    assert apply_default(own).extraction_schema.json_schema == SCHEMA


def test_generic_is_a_copy_the_caller_may_change():
    req = OpenReadingRequest.model_validate(_body(extraction_schema=INSTR))
    out = apply_default(req)
    out.extraction_schema.json_schema["properties"].clear()
    assert apply_default(req).extraction_schema.json_schema == GENERIC


def test_model_validate_alone_never_adds_a_schema():
    req = OpenReadingRequest.model_validate(_body(extraction_schema=INSTR))
    assert req.extraction_schema == ExtractionSchema(instructions="read the totals")
    again = OpenReadingRequest.model_validate(req.model_dump(mode="json"))
    assert again == req
    assert again.extraction_schema.json_schema is None


# --------------------------------------------------------------------------- the three surfaces
def test_cli_extract_alone_sends_generic_and_reports_its_ref(spy, tmp_path, capsys):
    doc = tmp_path / "d.pdf"
    doc.write_bytes(PDF)
    assert main(["parse", str(doc), "--backend", "spy-extract", "--extract"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_check"]["schema_ref"] == REF
    sent = spy.submitted[0].extraction_schema
    assert sent.json_schema == GENERIC
    assert sent.instructions == "extract key/value fields"


def test_cli_extract_with_text_keeps_the_instructions(spy, tmp_path):
    doc = tmp_path / "d.pdf"
    doc.write_bytes(PDF)
    assert main(["parse", str(doc), "--backend", "spy-extract", "--extract", "totals"]) == 0
    sent = spy.submitted[0].extraction_schema
    assert sent.json_schema == GENERIC and sent.instructions == "totals"


def test_python_run_with_instructions_only_gets_generic(spy):
    resp = api.run(PDF, backend="spy-extract", extraction_schema=INSTR)
    assert resp["schema_check"]["schema_ref"] == REF
    assert spy.submitted[0].extraction_schema.json_schema == GENERIC


def test_http_parse_with_instructions_only_gets_generic(client, spy):
    r = client.post("/v1/parse", json=_body(extraction_schema=INSTR))
    assert r.status_code == 200, r.text
    schemas.validate_response(r.json())
    assert r.json()["schema_check"]["schema_ref"] == REF
    assert spy.submitted[0].extraction_schema.json_schema == GENERIC


def test_a_caller_supplied_schema_reports_no_ref(spy, client):
    assert (
        api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": SCHEMA})[
            "schema_check"
        ]["schema_ref"]
        is None
    )
    r = client.post("/v1/parse", json=_body(extraction_schema={"json_schema": SCHEMA}))
    assert r.json()["schema_check"]["schema_ref"] is None


def test_an_exact_copy_of_generic_reports_its_ref(spy):
    resp = api.run(PDF, backend="spy-extract", extraction_schema={"json_schema": GENERIC})
    assert resp["schema_check"]["schema_ref"] == REF


# --------------------------------------------------------------------------- every ingress site
def test_ingress_build_request():
    req = api.build_request(PDF, None, extraction_schema=INSTR)
    assert req.extraction_schema.json_schema == GENERIC
    assert api.build_request(PDF, None).extraction_schema is None


def test_ingress_run_batch_native_and_platform(tmp_path, monkeypatch):
    native = Spy(native=True)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: native)
    for i in range(2):
        (tmp_path / f"d{i}.pdf").write_bytes(PDF)
    env = api.run_batch([str(tmp_path)], backend="spy-extract", extraction_schema=INSTR)
    assert [i["transport"] for i in env["items"]] == ["native", "native"]
    assert all(r.extraction_schema.json_schema == GENERIC for r in native.submitted)
    assert all(i["response"]["schema_check"]["schema_ref"] == REF for i in env["items"])


def test_ingress_server_route_and_jobs(client, spy, monkeypatch):
    from openreading.server import app as server_app

    seen: list[OpenReadingRequest] = []

    def record(req):
        out = apply_default(req)
        seen.append(out)
        return out

    monkeypatch.setattr(server_app, "apply_default", record)
    r = client.post("/v1/route", json=_body(extraction_schema=INSTR))
    assert r.status_code == 200, r.text
    assert [s.extraction_schema.json_schema for s in seen] == [GENERIC]

    poller = Spy(wait=WaitMode.POLL)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "spy-extract", lambda: poller)
    created = client.post("/v1/jobs", json=_body(extraction_schema=INSTR))
    assert created.status_code == 200, created.text
    rec = client.app.state.jobs[created.json()["job_id"]]
    assert rec.req.extraction_schema.json_schema == GENERIC


def test_ingress_server_batch_run_one(client, spy):
    docs = [
        {"bytes_base64": PDF_B64, "mime_type": "application/pdf", "filename": f"{i}.pdf"}
        for i in range(2)
    ]
    r = client.post(
        "/v1/batch",
        json={"documents": docs, "backend": "spy-extract", "extraction_schema": INSTR},
    )
    assert r.status_code == 200, r.text
    assert len(spy.submitted) == 2
    assert all(s.extraction_schema.json_schema == GENERIC for s in spy.submitted)
    assert all(i["response"]["schema_check"]["schema_ref"] == REF for i in r.json()["items"])


def test_ingress_server_batch_scope_precheck(spy, monkeypatch):
    from fastapi.testclient import TestClient

    from openreading.server import app as server_app
    from openreading.server import create_app

    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0007")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0007=spy-extract")
    seen: list[OpenReadingRequest] = []
    monkeypatch.setattr(
        server_app, "apply_default", lambda req: seen.append(apply_default(req)) or seen[-1]
    )
    doc = {"bytes_base64": PDF_B64, "mime_type": "application/pdf", "filename": "a.pdf"}
    TestClient(create_app()).post(
        "/v1/batch",
        json={"documents": [doc], "extraction_schema": INSTR},
        headers={"Authorization": "Bearer scoped-token-0007"},
    )
    # The pre-check builds the item before `run_one` does, so the scope decision sees the same
    # request the run would.
    assert seen and seen[0].extraction_schema.json_schema == GENERIC


# --------------------------------------------------------------------------- reload sites
def test_a_journal_from_before_the_default_replays_unchanged(tmp_path, monkeypatch):
    from openreading.api import _request_from_header
    from openreading.ledger.header import read_header
    from openreading.ledger.localfs import LocalFsBlobStore
    from tests.fakes import ScriptedBackend, scripted_registry
    from tests.test_ledger_replay import _cfg, _req, _run_once

    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    req = _req()
    req.extraction_schema = ExtractionSchema(instructions="read it")
    reg = scripted_registry(ScriptedBackend("solo", local=True, text="hello"))
    run_id, _, _ = _run_once(tmp_path / "ledger", reg, _cfg([{"backend": "solo"}]), req)

    header = read_header(tmp_path / "ledger", run_id)
    replayed = _request_from_header(header, LocalFsBlobStore(tmp_path / "ledger" / "blobs"))
    assert replayed.extraction_schema.json_schema is None
    assert replayed.extraction_schema.instructions == "read it"


def test_an_evals_case_with_instructions_only_runs_as_written():
    from openreading.evals.dataset import EvalCase
    from openreading.evals.runner import run_case

    leaf = Spy()
    body = OpenReadingRequest.model_validate(_body(extraction_schema=INSTR)).to_schema_dict()
    result = run_case(
        leaf, EvalCase(name="c", request_body=body, expected={"text_contains": ["spy"]})
    )
    assert result.error is None
    assert all(r.extraction_schema.json_schema is None for r in leaf.seen())


def test_typed_fields_alone_gets_no_schema_and_no_check(spy, client):
    resp = api.run(PDF, backend="spy-extract", outputs={"typed_fields": True})
    assert "schema_check" not in resp
    r = client.post("/v1/parse", json=_body(outputs={"typed_fields": True}))
    assert "schema_check" not in r.json()
    assert all(s.extraction_schema is None for s in spy.seen())


# --------------------------------------------------------------------------- per-adapter rows
def _generic_req(backend: str | None, **over: Any) -> OpenReadingRequest:
    """The request a caller gets from `--extract` alone, built through the real ingress."""
    operation = over.pop("operation", None)
    over.setdefault("extraction_schema", {"instructions": "read the totals"})
    req = api.build_request(PDF, backend, operation=operation, **over)
    assert req.extraction_schema.json_schema == GENERIC
    return req


def test_row_anthropic_claude_sends_generic_as_the_tool_schema():
    from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
    from tests.test_anthropic_claude import FakeClaudeClient, _run

    client = FakeClaudeClient("extract")
    _run(AnthropicClaudeAdapter(client=client), _generic_req("anthropic-claude"))
    tool = client.last_call["tools"][0]
    assert _has_generic(tool["input_schema"])


def test_row_google_gemini_sends_generic_as_the_response_schema():
    from openreading.adapters.google_gemini import GoogleGeminiAdapter
    from tests.test_google_gemini import FakeGeminiClient, _run

    client = FakeGeminiClient()
    _run(GoogleGeminiAdapter(client=client), _generic_req("google-gemini"))
    assert _has_generic(client.last_call["response_format"])


@respx.mock
def test_row_openai_compatible_uses_json_schema_mode():
    from tests.test_openai_compatible import (
        CHAT,
        KEYED,
        OpenAICompatibleAdapter,
        _reply,
        _run,
        _sent,
    )

    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply('{"title": "x"}')))
    req = _generic_req(None)  # a provider profile is not a registry slug
    req = req.model_copy(update={"backend": req.backend.model_copy(update={"id": KEYED.name})})
    _run(OpenAICompatibleAdapter(KEYED), req)
    fmt = _sent(route)["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == GENERIC


def test_row_mistral_ocr_annotates_with_generic_and_bills_as_extract():
    from openreading.adapters.mistral_ocr import MistralOCRAdapter
    from tests.test_mistral_ocr import FakeMistralOCRClient, _run

    client = FakeMistralOCRClient()
    resp, job = _run(MistralOCRAdapter(client=client), _generic_req("mistral-ocr"))
    fmt = client.last_body["document_annotation_format"]
    assert _has_generic(fmt)
    assert json.dumps(fmt).count('"strict": true') == 1
    assert client.last_body["document_annotation_prompt"] == "read the totals"
    assert job.raw.object_class == "extract"
    assert resp.backend.operation == "extract"


def test_row_pulse_sends_structured_output_and_schema_prompt():
    from openreading.adapters.pulse import PulseAdapter
    from tests.test_pulse import FakePulseClient, _run

    class Capture(FakePulseClient):
        config: dict | None = None

        def extract(self, document, config):
            self.config = config
            return super().extract(document, config)

    client = Capture()
    _run(PulseAdapter(client=client), _generic_req("pulse"))
    so = client.config["structured_output"]
    assert so["schema"] == GENERIC
    assert so["schemaPrompt"] == "read the totals"


def test_row_chunkr_extracts_with_generic():
    from openreading.adapters.chunkr import ChunkrAdapter
    from tests.test_chunkr import FakeChunkrClient, _run

    class Capture(FakeChunkrClient):
        schema: dict | None = None

        def create_extract_task(self, document, schema, config):
            self.schema = schema
            return super().create_extract_task(document, schema, config)

    client = Capture()
    _run(ChunkrAdapter(client=client), _generic_req("chunkr", operation="extract"))
    assert client.schema == GENERIC


def test_row_reducto_extract_sends_generic_and_parse_is_unchanged():
    from openreading.adapters.reducto import ReductoAdapter
    from tests.test_reducto import FakeReductoClient, _run_sync

    class Capture(FakeReductoClient):
        schema: dict | None = None

        def extract(self, document, schema, is_async):
            self.schema = schema
            return super().extract(document, schema, is_async)

    client = Capture()
    _run_sync(ReductoAdapter(client=client), _generic_req("reducto", operation="extract"))
    assert client.schema == GENERIC
    parse_client = Capture()
    _run_sync(ReductoAdapter(client=parse_client), _generic_req("reducto"))
    assert parse_client.schema is None and parse_client.last_parse_options is not None


def test_row_qwen_vl_puts_generic_in_the_prompt():
    pytest.importorskip("fitz")
    from openreading.adapters.qwen_vl import QwenVLAdapter
    from tests.test_qwen_vl import FakeQwenClient, _run

    client = FakeQwenClient(fixture="extract_response")
    _run(QwenVLAdapter(client=client), _generic_req("qwen-vl", pages={"ranges": [{"start": 1}]}))
    assert json.dumps(GENERIC) in client.calls[0][0]


def test_row_nuextract_translates_generic_into_a_template():
    from openreading.adapters.nuextract import NuExtractAdapter
    from openreading.adapters.nuextract.adapter import json_schema_to_template
    from tests.test_nuextract import FakeNuExtractClient, _run

    client = FakeNuExtractClient()
    _run(NuExtractAdapter(client=client), _generic_req("nuextract", operation="extract"))
    assert client.created_projects[0]["template"] == json_schema_to_template(GENERIC)


def _assert_all_generic_fields_missing(resp, req):
    finalize.apply_schema_check(resp, req)
    check = resp.schema_check
    assert check is not None and check.schema_ref == REF
    assert {f.path for f in check.fields} == set(GENERIC["properties"])
    assert {f.status for f in check.fields} == {"missing"}
    assert check.critical_ok is True
    schemas.validate_response(resp.to_schema_dict())


def test_row_aws_textract_returns_a_check_with_every_field_missing():
    from openreading.adapters.aws_textract import AWSTextractAdapter
    from tests.test_aws_textract import FakeTextractClient, _run_sync

    client = FakeTextractClient()
    req = _generic_req("aws-textract")
    resp = _run_sync(AWSTextractAdapter(client=client), req)
    _assert_all_generic_fields_missing(resp, req)


def test_row_azure_returns_a_check_with_every_field_missing():
    from openreading.adapters.azure_document_intelligence import AzureDocumentIntelligenceAdapter
    from tests.test_azure_document_intelligence import FakeAzureClient, _run, _succeeded

    adapter = AzureDocumentIntelligenceAdapter(client=FakeAzureClient(_succeeded("layout")))
    req = _generic_req("azure-document-intelligence", operation="prebuilt-layout")
    resp = asyncio.run(_run(adapter, req))
    _assert_all_generic_fields_missing(resp, req)


def test_row_google_document_ai_returns_a_check_with_every_field_missing():
    from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter
    from tests.test_google_document_ai import FakeDocAIClient, _run

    req = _generic_req("google-document-ai")
    resp = _run(GoogleDocumentAIAdapter(client=FakeDocAIClient()), req)
    _assert_all_generic_fields_missing(resp, req)
