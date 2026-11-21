"""The `openai-compatible` provider-profile adapter, against respx-mocked chat-completions routes.

The real httpx client runs in every test here, with respx intercepting the transport, so no
socket is ever opened. The sample PDF is really rasterized, so the request body carries a real
PNG data URL. Generation is not deterministic, so the conformance cases say so.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from openreading.adapters.openai_compatible import OpenAICompatibleAdapter, ProviderProfile
from openreading.liveness import check_liveness
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import ResponseState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.liveness import LivenessStatus
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

pytest.importorskip("pymupdf", reason="pymupdf (rasterizer) not installed")

BASE = "https://llm.example.test/v1"
CHAT = f"{BASE}/chat/completions"
KEYED = ProviderProfile(
    name="example-llm", base_url=BASE, model="vision-1", key_env="EXAMPLE_LLM_API_KEY"
)
KEYLESS = ProviderProfile(name="local-llm", base_url=BASE, model="llava")
PDF_B64 = base64.b64encode(build_sample_pdf()).decode()
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _reply(content: str | None, finish: str = "stop", **message) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


MARKDOWN = "# Invoice\n\n| Item | Total |\n| --- | --- |\n| Widget | 10 |\n"


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": KEYED.name},
        "pages": {"ranges": [{"start": 1, "end": 1}]},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _ctx(key: str | None = "sk-test-key") -> RunContext:
    creds = ResolvedCredentials(values={"api_key": key}, source="env") if key else None
    return RunContext(credentials=creds)


def _run(adapter, req, ctx=None):
    ctx = ctx or _ctx()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


# --- descriptor -------------------------------------------------------------------------------


def test_keyed_profile_declares_its_key_env_as_the_only_secret():
    from openreading import schemas

    desc = OpenAICompatibleAdapter(KEYED).descriptor
    schemas.validate_descriptor(desc.to_schema_dict())
    assert desc.id == "example-llm"
    assert [(f.key, f.env, f.secret, f.required) for f in desc.credentials_spec] == [
        ("api_key", ["EXAMPLE_LLM_API_KEY"], True, True)
    ]
    assert desc.config_spec == []
    assert desc.type.value == "hosted_api"
    assert desc.signup_url == "https://llm.example.test"
    assert desc.runtime.version_pin == "vision-1"
    assert desc.output.channels.block_confidence.value == "X"


def test_keyless_profile_declares_no_credentials():
    desc = OpenAICompatibleAdapter(KEYLESS).descriptor
    assert desc.credentials_spec == []
    assert desc.type.value == "self_hosted_model"
    assert desc.signup_url is None
    assert desc.provisioning.auth == "none"


# --- conformance --------------------------------------------------------------------------------


@respx.mock
def test_conforms_in_both_modes_with_fresh_instances():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    check_adapter_conformance(
        OpenAICompatibleAdapter(KEYED),
        [ConformanceCase(request=_req(), ctx=_ctx(), deterministic=False, label="parse")],
        strict_checks={"C1", "C6", "C10"},
        adapter_factory=lambda: OpenAICompatibleAdapter(KEYED),
    )
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply('{"total": 10}')))
    extract = _req(
        extraction_schema={"json_schema": {"type": "object", "properties": {"total": {}}}},
        outputs={"typed_fields": True},
    )
    check_adapter_conformance(
        OpenAICompatibleAdapter(KEYED),
        [ConformanceCase(request=extract, ctx=_ctx(), deterministic=False, label="extract")],
        strict_checks={"C1", "C6", "C10"},
        adapter_factory=lambda: OpenAICompatibleAdapter(KEYED),
    )


# --- parse ------------------------------------------------------------------------------------


@respx.mock
def test_parse_sends_one_image_call_per_page_and_returns_markdown():
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    resp = _run(OpenAICompatibleAdapter(KEYED), _req())
    assert route.call_count == 1
    sent = _sent(route)
    assert set(sent) == {"model", "messages"}  # the smallest body most endpoints accept
    assert sent["model"] == "vision-1"
    parts = sent["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test-key"
    d = resp.to_schema_dict()
    assert d["document"]["markdown"] == MARKDOWN
    assert "|" not in d["document"]["text"]
    assert d["document"]["pages"][0]["page_number"] == 1
    assert d["usage"] == {"input_tokens": 120, "output_tokens": 30}
    assert d["backend"]["id"] == "example-llm" and d["backend"]["version"] == "vision-1"
    assert {w["field"] for w in d["warnings"]} == {"blocks"}  # requested by default, never made
    assert "confidence" not in json.dumps(d["document"])


@respx.mock
def test_parse_keeps_source_page_numbers_under_subsetting():
    pdf = build_sample_pdf()
    import pymupdf

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    if doc.page_count < 2:
        doc.insert_page(-1, text="second page")
    two_pages = base64.b64encode(doc.tobytes()).decode()
    doc.close()
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply("page")))
    req = _req(
        document={"bytes_base64": two_pages, "mime_type": "application/pdf"},
        pages={"ranges": [{"start": 2, "end": 2}]},
    )
    resp = _run(OpenAICompatibleAdapter(KEYED), req)
    assert route.call_count == 1
    assert [p.page_number for p in resp.document.pages] == [2]


@respx.mock
def test_keyless_profile_sends_no_authorization_header():
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    _run(OpenAICompatibleAdapter(KEYLESS), _req(), ctx=RunContext())
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
def test_image_document_is_sent_as_one_page():
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply("hello")))
    req = _req(
        document={"bytes_base64": base64.b64encode(PNG_1PX).decode(), "mime_type": "image/jpg"},
        pages=None,
    )
    resp = _run(OpenAICompatibleAdapter(KEYED), req)
    url = _sent(route)["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert resp.document.page_count == 1


@respx.mock
def test_truncated_reply_is_partial_with_a_warning():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN, "length")))
    resp = _run(OpenAICompatibleAdapter(KEYED), _req())
    assert resp.status.state is ResponseState.PARTIAL
    assert any(w.code == "output_truncated" for w in resp.warnings or [])


@respx.mock
def test_disabled_outputs_suppress_channels_and_keep_backend_raw():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    req = _req(
        outputs={"markdown": False, "text": False, "blocks": False, "include_backend_raw": True}
    )
    resp = _run(OpenAICompatibleAdapter(KEYED), req)
    assert resp.document.markdown is None and resp.document.text is None
    assert not resp.warnings
    assert resp.backend_raw is not None and resp.backend_raw.payload["mode"] == "parse"


@respx.mock
def test_table_cells_request_is_warned_not_fabricated():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    resp = _run(OpenAICompatibleAdapter(KEYED), _req(outputs={"tables": "cells"}))
    assert any(w.field == "table_cells" for w in resp.warnings or [])


# --- extract ----------------------------------------------------------------------------------


@respx.mock
def test_extract_sends_json_schema_response_format_and_returns_typed_fields():
    schema = {"type": "object", "properties": {"total": {"type": "number"}}}
    route = respx.post(CHAT).mock(
        return_value=httpx.Response(200, json=_reply('```json\n{"total": 10}\n```'))
    )
    req = _req(
        extraction_schema={"json_schema": schema, "instructions": "Read the invoice."},
        outputs={"typed_fields": True},
    )
    resp = _run(OpenAICompatibleAdapter(KEYED), req)
    sent = _sent(route)
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "extraction", "schema": schema},
    }
    assert sent["messages"][0]["content"][0]["text"].startswith("Read the invoice.")
    assert resp.typed_fields["total"].value == 10
    assert resp.document.markdown is None
    fields = {w.field for w in resp.warnings or []}
    assert {"markdown", "text"} <= fields


@respx.mock
def test_extract_with_instructions_only_asks_for_a_json_object():
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply('{"a": 1}')))
    resp = _run(OpenAICompatibleAdapter(KEYED), _req(extraction_schema={"instructions": "fields"}))
    assert _sent(route)["response_format"] == {"type": "json_object"}
    assert resp.typed_fields["a"].value == 1


@respx.mock
def test_extract_reply_that_is_not_json_is_partial():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply("sorry, no")))
    resp = _run(OpenAICompatibleAdapter(KEYED), _req(extraction_schema={"instructions": "x"}))
    assert resp.status.state is ResponseState.PARTIAL
    assert resp.status.error is not None and resp.status.error.code == "backend_validation"
    assert resp.typed_fields is None


# --- failures ---------------------------------------------------------------------------------


def test_missing_key_is_no_credentials():
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(), RunContext())
    assert ei.value.backend_code == "no_credentials"
    assert "EXAMPLE_LLM_API_KEY" in str(ei.value)


@respx.mock
def test_model_that_refuses_images_fails_the_call_with_a_terminal_error():
    respx.post(CHAT).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "model does not support image input"}}
        )
    )
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(), _ctx())
    assert ei.value.backend_code == "http_400"
    assert "image input" in str(ei.value)


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (401, {"error": {"message": "bad key"}}, TerminalError, "auth_rejected"),
        (403, b"forbidden", TerminalError, "auth_rejected"),
        (429, {"error": "slow down"}, RetryableError, "http_429"),
    ],
)
@respx.mock
def test_http_errors_map_to_the_taxonomy(status, body, kind, code):
    if isinstance(body, bytes):
        response = httpx.Response(status, content=body)
    else:
        response = httpx.Response(status, json=body)
    respx.post(CHAT).mock(return_value=response)
    with pytest.raises(kind) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(), _ctx())
    assert ei.value.backend_code == code


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ({"choices": []}, "malformed_response"),
        (_reply(None, refusal="I can't help with that."), "model_refused"),
        (_reply(None), "malformed_response"),
    ],
)
@respx.mock
def test_unusable_replies_fail_the_call(reply, code):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=reply))
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(), _ctx())
    assert ei.value.backend_code == code


@respx.mock
def test_transport_failure_is_mapped():
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(), _ctx())
    assert ei.value.backend_code == "ConnectError"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            {"bytes_base64": base64.b64encode(b"plain words").decode(), "mime_type": "text/plain"},
            "unsupported_input",
        ),
        ({"url": "https://example.test/a.pdf"}, "unsupported_input"),
    ],
)
def test_unreadable_inputs_are_refused(document, code):
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(_req(document=document), _ctx())
    assert ei.value.backend_code == code


def test_corrupt_pdf_is_a_rasterization_failure():
    bad = base64.b64encode(b"%PDF-1.7 not really").decode()
    with pytest.raises(TerminalError) as ei:
        OpenAICompatibleAdapter(KEYED).submit(
            _req(document={"bytes_base64": bad, "mime_type": "application/pdf"}), _ctx()
        )
    assert "rasterization failed" in str(ei.value)


def test_path_document_is_read(tmp_path):
    class _Fake:
        def __init__(self):
            self.bodies = []

        def chat(self, body):
            self.bodies.append(body)
            return _reply("x")

    p = tmp_path / "a.pdf"
    p.write_bytes(build_sample_pdf())
    fake = _Fake()
    adapter = OpenAICompatibleAdapter(KEYED, client=fake)
    assert adapter.health().ready is True
    resp = _run(adapter, _req(document={"path": str(p)}, pages=None), ctx=RunContext())
    assert len(fake.bodies) == resp.document.page_count


def test_report_cost_sums_tokens_and_tolerates_no_result():
    adapter = OpenAICompatibleAdapter(KEYED)
    job = adapter.new_job(adapter.descriptor.wait_modes[0])
    assert adapter.report_cost(job).native_quantity == 0.0


@respx.mock
def test_report_cost_after_a_run():
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply(MARKDOWN)))
    adapter = OpenAICompatibleAdapter(KEYED)
    job = adapter.submit(_req(), _ctx())
    report = adapter.report_cost(job)
    assert (report.native_unit, report.native_quantity) == ("token", 150.0)


def test_health_without_a_client_reports_the_endpoint():
    health = OpenAICompatibleAdapter(KEYED).health()
    assert health.ready is True and "endpoint" in (health.detail or "")


# --- liveness ---------------------------------------------------------------------------------


@respx.mock
def test_probe_is_live_when_models_answers(monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-probe")
    route = respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vision-1"}]})
    )
    report = check_liveness(OpenAICompatibleAdapter(KEYED))
    assert report.status is LivenessStatus.LIVE
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-probe"


@respx.mock
def test_probe_reports_a_rejected_key_by_its_variable(monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-wrong")
    respx.get(f"{BASE}/models").mock(return_value=httpx.Response(401, json={"error": "no"}))
    report = check_liveness(OpenAICompatibleAdapter(KEYED))
    assert report.status is LivenessStatus.UNAUTHORIZED
    assert "EXAMPLE_LLM_API_KEY" in report.detail
    assert "sk-wrong" not in report.detail


@respx.mock
def test_keyless_probe_sends_no_key():
    route = respx.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json={}))
    report = check_liveness(OpenAICompatibleAdapter(KEYLESS))
    assert report.status is LivenessStatus.LIVE
    assert "authorization" not in route.calls.last.request.headers


# --- live -------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_parse_against_the_openai_api():  # pragma: no cover - keyed live lane only
    """One page through api.openai.com, gated on OPENAI_API_KEY. It skips cleanly without it."""
    import os

    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    profile = ProviderProfile(
        name="openai-live",
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        key_env="OPENAI_API_KEY",
    )
    resp = _run(OpenAICompatibleAdapter(profile), _req(), ctx=_ctx(os.environ["OPENAI_API_KEY"]))
    assert resp.document.markdown
