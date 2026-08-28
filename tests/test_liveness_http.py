"""Deployment-state simulation for the REAL `probe_http` httpx path (respx, NO network).

`tests/test_liveness.py` drives the taxonomy through an injected fake client, which proves the
mapping but never touches the actual `httpx.Client` construction, timeout wiring, or exception
types. This file closes that gap the same way `test_<slug>_http.py` closes it for each adapter's
real client (BL-151): every state a real deployment will actually hit, simulated against the
genuine httpx code path, with respx intercepting the transport so no socket is ever opened.

The states, and why each one is worth its own test rather than being folded into "it failed":

| deployment reality                       | must report      | why it is not the neighbouring state |
|------------------------------------------|------------------|--------------------------------------|
| host resolves, connection refused         | `unreachable`    | the classic "container not started"  |
| DNS does not resolve                      | `unreachable`    | a typo'd URL, not a down service     |
| connects, then never answers (read hang)  | `unreachable`    | must respect the bound, not hang     |
| answers 401/403 with a real key present   | `unauthorized`   | the key is the problem, NOT the host |
| answers 500                               | `error`          | it IS up; retrying may work          |
| answers 200 with unexpected content       | `live`           | something is serving; don't overreach|

Getting `unauthorized` confused with `unreachable` is the costliest of these to an operator: one
says "start your container", the other says "fix your key", and they are opposite actions.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from openreading.adapters.registry import make_adapter
from openreading.credentials import EnvCredentialBroker
from openreading.liveness import check_liveness, probe_http
from openreading.types.liveness import LivenessStatus, ProbeOutcome

_URL = "http://backend.test/health"


def _broker(**env):
    return EnvCredentialBroker(environ=dict(env))


# --------------------------------------------------------------------------- transport failures


@respx.mock
def test_connection_refused_is_unreachable():
    """The host resolves, nothing is listening — a stopped container, the single most common real
    failure and the exact case the user reported as "shows Ready"."""
    respx.get(_URL).mock(side_effect=httpx.ConnectError("[Errno 61] Connection refused"))
    result = probe_http(_URL, timeout_s=1.0, env_hint="BACKEND_URL")
    assert result.outcome is ProbeOutcome.UNREACHABLE
    assert "BACKEND_URL" in result.detail


@respx.mock
def test_dns_failure_is_unreachable_not_error():
    """A typo'd hostname is still "nothing answered" from the operator's point of view — the fix is
    the URL either way, so splitting it from a refused connection would be detail with no decision
    attached to it."""
    respx.get(_URL).mock(side_effect=httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    assert probe_http(_URL, timeout_s=1.0).outcome is ProbeOutcome.UNREACHABLE


@respx.mock
def test_a_connect_timeout_is_unreachable_and_names_the_bound():
    respx.get(_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    result = probe_http(_URL, timeout_s=2.0)
    assert result.outcome is ProbeOutcome.UNREACHABLE
    assert "2s" in result.detail  # the bound is reported, so the operator can judge it


@respx.mock
def test_a_server_that_accepts_then_never_answers_is_unreachable_not_a_hang():
    """The nastiest real case: the TCP connection succeeds — so a naive port check would say
    "up" — and then the response never comes. The probe must surface the bound it gave up at,
    never block the caller."""
    respx.get(_URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
    result = probe_http(_URL, timeout_s=0.5)
    assert result.outcome is ProbeOutcome.UNREACHABLE
    assert "0.5s" in result.detail


@respx.mock
def test_a_tls_failure_is_unreachable():
    respx.get("https://backend.test/health").mock(
        side_effect=httpx.ConnectError("certificate verify failed")
    )
    assert probe_http("https://backend.test/health", timeout_s=1.0).outcome is (
        ProbeOutcome.UNREACHABLE
    )


# --------------------------------------------------------------------------- answered states


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_present_but_rejected_key_is_unauthorized_never_unreachable(status):
    """The distinction that costs the most to get wrong: something DID answer, and it said no. The
    action is "fix your key", not "start your container"."""
    respx.get(_URL).mock(return_value=httpx.Response(status, json={"error": "invalid api key"}))
    assert probe_http(_URL, timeout_s=1.0).outcome is ProbeOutcome.UNAUTHORIZED


@respx.mock
def test_an_unauthorized_probe_never_echoes_the_vendor_body():
    """Providers have been seen echoing the rejected key back inside the error body (see
    `readiness.auth_rejected_hint`). The probe returns an EMPTY detail and lets the platform
    substitute its own key-free sentence."""
    respx.get(_URL).mock(
        return_value=httpx.Response(401, json={"error": "key sk-live-SECRET123 is invalid"})
    )
    result = probe_http(_URL, timeout_s=1.0)
    assert result.detail == ""
    assert "SECRET123" not in result.detail


@respx.mock
@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_an_answer_that_does_not_prove_health_is_error(status):
    """It is up — it answered — but not in a way that proves it can serve. `unreachable` would be
    wrong (the host is fine) and `live` would be a lie."""
    respx.get(_URL).mock(return_value=httpx.Response(status, text="nope"))
    result = probe_http(_URL, timeout_s=1.0)
    assert result.outcome is ProbeOutcome.ERROR
    assert str(status) in result.detail


@respx.mock
def test_a_200_with_unexpected_content_is_still_live():
    """A reverse proxy's default page, an HTML error rendered with a 200, a schema change — the
    honest reading is "something is serving here". Downgrading it would tell the operator to go
    fix a service that is running."""
    respx.get(_URL).mock(return_value=httpx.Response(200, text="<html>hello</html>"))
    assert probe_http(_URL, timeout_s=1.0).outcome is ProbeOutcome.LIVE


@respx.mock
def test_a_204_no_content_health_route_is_live():
    """Plenty of health endpoints answer 204. Any 2xx counts."""
    respx.get(_URL).mock(return_value=httpx.Response(204))
    assert probe_http(_URL, timeout_s=1.0).outcome is ProbeOutcome.LIVE


@respx.mock
def test_the_declared_timeout_actually_reaches_the_httpx_client():
    """The bound is not decoration: it has to be wired into the real client, or a hung backend
    parks the caller (and, on the server, a worker thread) indefinitely."""
    seen = {}

    def _capture(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200)

    respx.get(_URL).mock(side_effect=_capture)
    probe_http(_URL, timeout_s=3.0)
    assert seen["timeout"]["connect"] == 3.0
    assert seen["timeout"]["read"] == 3.0


@respx.mock
def test_headers_reach_the_wire():
    route = respx.get(_URL).mock(return_value=httpx.Response(200))
    probe_http(_URL, timeout_s=1.0, headers={"Authorization": "Bearer tok"})
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


# --------------------------------------------------------------------------- end to end


@respx.mock
def test_docling_end_to_end_unreachable_through_the_real_client():
    """The full user-reported case with nothing faked but the transport: DOCLING_SERVE_URL is set
    (so readiness says configured), the container is not running, and the ladder reports
    `unreachable` with a measured latency."""
    from openreading.readiness import backend_readiness

    respx.get("http://docling.test:5001/health").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    adapter = make_adapter("docling")
    broker = _broker(DOCLING_SERVE_URL="http://docling.test:5001")
    assert backend_readiness(adapter, broker=broker).ready is True  # the OLD, overclaiming answer
    report = check_liveness(adapter, broker=broker)
    assert report.status is LivenessStatus.UNREACHABLE
    assert report.measured is True and report.latency_ms is not None


@respx.mock
def test_docling_end_to_end_live_through_the_real_client():
    respx.get("http://docling.test:5001/health").mock(return_value=httpx.Response(200, json={}))
    report = check_liveness(
        make_adapter("docling"), broker=_broker(DOCLING_SERVE_URL="http://docling.test:5001")
    )
    assert report.status is LivenessStatus.LIVE and report.measured is True


@respx.mock
def test_qwen_vl_end_to_end_reports_the_served_model_through_the_real_client():
    respx.get("http://vllm.test:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-VL-8B-Instruct"}]})
    )
    report = check_liveness(
        make_adapter("qwen-vl"), broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1")
    )
    assert report.status is LivenessStatus.LIVE
    assert report.version == "Qwen/Qwen3-VL-8B-Instruct"


@respx.mock
def test_qwen_vl_end_to_end_unauthorized_when_a_gated_endpoint_rejects_the_token():
    """A self-hosted endpoint CAN require a token (vLLM's --api-key). A present-but-wrong one has
    to read as `unauthorized`, so the operator fixes QWEN_VL_API_KEY rather than restarting a
    server that is running fine."""
    respx.get("http://vllm.test:8000/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    report = check_liveness(
        make_adapter("qwen-vl"),
        broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1", QWEN_VL_API_KEY="wrong-token"),
    )
    assert report.status is LivenessStatus.UNAUTHORIZED
    assert "wrong-token" not in report.detail
    assert "QWEN_VL_API_KEY" in report.detail  # names the var to fix


@respx.mock
def test_qwen_vl_end_to_end_error_when_the_model_server_is_still_loading():
    """vLLM answers 503 while weights load. It is up, so `unreachable` would be wrong; it cannot
    serve yet, so `live` would be wrong too."""
    respx.get("http://vllm.test:8000/v1/models").mock(return_value=httpx.Response(503))
    report = check_liveness(
        make_adapter("qwen-vl"), broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1")
    )
    assert report.status is LivenessStatus.ERROR
