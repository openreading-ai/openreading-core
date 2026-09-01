"""HTTP API (GOAL2 8.1). Offline via FastAPI's TestClient (ASGI, no socket). /v1/parse speaks the
vendored request/response schemas both directions; the status-code table (D-v2-8) is asserted
here; the control-plane shapes (route/backends) match the documented dicts."""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("fastapi", reason="server extra not installed")
pytest.importorskip("fitz", reason="pymupdf not installed")

from datetime import UTC
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: E402

from openreading import schemas  # noqa: E402
from openreading.server import create_app  # noqa: E402
from openreading.testing.sample_pdf import build_sample_pdf  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


def _pdf_body(backend="pymupdf"):
    return {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": backend},
    }


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_backends_lists_all_with_readiness(client, monkeypatch):
    monkeypatch.setenv("REDUCTO_API_KEY", "sk_present")
    r = client.get("/v1/backends")
    assert r.status_code == 200
    rows = {b["slug"]: b for b in r.json()}
    assert len(rows) == 13
    assert rows["pymupdf"]["ready"] is True
    assert set(rows["reducto"]) == {
        "slug",
        "type",
        "extra_installed",
        "creds_found",
        "creds_missing",
        "ready",
        # Pulse: the STATIC liveness-probe declaration. Additive, and a descriptor read — this
        # endpoint stays free/offline/instant, which is why the liveness ANSWER lives on its own
        # POST endpoint instead (internal/design/liveness.md §6.1).
        "liveness_probe",
    }
    assert rows["reducto"]["liveness_probe"] == "none"  # no free vendor liveness call
    assert rows["docling"]["liveness_probe"] == "endpoint"
    assert rows["reducto"]["ready"] is True  # key present


def test_parse_pymupdf_returns_schema_valid_response(client):
    r = client.post("/v1/parse", json=_pdf_body("pymupdf"))
    assert r.status_code == 200
    body = r.json()
    schemas.validate_response(body)  # outgoing conforms to the vendored response schema
    assert body["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in body["document"]["text"]


def test_parse_auto_routes_and_runs_local(client):
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_local": True}
    r = client.post("/v1/parse", json=payload)
    assert r.status_code == 200
    assert r.json()["backend"]["id"] in {"pymupdf", "tesseract", "docling", "qwen-vl"}


def _warning_codes(body):
    return [w["code"] for w in body.get("warnings") or []]


def test_parse_auto_replays_from_the_app_state_cache(client):
    """D-v3-3: the app-state idempotency cache is wired into /v1/parse's `auto` chain — the second
    POST of the same document replays instead of re-running the backend."""
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_local": True}
    first = client.post("/v1/parse", json=payload).json()
    second = client.post("/v1/parse", json=payload).json()
    assert "idempotent_replay" not in _warning_codes(first)
    assert "idempotent_replay" in _warning_codes(second)
    assert second["document"]["text"] == first["document"]["text"]
    schemas.validate_response(second)


def test_parse_cache_does_not_cross_app_instances(client):
    """The cache is per `create_app`, not per process — a second app starts cold."""
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_local": True}
    client.post("/v1/parse", json=payload)
    other = TestClient(create_app())
    assert "idempotent_replay" not in _warning_codes(other.post("/v1/parse", json=payload).json())


def test_parse_named_backend_is_never_replayed(client):
    """Only the `auto` chain goes through the executor; a directly-named backend always re-runs."""
    body = _pdf_body("pymupdf")
    client.post("/v1/parse", json=body)
    assert "idempotent_replay" not in _warning_codes(client.post("/v1/parse", json=body).json())


def test_parse_unknown_backend_is_404(client):
    body = _pdf_body("pymupdf")
    body["backend"]["id"] = "does-not-exist"
    r = client.post("/v1/parse", json=body)
    # schema allows any string id, so this reaches make_adapter → KeyError → 404
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_backend"


def test_parse_missing_credentials_is_424(client, monkeypatch):
    for v in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROCESSOR_ID"):
        monkeypatch.delenv(v, raising=False)
    r = client.post("/v1/parse", json=_pdf_body("google-document-ai"))
    assert r.status_code == 424
    err = r.json()["error"]
    assert err["backend_code"] == "missing_credentials"
    assert "GCP_PROJECT_ID" in err["missing_env"]


def test_parse_invalid_body_is_400(client):
    r = client.post("/v1/parse", json={"not": "a valid request"})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "bad_request"


def test_parse_unsupported_feature_is_422(client):
    body = _pdf_body("pymupdf")
    body["extraction_schema"] = {"instructions": "extract fields"}  # pymupdf can't → 422
    r = client.post("/v1/parse", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "unsupported_feature"


def test_parse_corrupt_document_is_502_terminal_not_a_bare_500(client):
    # a raw fitz.FileDataError would escape _ADAPTER_ERRORS entirely → undocumented 500
    body = _pdf_body("pymupdf")
    body["document"]["bytes_base64"] = base64.b64encode(b"%PDF-1.7 not a real PDF body\n").decode()
    r = client.post("/v1/parse", json=body)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["category"] == "terminal"
    assert err["backend_code"] == "FileDataError"


# --- document.path gating over HTTP (H1) -----------------------------------------------


def test_parse_rejects_document_path_by_default(client, monkeypatch):
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    r = client.post(
        "/v1/parse",
        json={"document": {"path": "/etc/hosts"}, "backend": {"id": "pymupdf"}},
    )
    assert r.status_code == 400
    assert "document.path" in r.json()["error"]["message"]


def test_parse_allows_path_under_configured_root(client, monkeypatch, tmp_path):
    src = Path("examples")  # repo ships two synthetic PDFs
    pdf = next(src.glob("*.pdf"))
    doc = tmp_path / pdf.name
    doc.write_bytes(pdf.read_bytes())
    monkeypatch.setenv("OPENREADING_SERVER_PATH_ROOT", str(tmp_path))
    r = client.post(
        "/v1/parse",
        json={"document": {"path": str(doc)}, "backend": {"id": "pymupdf"}},
    )
    assert r.status_code == 200


def test_parse_rejects_symlink_escaping_root(client, monkeypatch, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.pdf").symlink_to(outside)
    monkeypatch.setenv("OPENREADING_SERVER_PATH_ROOT", str(root))
    r = client.post(
        "/v1/parse",
        json={
            "document": {"path": str(root / "link.pdf")},
            "backend": {"id": "pymupdf"},
        },
    )
    assert r.status_code == 400


def test_route_rejects_document_path_by_default(client, monkeypatch):
    # _parse_request (shared by /v1/route and /v1/jobs) raises ValueError(refusal) so this
    # endpoint's existing except->400 handles it exactly like any other bad body.
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    r = client.post(
        "/v1/route",
        json={"document": {"path": "/etc/hosts"}, "backend": {"id": "pymupdf"}},
    )
    assert r.status_code == 400
    assert "document.path" in r.json()["error"]["message"]


def test_route_returns_plan_shape(client):
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_baa": True, "no_train_on_data": True}
    r = client.post("/v1/route", json=payload)
    assert r.status_code == 200
    plan = r.json()
    assert set(plan) == {"chosen", "fallbacks", "dropped", "terminal_reason"}
    assert plan["chosen"] is not None
    assert plan["dropped"]["aws-textract"]["code"] == "trains_on_data"  # unconfirmed opt-out


def test_route_config_from_env_readmits_textract(client, monkeypatch):
    monkeypatch.setenv("OPENREADING_TRAIN_OPTOUT_CONFIRMED", "aws-textract")
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_baa": True, "no_train_on_data": True}
    r = client.post("/v1/route", json=payload)
    plan = r.json()
    assert "aws-textract" in [plan["chosen"], *plan["fallbacks"]]


def test_route_tier_gated_baa_needs_the_deploy_env_confirmation(client, monkeypatch):
    monkeypatch.delenv("OPENREADING_BAA_TIER_CONFIRMED", raising=False)
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_baa": True}
    plan = client.post("/v1/route", json=payload).json()
    assert plan["dropped"]["reducto"]["code"] == "no_baa"

    monkeypatch.setenv("OPENREADING_BAA_TIER_CONFIRMED", "reducto")
    plan = client.post("/v1/route", json=payload).json()
    assert "reducto" in [plan["chosen"], *plan["fallbacks"]]


# --- async jobs (8.2) ------------------------------------------------------------------


def test_jobs_local_backend_completes_immediately(client):
    r = client.post("/v1/jobs", json=_pdf_body("pymupdf"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "succeeded" and body["backend"] == "pymupdf"
    schemas.validate_response(body["response"])
    # GET the same job returns the stored result
    g = client.get(f"/v1/jobs/{body['job_id']}")
    assert g.status_code == 200 and g.json()["state"] == "succeeded"


def test_submit_job_normalize_crash_is_a_structured_error_not_a_500(client, monkeypatch):
    # BL-85: `_metered()`'s failure path is guarded on the webhook leg only (see
    # test_webhook_normalize_crash_is_a_structured_error_not_a_500) — submit_job's identical call
    # site had no enclosing try/except at all, so a non-adapter exception out of normalize() (any
    # ordinary adapter bug, not one of the five _ADAPTER_ERRORS taxonomy types) used to escape the
    # ASGI call entirely instead of becoming a failed job with the generic envelope.
    from openreading.adapters.pymupdf.adapter import PyMuPDFAdapter

    def _boom(self, job, ctx, req):
        raise ValueError("malformed page structure")

    monkeypatch.setattr(PyMuPDFAdapter, "normalize", _boom)

    r = client.post("/v1/jobs", json=_pdf_body("pymupdf"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"] == {"category": "error", "message": "malformed page structure"}


def test_submit_job_metered_redacts_a_secret_in_a_normalize_crash(monkeypatch):
    # BL-93 Leg 1: submit_job's terminal-shortcut `_metered()` call sat OUTSIDE the
    # `with auth_hinted(...)` block that wraps `adapter.submit()` a few lines up — so an
    # AdapterError `normalize()` raises here never got `attach_auth_hint`/`redact` applied, unlike
    # the identical failure from `submit()` itself. test_submit_job_normalize_crash_is_a_structured
    # _error_not_a_500 already proves the crash-safety half (BL-85) with a plain ValueError; this
    # is the secret-bearing-descriptor variant (ScriptedBackend's `required_env`, per this item's
    # own acceptance criteria) that would have failed before this fix and cannot be satisfied by a
    # message shape with nothing secret in it.
    import openreading.api as api_module
    import openreading.server.app as app_module
    from openreading.types.errors import TerminalError
    from tests.fakes import ScriptedBackend

    secret = "sk-leaky-0001-must-never-appear"
    monkeypatch.setenv("LEAKY_API_KEY", secret)

    class _LeakyBackend(ScriptedBackend):
        def normalize(self, job, ctx, req):
            raise TerminalError(f"upstream said: bad key {secret}", backend_code="upstream_error")

    backend = _LeakyBackend("leaky-submit", required_env=["LEAKY_API_KEY"])
    real_make_adapter = api_module.make_adapter
    fake_make_adapter = lambda bid: backend if bid == "leaky-submit" else real_make_adapter(bid)  # noqa: E731
    # BL-91: submit_job resolves the adapter via api.prepare_named_backend, which calls
    # make_adapter through api.py's own module globals, not server.app's — patching only
    # app_module.make_adapter (the old, pre-BL-91 call site) never reaches this path.
    monkeypatch.setattr(api_module, "make_adapter", fake_make_adapter)

    client = TestClient(app_module.create_app())
    r = client.post("/v1/jobs", json=_pdf_body("leaky-submit"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert secret not in body["error"]["message"]
    assert "***" in body["error"]["message"]


def test_submit_job_metered_redacts_a_plain_normalize_crash(monkeypatch):
    # BL-99: the identical submit-leg call site as the two tests above, but combining both of their
    # substitutions at once — a plain ValueError (not a TerminalError/AdapterError) carrying a
    # secret. test_submit_job_normalize_crash_is_a_structured_error_not_a_500 proves crash-safety
    # with nothing secret in the message; test_submit_job_metered_redacts_a_secret_in_a_normalize_
    # crash proves redaction with an AdapterError, which auth_hinted's ORIGINAL except clause
    # already caught before this item. Neither exercises auth_hinted's NEW except Exception clause.
    import openreading.api as api_module
    import openreading.server.app as app_module
    from tests.fakes import ScriptedBackend

    secret = "sk-leaky-plain-0008-must-never-appear"
    monkeypatch.setenv("LEAKYPLAIN_API_KEY", secret)

    backend = ScriptedBackend(
        "leaky-submit-plain",
        required_env=["LEAKYPLAIN_API_KEY"],
        normalize_error=ValueError(f"malformed output, key={secret}"),
    )
    real_make_adapter = api_module.make_adapter

    def fake_make_adapter(bid):
        return backend if bid == "leaky-submit-plain" else real_make_adapter(bid)

    monkeypatch.setattr(api_module, "make_adapter", fake_make_adapter)

    client = TestClient(app_module.create_app())
    r = client.post("/v1/jobs", json=_pdf_body("leaky-submit-plain"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert secret not in body["error"]["message"]
    assert "***" in body["error"]["message"]


def test_submit_job_cost_report_warning_redacts_a_secret(monkeypatch):
    # BL-93's fourth, wider-reaching sink: `apply_cost_report` degrades a `report_cost()` failure
    # into an unredacted warning on an otherwise-SUCCEEDED response — it catches its own exception
    # and never raises, so even a `with auth_hinted(...)` wrap around the whole call (Leg 1's own
    # fix, just above) never sees it; `apply_cost_report` previously never received credentials at
    # all. Reached from every `_metered()` call site (all three async legs) and from
    # `execute_plan`'s synchronous path alike — this exercises it via POST /v1/jobs, the simplest
    # of those to drive end to end.
    import openreading.api as api_module
    import openreading.server.app as app_module
    from tests.fakes import ScriptedBackend

    secret = "sk-meter-leak-0002-must-never-appear"
    monkeypatch.setenv("LEAKYMETER_API_KEY", secret)

    class _LeakyMeterBackend(ScriptedBackend):
        def report_cost(self, job):
            raise RuntimeError(f"billing endpoint rejected key {secret}")

    backend = _LeakyMeterBackend("leaky-meter", required_env=["LEAKYMETER_API_KEY"])
    real_make_adapter = api_module.make_adapter
    fake_make_adapter = lambda bid: backend if bid == "leaky-meter" else real_make_adapter(bid)  # noqa: E731
    # BL-91: submit_job resolves the adapter via api.prepare_named_backend, which calls
    # make_adapter through api.py's own module globals, not server.app's — patching only
    # app_module.make_adapter (the old, pre-BL-91 call site) never reaches this path.
    monkeypatch.setattr(api_module, "make_adapter", fake_make_adapter)

    client = TestClient(app_module.create_app())
    r = client.post("/v1/jobs", json=_pdf_body("leaky-meter"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "succeeded"
    warnings = body["response"]["warnings"]
    assert warnings and warnings[0]["code"] == "cost_unavailable"
    assert secret not in warnings[0]["message"]
    assert "***" in warnings[0]["message"]


def test_jobs_auto_backend_rejected(client):
    r = client.post("/v1/jobs", json=_pdf_body("auto"))
    assert r.status_code == 400


def test_jobs_rejects_document_path_by_default(client, monkeypatch):
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    r = client.post(
        "/v1/jobs",
        json={"document": {"path": "/etc/hosts"}, "backend": {"id": "pymupdf"}},
    )
    assert r.status_code == 400
    assert "document.path" in r.json()["error"]["message"]


def test_jobs_strategy_id_wraps_the_walk_as_a_synthetic_job(tmp_path, monkeypatch):
    # POST /v1/jobs with a strategy: id runs the WHOLE walk as one synthetic job (integration.md §3.4)
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf, tesseract]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    c = TestClient(create_app())
    r = c.post("/v1/jobs", json=_pdf_body("strategy:cheap"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "succeeded" and body["backend"] == "strategy:cheap"
    assert body["response"]["backend"]["id"] == "pymupdf"
    assert "orchestration" in body["response"]  # the walk's orchestration block rides along
    schemas.validate_response(body["response"])
    g = c.get(f"/v1/jobs/{body['job_id']}")
    assert g.status_code == 200 and g.json()["state"] == "succeeded"


def test_jobs_unknown_strategy_is_rejected(tmp_path, monkeypatch):
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    c = TestClient(create_app())
    r = c.post("/v1/jobs", json=_pdf_body("strategy:ghost"))
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "unknown_strategy"


def test_get_unknown_job_is_404(client):
    r = client.get("/v1/jobs/omjob_nope")
    assert r.status_code == 404 and r.json()["error"]["category"] == "unknown_job"


def test_get_job_drives_poll_to_completion():
    # seed a RUNNING poll job whose adapter completes on the first poll; GET drives it to done.
    from openreading.router.clock import RealClock  # noqa: F401
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RawResult
    from tests.fakes import ConfigurableBackend, make_backend

    class _PollBackend(ConfigurableBackend):
        def submit(self, req, ctx):
            j = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            j.next_poll_at = 0.0
            return j

        def poll(self, job, ctx):
            job.state = JobState.SUCCEEDED
            job.raw = RawResult(payload="polled-done")
            return job

    app = create_app()
    client = TestClient(app)
    adapter = _PollBackend(make_backend("pollbk", local=True).descriptor)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "pollbk"}}
    )
    from openreading.types.runtime import RunContext

    job = adapter.submit(req, RunContext())
    app.state.jobs[job.id] = JobRecord(job.id, "pollbk", adapter, job, req, 0)
    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["state"] == "succeeded"


def test_get_job_poll_failure_records_the_structured_error():
    # BL-33: a failure one poll AFTER submit must carry the same structured body the identical
    # failure carries inline — the auth_rejected TerminalError that /v1/parse renders as a 424
    # with category/backend_code, not a bare string. The GET itself is still 200 (the fetch
    # succeeded; the job failed).
    from openreading.server.app import JobRecord, _error_response
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.errors import TerminalError
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import ConfigurableBackend, make_backend

    class _FailingPollBackend(ConfigurableBackend):
        def submit(self, req, ctx):
            j = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            j.next_poll_at = 0.0
            return j

        def poll(self, job, ctx):
            raise TerminalError("reducto rejected the key", backend_code="auth_rejected")

    app = create_app()
    client = TestClient(app)
    adapter = _FailingPollBackend(make_backend("pollfail", local=True).descriptor)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "pollfail"}}
    )
    job = adapter.submit(req, RunContext())
    app.state.jobs[job.id] = JobRecord(job.id, "pollfail", adapter, job, req, 0)

    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    # the poll path runs inside `with auth_hinted(descriptor):` (BL-6), which rewrites an
    # auth_rejected message into the actionable hint in place — so the recorded error carries the
    # hint, not the adapter's raw provider text.
    assert body["error"] == {
        "category": "terminal",
        "message": "key was found but rejected by pollfail",
        "backend_code": "auth_rejected",
    }
    # …and it is byte-for-byte the envelope submit time would have produced (424 there, 200 here) —
    # submit time reaches _error_response via the same `auth_hinted` wrapper, so simulate that here
    # too rather than comparing against an un-hinted exception.
    from openreading.readiness import attach_auth_hint

    inline_exc = TerminalError("reducto rejected the key", backend_code="auth_rejected")
    attach_auth_hint(inline_exc, adapter.descriptor)
    inline = _error_response(inline_exc)
    assert inline.status_code == 424
    assert json.loads(inline.body)["error"] == body["error"]


def test_get_job_normalize_crash_is_a_structured_error_not_a_500():
    # BL-85: get_job's _metered() call site was guarded ONLY by `except _ADAPTER_ERRORS`, which
    # does not include a generic Exception from normalize() — mirrors
    # test_webhook_normalize_crash_is_a_structured_error_not_a_500 for the poll leg.
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import ConfigurableBackend, make_backend

    class _CrashOnNormalizeBackend(ConfigurableBackend):
        def submit(self, req, ctx):
            j = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            j.next_poll_at = 0.0
            return j

        def poll(self, job, ctx):
            job.state = JobState.SUCCEEDED
            return job

        def normalize(self, job, ctx, req):
            raise ValueError("malformed poll result")

    app = create_app()
    client = TestClient(app)
    adapter = _CrashOnNormalizeBackend(make_backend("pollcrash", local=True).descriptor)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "pollcrash"}}
    )
    job = adapter.submit(req, RunContext())
    app.state.jobs[job.id] = JobRecord(job.id, "pollcrash", adapter, job, req, 0)

    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"] == {"category": "error", "message": "malformed poll result"}


def test_get_job_metered_redacts_a_secret_in_a_plain_normalize_crash(monkeypatch):
    # BL-99: the identical poll-leg call site as immediately above, but with a secret-bearing
    # descriptor — the substitution none of BL-85's/BL-93's eight existing tests make (BL-93 never
    # added its own get_job redaction test at all; only submit_job and webhook got one). auth_hinted's
    # new except Exception clause must redact the secret before it reaches rec.error, exactly as its
    # original except AdapterError clause already does for a TerminalError raised from this same
    # call site.
    from openreading.server.app import JobRecord
    from openreading.types.descriptor import CredentialField
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import ConfigurableBackend, make_backend

    secret = "sk-getjob-leak-0006-must-never-appear"
    monkeypatch.setenv("POLLLEAK_API_KEY", secret)

    class _LeakyPollBackend(ConfigurableBackend):
        def submit(self, req, ctx):
            j = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            j.next_poll_at = 0.0
            return j

        def poll(self, job, ctx):
            job.state = JobState.SUCCEEDED
            return job

        def normalize(self, job, ctx, req):
            raise ValueError(f"malformed poll result, key={secret}")

    desc = make_backend("pollleak").descriptor.model_copy(
        update={
            "credentials_spec": [
                CredentialField(key="api_key", required=True, secret=True, env=["POLLLEAK_API_KEY"])
            ]
        }
    )
    app = create_app()
    client = TestClient(app)
    adapter = _LeakyPollBackend(desc)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "pollleak"}}
    )
    job = adapter.submit(req, RunContext())
    app.state.jobs[job.id] = JobRecord(job.id, "pollleak", adapter, job, req, 0)

    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert secret not in body["error"]["message"]
    assert "***" in body["error"]["message"]


def test_get_job_deadline_exceeded_keeps_running_then_reaches_succeeded(monkeypatch):
    # BL-77: `get_job` used to latch ANY RetryableError reaching it — including the driver's OWN
    # per-call deadline check — as a permanent `rec.error`, with nothing anywhere ever clearing it.
    # A still-healthy POLL job whose GETs span more than one internal backoff cycle would get stuck
    # "failed" forever even though the backend never claimed anything worse than "still processing".
    # The fix tells the driver's own slice-expiry (`_DriveSliceExpired`) apart by TYPE and leaves
    # the job "running" instead of latching it failed, and each GET drives a fresh slice — so the
    # two halves this test guards still hold: repeated GETs whose slice expires mid-backoff must
    # keep reporting "running", never "failed", and the SAME job must still finish once its backend
    # actually does.
    #
    # No-overshoot (H4) cadence: the corrected driver caps every sleep at the deadline and never
    # polls past it, so a GET makes contact only when the next poll is genuinely due within its
    # slice. DEFAULT_DEADLINE_MS is set below the driver's 500ms base backoff, so the first GET's
    # slice expires right after a single transient poll (mid-backoff); each subsequent tiny slice
    # advances the virtual clock one DEFAULT_DEADLINE_MS until it reaches that pending next-poll
    # time, at which the final GET polls again and the job succeeds.
    from openreading.router.clock import FakeClock
    from openreading.server import app as app_module
    from openreading.server.app import JobRecord
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import PollFake

    clock = FakeClock()
    monkeypatch.setattr(app_module, "RealClock", lambda: clock)
    monkeypatch.setattr(app_module, "DEFAULT_DEADLINE_MS", 150.0)  # below the 500ms base backoff

    # One RetryableError("transient", ...) — never a claim of death — then succeeds on the 2nd poll.
    # Just one transient fault: under the no-overshoot driver the 500ms backoff it schedules already
    # outruns the tiny slice, so a second transient would only be reached many empty slices later —
    # the exponential backoff, not the fault count, is what paces this test now.
    adapter = PollFake(polls_needed=1, flaky=1)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "poll-fake"}}
    )
    job = adapter.submit(req, RunContext())

    app = create_app()
    client = TestClient(app)
    deadline_ms = clock.now_ms() + app_module.DEFAULT_DEADLINE_MS
    app.state.jobs[job.id] = JobRecord(
        job.id, "poll-fake", adapter, job, req, 0, deadline_ms=deadline_ms
    )

    # First GET: makes genuine backend contact (one transient poll), then its tiny slice expires
    # while the job is still mid-backoff. Genuine contact followed by a slice-expiry must report
    # "running" with no error — never latch "failed" (the BL-77 property).
    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "running"
    assert body.get("error") is None
    assert adapter.poll_calls > 0  # the slice that expired had actually reached the vendor

    # Further GETs keep expiring mid-backoff — the backend's next poll isn't due within the tiny
    # slice yet, so these correctly make no contact under the no-overshoot driver — and every one
    # must still report "running", never latch "failed".
    for _ in range(2):
        r = client.get(f"/v1/jobs/{job.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "running"
        assert body.get("error") is None

    # ...and once the clock has reached the backend's next-poll time, the SAME job polls again and
    # reaches "succeeded" — the earlier slice-expiries never permanently disabled driving it.
    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"


def test_get_job_deadline_survives_a_caller_gap_between_polls(monkeypatch):
    # BL-92: extends the test above with the ONE dimension the other BL-77/BL-88 tests don't — a
    # caller-side gap BETWEEN two `client.get()` calls, modeling a considerate "don't hammer the
    # endpoint" polling cadence rather than back-to-back calls. `get_job` used to hand `_drive_job`
    # the JobRecord's own anchored `deadline_ms` (renewed to `now + DEFAULT_DEADLINE_MS` at the END
    # of the call that last renewed it); once the caller's gap since that renewal outran the anchor,
    # the NEXT GET began already past its deadline — driver.py's deadline check is the first thing
    # the loop body does, so it raised before `adapter.poll()` ran, `get_job` rolled the anchor
    # forward, and the GET reported "running" having made ZERO contact with the backend. The fix:
    # `get_job` passes `None` to `_drive_job`, never `rec.deadline_ms`, so every call measures its
    # own slice from THAT call's own start, regardless of any prior gap.
    #
    # No-overshoot (H4) cadence: the corrected driver never polls past the deadline, so the honest
    # way to exercise "contact resumes after a gap" is to make the backend genuinely DUE at the
    # second GET — advance the caller-side clock PAST the pending next_poll_at (the driver's base
    # backoff after the first transient poll), not merely past DEFAULT_DEADLINE_MS. The strict-
    # increase `poll_calls` assertion is the guard: it tells a real poll apart from a "running"
    # result that never reached the vendor at all.
    from openreading.router.clock import FakeClock
    from openreading.server import app as app_module
    from openreading.server.app import JobRecord
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import PollFake

    clock = FakeClock()
    monkeypatch.setattr(app_module, "RealClock", lambda: clock)
    monkeypatch.setattr(app_module, "DEFAULT_DEADLINE_MS", 100.0)  # tiny; backoff outruns it

    # One RetryableError("transient", ...) then succeeds on the 2nd poll. Under the no-overshoot
    # driver the first GET makes exactly ONE genuine poll (the transient fault) before the 500ms
    # backoff it schedules outruns the tiny slice and it expires _DriveSliceExpired, without
    # resolving — leaving exactly one more genuine poll, made by the post-gap second GET, to finish.
    adapter = PollFake(polls_needed=1, flaky=1)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "poll-fake"}}
    )
    job = adapter.submit(req, RunContext())

    app = create_app()
    client = TestClient(app)
    deadline_ms = clock.now_ms() + app_module.DEFAULT_DEADLINE_MS
    app.state.jobs[job.id] = JobRecord(
        job.id, "poll-fake", adapter, job, req, 0, deadline_ms=deadline_ms
    )

    # First GET: makes genuine contact (one transient poll, poll_calls > 0) then its slice expires
    # once the 500ms backoff outruns the tiny deadline — the job does not resolve and stays running.
    r1 = client.get(f"/v1/jobs/{job.id}")
    assert r1.status_code == 200
    assert r1.json()["state"] == "running"
    polls_after_first_call = adapter.poll_calls
    assert polls_after_first_call > 0

    # An ordinary caller gap elapses before the NEXT GET — real elapsed time on the caller's side,
    # not something `_drive_job`'s own `clock.sleep()` calls would ever produce. It must reach PAST
    # the backend's pending next_poll_at (the driver's base backoff after GET1's transient poll) so
    # the backend is genuinely DUE at the second GET; a gap merely bigger than DEFAULT_DEADLINE_MS
    # would leave it not-yet-due, which the corrected driver correctly reports as a contact-free
    # slice expiry. Read the pending time off the record so this stays correct if the backoff moves.
    pending_next_poll = app.state.jobs[job.id].job.next_poll_at
    clock._now = pending_next_poll + app_module.DEFAULT_DEADLINE_MS

    # Second GET: pre-fix, the renewed-but-now-stale anchor makes this raise _DriveSliceExpired on
    # its very first loop check, before adapter.poll() ever runs again — poll_calls does not move,
    # and the job reports "running" having made zero contact with the backend this call. Post-fix,
    # this call measures a fresh slice from ITS OWN start regardless of the gap, so it reaches the
    # now-due backend — and since only one more genuine poll was ever needed, resolves.
    r2 = client.get(f"/v1/jobs/{job.id}")
    assert r2.status_code == 200
    assert adapter.poll_calls > polls_after_first_call  # genuine contact was made this call
    assert r2.json()["state"] == "succeeded"


def test_get_job_max_consecutive_faults_exhaustion_still_latches_failed(monkeypatch):
    # BL-77 guardrail: the fix above must exempt ONLY a deadline-only RetryableError. A genuine
    # MAX_CONSECUTIVE_FAULTS exhaustion — the backend really was asked, and really kept saying "still
    # processing", enough times to give up — is not that; it's the OTHER case bullet 2 explicitly
    # reserves `rec.error`/"failed" for, and must still latch exactly as before this fix. A
    # generous (never-elapsing) deadline isolates this from the deadline-exceeded path entirely.
    from openreading.router.clock import FakeClock
    from openreading.server import app as app_module
    from openreading.server.app import JobRecord
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import PollFake

    clock = FakeClock()
    monkeypatch.setattr(app_module, "RealClock", lambda: clock)
    monkeypatch.setattr(app_module, "DEFAULT_DEADLINE_MS", 1e12)  # never elapses in this test

    # Always retryable — MAX_CONSECUTIVE_FAULTS (120) is exceeded long before it would ever succeed.
    adapter = PollFake(polls_needed=1, flaky=200)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "poll-fake"}}
    )
    job = adapter.submit(req, RunContext())

    app = create_app()
    client = TestClient(app)
    deadline_ms = clock.now_ms() + app_module.DEFAULT_DEADLINE_MS
    app.state.jobs[job.id] = JobRecord(
        job.id, "poll-fake", adapter, job, req, 0, deadline_ms=deadline_ms
    )

    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"]["category"] == "retryable_exhausted"


def test_get_job_exhaustion_carrying_the_deadline_sentinel_still_latches_failed(monkeypatch):
    # BL-88: `get_job`'s discriminator used to be a bare string comparison
    # (`e.backend_code == "deadline_exceeded"`), not a type check. `backend_code` is ordinary
    # adapter-writable free text with no uniqueness constraint, and the openreading.strategies.model docstring's own
    # classifier table names "deadline_exceeded" as vocabulary adapters SHOULD prefer for a real
    # vendor deadline — so a GENUINE MAX_CONSECUTIVE_FAULTS exhaustion whose adapter-raised
    # RetryableError happens to carry that exact sentinel used to be misread as a harmless
    # per-call slice expiry: rec.error was never set, the job reported "running" forever, and
    # every GET re-invoked adapter.poll() with no way to ever resolve. This is
    # test_get_job_max_consecutive_faults_exhaustion_still_latches_failed above with ONE field changed
    # — the exhaustion's own backend_code is now the sentinel string — asserting the fix
    # (isinstance(e, _DriveSliceExpired), a type only driver.py's own deadline check ever raises)
    # still latches "failed" here exactly as that test does.
    from openreading.router.clock import FakeClock
    from openreading.server import app as app_module
    from openreading.server.app import JobRecord
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RunContext
    from tests.fakes import PollFake

    clock = FakeClock()
    monkeypatch.setattr(app_module, "RealClock", lambda: clock)
    monkeypatch.setattr(app_module, "DEFAULT_DEADLINE_MS", 1e12)  # never elapses in this test

    # Always retryable, exceeding MAX_CONSECUTIVE_FAULTS (120) — and every "transient" failure carries
    # the EXACT sentinel string driver.py's own deadline check uses, proving the discriminator
    # tells them apart by type, not by re-deriving a "safer" string comparison.
    adapter = PollFake(polls_needed=1, flaky=200, backend_code="deadline_exceeded")
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "poll-fake"}}
    )
    job = adapter.submit(req, RunContext())

    app = create_app()
    client = TestClient(app)
    deadline_ms = clock.now_ms() + app_module.DEFAULT_DEADLINE_MS
    app.state.jobs[job.id] = JobRecord(
        job.id, "poll-fake", adapter, job, req, 0, deadline_ms=deadline_ms
    )

    r = client.get(f"/v1/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"]["category"] == "retryable_exhausted"


def test_concurrent_get_job_does_not_double_drive_the_same_job():
    # BL-83: rec.job is one mutable object handed to run_in_threadpool(_drive_job, ...); two
    # concurrent GET /v1/jobs/{job_id} calls for the same still-pending job must not both invoke
    # adapter.poll() on it at once. poll() records (thread, event, ts) and sleeps briefly so an
    # unsynchronized second call would genuinely overlap the first in wall-clock time — mirrors
    # the live repro in the backlog item exactly (0.3s sleep, TestClient, separate Python
    # threads dispatched to their own worker thread).
    import threading
    import time as _time

    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest
    from openreading.types.runtime import RawResult, RunContext
    from tests.fakes import ConfigurableBackend, make_backend

    calls: list[tuple[str, str, float]] = []
    guard = threading.Lock()

    class _SlowPollBackend(ConfigurableBackend):
        def submit(self, req, ctx):
            j = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            j.next_poll_at = 0.0
            return j

        def poll(self, job, ctx):
            with guard:
                calls.append((threading.current_thread().name, "enter", _time.monotonic()))
            _time.sleep(0.3)
            with guard:
                calls.append((threading.current_thread().name, "exit", _time.monotonic()))
            job.state = JobState.SUCCEEDED
            job.raw = RawResult(payload="polled-done")
            return job

    app = create_app()
    client = TestClient(app)
    adapter = _SlowPollBackend(make_backend("pollslow", local=True).descriptor)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "pollslow"}}
    )
    job = adapter.submit(req, RunContext())
    # A real created_ms, not the usual placeholder 0: this test's own final GET below runs AFTER
    # the job reaches terminal, and 0 would put it outside the TTL sweep's window (M4), deleting
    # it out from under that assertion instead of exercising the double-drive guard it tests.
    created_ms = int(_time.time() * 1000)
    app.state.jobs[job.id] = JobRecord(job.id, "pollslow", adapter, job, req, created_ms)

    ready = threading.Barrier(2, timeout=30)
    responses: list = []

    def worker():
        ready.wait()  # both threads fire their GET from the same instant
        r = client.get(f"/v1/jobs/{job.id}")
        with guard:
            responses.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(responses) == 2
    for r in responses:
        assert r.status_code == 200
    poll_enters = [c for c in calls if c[1] == "enter"]
    assert len(poll_enters) == 1, f"expected exactly one adapter.poll() call, got {calls}"
    # the winning caller's drive completed the job; the loser must not have hijacked it.
    assert any(r.json()["state"] == "succeeded" for r in responses)
    # ...and the completion is durable, not a fluke of response ordering.
    assert client.get(f"/v1/jobs/{job.id}").json()["state"] == "succeeded"


# --- bounded job store: TTL sweep, capacity cap, DELETE (M4) ---------------------------
# The store used to grow forever and every record retained the FULL request (base64 document
# bytes, document.password) until process exit, with no way to remove one early.


def _seed_job(app, job_id, *, state, created_ms):
    # Minimal JobRecord for the sweep/cap tests below: adapter=None is safe because none of them
    # ever GET this job's OWN id (which would try to drive/poll it) -- they either assert on
    # app.state.jobs directly or hit an unrelated path to trigger the sweep as a side effect, the
    # same lookup test_get_unknown_job_is_404 already exercises.
    from openreading.server.app import JobRecord
    from openreading.types.enums import WaitMode
    from openreading.types.job import Job
    from openreading.types.request import OpenReadingRequest

    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "boundsbk"}}
    )
    job = Job(id=job_id, backend_id="boundsbk", wait_mode=WaitMode.INLINE, state=state)
    app.state.jobs[job_id] = JobRecord(job_id, "boundsbk", None, job, req, created_ms)


def test_expired_terminal_job_is_swept_on_get_access():
    from openreading.types.enums import JobState

    app = create_app()
    client = TestClient(app)
    _seed_job(app, "old-done", state=JobState.SUCCEEDED, created_ms=0)  # epoch: always past TTL

    # ANY jobs-store access sweeps it, not only a GET of this specific id -- ask about an
    # unrelated, nonexistent job (test_get_unknown_job_is_404's own request) to prove the sweep
    # runs as a side effect of the handler, independent of what was actually requested.
    assert client.get("/v1/jobs/does-not-exist").status_code == 404
    assert "old-done" not in app.state.jobs


def test_expired_terminal_job_is_swept_on_submit_access():
    from openreading.types.enums import JobState

    app = create_app()
    client = TestClient(app)
    _seed_job(app, "old-done", state=JobState.SUCCEEDED, created_ms=0)

    r = client.post("/v1/jobs", json=_pdf_body("pymupdf"))
    assert r.status_code == 200
    assert "old-done" not in app.state.jobs


def test_sweep_never_removes_a_non_terminal_job_regardless_of_age():
    # A still-running job must never be reaped out from under a caller mid-poll -- only TERMINAL
    # records are ever swept, no matter how old created_ms is.
    from openreading.types.enums import JobState

    app = create_app()
    client = TestClient(app)
    _seed_job(app, "old-running", state=JobState.RUNNING, created_ms=0)

    assert client.get("/v1/jobs/does-not-exist").status_code == 404
    assert "old-running" in app.state.jobs


def test_submit_job_rejected_with_429_when_store_is_at_capacity(monkeypatch):
    import openreading.server.app as app_module
    from openreading.types.enums import JobState

    monkeypatch.setattr(app_module, "_MAX_ASYNC_JOBS", 1)
    app = create_app()
    client = TestClient(app)
    # RUNNING (not terminal): occupies a slot without being swept away by this same request's own
    # top-of-handler sweep before the cap check runs.
    _seed_job(app, "occupant", state=JobState.RUNNING, created_ms=0)

    r = client.post("/v1/jobs", json=_pdf_body("pymupdf"))

    assert r.status_code == 429
    assert r.json()["error"]["category"] == "rate_limited"
    assert set(app.state.jobs) == {"occupant"}  # rejected BEFORE insertion -- store untouched


def test_delete_job_then_get_is_404(client):
    submit = client.post("/v1/jobs", json=_pdf_body("pymupdf"))
    job_id = submit.json()["job_id"]

    d = client.delete(f"/v1/jobs/{job_id}")
    assert d.status_code == 204
    assert d.content == b""

    g = client.get(f"/v1/jobs/{job_id}")
    assert g.status_code == 404
    assert g.json()["error"]["category"] == "unknown_job"


def test_delete_unknown_job_is_404(client):
    r = client.delete("/v1/jobs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"


# --- webhook ingress (8.2) -------------------------------------------------------------


def _svix_headers(secret, payload_str):
    from datetime import datetime

    from svix.webhooks import Webhook

    wh = Webhook(secret)
    msg_id = "msg_test_1"
    ts = datetime.now(UTC)
    sig = wh.sign(msg_id, ts, payload_str)
    return {"svix-id": msg_id, "svix-timestamp": str(int(ts.timestamp())), "svix-signature": sig}


def _seed_reducto_webhook_job(app):
    import json as _json
    from pathlib import Path

    from openreading.adapters.reducto import ReductoAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    adapter = ReductoAdapter()  # client None → resolve_webhook builds its own from ctx (T4a)
    job = adapter.new_job(WaitMode.WEBHOOK, state=JobState.RUNNING)
    job.backend_job_id = "job_abc"
    job.webhook_token = "job_abc"
    job.poll_handle = {"op": "parse"}
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.pdf"}, "backend": {"id": "reducto"}}
    )
    app.state.jobs[job.id] = JobRecord(job.id, "reducto", adapter, job, req, 0)
    fixture = _json.loads(
        (Path(__file__).parent / "fixtures" / "reducto" / "parse.json").read_text()
    )
    return job.id, fixture


def _test_secret():
    return "whsec_" + base64.b64encode(b"openreading-test-secret-0001").decode()


def test_webhook_valid_signature_completes_job(monkeypatch):
    import json as _json

    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_reducto_webhook_job(app)
    payload = _json.dumps({"job_id": "job_abc", "data": fixture})
    headers = _svix_headers(_test_secret(), payload)
    r = client.post("/v1/webhooks/reducto", content=payload, headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"
    schemas.validate_response(r.json()["response"])


def test_webhook_completed_job_carries_a_metered_response(monkeypatch):
    # the async surface returns the same response envelope as /v1/parse, so report_cost() reaches
    # usage.cost_usd there too (reducto: 1.0 credit from the captured fixture → $0.015)
    import json as _json

    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    app = create_app()
    client = TestClient(app)
    _, fixture = _seed_reducto_webhook_job(app)
    payload = _json.dumps({"job_id": "job_abc", "data": fixture})
    r = client.post(
        "/v1/webhooks/reducto", content=payload, headers=_svix_headers(_test_secret(), payload)
    )
    usage = r.json()["response"]["usage"]
    assert usage["cost_usd"] == 0.015 and usage["cost_basis"] == "billed"


def test_webhook_tampered_signature_is_401(monkeypatch):
    import json as _json

    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    app = create_app()
    client = TestClient(app)
    _seed_reducto_webhook_job(app)
    payload = _json.dumps({"job_id": "job_abc", "data": {}})
    headers = _svix_headers(_test_secret(), payload)
    tampered = _json.dumps(
        {"job_id": "job_abc", "data": {"evil": True}}
    )  # signed for a different body
    r = client.post("/v1/webhooks/reducto", content=tampered, headers=headers)
    assert r.status_code == 401 and r.json()["error"]["category"] == "bad_signature"


def test_webhook_missing_secret_is_401(monkeypatch):
    # BL-50: reducto declares a webhook_secret field; when the operator hasn't configured one, a
    # well-formed but completely unsigned event must be rejected outright — not silently processed
    # as a trusted vendor result. Previously this fell through `if secret:` with no `else` and
    # would have completed the job with attacker-controlled data, including a fabricated cost_usd.
    import json as _json

    monkeypatch.delenv("REDUCTO_WEBHOOK_SECRET", raising=False)
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_reducto_webhook_job(app)
    payload = _json.dumps({"job_id": "job_abc", "data": fixture})
    r = client.post("/v1/webhooks/reducto", content=payload)  # no svix-* headers at all
    assert r.status_code == 401 and r.json()["error"]["category"] == "bad_signature"
    # the forged/unsigned event must never have reached job resolution — still running, not
    # hijacked into a fabricated "succeeded" result.
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "running"


def test_webhook_unknown_backend_is_404(client):
    r = client.post("/v1/webhooks/does-not-exist", content=b"{}")
    assert r.status_code == 404


def test_webhook_bound_clients_signature_check_succeeds_end_to_end(monkeypatch):
    # BL-82: resolve_webhook's own `client is not None and not client.verify_webhook(...)` check
    # used to be dead in production only because the server always built a fresh, client-less
    # adapter here (`make_adapter("reducto")` -> `_client=None`, and nothing else ever bound one on
    # that instance) — the finding's own danger was a future refactor that reused a credential-
    # bound adapter instead, which previously could only ever REJECT: the event dict this route
    # builds never carried `_raw`, so verify_webhook always checked body.get("_raw", b"") against
    # an unsigned empty string, no matter how genuinely valid the real signature was.
    #
    # Ledger T4a closes this for real, not just for a hypothetical future refactor: `webhook()` now
    # builds a real RunContext from configured env credentials and resolve_webhook's own
    # `_get_webhook_client(ctx)` constructs a genuine, working client from it on EVERY call — a
    # fresh adapter instance with no client manually bound onto it (no more `_active_client`
    # attribute exists to bind at all) still gets its signature checked for real, which is what
    # this test now exercises with a perfectly ordinary fresh adapter. Proves both halves of the
    # BL-82 fix together — the route threads `_raw` through, and the ctx-built client's own
    # verify_webhook accepts the resulting genuine signature.
    import openreading.server.app as app_module

    secret = _test_secret()
    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", secret)
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_reducto_webhook_job(app)
    rec = app.state.jobs[job_id]

    real_make_adapter = app_module.make_adapter

    def _make_adapter(backend_id):
        return rec.adapter if backend_id == "reducto" else real_make_adapter(backend_id)

    monkeypatch.setattr(app_module, "make_adapter", _make_adapter)

    payload = json.dumps({"job_id": "job_abc", "data": fixture})
    headers = _svix_headers(secret, payload)
    r = client.post("/v1/webhooks/reducto", content=payload, headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"


def test_webhook_bound_clients_signature_check_rejects_a_tampered_body(monkeypatch):
    # BL-82's mirror case: with a real, ctx-built client (Ledger T4a's `_get_webhook_client(ctx)`,
    # no `_active_client` binding needed anymore — see the sibling test above) and `_raw` wired
    # through, a genuinely tampered body (this dispatcher's own _verify_svix gate is what actually
    # protects production traffic; this proves the adapter-side backstop independently raises this
    # codebase's own TerminalError, not an unhandled WebhookVerificationError 500, if it's ever the
    # one reached).
    import openreading.server.app as app_module

    secret = _test_secret()
    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", secret)
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_reducto_webhook_job(app)
    rec = app.state.jobs[job_id]

    real_make_adapter = app_module.make_adapter

    def _make_adapter(backend_id):
        return rec.adapter if backend_id == "reducto" else real_make_adapter(backend_id)

    monkeypatch.setattr(app_module, "make_adapter", _make_adapter)

    # sign one payload but POST a different one — the dispatcher's own _verify_svix would normally
    # catch this first; disable it here (no env secret at verify time is not an option since the
    # route requires one when the backend declares webhook_secret) by monkeypatching _verify_svix to
    # a no-op, isolating the assertion to the adapter-side backstop alone.
    monkeypatch.setattr(app_module, "_verify_svix", lambda secret, raw, headers: None)
    signed_payload = json.dumps({"job_id": "job_abc", "data": fixture})
    headers = _svix_headers(secret, signed_payload)
    tampered_payload = json.dumps({"job_id": "job_abc", "data": {"tampered": True}})
    r = client.post("/v1/webhooks/reducto", content=tampered_payload, headers=headers)
    # resolve_webhook's TerminalError propagates to the route's own `except _ADAPTER_ERRORS as e:
    # return _error_response(e)` (502 for a "terminal" backend_code outside the {424, 413} table) —
    # a clean structured error, not an unhandled WebhookVerificationError crash.
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["category"] == "terminal"
    assert body["error"]["backend_code"] == "bad_signature"
    # the tampered event must never have reached job resolution — still running, not hijacked.
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "running"


def _deliver_signed_reducto_webhook(app):
    """POST a validly-signed completion event for the job `_seed_reducto_webhook_job` seeded."""
    _job_id, fixture = _seed_reducto_webhook_job(app)
    payload = json.dumps({"job_id": "job_abc", "data": fixture})
    return TestClient(app).post(
        "/v1/webhooks/reducto", content=payload, headers=_svix_headers(_test_secret(), payload)
    )


def test_webhook_normalize_terminal_error_is_a_structured_error(monkeypatch):
    # BL-33: the leg after signature verification fails the same way submit does, so it reports
    # the same body — category/backend_code, not str(e).
    from openreading.adapters.reducto import ReductoAdapter
    from openreading.types.errors import TerminalError

    def _boom(self, job, ctx, req):
        raise TerminalError("webhook result unreadable", backend_code="bad_payload")

    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    monkeypatch.setattr(ReductoAdapter, "normalize", _boom)

    r = _deliver_signed_reducto_webhook(create_app())
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"] == {
        "category": "terminal",
        "message": "webhook result unreadable",
        "backend_code": "bad_payload",
    }


def test_webhook_normalize_crash_is_a_structured_error_not_a_500(monkeypatch):
    # A non-adapter exception out of normalize still becomes a failed job with the generic
    # envelope — never an unhandled 500.
    from openreading.adapters.reducto import ReductoAdapter

    def _boom(self, job, ctx, req):
        raise ValueError("malformed webhook data")

    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    monkeypatch.setattr(ReductoAdapter, "normalize", _boom)

    r = _deliver_signed_reducto_webhook(create_app())
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert body["error"] == {"category": "error", "message": "malformed webhook data"}


def test_webhook_metered_redacts_a_secret_in_a_normalize_crash(monkeypatch):
    # BL-93 Leg 3: the webhook leg's OWN `with auth_hinted(...)` block closes right after
    # `resolve_webhook()` a few lines up — the terminal-completion `_metered()` call sat outside
    # it, the webhook-side twin of Leg 1's identical gap (test_submit_job_metered_redacts_a_secret_
    # in_a_normalize_crash). Uses reducto's real `api_key` credential field (the same field the
    # broker resolves this leg's own `creds` from) rather than a plain ValueError, so — unlike
    # test_webhook_normalize_crash_is_a_structured_error_not_a_500 above — this would have failed
    # before this fix.
    from openreading.adapters.reducto import ReductoAdapter
    from openreading.types.errors import TerminalError

    secret = "sk-reducto-leak-0003-must-never-appear"

    def _boom(self, job, ctx, req):
        raise TerminalError(f"upstream said: bad key {secret}", backend_code="upstream_error")

    monkeypatch.setenv("REDUCTO_API_KEY", secret)
    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    monkeypatch.setattr(ReductoAdapter, "normalize", _boom)

    r = _deliver_signed_reducto_webhook(create_app())
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert secret not in body["error"]["message"]
    assert "***" in body["error"]["message"]


def test_webhook_metered_redacts_a_secret_in_a_plain_normalize_crash(monkeypatch):
    # BL-99: the identical webhook-leg call site as the two tests above, but combining both of their
    # substitutions at once — a plain ValueError (not a TerminalError/AdapterError) carrying
    # reducto's real REDUCTO_API_KEY secret. test_webhook_normalize_crash_is_a_structured_error_not_
    # a_500 proves crash-safety with nothing secret in the message; test_webhook_metered_redacts_a_
    # secret_in_a_normalize_crash proves redaction with a secret-bearing TerminalError, already
    # caught by auth_hinted's ORIGINAL except clause before this item. Neither exercises the NEW
    # except Exception clause this item adds.
    from openreading.adapters.reducto import ReductoAdapter

    secret = "sk-reducto-leak-0007-must-never-appear"

    def _boom(self, job, ctx, req):
        raise ValueError(f"upstream said: bad key {secret}")

    monkeypatch.setenv("REDUCTO_API_KEY", secret)
    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    monkeypatch.setattr(ReductoAdapter, "normalize", _boom)

    r = _deliver_signed_reducto_webhook(create_app())
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "failed"
    assert secret not in body["error"]["message"]
    assert "***" in body["error"]["message"]


# --- webhook cross-backend / event-key scoping (BL-66) ---------------------------------


def _seed_chunkr_webhook_job(app):
    import time
    from pathlib import Path

    from openreading.adapters.chunkr import ChunkrAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    adapter = ChunkrAdapter()  # client None → resolve_webhook won't attempt a refetch
    job = adapter.new_job(WaitMode.WEBHOOK, state=JobState.RUNNING)
    job.backend_job_id = "task_wh_1"
    job.webhook_token = "task_wh_1"
    job.poll_handle = {"op": "parse"}
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.pdf"}, "backend": {"id": "chunkr"}}
    )
    # A real created_ms, not the usual placeholder 0: the caller's own GET after the webhook
    # resolves this job to terminal would otherwise fall outside the TTL sweep's window (M4).
    created_ms = int(time.time() * 1000)
    app.state.jobs[job.id] = JobRecord(job.id, "chunkr", adapter, job, req, created_ms)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "chunkr" / "parse.json").read_text())
    return job.id, fixture


def _seed_open_ocr_webhook_job(app):
    import time
    from pathlib import Path

    from openreading.adapters.open_ocr import OpenOCRAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    adapter = OpenOCRAdapter()  # client None → resolve_webhook won't attempt a refetch
    job = adapter.new_job(WaitMode.WEBHOOK, state=JobState.RUNNING)
    job.backend_job_id = "req_wh_1"
    job.webhook_token = "req_wh_1"
    job.poll_handle = {"request_id": "req_wh_1"}
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.png"}, "backend": {"id": "open-ocr"}}
    )
    # A real created_ms, not the usual placeholder 0: the caller's own GET after the webhook
    # resolves this job to terminal would otherwise fall outside the TTL sweep's window (M4).
    created_ms = int(time.time() * 1000)
    app.state.jobs[job.id] = JobRecord(job.id, "open-ocr", adapter, job, req, created_ms)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "open-ocr" / "ocr.json").read_text())
    return job.id, fixture


def test_webhook_cross_backend_hijack_is_404(monkeypatch):
    # BL-66 Defect 1: an event posted to a DIFFERENT backend's webhook URL must never resolve a
    # job that isn't that backend's own, even when the id matches — previously the lookup ignored
    # backend_id entirely and matched on id alone across the whole in-memory job store. reducto's
    # secret is configured (and genuinely enforced on ITS OWN url — see
    # test_webhook_valid_signature_completes_job et al.) to show that routing the identical id
    # through chunkr's URL instead bypasses that protection completely.
    monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", _test_secret())
    app = create_app()
    client = TestClient(app)
    job_id, _fixture = _seed_reducto_webhook_job(
        app
    )  # backend_job_id == webhook_token == "job_abc"
    # chunkr declares no webhook_secret at all, so this unsigned forged event reaches the lookup
    # unauthenticated — shaped as a genuine chunkr callback (task_id), naming the reducto job's id.
    r = client.post("/v1/webhooks/chunkr", content=json.dumps({"task_id": "job_abc"}))
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"
    # the reducto job itself is untouched — not hijacked into "succeeded" via the sibling URL.
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "running"


def test_webhook_wrong_wait_mode_hijack_is_404():
    # BL-66 Defect 1's other half: a POLL-only job of the SAME backend — no webhook support for
    # this particular job at all — must never resolve via /v1/webhooks/{backend_id} either, even
    # though the backend matches and the id matches. Chunkr sets backend_job_id unconditionally,
    # poll or webhook mode alike, so a poll-mode job carries the identical exposed value.
    from openreading.adapters.chunkr import ChunkrAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    app = create_app()
    client = TestClient(app)
    adapter = ChunkrAdapter()
    job = adapter.new_job(WaitMode.POLL, state=JobState.RUNNING)
    job.backend_job_id = "task_poll_1"
    job.webhook_token = None  # never set for a POLL-mode job
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.pdf"}, "backend": {"id": "chunkr"}}
    )
    rec = JobRecord(job.id, "chunkr", adapter, job, req, 0)
    app.state.jobs[job.id] = rec
    r = client.post("/v1/webhooks/chunkr", content=json.dumps({"task_id": "task_poll_1"}))
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"
    # the job record itself is untouched — resolve_webhook was never reached. (Not asserted via
    # GET /v1/jobs/{id}: that endpoint drives a pending POLL-mode job on demand — correct,
    # documented, unrelated behavior — which would confuse "driven, no client bound" with
    # "hijacked" for this deliberately client-less fixture.)
    assert rec.job.state is JobState.RUNNING
    assert rec.response is None and rec.error is None


def test_webhook_chunkr_genuine_event_resolves():
    # BL-66 Defect 2: a genuinely-shaped chunkr callback (its own task_id field; no job_id anywhere
    # in the event) must resolve — previously the dispatcher only ever read event["job_id"]
    # (reducto's own field name), so this exact shape 404'd regardless of authentication.
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_chunkr_webhook_job(app)
    payload = json.dumps({"task_id": "task_wh_1", "data": fixture})
    r = client.post("/v1/webhooks/chunkr", content=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "succeeded"
    schemas.validate_response(body["response"])
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "succeeded"


def test_webhook_open_ocr_genuine_event_resolves():
    # BL-66 Defect 2, open-ocr's own field name (request_id; no job_id anywhere in the event).
    app = create_app()
    client = TestClient(app)
    job_id, fixture = _seed_open_ocr_webhook_job(app)
    payload = json.dumps({"request_id": "req_wh_1", "data": fixture})
    r = client.post("/v1/webhooks/open-ocr", content=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "succeeded"
    schemas.validate_response(body["response"])
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "succeeded"


def test_webhook_secret_required_by_backend():
    # Folded in (BL-66) from a third reviewer's finding: a cheap, HTTP-round-trip-free lock on the
    # BL-50 gate itself — reducto is the only backend that ever declares webhook_secret.
    from openreading.server.app import _webhook_secret_required

    assert _webhook_secret_required("reducto") is True
    assert _webhook_secret_required("chunkr") is False
    assert _webhook_secret_required("open-ocr") is False


# --- webhook id-less hijack (BL-70) -----------------------------------------------------


def _seed_chunkr_webhook_job_no_id(app):
    from openreading.adapters.chunkr import ChunkrAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    adapter = ChunkrAdapter()  # client None → resolve_webhook won't attempt a refetch
    job = adapter.new_job(WaitMode.WEBHOOK, state=JobState.RUNNING)
    job.backend_job_id = None  # a vendor create-task 2xx that omitted its id field
    job.webhook_token = None
    job.poll_handle = {"op": "parse"}
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.pdf"}, "backend": {"id": "chunkr"}}
    )
    app.state.jobs[job.id] = JobRecord(job.id, "chunkr", adapter, job, req, 0)
    return job.id


def _seed_open_ocr_webhook_job_no_id(app):
    from openreading.adapters.open_ocr import OpenOCRAdapter
    from openreading.server.app import JobRecord
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.request import OpenReadingRequest

    adapter = OpenOCRAdapter()  # client None → resolve_webhook won't attempt a refetch
    job = adapter.new_job(WaitMode.WEBHOOK, state=JobState.RUNNING)
    job.backend_job_id = None  # a vendor create-request 2xx that omitted its id field
    job.webhook_token = None
    req = OpenReadingRequest.model_validate(
        {"document": {"url": "https://x/d.png"}, "backend": {"id": "open-ocr"}}
    )
    app.state.jobs[job.id] = JobRecord(job.id, "open-ocr", adapter, job, req, 0)
    return job.id


def test_webhook_idless_job_not_hijacked_by_idless_event_chunkr():
    # BL-70: submit() can leave a WaitMode.WEBHOOK job with backend_job_id == webhook_token ==
    # None (a vendor 2xx create-task response that omitted its own id field). Previously
    # `jid in (None, None)` was True for jid = None BY CONSTRUCTION, so an unauthenticated POST
    # whose body simply omits the id key would resolve straight to this job — hijacking it to
    # "succeeded" with attacker-controlled content and a fabricated cost. Direct mirror of BL-66's
    # own test_webhook_cross_backend_hijack_is_404 shape, for this defect instead.
    app = create_app()
    client = TestClient(app)
    job_id = _seed_chunkr_webhook_job_no_id(app)
    r = client.post("/v1/webhooks/chunkr", content=json.dumps({}))  # no task_id key at all
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"
    # the id-less job itself is untouched — not hijacked into "succeeded" via the id-less event.
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "running"


def test_webhook_idless_job_not_hijacked_by_idless_event_open_ocr():
    # BL-70, open-ocr's own field name (request_id) — the identical id-less hijack shape.
    app = create_app()
    client = TestClient(app)
    job_id = _seed_open_ocr_webhook_job_no_id(app)
    r = client.post("/v1/webhooks/open-ocr", content=json.dumps({}))  # no request_id key at all
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"
    assert client.get(f"/v1/jobs/{job_id}").json()["state"] == "running"


def test_server_builds_fresh_adapters_per_request():
    # D-v2-6 invariant: the server never reuses a credential-bound adapter across requests.
    from openreading.adapters.registry import make_adapter

    assert make_adapter("reducto") is not make_adapter("reducto")


# --- status table: the remaining codes (GAP-1 fix) -------------------------------------


def test_parse_named_backend_violating_compliance_is_403(client):
    # a directly-named hosted backend that violates the request's compliance is REFUSED (403),
    # not silently run. (reducto is not local → fails require_local; compliance gate fires before
    # the credential check, so this needs no key.)
    body = _pdf_body("reducto")
    body["compliance"] = {"require_local": True}
    r = client.post("/v1/parse", json=body)
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "compliance_refused"


def test_jobs_named_backend_violating_compliance_is_403(client):
    # BL-91: mirrors test_parse_named_backend_violating_compliance_is_403 above, but against
    # /v1/jobs — submit_job's named-backend branch must compliance-gate exactly like /v1/parse's
    # run_request does, not silently drive a non-compliant backend to completion.
    body = _pdf_body("reducto")
    body["compliance"] = {"require_local": True}
    r = client.post("/v1/jobs", json=body)
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "compliance_refused"


def test_jobs_missing_credentials_message_includes_signup_url(client, monkeypatch):
    # BL-91: /v1/jobs's missing-credentials message must match /v1/parse's wording exactly —
    # including the signup_url hint — for the identical failure, so a future hand-copy can't let
    # the two sides drift again. chunkr declares signup_url="https://chunkr.ai" and requires only
    # CHUNKR_API_KEY.
    monkeypatch.delenv("CHUNKR_API_KEY", raising=False)
    body = _pdf_body("chunkr")

    parse_err = client.post("/v1/parse", json=body).json()["error"]
    jobs_err = client.post("/v1/jobs", json=body).json()["error"]

    assert "Sign up / configure: https://chunkr.ai" in parse_err["message"]
    assert jobs_err["message"] == parse_err["message"]
    assert jobs_err["backend_code"] == parse_err["backend_code"] == "missing_credentials"
    assert jobs_err["missing_env"] == parse_err["missing_env"] == ["CHUNKR_API_KEY"]


def test_error_response_status_table():
    # unit-cover the mapping for the codes without a convenient offline integration trigger.
    from openreading.server.app import _error_response
    from openreading.types.errors import (
        PlanExhaustedError,
        RetryableError,
        TerminalError,
    )

    assert _error_response(PlanExhaustedError("all failed", trail=[])).status_code == 502
    assert (
        _error_response(TerminalError("too big", backend_code="doc_too_large")).status_code == 413
    )
    assert _error_response(RetryableError("deadline")).status_code == 504
    assert _error_response(TerminalError("boom", backend_code="other")).status_code == 502


@pytest.mark.parametrize(
    "path",
    [
        "/v1/parse",
        "/v1/route",
        "/v1/compare",
        "/v1/batch",
        "/v1/jobs",
        "/v1/webhooks/reducto",
    ],
)
def test_malformed_json_body_is_400(client, monkeypatch, path):
    # every body-taking endpoint rejects unparseable JSON the same way (documented 400 row).
    # /v1/webhooks verifies the signature BEFORE parsing; since BL-50 a missing secret is now
    # rejected too (401), so reaching the reducto case's JSON branch needs a configured secret and
    # a validly-signed body, not just an absent one (`delenv` alone no longer gets past the gate).
    body = b'{"document": '
    headers = {"content-type": "application/json"}
    if path == "/v1/webhooks/reducto":
        secret = _test_secret()
        monkeypatch.setenv("REDUCTO_WEBHOOK_SECRET", secret)
        headers = _svix_headers(secret, body.decode())
    r = client.post(path, content=body, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "bad_request"


# --- M2: transport request-body cap + compare collection cap ---------------------------


def test_request_body_over_declared_content_length_is_413(monkeypatch):
    # Content-Length path: _BodyLimitMiddleware must 413 BEFORE the app ever reads the body.
    # A monkeypatched-tiny cap plus a real oversized JSON payload proves the declared-length
    # leg fires without needing an actual 150MB body in the test.
    import openreading.server.app as app_module

    monkeypatch.setattr(app_module, "_MAX_BODY_BYTES", 100)
    app = create_app()
    client = TestClient(app)
    oversized = json.dumps({"document": {"bytes_base64": "A" * 1000}}).encode()
    assert len(oversized) > 100  # the whole point: a real body over the (tiny) cap

    r = client.post("/v1/parse", content=oversized, headers={"content-type": "application/json"})

    assert r.status_code == 413
    body = r.json()
    assert body["error"]["backend_code"] == "doc_too_large"
    assert "too large" in body["error"]["message"]


def test_request_body_under_cap_is_unaffected(monkeypatch):
    # A small body under a small cap must pass straight through the middleware — proves the
    # 413 above is about SIZE, not a middleware that rejects every request.
    import openreading.server.app as app_module

    monkeypatch.setattr(app_module, "_MAX_BODY_BYTES", 1_000_000)
    client = TestClient(create_app())

    r = client.post("/v1/compare", json={"nope": 1})

    assert r.status_code == 400  # the existing shape-check 400, not a 413
    assert r.json()["error"]["category"] == "bad_request"


def test_chunked_body_over_cap_is_cut_off(monkeypatch):
    # The no-Content-Length (chunked) leg: best-effort by design (class docstring) — it disconnects
    # mid-stream rather than answering a clean 413, and what the app does with a disconnected
    # receive is whatever Starlette's own Request.stream() does with one (here: the truncated body
    # fails JSON decoding, a 400). The one thing that MUST hold regardless of the exact status is
    # the security property this middleware exists for: an oversized streamed body is never fully
    # buffered and accepted. Proven against a body that would otherwise SUCCEED (a real, complete,
    # valid pymupdf parse request, streamed a slice at a time so httpx/TestClient never precomputes
    # a Content-Length and this leg — not the declared-length fast path above — is what runs): if
    # the cutoff did nothing, this would be a 200, so a non-200 here is the cutoff actually firing,
    # not just "the request happened to be malformed."
    import openreading.server.app as app_module

    raw = json.dumps(_pdf_body("pymupdf")).encode()
    monkeypatch.setattr(app_module, "_MAX_BODY_BYTES", len(raw) // 2)
    client = TestClient(app_module.create_app())

    def chunks():
        step = 2000
        for i in range(0, len(raw), step):
            yield raw[i : i + step]

    r = client.post("/v1/parse", content=chunks(), headers={"content-type": "application/json"})

    assert r.status_code != 200


def test_compare_over_ceiling_is_400_naming_count_and_limit(client):
    from openreading.server.app import _MAX_COMPARE_RESPONSES

    responses = [{"id": i} for i in range(_MAX_COMPARE_RESPONSES + 1)]
    r = client.post("/v1/compare", json={"responses": responses})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["category"] == "bad_request"
    assert str(_MAX_COMPARE_RESPONSES + 1) in body["error"]["message"]
    assert str(_MAX_COMPARE_RESPONSES) in body["error"]["message"]


def test_compare_at_ceiling_is_not_rejected_for_count_alone(client):
    # Boundary check: exactly _MAX_COMPARE_RESPONSES must not be rejected by the count guard
    # itself. These placeholder dicts are not valid response envelopes, so the request still
    # ends up 400 — but on schema validation of input #1, not on the count message, which
    # proves the request got PAST the count check.
    from openreading.server.app import _MAX_COMPARE_RESPONSES

    responses = [{"id": i} for i in range(_MAX_COMPARE_RESPONSES)]
    r = client.post("/v1/compare", json={"responses": responses})
    assert r.status_code == 400
    assert "too many responses" not in r.json()["error"]["message"]


# --- /v1/batch (Manifest v0.6) ----------------------------------------------------------


def _batch_doc(name="a.pdf"):
    return {
        "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
        "mime_type": "application/pdf",
        "filename": name,
    }


def test_batch_endpoint_returns_schema_valid_envelope(client):
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")], "backend": "pymupdf"},
    )
    assert r.status_code == 200
    env = r.json()
    schemas.validate_batch_result(env)
    assert env["schema_version"] == "0.1" and "items" in env
    assert env["summary"]["succeeded"] == 2 and env["status"]["state"] == "succeeded"
    assert all(i["transport"] == "platform" for i in env["items"])


def test_batch_endpoint_missing_documents_is_400(client):
    r = client.post("/v1/batch", json={"backend": "pymupdf"})
    assert r.status_code == 400


def test_batch_endpoint_unknown_backend_is_404(client):
    r = client.post("/v1/batch", json={"documents": [_batch_doc()], "backend": "nope-xyz"})
    assert r.status_code == 404


def test_batch_endpoint_object_form_backend_is_a_400_envelope_not_a_bare_500(client):
    """`{"backend": {"id": "pymupdf"}}` is the shape `/v1/parse` and the vendored request schema
    use, so a client that reuses its own body builder sends it here. It reached `make_adapter`
    unstringified and escaped as an unhandled exception: HTTP 500 with a plain-text
    `Internal Server Error` body — the one response a client written from the error ladder
    ("every error body has one shape") cannot parse.

    400, not 500: this endpoint's contract is a string `backend`, so an object is a malformed
    body in exactly the class of `"jobs": "many"` — the crash was the bug, the status never was.
    The object is refused rather than reduced to its `id` because `backend` also carries
    `operation`, `version`, `credentials_ref` and `runtime`, every one of which changes what the
    parse does; accepting the shape and keeping only `id` would silently run the wrong operation.
    """
    r = client.post("/v1/batch", json={"documents": [_batch_doc()], "backend": {"id": "pymupdf"}})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"]["category"] == "bad_request"
    assert '"backend": "pymupdf"' in body["error"]["message"]  # names the exact fix


def test_batch_endpoint_non_string_backend_is_a_400_envelope(client):
    for bad in (["pymupdf"], 7, None):
        r = client.post("/v1/batch", json={"documents": [_batch_doc()], "backend": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"]["category"] == "bad_request"


def test_batch_endpoint_documents_over_max_is_400(client):
    from openreading.server.app import MAX_BATCH_DOCUMENTS

    docs = [_batch_doc(f"{i}.pdf") for i in range(MAX_BATCH_DOCUMENTS + 1)]
    r = client.post("/v1/batch", json={"documents": docs, "backend": "pymupdf"})
    assert r.status_code == 400
    assert str(MAX_BATCH_DOCUMENTS) in r.json()["error"]["message"]


def test_batch_endpoint_jobs_must_be_an_integer(client):
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc()], "backend": "pymupdf", "jobs": "not-a-number"},
    )
    assert r.status_code == 400


def test_batch_endpoint_jobs_over_ceiling_is_400(client):
    from openreading.types.batch import MAX_BATCH_JOBS

    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc()], "backend": "pymupdf", "jobs": MAX_BATCH_JOBS + 1},
    )
    assert r.status_code == 400
    assert str(MAX_BATCH_JOBS + 1) in r.json()["error"]["message"]


def test_batch_endpoint_echoes_jobs_actually_used(client):
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "jobs": 2,
        },
    )
    assert r.status_code == 200
    assert r.json()["request"]["jobs"] == 2


def test_batch_endpoint_empty_documents_surfaces_empty_batch_warning(client):
    # BL-147 criterion (c): the runner-level empty_batch fix is a schema-level change to
    # assemble_result, so the server's POST /v1/batch picks it up with no server-side code change
    # — closing the scope-handoff the review 1:1 named but didn't chase.
    r = client.post("/v1/batch", json={"documents": []})
    assert r.status_code == 200
    env = r.json()
    schemas.validate_batch_result(env)
    assert env["summary"]["total"] == 0
    assert [w["code"] for w in env.get("warnings", [])] == ["empty_batch"]
    assert env["warnings"][0]["message"] == "no source resolved to a document to process"


def test_batch_endpoint_per_item_isolation(client):
    bad = {
        "bytes_base64": base64.b64encode(b"not a pdf at all").decode(),
        "mime_type": "application/pdf",
        "filename": "bad.pdf",
    }
    r = client.post(
        "/v1/batch", json={"documents": [_batch_doc("good.pdf"), bad], "backend": "pymupdf"}
    )
    assert r.status_code == 200
    env = r.json()
    assert env["status"]["state"] == "partial"
    assert (env["summary"]["succeeded"], env["summary"]["failed"]) == (1, 1)


def test_batch_endpoint_rejects_document_path_by_default(client, monkeypatch):
    # M6 per-item isolation (see test_batch_endpoint_per_item_isolation): a refused document.path
    # fails that ONE item rather than the whole batch, the same as any other bad item.
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    r = client.post(
        "/v1/batch",
        json={"documents": [{"path": "/etc/hosts"}], "backend": "pymupdf"},
    )
    assert r.status_code == 200
    env = r.json()
    assert env["summary"]["failed"] == 1
    assert "document.path" in env["items"][0]["error"]["message"]


def test_batch_endpoint_duration_ms_is_not_the_fabricated_zero(client, monkeypatch):
    # BL-78: the endpoint used to call assemble_result directly with its literal duration_ms=0
    # default. A batch that provably took real wall-clock time must report it (never 0/absent).
    # A tiny sleep on the real per-item entry point makes "the batch took nonzero time" true by
    # construction, so this assertion can never pass by coincidence of a fast CPU racing a clock.
    import time as _time

    from openreading import api as api_module

    real_run_request = api_module.run_request

    def slow_run_request(*args, **kwargs):
        _time.sleep(0.02)
        return real_run_request(*args, **kwargs)

    monkeypatch.setattr(api_module, "run_request", slow_run_request)

    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")], "backend": "pymupdf"},
    )
    assert r.status_code == 200
    env = r.json()
    schemas.validate_batch_result(env)
    assert env["summary"]["duration_ms"] is not None
    assert env["summary"]["duration_ms"] > 0  # not the fabricated literal 0


def test_batch_endpoint_echoes_the_caller_supplied_jobs(client):
    # BL-78: `jobs` used to be read only far enough to keep it out of the per-item request, then
    # discarded — never echoed, honored, or rejected. It must now be visible on `request.jobs`.
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "jobs": 4,
        },
    )
    assert r.status_code == 200
    env = r.json()
    schemas.validate_batch_result(env)
    assert env["request"]["jobs"] == 4
    assert env["summary"]["succeeded"] == 2


def test_batch_endpoint_jobs_defaults_to_one(client):
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc("a.pdf")], "backend": "pymupdf"},
    )
    assert r.status_code == 200
    assert r.json()["request"]["jobs"] == 1


# --- BL-84: jobs/documents floor & ceiling on the server surface ------------------------


def test_batch_endpoint_jobs_floor_clamps_to_one(client):
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "jobs": 0,
        },
    )
    assert r.status_code == 200
    env = r.json()
    schemas.validate_batch_result(env)
    assert env["request"]["jobs"] == 1  # clamped, not the raw 0
    assert env["summary"]["succeeded"] == 2


def test_batch_endpoint_jobs_over_ceiling_is_400_naming_count_and_limit(client):
    from openreading.batch.runner import MAX_BATCH_JOBS

    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf")],
            "backend": "pymupdf",
            "jobs": MAX_BATCH_JOBS + 1,
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["category"] == "bad_request"
    assert str(MAX_BATCH_JOBS + 1) in body["error"]["message"]
    assert str(MAX_BATCH_JOBS) in body["error"]["message"]


def test_batch_endpoint_jobs_wildly_over_ceiling_is_400_not_5000000_real_threads(client):
    # The live-reproduced BL-84 repro: this must never reach ThreadPoolExecutor(max_workers=...).
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc("a.pdf")], "backend": "pymupdf", "jobs": 5000000},
    )
    assert r.status_code == 400


def test_batch_endpoint_jobs_ceiling_has_no_caller_facing_override(client):
    # Unlike CLI/Python-API, the request body is the untrusted-input boundary itself — there is no
    # "max_jobs" field a caller can set to raise it.
    from openreading.batch.runner import MAX_BATCH_JOBS

    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf")],
            "backend": "pymupdf",
            "jobs": MAX_BATCH_JOBS + 1,
            "max_jobs": MAX_BATCH_JOBS + 1,
        },
    )
    assert r.status_code == 400


def test_batch_endpoint_documents_over_ceiling_is_400_naming_count_and_limit(client):
    from openreading.server.app import MAX_BATCH_DOCUMENTS

    docs = [{"filename": f"{i}.pdf"} for i in range(MAX_BATCH_DOCUMENTS + 1)]
    r = client.post("/v1/batch", json={"documents": docs, "backend": "pymupdf"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["category"] == "bad_request"
    assert str(MAX_BATCH_DOCUMENTS + 1) in body["error"]["message"]
    assert str(MAX_BATCH_DOCUMENTS) in body["error"]["message"]


def test_batch_endpoint_documents_at_ceiling_is_not_rejected_for_count_alone(client):
    # Boundary check: exactly MAX_BATCH_DOCUMENTS must not be rejected by the count guard itself
    # (an unknown backend 404 proves the request got PAST the documents-count check).
    from openreading.server.app import MAX_BATCH_DOCUMENTS

    docs = [{"filename": f"{i}.pdf"} for i in range(MAX_BATCH_DOCUMENTS)]
    r = client.post("/v1/batch", json={"documents": docs, "backend": "nope-xyz"})
    assert r.status_code == 404  # unknown backend, not the documents-count 400


# --- BL-102: unrecognized top-level batch-body fields are rejected once, up front -------


def test_batch_endpoint_unrecognized_field_max_jobs_is_400_not_per_item_failures(client):
    # max_jobs is the foreseeable typo, not a contrived one: it's the correctly-spelled parameter
    # name for the identical semantic control on this feature's other two surfaces (--max-jobs on
    # the CLI, max_jobs= in the Python API), sitting one field below `jobs` -- which this endpoint
    # DOES accept -- in the same request body. Before the fix, this merged into every per-item
    # request and surfaced as HTTP 200 with every item `state: "failed"`, `error.code:
    # "ValidationError"`, naming `OpenReadingRequest` -- a name that appears nowhere in the
    # /v1/batch request docs. It must instead be one single, request-level 400.
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "jobs": 2,
            "max_jobs": 4,
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "items" not in body  # not a batch-result envelope with per-item failures
    assert body["error"]["category"] == "bad_request"
    assert "max_jobs" in body["error"]["message"]
    assert "OpenReadingRequest" not in body["error"]["message"]


def test_batch_endpoint_unrecognized_field_typo_is_400_not_per_item_failures(client):
    # The mechanism is general, not specific to max_jobs: any unrecognized top-level key gets the
    # identical treatment -- a second, arbitrary typo'd key reproduces the same single 400.
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc()],
            "backend": "pymupdf",
            "otuput_format": "markdown",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "items" not in body
    assert body["error"]["category"] == "bad_request"
    assert "otuput_format" in body["error"]["message"]


def test_batch_endpoint_unrecognized_field_names_all_bad_keys_at_once(client):
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc()], "backend": "pymupdf", "bogus_a": 1, "bogus_b": 2},
    )
    assert r.status_code == 400
    message = r.json()["error"]["message"]
    assert "bogus_a" in message and "bogus_b" in message


def test_batch_endpoint_recognized_shared_field_still_applies_to_every_item(client):
    # Positive control: a real OpenReadingRequest field shared across the batch (not
    # backend/documents/jobs) must keep working -- the fix rejects only what's genuinely
    # unrecognized, not the legitimate shared-field mechanism itself. `outputs.text=False` here
    # means every item's response should drop the "text" key entirely (confirming `shared` was
    # actually applied, not just tolerated).
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "outputs": {"markdown": True, "text": False, "blocks": False},
        },
    )
    assert r.status_code == 200
    env = r.json()
    assert env["summary"]["succeeded"] == 2
    assert all("text" not in i["response"]["document"] for i in env["items"])
    assert all("markdown" in i["response"]["document"] for i in env["items"])


# --- BL-105: a caller-supplied top-level `document` field must never silently override every ---
# --- batch item's real content -------------------------------------------------------------------


def test_batch_endpoint_top_level_document_field_is_400_not_silent_override(client):
    # `document` (singular) genuinely IS an OpenReadingRequest field name, so it sailed through
    # BL-102's "is this a real field name" check untouched and landed in `shared` exactly like a
    # legitimate shared field would. run_one's dict literal ({"document": docs_by_relpath[relpath],
    # "backend": {...}, **shared}) then let shared's own `document` key -- appearing last -- win
    # over every item's real per-item document, silently. A caller adapting a working single-
    # document request template into a batch call by adding "documents" without deleting the
    # original "document" key reproduces this exactly. Must be one clean, request-level 400 naming
    # the field -- never a batch-result envelope reporting `state: "succeeded"` with honest per-item
    # filenames for content that was never actually parsed.
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc("a.pdf"), _batch_doc("b.pdf")],
            "backend": "pymupdf",
            "document": _batch_doc("evil.pdf"),
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "items" not in body  # never a batch-result envelope with silent per-item substitution
    assert body["error"]["category"] == "bad_request"
    assert "document" in body["error"]["message"]


def test_batch_endpoint_shared_fields_are_an_allowlist_not_any_real_field_name(client):
    # The fix is an ALLOWLIST of the fields meant to apply batch-wide (matching the code's own
    # comment: outputs / extraction_schema / features / pages / compliance) -- not "any name
    # OpenReadingRequest happens to recognize". `routing` is a real OpenReadingRequest field,
    # exactly like `document`, but was never one of the documented shared fields (the openreading.server docstring);
    # it must be rejected the same way `document` is, not silently accepted into `shared` just
    # because pydantic recognizes the name. Forward-safe against any future OpenReadingRequest
    # field that shouldn't become an implicit batch-wide override.
    r = client.post(
        "/v1/batch",
        json={
            "documents": [_batch_doc()],
            "backend": "pymupdf",
            "routing": {"optimize_for": "cost"},
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "items" not in body
    assert "routing" in body["error"]["message"]


# --- caller authentication (BL-159) -----------------------------------------------------
#
# Every test below that configures OPENREADING_API_KEYS[_SCOPES] builds its OWN TestClient
# AFTER setting the env, rather than taking the shared `client` fixture — `_load_api_key_config()`
# runs ONCE inside create_app() (AC-7: it must fail server startup, not the first request), so a
# test using the module-level `client` fixture would have its app already constructed, with a
# frozen (empty) api_key_config, before the test body's own monkeypatch.setenv ever ran. The
# strategy-config fail-fast precedent this mirrors has the identical constraint, which is why the
# existing strategy tests below (e.g. test_jobs_strategy_id_wraps_the_walk_as_a_synthetic_job)
# never use the shared fixture either.

_PROTECTED_POST_PATHS = [
    "/v1/parse",
    "/v1/route",
    "/v1/compare",
    "/v1/batch",
    "/v1/jobs",
    # Pulse: this one resolves a vendor credential and emits a call to that vendor, so it is
    # exactly the boundary the key allow-list exists for — it must never escape this sweep.
    "/v1/backends/pymupdf/liveness",
]
_PROTECTED_GET_PATHS = ["/v1/backends", "/v1/jobs/does-not-exist"]


def test_caller_auth_off_by_default_leaves_every_endpoint_byte_for_byte_unchanged(client):
    # BL-159.1 (AC-1): zero OPENREADING_API_KEYS configured — the `client` fixture's own default,
    # and this whole file's status quo — is already proven continuously by every other test in it;
    # this is the single, explicit, dedicated check spanning /healthz, a webhook POST, and a GET +
    # POST control-plane/document-path endpoint in one place, each assertion mirroring an existing,
    # already-passing test's own exact expectation (test_healthz,
    # test_backends_lists_all_with_readiness, test_parse_pymupdf_returns_schema_valid_response,
    # test_jobs_local_backend_completes_immediately, test_get_unknown_job_is_404,
    # test_webhook_unknown_backend_is_404).
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/backends").status_code == 200
    assert client.post("/v1/parse", json=_pdf_body("pymupdf")).status_code == 200
    assert client.post("/v1/jobs", json=_pdf_body("pymupdf")).status_code == 200
    assert client.get("/v1/jobs/does-not-exist").status_code == 404  # its own 404, not a 401
    assert client.post("/v1/webhooks/does-not-exist", json={}).status_code == 404  # unknown_backend


@pytest.mark.parametrize("path", _PROTECTED_POST_PATHS)
def test_caller_auth_missing_or_wrong_token_is_401_on_post_endpoints(monkeypatch, path):
    # BL-159.2 (AC-2): the auth gate runs in ASGI middleware, before any endpoint reads its own
    # body — an arbitrary body is fine here, every one of these must 401 before its own body-shape
    # validation ever gets a chance to run. Spans every status-mapping family this module's
    # docstring documents, not /v1/parse alone.
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0001")
    client = TestClient(create_app())
    r_missing = client.post(path, json={})
    assert r_missing.status_code == 401
    assert r_missing.json()["error"]["category"] == "unauthorized"
    r_wrong = client.post(path, json={}, headers={"Authorization": "Bearer wrong-token"})
    assert r_wrong.status_code == 401
    assert r_wrong.json()["error"]["category"] == "unauthorized"


@pytest.mark.parametrize("path", _PROTECTED_GET_PATHS)
def test_caller_auth_missing_or_wrong_token_is_401_on_get_endpoints(monkeypatch, path):
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0002")
    client = TestClient(create_app())
    r_missing = client.get(path)
    assert r_missing.status_code == 401
    assert r_missing.json()["error"]["category"] == "unauthorized"
    r_wrong = client.get(path, headers={"Authorization": "Bearer wrong-token"})
    assert r_wrong.status_code == 401
    assert r_wrong.json()["error"]["category"] == "unauthorized"


def test_caller_auth_healthz_and_webhook_stay_open_even_with_a_key_configured(monkeypatch):
    # BL-159.2 (AC-8): the two intentionally-open paths never require a header, whether or not any
    # key is configured — proven here WITH a key configured (the off-by-default case is already
    # proven above). No Authorization header at all reaches chunkr's own webhook logic (it
    # declares no webhook_secret field, so BL-50's fail-closed gate never applies to it either) —
    # a genuine unknown-job 404, never this version's own 401 unauthorized.
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0003")
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200
    r = client.post("/v1/webhooks/chunkr", json={"task_id": "does-not-exist"})
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_job"


def test_caller_auth_valid_token_succeeds_exactly_like_no_auth_configured(monkeypatch):
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0004")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse", json=_pdf_body("pymupdf"), headers={"Authorization": "Bearer realtoken-0004"}
    )
    assert r.status_code == 200
    schemas.validate_response(r.json())


def test_caller_auth_scope_narrows_direct_named_backend_access(monkeypatch):
    # BL-159.3/AC-4: token scoped to ["pymupdf"] only. A request naming "tesseract" directly is
    # rejected by SCOPE ALONE, before any adapter is touched — true regardless of whether the
    # tesseract binary happens to be installed in the environment running this test.
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0006")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0006=pymupdf")
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer scoped-token-0006"}

    ok = client.post("/v1/parse", json=_pdf_body("pymupdf"), headers=headers)
    assert ok.status_code == 200

    denied = client.post("/v1/parse", json=_pdf_body("tesseract"), headers=headers)
    assert denied.status_code == 403
    err = denied.json()["error"]
    assert err["category"] == "scope_denied"
    assert err["backend_code"] == "tesseract"


def test_caller_auth_unscoped_key_is_never_blocked_by_scope(monkeypatch):
    # AC-4: a key with no allow-list configured is never rejected by the SCOPE layer for any
    # backend — proven against the identical "tesseract" request the scoped key above IS denied
    # for. Whatever tesseract itself does next (succeed, or fail for an unrelated, environment-
    # specific reason such as a missing binary) is outside what this check is about; what matters
    # is that scope itself never fires for an unscoped key.
    monkeypatch.setenv("OPENREADING_API_KEYS", "unscoped-token-0007")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("tesseract"),
        headers={"Authorization": "Bearer unscoped-token-0007"},
    )
    assert not (r.status_code == 403 and r.json()["error"]["category"] == "scope_denied")


def test_caller_auth_scope_denial_happens_before_credential_resolution(monkeypatch):
    # BL-159.3 (AC-3): point the scoped-out request at a backend whose own credentials are ALSO
    # deliberately absent (mirrors test_parse_missing_credentials_is_424's own delenv pattern) — if
    # scope were checked AFTER credential resolution (the wrong order), this would 424
    # missing_credentials instead of 403 scope_denied.
    for v in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROCESSOR_ID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0005")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0005=pymupdf")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("google-document-ai"),
        headers={"Authorization": "Bearer scoped-token-0005"},
    )
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["category"] == "scope_denied"
    assert err["backend_code"] == "google-document-ai"


def test_caller_auth_scope_reroutes_an_auto_request_around_an_out_of_scope_first_pick(
    monkeypatch,
):
    """`auto` asks the router to choose, so a scope on the token bounds WHAT IT MAY CHOOSE FROM
    rather than vetoing the request whenever the unconstrained top pick falls outside it. The
    chain is pruned to the allow-list and routing proceeds over what survives — the same
    subtraction, and the same prune-then-run outcome, a `strategy:` walk already gets.

    The router's own pick is learned via a throwaway /v1/route call (which executes nothing), so
    this never hardcodes which of the four local candidates scoring picks.
    """
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_local": True}
    probe = TestClient(create_app())
    chosen = probe.post("/v1/route", json=payload).json()["chosen"]
    assert chosen is not None

    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0008")
    others = sorted({"pymupdf", "tesseract", "docling", "qwen-vl"} - {chosen})
    assert others  # sanity: excluding one of four still leaves a real, well-formed allow-list
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", f"scoped-token-0008={'|'.join(others)}")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse", json=payload, headers={"Authorization": "Bearer scoped-token-0008"}
    )
    # The one thing that must never happen is the out-of-scope pick running. Whether an in-scope
    # local backend then succeeds or fails for an environment-specific reason (a missing binary)
    # is not what this is about, so both outcomes are checked for the same property: every backend
    # this request reached was one the token allows.
    assert r.status_code != 403
    if r.status_code == 200:
        assert r.json()["backend"]["id"] in set(others)
    else:
        reached = {a["backend"] for a in r.json()["error"].get("trail", [])}
        assert reached <= set(others)


def test_caller_auth_scope_bounds_the_whole_auto_fallback_chain_not_just_the_first_pick(
    monkeypatch,
):
    """A RoutePlan's chain is `chosen` PLUS every fallback the compliance router computed, and the
    `auto` arm walks all of it. A check that reads `chosen` alone therefore guards the first
    backend and none of the rest: the moment the in-scope pick fails on a document, the request
    walks the whole eligible registry and hands the document to backends the same token is refused
    BY NAME, one HTTP call earlier.

    Proven on the attempt trail, which carries exactly one entry per chain member — including a
    `skipped` entry for one whose credentials did not resolve. An id absent from that trail was
    never in the chain at all, so no run context was built and no credential was resolved for it,
    which is the property the allow-list exists to provide.
    """
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0026")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0026=pymupdf")
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer scoped-token-0026"}

    for other in ("tesseract", "docling"):
        denied = client.post("/v1/parse", json=_pdf_body(other), headers=headers)
        assert denied.status_code == 403
        assert denied.json()["error"]["backend_code"] == other

    # A document the in-scope backend cannot parse is what drives the chain past its first rung.
    body = _pdf_body("auto")
    body["document"]["bytes_base64"] = base64.b64encode(b"not a pdf").decode()
    r = client.post("/v1/parse", json=body, headers=headers)
    assert r.status_code == 502
    trail = [a["backend"] for a in r.json()["error"]["trail"]]
    assert trail == ["pymupdf"]


def test_caller_auth_scope_denies_auto_when_it_leaves_no_backend_in_the_chain(monkeypatch):
    """Fail closed, not open. When the allow-list removes every backend the router found eligible,
    the request is refused 403 `scope_denied` — never 502 (which reads as "they tried and failed",
    when none was allowed to try) and never a success on nothing.

    Scope names itself as the cause because it is the narrower and later subtraction: the fix is
    the token's allow-list, not the request's compliance policy, and reporting it as a compliance
    refusal would send the operator to the wrong file. An already-empty router plan stays
    ComplianceRefused, because there scope removed nothing.
    """
    payload = _pdf_body("auto")
    payload["compliance"] = {"require_local": True}  # bounds the eligible set to local backends
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0027")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0027=reducto")  # never local
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse", json=payload, headers={"Authorization": "Bearer scoped-token-0027"}
    )
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "scope_denied"


def test_a_scoped_runs_allowlist_survives_into_resume_via_the_headers_pinned_set(
    tmp_path, monkeypatch
):
    """`openreading resume` recompiles the strategy with NO allow-list, because a resume is an
    operator action on the CLI where there is no token. The server can still arm a ledger run for a
    scoped caller, so the question is whether resuming one re-drives it across backends the
    original token was refused.

    It does not, and the reason is the ledger rather than the recompile. A scope that pruned a
    named rung changes the compiled tree and `plan_hash` refuses the resume outright. A scope that
    only narrowed the eligible set — this `auto`-rung case, where the tree is `{backend: auto}`
    either way — leaves `plan_hash` matching, and the header's `pinned_eligible` is what carries
    the scope: `_arm_ledger(resume=True)` arms the resumed executor's per-step gate from the header
    rather than a freshly recomputed set.

    Pinned on the header's own recorded set, not on which backend happens to run, so this says the
    same thing on a machine with no tesseract binary. The unscoped contrast is what makes it a
    statement about the scope and not about the router.
    """
    from openreading import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    (tmp_path / "openreading.yaml").write_text("version: 1\nstrategies:\n  s: [auto]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "openreading.yaml"))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0029")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0029=pymupdf")

    r = TestClient(create_app()).post(
        "/v1/parse",
        json=_pdf_body("strategy:s"),
        headers={"Authorization": "Bearer scoped-token-0029"},
    )
    assert r.status_code == 200 and r.json()["backend"]["id"] == "pymupdf"

    headers = sorted((tmp_path / "ledger").glob("*.header.json"))
    assert len(headers) == 1
    header = json.loads(headers[0].read_text())
    # The narrowed set, not the whole eligible registry an unscoped run of the same strategy pins.
    assert sorted(header["pinned_eligible"]) == ["pymupdf"]

    resumed = api.resume_run(header["run_id"])
    assert resumed["status"]["state"] == "succeeded"
    assert resumed["backend"]["id"] == "pymupdf"


def test_an_unscoped_run_of_the_same_strategy_pins_the_whole_eligible_set(tmp_path, monkeypatch):
    """The contrast the test above rests on: without a scope, the same `auto` strategy pins every
    eligible backend, so the single-entry pinned set there is the allow-list's doing and not a
    property of `auto` rungs."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    (tmp_path / "openreading.yaml").write_text("version: 1\nstrategies:\n  s: [auto]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "openreading.yaml"))
    monkeypatch.delenv("OPENREADING_API_KEYS", raising=False)

    r = TestClient(create_app()).post("/v1/parse", json=_pdf_body("strategy:s"))
    assert r.status_code == 200
    header = json.loads(sorted((tmp_path / "ledger").glob("*.header.json"))[0].read_text())
    assert len(header["pinned_eligible"]) > 1


def test_create_app_reaps_expired_ledger_content_at_startup(tmp_path, monkeypatch):
    """M7: the reaper used to run only when a NEW run arms (`_arm_ledger`'s own sweep), so a
    server that took its last request long ago held that run's expired content (encrypted document
    blobs, the key that unlocks them) past its retention ceiling indefinitely — nothing else ever
    swept the ledger root. `create_app()` now calls `api.reap_expired_now()` once at startup so a
    process that never receives another request still enforces expiry on its own. The journal file
    itself is untouched either way — audit metadata survives erasure by design (retention.py §9.4)."""
    from openreading.ledger.localfs import LocalFsBlobStore, LocalFsKeyStore
    from openreading.ledger.retention import stamp_run

    ledger_root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger_root))

    run_id = "expired-before-startup"
    keys = LocalFsKeyStore(ledger_root / "keys")
    blobs = LocalFsBlobStore(ledger_root / "blobs", keys)
    blobs.put(run_id, "sha256:" + "a" * 64, b"encrypted document bytes", "application/pdf")
    stamp_run(ledger_root, run_id, expires_epoch_ms=1, zdr=False)  # epoch 1ms: already expired
    journal = ledger_root / f"{run_id}.jsonl"
    journal.write_text('{"seq": 0}\n', encoding="utf-8")

    create_app()

    assert not (ledger_root / "blobs" / run_id).exists()
    assert not (ledger_root / "keys" / f"{run_id}.key").exists()
    assert journal.exists()


def test_caller_auth_multiple_keys_some_scoped_some_not(monkeypatch):
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-key-0013,unscoped-key-0013")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-key-0013=pymupdf")
    client = TestClient(create_app())

    ok = client.post(
        "/v1/parse", json=_pdf_body("pymupdf"), headers={"Authorization": "Bearer scoped-key-0013"}
    )
    assert ok.status_code == 200

    denied = client.post(
        "/v1/parse",
        json=_pdf_body("tesseract"),
        headers={"Authorization": "Bearer scoped-key-0013"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["category"] == "scope_denied"

    unscoped = client.post(
        "/v1/parse",
        json=_pdf_body("tesseract"),
        headers={"Authorization": "Bearer unscoped-key-0013"},
    )
    assert not (
        unscoped.status_code == 403 and unscoped.json()["error"]["category"] == "scope_denied"
    )


def test_caller_auth_scope_denies_a_direct_named_batch_backend(monkeypatch):
    # BL-159 AC-3 extended to /v1/batch's top-level `backend` (shared by every item in the batch).
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0009")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0009=pymupdf")
    client = TestClient(create_app())
    r = client.post(
        "/v1/batch",
        json={"documents": [_batch_doc()], "backend": "tesseract"},
        headers={"Authorization": "Bearer scoped-token-0009"},
    )
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["category"] == "scope_denied"
    assert err["backend_code"] == "tesseract"


def test_caller_auth_scope_denies_an_auto_batch_no_item_can_run_in_scope(monkeypatch):
    # BL-159 AC-3 extended to /v1/batch's "auto": each item can route differently by its own
    # document, so the pre-check walks every item's own plan (see _out_of_scope_backend's own
    # docstring) rather than a single upfront decision. The refusal is up front — before run_batch
    # calls run_one for real — so no earlier item spends at a real backend while a later one is
    # still being found out of scope mid-pool.
    doc = _batch_doc()
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0010")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0010=reducto")  # never local
    client = TestClient(create_app())
    r = client.post(
        "/v1/batch",
        json={"documents": [doc], "backend": "auto", "compliance": {"require_local": True}},
        headers={"Authorization": "Bearer scoped-token-0010"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "scope_denied"


def test_caller_auth_scope_bounds_the_auto_fallback_chain_of_every_batch_item(monkeypatch):
    """/v1/batch runs the plain `auto` arm once per document, so the chain bypass was one delivery
    to an out-of-scope backend PER ITEM.

    A batch item's error carries no attempt trail, only the exhaustion message — which counts the
    chain (`len(plan.chain)`), so "all 1" is an exact statement that this item's chain held the one
    backend the token allows and nothing else. Unscoped, the same request reports all 13.
    """
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0028")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0028=pymupdf")
    client = TestClient(create_app())
    bad = {"filename": "bad.pdf", "bytes_base64": base64.b64encode(b"not a pdf").decode()}
    r = client.post(
        "/v1/batch",
        json={"documents": [bad, bad], "backend": "auto"},
        headers={"Authorization": "Bearer scoped-token-0028"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["state"] == "failed"
        assert item["error"]["message"] == "all 1 eligible backend(s) failed or were skipped"


def test_caller_auth_scope_denies_a_direct_named_jobs_backend(monkeypatch):
    # BL-159 AC-3 extended to POST /v1/jobs's named-backend branch — its only branch, since "auto"
    # is already rejected with its own 400 regardless of auth.
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0017")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0017=pymupdf")
    client = TestClient(create_app())
    r = client.post(
        "/v1/jobs",
        json=_pdf_body("tesseract"),
        headers={"Authorization": "Bearer scoped-token-0017"},
    )
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["category"] == "scope_denied"
    assert err["backend_code"] == "tesseract"


def test_caller_auth_scope_is_enforced_across_a_real_strategy_walk(tmp_path, monkeypatch):
    """A `strategy:<name>` id must not be a way around the allow-list.

    This test used to assert the opposite — that a real strategy walk is "deliberately not
    scope-checked" — which made `strategy:<anything>` a universal bypass: the same token refused
    a backend directly reached and RAN it through a strategy, and the four presets run configless,
    so every caller has one. With hosted keys configured server-side that is a token scoped to a
    free local parser spending vendor credits on whatever a preset's rungs touch.

    Scoped to "tesseract" only; the strategy's single rung is "pymupdf". Nothing the walk can
    reach is in scope, so the request is refused for the same reason and with the same category
    the direct call already gives, naming the backend that was denied.
    """
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0016")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0016=tesseract")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("strategy:cheap"),
        headers={"Authorization": "Bearer scoped-token-0016"},
    )
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["category"] == "scope_denied"
    assert err["backend_code"] == "pymupdf"


def test_caller_auth_scope_prunes_an_out_of_scope_rung_and_runs_the_rest(tmp_path, monkeypatch):
    """A scope SUBTRACTS from what compliance and routing already allow — it does not veto the
    whole walk the moment one rung is out of bounds, which is exactly how the compliance filter
    upstream of it already behaves. The in-scope rung runs; the out-of-scope rung is pruned before
    the walk starts, so no adapter for it is ever built and no credential for it is ever resolved.
    The prune is recorded in `orchestration.dropped`, so a caller can see WHY the strategy did not
    escalate rather than silently getting a shorter cascade."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  two_rung: [pymupdf, tesseract]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0018")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0018=pymupdf")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("strategy:two_rung"),
        headers={"Authorization": "Bearer scoped-token-0018"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["backend"]["id"] == "pymupdf"
    dropped = {d["backend"]: d["code"] for d in body["orchestration"].get("dropped", [])}
    assert dropped.get("tesseract") == "scope_denied"
    assert all(a["backend"] != "tesseract" for a in body["orchestration"]["attempts"])


def test_caller_auth_scope_bounds_an_auto_rung_inside_a_strategy(tmp_path, monkeypatch):
    """The dynamic case, and the reason a static reachable-set computed from the config is not
    enough: an `auto` rung names no backend at all, and resolves at walk time against the router's
    eligible order over the WHOLE registry. Scoped to "tesseract", an `auto` rung must never
    resolve to pymupdf — the backend the same token is refused directly, and the one the router
    picks for this local-only request when nothing narrows it."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  anything: [auto]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0019")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0019=tesseract")
    client = TestClient(create_app())
    payload = _pdf_body("strategy:anything")
    payload["compliance"] = {"require_local": True}
    r = client.post(
        "/v1/parse", json=payload, headers={"Authorization": "Bearer scoped-token-0019"}
    )
    # Whatever tesseract itself then does (run, or fail for an environment-specific reason such as
    # a missing binary) is not what this is about: `auto` must not have resolved to pymupdf.
    assert not (r.status_code == 200 and r.json()["backend"]["id"] == "pymupdf")
    if r.status_code == 200:
        assert r.json()["backend"]["id"] == "tesseract"


def test_caller_auth_scope_is_enforced_across_a_builtin_preset(monkeypatch):
    """The presets need no config file, so `strategy:offline_first` is available to every caller
    of every deployment — which is what made the bypass universal rather than a property of one
    operator's openreading.yaml. Scoped to "docling", the preset's first rung (pymupdf) must not
    run, with no OPENREADING_CONFIG set at all."""
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0020")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0020=docling")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("strategy:offline_first"),
        headers={"Authorization": "Bearer scoped-token-0020"},
    )
    assert not (r.status_code == 200 and r.json()["backend"]["id"] == "pymupdf")


def test_caller_auth_unscoped_key_still_walks_every_rung_of_a_strategy(tmp_path, monkeypatch):
    """The other direction of the same gate: a key with no allow-list configured is never narrowed
    by scope, so enforcing scope across strategy walks must not change a single byte for the
    unscoped case (BL-159 AC-1/AC-4)."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  two_rung: [pymupdf, tesseract]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "unscoped-token-0021")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("strategy:two_rung"),
        headers={"Authorization": "Bearer unscoped-token-0021"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["backend"]["id"] == "pymupdf"
    assert not body["orchestration"].get("dropped")


def test_caller_auth_never_leaks_a_configured_key_value(monkeypatch, caplog):
    # BL-159.4 (AC-5): the presented AND the configured key values are absent from every 401/403
    # response body this version introduces, and from caplog's captured records (this module logs
    # nothing about the auth check today — this also guards against a future regression that would
    # add a logging call carrying the raw value).
    real_secret = "sk-real-caller-secret-must-never-appear-0011"
    wrong_secret = "sk-presented-wrong-value-must-never-appear-0011"
    monkeypatch.setenv("OPENREADING_API_KEYS", f"{real_secret},other-key-0011")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "other-key-0011=pymupdf")
    client = TestClient(create_app())

    missing = client.get("/v1/backends")
    assert missing.status_code == 401
    assert real_secret not in missing.text

    wrong = client.get("/v1/backends", headers={"Authorization": f"Bearer {wrong_secret}"})
    assert wrong.status_code == 401
    assert real_secret not in wrong.text
    assert wrong_secret not in wrong.text

    scope_denied = client.post(
        "/v1/parse",
        json=_pdf_body("tesseract"),
        headers={"Authorization": "Bearer other-key-0011"},
    )
    assert scope_denied.status_code == 403
    assert real_secret not in scope_denied.text
    assert "other-key-0011" not in scope_denied.text

    for record in caplog.records:
        message = record.getMessage()
        assert real_secret not in message
        assert wrong_secret not in message
        assert "other-key-0011" not in message


def test_caller_auth_uses_constant_time_comparison(monkeypatch):
    # BL-159.6 tightens AC-6 to a by-construction check: the comparison call site must use
    # hmac.compare_digest (never ==/in/startswith against the raw configured value), proved by
    # wrapping/spying the primitive and asserting it was called — never by measuring elapsed time.
    import hmac as hmac_module

    calls = []
    real_compare_digest = hmac_module.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(hmac_module, "compare_digest", _spy)
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0012")
    client = TestClient(create_app())
    r = client.get("/v1/backends", headers={"Authorization": "Bearer wrong-value-0012"})
    assert r.status_code == 401
    assert calls  # hmac.compare_digest was actually invoked while checking the presented token
    assert any(a == "wrong-value-0012" for a, _ in calls)


@pytest.mark.parametrize(
    "env",
    [
        {"OPENREADING_API_KEYS": "tokenA,,tokenB"},
        {"OPENREADING_API_KEYS": ",tokenA"},
        {"OPENREADING_API_KEYS": "tokenA,"},
        {"OPENREADING_API_KEYS": "tokenA", "OPENREADING_API_KEY_SCOPES": "tokenA=pymupdf,"},
        {"OPENREADING_API_KEYS": "tokenA", "OPENREADING_API_KEY_SCOPES": "tokenApymupdf"},
        {"OPENREADING_API_KEYS": "tokenA", "OPENREADING_API_KEY_SCOPES": "=pymupdf"},
        {"OPENREADING_API_KEYS": "tokenA", "OPENREADING_API_KEY_SCOPES": "tokenB=pymupdf"},
        {"OPENREADING_API_KEYS": "tokenA", "OPENREADING_API_KEY_SCOPES": "tokenA="},
        {
            "OPENREADING_API_KEYS": "tokenA",
            "OPENREADING_API_KEY_SCOPES": "tokenA=pymupdf||tesseract",
        },
        {
            "OPENREADING_API_KEYS": "tokenA",
            "OPENREADING_API_KEY_SCOPES": "tokenA=pymupdf,tokenA=tesseract",
        },
        {"OPENREADING_API_KEY_SCOPES": "tokenA=pymupdf"},  # scopes set, no keys at all
    ],
)
def test_caller_auth_malformed_config_fails_at_create_app_not_on_first_request(monkeypatch, env):
    # BL-159.5 (AC-7): every shape below must raise from create_app() ITSELF — server construction
    # — never lazily on the first request, mirroring the existing strategy-config fail-fast
    # precedent (_load_strategy_config(None, allow_cwd=False) inside create_app).
    from openreading.server.app import ServerConfigError

    monkeypatch.delenv("OPENREADING_API_KEYS", raising=False)
    monkeypatch.delenv("OPENREADING_API_KEY_SCOPES", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ServerConfigError):
        create_app()


def test_caller_auth_well_formed_config_never_raises_at_startup(monkeypatch):
    # The positive counterpart to the malformed-config sweep above: a real, well-formed multi-key,
    # partially-scoped config constructs cleanly (no ServerConfigError).
    monkeypatch.setenv("OPENREADING_API_KEYS", "tokenA,tokenB,tokenC")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "tokenA=pymupdf|tesseract,tokenC=reducto")
    create_app()  # must not raise


def test_caller_auth_cors_preflight_is_answered_before_auth_when_both_configured(monkeypatch):
    # CORSMiddleware is registered OUTERMOST (see create_app's own comment) precisely so a
    # browser's unauthenticated preflight OPTIONS still gets answered instead of 401ing — otherwise
    # a CORS-enabled, auth-enabled deployment would be unreachable from any browser at all.
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0015")
    client = TestClient(create_app(cors_origins=["https://app.example.com"]))
    r = client.request(
        "OPTIONS",
        "/v1/parse",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_body_limit_outranks_auth_and_still_gets_cors_headers_when_all_three_configured(
    monkeypatch,
):
    # M2's must-hold invariant, committed rather than left to a throwaway script: with caller auth
    # AND CORS both configured, an oversized body from an UNAUTHENTICATED cross-origin caller must
    # still 413 (the size gate runs before the token check ever reads a header) — never 401, which
    # would mean the auth gate ran first and paid the cost of parsing an oversized request just to
    # reject it for the wrong reason. And because CORSMiddleware is registered OUTERMOST of all
    # three (create_app's own comment), that 413 must still carry CORS headers — the same "a
    # browser can actually read the error" property test_caller_auth_cors_preflight_is_answered_
    # before_auth_when_both_configured proves for a 401, extended one layer further out.
    import openreading.server.app as app_module

    monkeypatch.setattr(app_module, "_MAX_BODY_BYTES", 100)
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0099")
    client = TestClient(app_module.create_app(cors_origins=["https://app.example.com"]))
    oversized = json.dumps({"document": {"bytes_base64": "A" * 1000}}).encode()

    r = client.post(
        "/v1/parse",
        content=oversized,
        headers={"content-type": "application/json", "Origin": "https://app.example.com"},
    )

    assert r.status_code == 413  # NOT 401 — the size gate must run before the auth gate
    assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


# --- liveness (internal/design/liveness.md §6) -----------------------------------------------


def test_liveness_returns_a_schema_valid_report_for_a_local_backend(client):
    r = client.post("/v1/backends/pymupdf/liveness")
    assert r.status_code == 200
    body = r.json()
    schemas.validate_liveness_report(body)  # outgoing conforms to the vendored schema
    assert body["backend"] == "pymupdf"
    assert body["status"] == "live" and body["measured"] is True
    assert body["probe"] == "local"


def test_liveness_of_an_unconfigured_backend_is_200_not_configured(client, monkeypatch):
    monkeypatch.delenv("CHUNKR_API_KEY", raising=False)
    r = client.post("/v1/backends/chunkr/liveness")
    assert r.status_code == 200
    assert r.json()["status"] == "not_configured"
    assert r.json()["measured"] is False


def test_liveness_of_a_configured_but_unprobeable_backend_is_the_labelled_inference(
    client, monkeypatch
):
    """The mid-flight refinement: no free vendor liveness call does NOT mean "cannot be tested" —
    it means the configured state, reported as an INFERENCE (measured=false, no latency)."""
    monkeypatch.setenv("CHUNKR_API_KEY", "sk-present")
    r = client.post("/v1/backends/chunkr/liveness")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "configured_unverified"
    assert body["measured"] is False and body["latency_ms"] is None


def test_liveness_never_maps_a_down_backend_to_a_5xx(client, monkeypatch):
    """ "The backend is down" is a SUCCESSFUL diagnostic, not a failure of this API — 200 with a
    report, never a 502 that would conflate the two (§6.4).

    The adapter's own `probe_http` binding is patched rather than pointing at a closed port: this
    suite must never open a socket, so the "nothing answered" branch is injected while the whole
    endpoint path around it stays real."""
    from openreading.adapters.docling import adapter as docling_adapter
    from openreading.types.liveness import ProbeResult

    monkeypatch.setenv("DOCLING_SERVE_URL", "http://docling.invalid:5001")
    monkeypatch.setattr(
        docling_adapter,
        "probe_http",
        lambda *a, **kw: ProbeResult.unreachable("no response (DOCLING_SERVE_URL) within 5s"),
    )
    r = client.post("/v1/backends/docling/liveness", json={"timeout_s": 0.2})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unreachable" and body["measured"] is True
    schemas.validate_liveness_report(body)


def test_liveness_unknown_backend_is_404(client):
    r = client.post("/v1/backends/not-a-backend/liveness")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_backend"


def test_liveness_rejects_a_non_numeric_timeout(client):
    r = client.post("/v1/backends/pymupdf/liveness", json={"timeout_s": "soon"})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "bad_request"


def test_liveness_accepts_an_absent_body(client):
    assert client.post("/v1/backends/pymupdf/liveness").status_code == 200


def test_liveness_is_scope_gated_before_any_credential_is_resolved(monkeypatch):
    """BL-159 AC-3/AC-4: an out-of-scope key must not be able to make the server touch a vendor."""
    monkeypatch.setenv("OPENREADING_API_KEYS", "realtoken-0100")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "realtoken-0100=pymupdf")
    client = TestClient(create_app())
    auth = {"Authorization": "Bearer realtoken-0100"}
    denied = client.post("/v1/backends/reducto/liveness", headers=auth)
    assert denied.status_code == 403
    assert denied.json()["error"]["category"] == "scope_denied"
    assert client.post("/v1/backends/pymupdf/liveness", headers=auth).status_code == 200


def test_backends_endpoint_carries_the_static_probe_declaration_without_probing(client):
    """GET /v1/backends stays free, offline and instant: it gains only the DECLARATION, so a UI can
    render "cannot be tested" without the endpoint turning one page load into 13 vendor calls."""
    rows = {b["slug"]: b for b in client.get("/v1/backends").json()}
    assert rows["pymupdf"]["liveness_probe"] == "local"
    assert rows["anthropic-claude"]["liveness_probe"] == "vendor"
    assert rows["chunkr"]["liveness_probe"] == "none"
    # and it does NOT leak a liveness ANSWER into this endpoint
    assert "status" not in rows["pymupdf"] and "measured" not in rows["pymupdf"]


def test_caller_auth_scope_is_enforced_on_a_strategy_job(tmp_path, monkeypatch):
    """`POST /v1/jobs` wraps a whole strategy walk as one synthetic job and runs it through the
    same api.run_request. Wrapping the walk in a job must not be a way to reach a backend the same
    token is refused when it asks synchronously."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0022")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0022=tesseract")
    client = TestClient(create_app())
    r = client.post(
        "/v1/jobs",
        json=_pdf_body("strategy:cheap"),
        headers={"Authorization": "Bearer scoped-token-0022"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "scope_denied"


def test_caller_auth_scope_is_enforced_on_every_item_of_a_strategy_batch(tmp_path, monkeypatch):
    """A batch is the highest-volume shape: one request, `documents[]` items of spend. The
    top-level `backend` check deliberately skips a `strategy:` id (a walk picks its own backends),
    so the allow-list has to reach every item's own run."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\nstrategies:\n  cheap: [pymupdf]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0023")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0023=tesseract")
    client = TestClient(create_app())
    doc = {
        "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
        "mime_type": "application/pdf",
    }
    r = client.post(
        "/v1/batch",
        json={"documents": [doc, doc], "backend": "strategy:cheap"},
        headers={"Authorization": "Bearer scoped-token-0023"},
    )
    assert r.status_code == 200  # a batch reports per-item outcomes, not an HTTP error
    items = r.json()["items"]
    assert len(items) == 2
    # Every item fails identically and nothing runs: the refusal happens while compiling the walk,
    # so no item spends before a later one is found out of scope.
    assert all(i["state"] == "failed" for i in items)
    assert all("not scoped" in i["error"]["message"] for i in items)
    assert r.json()["summary"]["succeeded"] == 0


def test_caller_auth_scope_is_enforced_when_defaults_strategy_makes_auto_a_walk(
    tmp_path, monkeypatch
):
    """The bypass that hides behind an ordinary id: with `defaults.strategy` configured, a plain
    `auto` request runs a STRATEGY, not the plain router. Gating it at the door against whatever
    the plain router would have picked checks a backend the request never uses, so `auto` here has
    to be handed to the walk's own enforcement like any other strategy."""
    cfg = tmp_path / "om.yaml"
    cfg.write_text("version: 1\ndefaults:\n  strategy: cheap\nstrategies:\n  cheap: [pymupdf]\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(cfg))
    monkeypatch.setenv("OPENREADING_API_KEYS", "scoped-token-0024")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "scoped-token-0024=tesseract")
    client = TestClient(create_app())
    r = client.post(
        "/v1/parse",
        json=_pdf_body("auto"),
        headers={"Authorization": "Bearer scoped-token-0024"},
    )
    assert not (r.status_code == 200 and r.json()["backend"]["id"] == "pymupdf")
    assert r.status_code == 403
    assert r.json()["error"]["category"] == "scope_denied"
