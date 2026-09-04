"""Azure DI adapter — fault-injection for the branches the happy-path LRO fixtures skip:
readiness without an injected client (httpx present / absent), the no-credentials guard, the
intake variants (urlSource, path→base64Source, file_id rejected), submit's error mapping
(taxonomy re-raised, anything else wrapped), poll's non-429 HTTP mapping, poll's status=="failed"
mapping (with and without a provider error body), and the normalize edges a real AnalyzeResult
only sometimes carries (a region without a polygon, a keyless keyValuePair, address/content
field values). All offline via an injected scripted client — no Azure calls."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from openreading.adapters.azure_document_intelligence import (
    AzureDocumentIntelligenceAdapter,
    PollResp,
)
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

FIX = Path(__file__).parent / "fixtures" / "azure-document-intelligence"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()
OP_LOC = "https://x.cognitiveservices.azure.com/.../analyzeResults/abc"


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "azure-document-intelligence", "type": "hosted_api"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _ScriptedClient:
    """analyze records the request body (or raises); get walks a scripted PollResp list, holding
    on the last entry."""

    def __init__(
        self, polls: list[PollResp] | None = None, *, analyze_exc: Exception | None = None
    ):
        self._polls = list(polls or [PollResp(200, _fixture("layout"))])
        self._i = 0
        self._analyze_exc = analyze_exc
        self.bodies: list[dict] = []

    def analyze(self, model_id: str, body: dict, content_format: str) -> str:
        if self._analyze_exc is not None:
            raise self._analyze_exc
        self.bodies.append(body)
        return OP_LOC

    def get(self, operation_location: str) -> PollResp:
        item = self._polls[min(self._i, len(self._polls) - 1)]
        self._i += 1
        return item


def _analyze_result(**ar) -> dict:
    return {"status": "succeeded", "analyzeResult": {"apiVersion": "2024-11-30", **ar}}


_PAGE = {"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch", "angle": 0.0}


def _normalize(body: dict):
    req = _req()
    adapter = AzureDocumentIntelligenceAdapter(client=_ScriptedClient([PollResp(200, body)]))
    ctx = RunContext()
    job = adapter.poll(adapter.submit(req, ctx), ctx)
    return adapter.normalize(job, ctx, req)


# ---- readiness --------------------------------------------------------------------------------


def test_health_without_injected_client_is_ready_when_httpx_is_installed():
    h = AzureDocumentIntelligenceAdapter().health()
    assert h.ready and not h.missing_deps


def test_health_names_the_extra_when_httpx_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", None)  # `import httpx` then raises ImportError
    h = AzureDocumentIntelligenceAdapter().health()
    assert not h.ready
    assert any("azure-document-intelligence" in d for d in h.missing_deps)


# ---- credentials ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        RunContext(),
        RunContext(credentials=ResolvedCredentials(values={})),
        RunContext(credentials=ResolvedCredentials(values={"endpoint": "https://r.azure.com"})),
        RunContext(credentials=ResolvedCredentials(values={"key": "sk-1"})),
    ],
    ids=["no-context-creds", "empty-bag", "endpoint-only", "key-only"],
)
def test_missing_or_partial_credentials_is_terminal(ctx):
    # BYO endpoint AND key: half a credential pair never gets to build a client.
    adapter = AzureDocumentIntelligenceAdapter()
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), ctx)
    assert exc.value.backend_code == "no_credentials"


# ---- BL-162 part 3: poll_handle origin revalidation ------------------------------------------


def test_poll_refuses_when_operation_location_host_mismatches_configured_endpoint():
    """A tampered or captured operation-location (a compromised vendor response, a MITM, or a
    stored job record edited between submit and poll) must not redirect the second, authenticated
    fetch to a different host than the operator configured — the credential attached to that
    fetch would go to whoever controls the mismatched host."""
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://real.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job.poll_handle["operation_location"] = "https://attacker.example.com/analyzeResults/abc"
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_refuses_when_a_fresh_instance_resumes_a_tampered_job():
    """Ledger T4a fix: the origin check must not be silently inert on the fresh/resumed-instance
    path T4a's own R1 conformance case exercises. Submit on instance A with a real configured
    endpoint, round-trip the Job through to_dict/json/from_dict exactly as a resume would, tamper
    the persisted operation_location, then poll on a SEPARATE, freshly constructed instance B with
    the same ctx a real resume would rebuild. Before the fix, B had never called submit() so its
    (then instance-level) `_expected_origin` was still None and the check never activated — this
    pins that the check now fires identically for a fresh instance as it does for the same one."""
    a_client = _ScriptedClient()
    instance_a = AzureDocumentIntelligenceAdapter(client=a_client)
    ctx = RunContext(runtime={"endpoint": "https://real.cognitiveservices.azure.com"})
    job = instance_a.submit(_req(), ctx)

    resumed = Job.from_dict(json.loads(json.dumps(job.to_dict())))
    resumed.poll_handle["operation_location"] = "https://attacker.example.com/analyzeResults/abc"

    b_client = _ScriptedClient()
    instance_b = AzureDocumentIntelligenceAdapter(client=b_client)
    with pytest.raises(TerminalError) as exc:
        instance_b.poll(resumed, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"
    assert b_client._i == 0  # refused before the credentialed fetch was ever attempted


def test_poll_refuses_a_same_host_different_port():
    # BL-162: comparing hostname alone let a same-host attacker-chosen port
    # through unnoticed — a real bypass of the check's own purpose. x.cognitiveservices.azure.com
    # is the real host; :4444 is not the configured origin.
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://x.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job.poll_handle["operation_location"] = "https://x.cognitiveservices.azure.com:4444/x"
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_refuses_a_scheme_downgrade():
    # BL-162: an HTTPS->HTTP downgrade on the same host also passed the
    # hostname-only check — the credential header would go out over plaintext.
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://x.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job.poll_handle["operation_location"] = "http://x.cognitiveservices.azure.com/x"
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_succeeds_when_operation_location_origin_matches_configured_endpoint():
    # OP_LOC's origin (https://x.cognitiveservices.azure.com, default port 443) matches the
    # configured endpoint below — proves the check doesn't false-positive on the legitimate,
    # untampered case, including default-port normalization (neither side names :443 explicitly).
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://x.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job = adapter.poll(job, ctx)
    assert job.state.value == "succeeded"


def test_poll_refuses_rather_than_crashes_on_a_malformed_port():
    # BL-162: a non-numeric port makes urlparse's `.port` raise
    # ValueError instead of degrading — must still yield a clean TerminalError refusal, never an
    # unhandled crash, on either side of the comparison (a configured endpoint typo or an
    # attacker-influenced operation-location).
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://x.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job.poll_handle["operation_location"] = "https://x.cognitiveservices.azure.com:not-a-number/x"
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_refuses_explicit_port_zero_not_the_scheme_default():
    # BL-162: `p.port or default` treated an explicit port 0 as
    # absent (falsy-zero) and silently promoted it to the scheme default (443) — port 0 must
    # compare as its own distinct origin, not match a default-port endpoint.
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "https://x.cognitiveservices.azure.com"})
    job = adapter.submit(_req(), ctx)
    job.poll_handle["operation_location"] = "https://x.cognitiveservices.azure.com:0/x"
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_origin_check_activates_even_for_an_unparseable_configured_endpoint():
    # BL-162: a truthy-but-unparseable configured endpoint used
    # to leave _expected_host as None, silently disabling the check entirely (indistinguishable
    # from "no endpoint configured"). _origin() always returns a 3-tuple, so the comparison stays
    # active and correctly refuses rather than waving a real vendor response through.
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    ctx = RunContext(runtime={"endpoint": "not-a-url"})
    job = adapter.submit(_req(), ctx)
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, ctx)
    assert exc.value.backend_code == "operation_location_origin_mismatch"


def test_poll_origin_check_is_a_no_op_without_a_configured_endpoint():
    # Every other test in this module submits via RunContext() (no runtime endpoint) with an
    # injected client — this pins that the check stays inert in that harness shape rather than
    # spuriously refusing every existing fault test.
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    job = adapter.poll(job, RunContext())
    assert job.state.value == "succeeded"


def test_poll_origin_check_is_present_in_source():
    """Pinned structurally (shape of tests/test_liveness.py:756-764): a future refactor that
    quietly drops the operation-location origin check fails this test, not just a live incident.
    Ledger T4a fix: also pins that the expected origin is derived fresh from ctx.runtime INSIDE
    poll() itself, not read off `self` — the fix for the fresh-instance-resume gap this module's
    `test_poll_refuses_when_a_fresh_instance_resumes_a_tampered_job` regression-tests."""
    import inspect

    src = inspect.getsource(AzureDocumentIntelligenceAdapter.poll)
    assert "expected_origin" in src
    assert "_origin(" in src
    assert "ctx.runtime" in src
    assert "self._expected_origin" not in src and "self.expected_origin" not in src


# ---- intake variants --------------------------------------------------------------------------


def test_url_intake_is_sent_as_url_source():
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    url = "https://example.com/loan.pdf"
    adapter.submit(_req(document={"url": url, "mime_type": "application/pdf"}), RunContext())
    assert client.bodies == [{"urlSource": url}]  # never re-uploaded as bytes


def test_path_intake_is_read_and_base64_encoded(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(b"%PDF-1.7 local")
    client = _ScriptedClient()
    adapter = AzureDocumentIntelligenceAdapter(client=client)
    adapter.submit(_req(document={"path": str(pdf), "mime_type": "application/pdf"}), RunContext())
    assert client.bodies == [{"base64Source": base64.b64encode(b"%PDF-1.7 local").decode()}]


def test_file_id_intake_is_unsupported():
    adapter = AzureDocumentIntelligenceAdapter(client=_ScriptedClient())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(
            _req(document={"file_id": "file_1", "mime_type": "application/pdf"}), RunContext()
        )
    assert exc.value.backend_code == "unsupported_input"  # Azure has no file handle intake


# ---- submit error mapping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TerminalError("rejected", backend_code="auth_rejected"),
        RetryableError("throttled", backend_code="429", retry_after=7.0),
    ],
    ids=["terminal", "retryable"],
)
def test_submit_reraises_taxonomy_errors_unchanged(exc):
    adapter = AzureDocumentIntelligenceAdapter(client=_ScriptedClient(analyze_exc=exc))
    with pytest.raises(type(exc)) as raised:
        adapter.submit(_req(), RunContext())
    assert raised.value is exc  # no re-wrapping: the taxonomy verdict is already right


def test_submit_maps_unexpected_error():
    adapter = AzureDocumentIntelligenceAdapter(
        client=_ScriptedClient(analyze_exc=ValueError("boom"))
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"
    assert "boom" in str(exc.value)


# ---- poll: HTTP faults ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "error_type", "backend_code"),
    [
        (503, RetryableError, "http_503"),
        (500, RetryableError, "http_500"),
        (403, TerminalError, "auth_rejected"),
        (404, TerminalError, "http_404"),
    ],
)
def test_poll_non_429_http_errors_map_by_status(status_code, error_type, backend_code):
    adapter = AzureDocumentIntelligenceAdapter(
        client=_ScriptedClient([PollResp(status_code, {"error": {"code": "Boom"}})])
    )
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(error_type) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == backend_code


# ---- poll: provider-side failure ---------------------------------------------------------------


def test_poll_failed_status_surfaces_the_provider_code_and_message():
    body = {
        "status": "failed",
        "error": {"code": "InvalidContentDimensions", "message": "page 1 exceeds 10000 pixels"},
    }
    adapter = AzureDocumentIntelligenceAdapter(client=_ScriptedClient([PollResp(200, body)]))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "InvalidContentDimensions"
    assert "exceeds 10000 pixels" in str(exc.value)


def test_poll_failed_status_without_an_error_body_still_terminates():
    adapter = AzureDocumentIntelligenceAdapter(
        client=_ScriptedClient([PollResp(200, {"status": "failed"})])
    )
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "failed"  # never a success, never a silent retry loop


# ---- normalize edges ----------------------------------------------------------------------------


def test_bounding_region_without_a_polygon_yields_no_bbox():
    resp = _normalize(
        _analyze_result(
            content="No geometry here.",
            pages=[_PAGE],
            paragraphs=[
                {
                    "content": "No geometry here.",
                    "spans": [{"offset": 0, "length": 17}],
                    "boundingRegions": [{"pageNumber": 1}],
                }
            ],
        )
    )
    block = resp.document.pages[0].blocks[0]
    assert block.text == "No geometry here."
    assert block.bbox is None  # geometry absent upstream is never invented


def test_key_value_pair_without_a_key_is_skipped():
    resp = _normalize(
        _analyze_result(
            content="",
            pages=[_PAGE],
            keyValuePairs=[
                {"value": {"content": "orphan value"}},
                {"key": {"content": "Term"}, "value": {"content": "30y"}, "confidence": 0.8},
            ],
        )
    )
    assert resp.typed_fields is not None
    assert list(resp.typed_fields) == ["Term"]  # an unnamed pair has nowhere to go
    assert resp.typed_fields["Term"].citations is None  # no value geometry → no fabricated cite


def test_field_value_falls_back_to_address_then_raw_content():
    resp = _normalize(
        _analyze_result(
            content="",
            pages=[_PAGE],
            documents=[
                {
                    "docType": "invoice",
                    "confidence": 0.9,
                    "fields": {
                        "VendorAddress": {
                            "type": "address",
                            "valueAddress": {"city": "Redmond", "state": "WA"},
                            "content": "1 Way, Redmond WA",
                        },
                        "CustomerNotes": {"type": "string", "content": "net 30"},
                    },
                }
            ],
        )
    )
    address = resp.typed_fields["VendorAddress"]
    assert address.value == {"city": "Redmond", "state": "WA"}
    assert address.normalized_value == {"city": "Redmond", "state": "WA"}
    assert resp.typed_fields["CustomerNotes"].value == "net 30"  # no value* key → raw content
