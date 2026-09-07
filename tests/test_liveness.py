"""Liveness (internal/design/liveness.md "Pulse") — the optional 9th adapter method, the platform
ladder, the vendored report schema, and the three surfaces.

Everything here is OFFLINE. The probes that really touch a network are driven through injected
fake clients; the real ones live in the keyed live lane (`@pytest.mark.live` in each adapter's own
test file). No test in this file may pass or fail depending on a socket — that is the invariant
the whole design rests on (§8), and `test_offline_suite_never_reaches_a_real_probe` below pins the
structural half of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from openreading import schemas
from openreading.adapters.base import BackendAdapter, LivenessProbeAdapter
from openreading.adapters.registry import BUILTIN_ADAPTERS, make_adapter
from openreading.credentials import EnvCredentialBroker
from openreading.liveness import (
    DEFAULT_PROBE_TIMEOUT_S,
    MAX_PROBE_TIMEOUT_S,
    MIN_PROBE_TIMEOUT_S,
    check_liveness,
    infer_liveness,
    probe_http,
    probe_kind,
    resolve_timeout,
)
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    Cost,
    CredentialField,
    LivenessProbe,
    Provisioning,
    RuntimeProfile,
)
from openreading.types.enums import BackendType, WaitMode
from openreading.types.liveness import (
    LivenessReport,
    LivenessStatus,
    ProbeKind,
    ProbeOutcome,
    ProbeResult,
    is_measured,
)

GOLDEN = Path(__file__).parent / "golden"


# --------------------------------------------------------------------------- fakes


def _descriptor(slug="fake", *, probe="none", creds=None, config=None, **kw) -> AdapterDescriptor:
    return AdapterDescriptor(
        id=slug,
        type=BackendType.HOSTED_API,
        protocol_version=kw.pop("protocol_version", 1),
        provisioning=Provisioning(byo_mode=["api_key"], auth="api_key"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(ocr="claimed"),
        cost=Cost(native_unit="page"),
        runtime=RuntimeProfile(offline_capable=False),
        credentials_spec=creds if creds is not None else [],
        config_spec=config if config is not None else [],
        liveness=LivenessProbe(probe=probe) if probe != "none" else None,
        signup_url="https://example.test/signup",
        **kw,
    )


class _NoProbeAdapter(BackendAdapter):
    """A backend declaring an API key and NO probe — the common hosted case: the platform must
    infer rather than dead-end at "cannot be tested"."""

    def __init__(self, slug="inferable"):
        self.descriptor = _descriptor(
            slug,
            creds=[CredentialField(key="api_key", required=True, env=["FAKE_API_KEY"])],
        )

    def health(self):
        from openreading.types.runtime import Health

        return Health(ready=True)

    def submit(self, req, ctx):  # pragma: no cover - never executed here
        raise NotImplementedError

    def normalize(self, job, ctx, req):  # pragma: no cover
        raise NotImplementedError

    def report_cost(self, job):  # pragma: no cover
        raise NotImplementedError


class _BareAdapter(_NoProbeAdapter):
    """Declares NOTHING — no probe, no credentials, no config. The one shape with nothing to
    measure AND nothing to infer from."""

    def __init__(self):
        self.descriptor = _descriptor("bare", creds=[], config=[])


class _ProbingAdapter(_NoProbeAdapter):
    """Declares a probe and returns whatever the test scripts, recording that it was called."""

    def __init__(self, result: ProbeResult | Exception, *, probe="endpoint", slug="probing"):
        self.descriptor = _descriptor(
            slug,
            probe=probe,
            creds=[CredentialField(key="api_key", required=True, env=["FAKE_API_KEY"])],
        )
        self._result = result
        self.calls = 0
        self.timeouts: list[float] = []

    def probe_liveness(self, ctx, *, timeout_s):
        self.calls += 1
        self.timeouts.append(timeout_s)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeHttp:
    """The seam `probe_http(client=...)` takes, so the taxonomy is provable with no socket."""

    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.url = None
        self.headers = None

    def get(self, url, headers=None):
        self.url, self.headers = url, headers
        if self._raises is not None:
            raise self._raises
        return self._response


def _broker(**env):
    return EnvCredentialBroker(environ=dict(env))


# --------------------------------------------------------------------------- schema family


def test_liveness_report_schema_const_is_0_1():
    assert schemas.liveness_report_schema()["properties"]["schema_version"]["const"] == "0.1"


def test_liveness_report_pydantic_round_trips_to_schema_valid():
    report = LivenessReport(
        backend="docling",
        status=LivenessStatus.UNREACHABLE,
        measured=True,
        probe=ProbeKind.ENDPOINT,
        checked_at="2026-08-17T09:41:07Z",
        latency_ms=2.9,
        detail="no response (DOCLING_SERVE_URL) within 5s (ConnectError)",
    )
    doc = report.to_schema_dict()
    assert doc["schema_version"] == "0.1"  # producer default stamped
    schemas.validate_liveness_report(doc)


def test_liveness_report_inferred_round_trips_with_null_latency():
    report = LivenessReport(
        backend="chunkr",
        status=LivenessStatus.CONFIGURED_UNVERIFIED,
        measured=False,
        checked_at="2026-08-17T09:41:07Z",
    )
    doc = report.to_schema_dict()
    assert doc["latency_ms"] is None and doc["probe"] == "none"
    schemas.validate_liveness_report(doc)


def test_liveness_report_rejects_an_out_of_ladder_status():
    bad = {
        "schema_version": "0.1",
        "backend": "x",
        "status": "probably_fine",
        "measured": True,
        "probe": "none",
        "checked_at": "2026-08-17T09:41:07Z",
    }
    with pytest.raises(ValidationError):
        schemas.validate_liveness_report(bad)


def test_liveness_report_rejects_an_unknown_probe_kind():
    bad = {
        "schema_version": "0.1",
        "backend": "x",
        "status": "live",
        "measured": True,
        "probe": "telepathy",
        "checked_at": "2026-08-17T09:41:07Z",
    }
    with pytest.raises(ValidationError):
        schemas.validate_liveness_report(bad)


def test_golden_liveness_report_validates():
    schemas.validate_liveness_report(
        json.loads((GOLDEN / "liveness-report" / "v0.1.json").read_text())
    )


# --------------------------------------------------------------------------- the protocol


def test_backend_adapter_default_probe_is_unsupported():
    """The backward-compatibility keystone: every existing adapter — all 15 built-ins and every
    third-party subclass — keeps working untouched and simply reports "no probe"."""
    assert _NoProbeAdapter().probe_liveness(None, timeout_s=1.0).outcome is ProbeOutcome.UNSUPPORTED


def _protocol_members(proto: type) -> frozenset[str]:
    """The names a `typing.Protocol` requires, on every supported interpreter. 3.13+ has the
    public `typing.get_protocol_members`; 3.12 exposes `__protocol_attrs__`; 3.11 has neither
    (the CI leg that caught this) and only the private `typing._get_protocol_attrs`."""
    import typing

    getter = getattr(typing, "get_protocol_members", None)
    if getter is not None:
        return frozenset(getter(proto))
    attrs = getattr(proto, "__protocol_attrs__", None)
    if attrs is not None:
        return frozenset(attrs)
    return frozenset(typing._get_protocol_attrs(proto))  # type: ignore[attr-defined]


def test_adapter_protocol_still_has_exactly_the_eight_required_methods():
    """`probe_liveness` must NOT have been added to `AdapterProtocol`: that Protocol is
    runtime_checkable, so a 9th member would make every third-party adapter that doesn't implement
    it fail `isinstance` — the exact break the design forbids (§3.1)."""
    from openreading.adapters.base import AdapterProtocol

    members = _protocol_members(AdapterProtocol)
    assert "probe_liveness" not in members
    assert members == {
        "descriptor",
        "capabilities",
        "health",
        "submit",
        "poll",
        "resolve_webhook",
        "cancel",
        "normalize",
        "report_cost",
    }


def test_liveness_probe_adapter_protocol_is_feature_detectable():
    assert isinstance(_ProbingAdapter(ProbeResult.live()), LivenessProbeAdapter)


@pytest.mark.parametrize("slug", sorted(BUILTIN_ADAPTERS))
def test_every_builtin_descriptor_declares_an_honest_probe_kind(slug):
    """The declaration is static and must round-trip through the vendored schema for all 15, so a
    UI can read "can this be tested" offline with no call."""
    descriptor = make_adapter(slug).descriptor
    schemas.validate_descriptor(descriptor.to_schema_dict())
    assert probe_kind(descriptor) in set(ProbeKind)


def test_the_five_backends_with_real_probes_are_exactly_the_documented_set():
    """Pins internal/design/liveness.md §4's table. A sixth probe appearing without the design doc
    moving is the drift this catches."""
    probing = {s: probe_kind(make_adapter(s).descriptor).value for s in BUILTIN_ADAPTERS}
    assert {s: k for s, k in probing.items() if k != "none"} == {
        "docling": "endpoint",
        "qwen-vl": "endpoint",
        "pymupdf": "local",
        "tesseract": "local",
        "anthropic-claude": "vendor",
    }


# --------------------------------------------------------------------------- the ladder


def test_not_configured_when_a_required_var_is_missing():
    report = check_liveness(_NoProbeAdapter(), broker=_broker())
    assert report.status is LivenessStatus.NOT_CONFIGURED
    assert report.measured is False and report.latency_ms is None
    assert "FAKE_API_KEY" in report.detail


def test_configured_unverified_is_the_inference_for_a_backend_with_no_probe():
    """The mid-flight refinement: a vendor with no cheap liveness call does NOT dead-end at
    "cannot be tested" — it falls back to the configured state, labelled as the inference it is."""
    report = check_liveness(_NoProbeAdapter(), broker=_broker(FAKE_API_KEY="sk-x"))
    assert report.status is LivenessStatus.CONFIGURED_UNVERIFIED
    assert report.measured is False
    assert report.latency_ms is None  # nothing was measured, so nothing is claimed
    assert "inferred" in report.detail


def test_not_configured_beats_the_optimistic_inference():
    """A known negative is never softened into a guess, whichever way the backend is declared."""
    for adapter in (_NoProbeAdapter(), _ProbingAdapter(ProbeResult.live())):
        assert check_liveness(adapter, broker=_broker()).status is LivenessStatus.NOT_CONFIGURED


def test_not_configured_short_circuits_the_probe_entirely():
    """No network call is ever spent proving that a backend with no API key cannot authenticate."""
    adapter = _ProbingAdapter(ProbeResult.live())
    check_liveness(adapter, broker=_broker())
    assert adapter.calls == 0


def test_not_supported_when_there_is_no_probe_and_nothing_to_infer_from():
    """The floor state (§2 M4): "every declared requirement resolves" is vacuous for a backend
    that declares nothing, so the platform declines to guess rather than overstating it as
    `configured_unverified`. No BUILT-IN reports this today — both zero-declaration backends
    (pymupdf, tesseract) implement a real local probe — so this fake is what keeps the state
    reachable and honest rather than decorative."""
    report = check_liveness(_BareAdapter(), broker=_broker())
    assert report.status is LivenessStatus.NOT_SUPPORTED
    assert report.measured is False
    assert "cannot be tested" in report.detail


def test_every_inferred_detail_uses_a_colon_not_an_em_dash():
    """`openreading backends --check` prints these three strings in its DETAIL column, and the
    house prose rule bans an em dash in text a reader sees at the terminal. One test over all
    three inferred states, so a separator cannot drift back in one row at a time."""
    not_configured = check_liveness(_NoProbeAdapter(), broker=_broker()).detail
    unverified = check_liveness(_NoProbeAdapter(), broker=_broker(FAKE_API_KEY="sk-x")).detail
    not_supported = check_liveness(_BareAdapter(), broker=_broker()).detail

    assert [d for d in (not_configured, unverified, not_supported) if "—" in d] == []
    assert not_configured.startswith("not configured: set ")
    assert unverified.startswith("no free liveness check: ")
    assert not_supported.startswith("cannot be tested: ")


def test_no_builtin_backend_reports_not_supported():
    """Documents the audit in §2 M4 as an executable claim rather than prose."""
    statuses = {
        s: check_liveness(make_adapter(s), broker=_broker()).status for s in BUILTIN_ADAPTERS
    }
    assert LivenessStatus.NOT_SUPPORTED not in statuses.values()


@pytest.mark.parametrize(
    "result,expected",
    [
        (ProbeResult.live("up"), LivenessStatus.LIVE),
        (ProbeResult.unreachable("nothing there"), LivenessStatus.UNREACHABLE),
        (ProbeResult.unauthorized(), LivenessStatus.UNAUTHORIZED),
        (ProbeResult.error("weird 500"), LivenessStatus.ERROR),
    ],
    ids=["live", "unreachable", "unauthorized", "error"],
)
def test_measured_outcomes_map_onto_the_ladder_and_carry_a_latency(result, expected):
    report = check_liveness(_ProbingAdapter(result), broker=_broker(FAKE_API_KEY="sk-x"))
    assert report.status is expected
    assert report.measured is True
    assert report.latency_ms is not None and report.latency_ms >= 0


def test_measured_flag_partitions_the_ladder_exactly():
    for status in LivenessStatus:
        assert is_measured(status) == (
            status
            in (
                LivenessStatus.LIVE,
                LivenessStatus.UNREACHABLE,
                LivenessStatus.UNAUTHORIZED,
                LivenessStatus.ERROR,
            )
        )


def test_unauthorized_detail_is_the_shared_key_free_hint():
    """Reuses `readiness.auth_rejected_hint` rather than inventing a second vocabulary — and it
    never carries the provider's response body, which has been seen echoing the rejected key."""
    report = check_liveness(
        _ProbingAdapter(ProbeResult.unauthorized()), broker=_broker(FAKE_API_KEY="sk-x")
    )
    assert "rejected" in report.detail and "FAKE_API_KEY" in report.detail
    assert "sk-x" not in report.detail


def test_a_probe_that_raises_becomes_an_error_finding_not_a_crash():
    report = check_liveness(
        _ProbingAdapter(RuntimeError("boom")), broker=_broker(FAKE_API_KEY="sk-x")
    )
    assert report.status is LivenessStatus.ERROR
    assert report.measured is True
    assert "RuntimeError" in report.detail and "boom" in report.detail


def test_a_declared_probe_that_returns_unsupported_falls_back_to_the_inference():
    """The descriptor is allowed to run ahead of the implementation; what must not happen is a
    report claiming a measurement that never occurred."""
    report = check_liveness(
        _ProbingAdapter(ProbeResult.unsupported()), broker=_broker(FAKE_API_KEY="sk-x")
    )
    assert report.status is LivenessStatus.CONFIGURED_UNVERIFIED
    assert report.measured is False and report.latency_ms is None


def test_probe_result_is_echoed_with_its_static_kind_and_version():
    report = check_liveness(
        _ProbingAdapter(ProbeResult.live("up", version="Qwen/Qwen3-VL-8B"), probe="vendor"),
        broker=_broker(FAKE_API_KEY="sk-x"),
    )
    assert report.probe is ProbeKind.VENDOR
    assert report.version == "Qwen/Qwen3-VL-8B"


def test_every_ladder_state_serializes_to_a_schema_valid_report():
    for adapter, env in (
        (_NoProbeAdapter(), {}),
        (_NoProbeAdapter(), {"FAKE_API_KEY": "sk-x"}),
        (_BareAdapter(), {}),
        (_ProbingAdapter(ProbeResult.live()), {"FAKE_API_KEY": "sk-x"}),
        (_ProbingAdapter(ProbeResult.unreachable("x")), {"FAKE_API_KEY": "sk-x"}),
        (_ProbingAdapter(ProbeResult.unauthorized()), {"FAKE_API_KEY": "sk-x"}),
        (_ProbingAdapter(RuntimeError("x")), {"FAKE_API_KEY": "sk-x"}),
    ):
        schemas.validate_liveness_report(
            check_liveness(adapter, broker=_broker(**env)).to_schema_dict()
        )


# --------------------------------------------------------------------------- redaction


def test_a_secret_leaked_into_a_probe_detail_is_redacted():
    """Defense in depth: the adapter is the first line, `check_liveness` is the second — the same
    posture `readiness.auth_hinted` applies at the execution boundary."""
    report = check_liveness(
        _ProbingAdapter(ProbeResult.error("upstream said: key sk-super-secret is bad")),
        broker=_broker(FAKE_API_KEY="sk-super-secret"),
    )
    assert "sk-super-secret" not in report.detail
    assert "***" in report.detail


def test_a_secret_leaked_into_a_probe_version_is_redacted():
    report = check_liveness(
        _ProbingAdapter(ProbeResult.live("ok", version="build-sk-super-secret")),
        broker=_broker(FAKE_API_KEY="sk-super-secret"),
    )
    assert report.version is not None and "sk-super-secret" not in report.version


def test_no_report_field_ever_carries_an_endpoint_url():
    """A URL can embed credentials (`https://user:token@host`), so reports name the env VAR — the
    posture the openreading.credentials docstring and the Backends page already hold."""
    adapter = make_adapter("docling")
    # Injected seam, not a real socket: this file must never depend on the network (§8).
    adapter._probe_client = _FakeHttp(raises=OSError("Connection refused"))
    report = check_liveness(
        adapter,
        broker=_broker(DOCLING_SERVE_URL="http://user:hunter2@docling.internal:5001"),
        timeout_s=0.1,
    )
    dumped = json.dumps(report.to_schema_dict())
    assert "hunter2" not in dumped and "docling.internal" not in dumped
    assert "DOCLING_SERVE_URL" in report.detail


# --------------------------------------------------------------------------- timeout


def test_timeout_defaults_to_the_adapters_own_recommendation_then_the_global_default():
    assert resolve_timeout(_descriptor(probe="endpoint")) == DEFAULT_PROBE_TIMEOUT_S
    declared = _descriptor(probe="endpoint")
    declared.liveness = LivenessProbe(probe="endpoint", timeout_s=2.0)
    assert resolve_timeout(declared) == 2.0


def test_timeout_is_clamped_so_a_caller_cannot_park_a_worker():
    descriptor = _descriptor(probe="endpoint")
    assert resolve_timeout(descriptor, 10_000) == MAX_PROBE_TIMEOUT_S
    assert resolve_timeout(descriptor, 0) == MIN_PROBE_TIMEOUT_S
    assert resolve_timeout(descriptor, -5) == MIN_PROBE_TIMEOUT_S


def test_the_clamped_timeout_is_what_reaches_the_adapter():
    adapter = _ProbingAdapter(ProbeResult.live())
    check_liveness(adapter, broker=_broker(FAKE_API_KEY="sk-x"), timeout_s=10_000)
    assert adapter.timeouts == [MAX_PROBE_TIMEOUT_S]


# --------------------------------------------------------------------------- probe_http taxonomy


def test_probe_http_maps_a_2xx_to_live():
    http = _FakeHttp(_FakeResponse(200))
    result = probe_http("http://x.test/health", timeout_s=1.0, client=http, env_hint="FOO_URL")
    assert result.outcome is ProbeOutcome.LIVE and "FOO_URL" in result.detail
    assert http.url == "http://x.test/health"


def test_probe_http_maps_a_connection_failure_to_unreachable():
    http = _FakeHttp(raises=OSError("Connection refused"))
    result = probe_http("http://x.test/health", timeout_s=1.0, client=http, env_hint="FOO_URL")
    assert result.outcome is ProbeOutcome.UNREACHABLE
    assert "FOO_URL" in result.detail and "1s" in result.detail


@pytest.mark.parametrize("status", [401, 403])
def test_probe_http_maps_401_403_to_unauthorized_with_no_body_echoed(status):
    http = _FakeHttp(_FakeResponse(status))
    result = probe_http("http://x.test/health", timeout_s=1.0, client=http)
    assert result.outcome is ProbeOutcome.UNAUTHORIZED
    assert result.detail == ""  # filled by check_liveness's key-free hint, never the vendor body


@pytest.mark.parametrize("status", [404, 500, 503])
def test_probe_http_maps_other_non_2xx_to_error(status):
    result = probe_http("http://x.test/h", timeout_s=1.0, client=_FakeHttp(_FakeResponse(status)))
    assert result.outcome is ProbeOutcome.ERROR
    assert str(status) in result.detail


def test_probe_http_forwards_headers_and_runs_on_ok():
    http = _FakeHttp(_FakeResponse(200, {"data": [{"id": "m-1"}]}))
    result = probe_http(
        "http://x.test/models",
        timeout_s=1.0,
        headers={"Authorization": "Bearer k"},
        client=http,
        on_ok=lambda r: ProbeResult.live("ok", version=r.json()["data"][0]["id"]),
    )
    assert http.headers == {"Authorization": "Bearer k"}
    assert result.version == "m-1"


# --------------------------------------------------------------------------- the real probes


def test_docling_probe_hits_the_containers_health_endpoint():
    http = _FakeHttp(_FakeResponse(200))
    adapter = make_adapter("docling")
    adapter._probe_client = http
    report = check_liveness(adapter, broker=_broker(DOCLING_SERVE_URL="http://dl.test:5001/"))
    assert report.status is LivenessStatus.LIVE and report.probe is ProbeKind.ENDPOINT
    assert http.url == "http://dl.test:5001/health"


def test_docling_probe_reports_unreachable_when_nothing_answers():
    """The exact defect this milestone exists for: DOCLING_SERVE_URL set (so readiness says
    configured) but nothing listening."""
    from openreading.readiness import backend_readiness

    adapter = make_adapter("docling")
    adapter._probe_client = _FakeHttp(raises=OSError("Connection refused"))
    broker = _broker(DOCLING_SERVE_URL="http://127.0.0.1:9")
    assert backend_readiness(adapter, broker=broker).ready is True  # the OLD answer
    assert check_liveness(adapter, broker=broker).status is LivenessStatus.UNREACHABLE


def test_qwen_vl_probe_lists_models_and_reports_the_served_model_as_version():
    http = _FakeHttp(_FakeResponse(200, {"data": [{"id": "Qwen/Qwen3-VL-8B-Instruct"}]}))
    adapter = make_adapter("qwen-vl")
    adapter._probe_client = http
    report = check_liveness(
        adapter, broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1", QWEN_VL_API_KEY="k")
    )
    assert report.status is LivenessStatus.LIVE
    assert http.url == "http://vllm.test:8000/v1/models"
    assert http.headers == {"Authorization": "Bearer k"}
    assert report.version == "Qwen/Qwen3-VL-8B-Instruct"


def test_qwen_vl_probe_stays_live_when_the_model_list_is_unreadable():
    """A 2xx we cannot parse still means something is serving. Downgrading to `error`, or inventing
    a version, would both be dishonest."""
    adapter = make_adapter("qwen-vl")
    adapter._probe_client = _FakeHttp(_FakeResponse(200, {"unexpected": True}))
    report = check_liveness(adapter, broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1"))
    assert report.status is LivenessStatus.LIVE and report.version is None


def test_qwen_vl_probe_sends_no_authorization_header_without_a_key():
    adapter = make_adapter("qwen-vl")
    http = _FakeHttp(_FakeResponse(200, {"data": []}))
    adapter._probe_client = http
    check_liveness(adapter, broker=_broker(QWEN_VL_ENDPOINT="http://vllm.test:8000/v1"))
    assert http.headers == {}  # no Authorization at all, not an empty bearer


class _FakeAnthropicProbe:
    def __init__(self, models=None, raises: Exception | None = None):
        self._models = models or ["claude-opus-4-8"]
        self._raises = raises

    def list_models(self):
        if self._raises is not None:
            raise self._raises
        return self._models


class _AuthenticationError(Exception):
    """Named to match the anthropic SDK's own class name, which is the stable mapping signal."""


class _APIConnectionError(Exception):
    pass


def test_anthropic_probe_lists_models_and_never_creates_a_message():
    from openreading.adapters.anthropic_claude.adapter import AnthropicClaudeAdapter

    adapter = AnthropicClaudeAdapter(probe_client=_FakeAnthropicProbe())
    report = check_liveness(adapter, broker=_broker(ANTHROPIC_API_KEY="sk-ant-x"))
    assert report.status is LivenessStatus.LIVE and report.probe is ProbeKind.VENDOR
    assert report.version == "claude-opus-4-8"


def test_anthropic_probe_maps_an_sdk_auth_error_to_unauthorized():
    from openreading.adapters.anthropic_claude.adapter import AnthropicClaudeAdapter

    adapter = AnthropicClaudeAdapter(
        probe_client=_FakeAnthropicProbe(raises=_AuthenticationError("bad key sk-ant-x"))
    )
    report = check_liveness(adapter, broker=_broker(ANTHROPIC_API_KEY="sk-ant-x"))
    assert report.status is LivenessStatus.UNAUTHORIZED
    assert "sk-ant-x" not in report.detail  # the SDK's message is never echoed


def test_anthropic_probe_maps_a_connection_error_to_unreachable():
    from openreading.adapters.anthropic_claude.adapter import AnthropicClaudeAdapter

    adapter = AnthropicClaudeAdapter(
        probe_client=_FakeAnthropicProbe(raises=_APIConnectionError("no route"))
    )
    report = check_liveness(adapter, broker=_broker(ANTHROPIC_API_KEY="sk-ant-x"))
    assert report.status is LivenessStatus.UNREACHABLE


def test_anthropic_probe_maps_anything_else_to_error():
    from openreading.adapters.anthropic_claude.adapter import AnthropicClaudeAdapter

    adapter = AnthropicClaudeAdapter(probe_client=_FakeAnthropicProbe(raises=ValueError("odd")))
    report = check_liveness(adapter, broker=_broker(ANTHROPIC_API_KEY="sk-ant-x"))
    assert report.status is LivenessStatus.ERROR


def test_pymupdf_local_probe_is_measured_and_reports_a_version():
    pytest.importorskip("fitz")
    report = check_liveness(make_adapter("pymupdf"), broker=_broker())
    assert report.status is LivenessStatus.LIVE
    assert report.measured is True and report.probe is ProbeKind.LOCAL
    assert report.version


def test_tesseract_local_probe_measures_the_binary_through_the_injected_runner():
    from openreading.adapters.tesseract.adapter import TesseractAdapter

    class _Runner:
        def image_to_data(self, image, lang, dpi, timeout):  # pragma: no cover
            raise NotImplementedError

        def version(self):
            return "5.5.3"

    report = check_liveness(TesseractAdapter(runner=_Runner()), broker=_broker())
    assert report.status is LivenessStatus.LIVE and report.version == "5.5.3"


def test_tesseract_probe_reports_unreachable_when_the_binary_does_not_answer():
    from openreading.adapters.tesseract.adapter import TesseractAdapter

    class _Runner:
        def image_to_data(self, image, lang, dpi, timeout):  # pragma: no cover
            raise NotImplementedError

        def version(self):
            raise OSError("tesseract: not executable")

    report = check_liveness(TesseractAdapter(runner=_Runner()), broker=_broker())
    assert report.status is LivenessStatus.UNREACHABLE


# --------------------------------------------------------------------------- offline-gate safety


def test_infer_liveness_never_reaches_a_probe():
    """`infer_liveness` is what the web UI calls on page render, so "a page load never probes" has
    to be structural rather than a promise in a comment (§8)."""
    adapter = _ProbingAdapter(ProbeResult.live())
    assert infer_liveness(adapter, broker=_broker(FAKE_API_KEY="sk-x")) is None
    assert adapter.calls == 0


def test_infer_liveness_answers_without_a_probe_wherever_it_can():
    assert (
        infer_liveness(_NoProbeAdapter(), broker=_broker(FAKE_API_KEY="sk-x")).status
        is LivenessStatus.CONFIGURED_UNVERIFIED
    )
    assert infer_liveness(_NoProbeAdapter(), broker=_broker()).status is (
        LivenessStatus.NOT_CONFIGURED
    )
    assert infer_liveness(_BareAdapter(), broker=_broker()).status is LivenessStatus.NOT_SUPPORTED


def test_offline_suite_never_reaches_a_real_probe():
    """`readiness.backend_readiness` — which IS called all over the offline gate, the CLI's default
    listing, and `GET /v1/backends` — must never call `probe_liveness`. If it ever did, `make
    verify` would start making network calls on fifteen backends."""
    adapter = _ProbingAdapter(ProbeResult.live())
    from openreading.readiness import backend_readiness

    backend_readiness(adapter, broker=_broker(FAKE_API_KEY="sk-x"))
    assert adapter.calls == 0


def test_a_probe_signature_cannot_carry_caller_content():
    """A probe takes no OpenReadingRequest, so it can never become a data path (§7). Enforced by
    the signature rather than by discipline — this pins it."""
    import inspect

    params = inspect.signature(BackendAdapter.probe_liveness).parameters
    assert list(params) == ["self", "ctx", "timeout_s"]
