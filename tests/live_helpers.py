"""Helpers for the keyed live-test lane (GOAL2 10.1). A live test skips cleanly unless its
backend's full credential set is present (derived from the descriptor's `live_gate_env`), and can
capture a scrubbed fixture from the real response when OPENREADING_RECORD_FIXTURES=1."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openreading.adapters.registry import make_adapter
from openreading.testing import scrub_fixture

_FIX_ROOT = Path(__file__).parent / "fixtures"


def gate_env(slug: str) -> list[str]:
    """The env vars whose presence is required to run a backend's live test. Uses the descriptor's
    explicit `live_gate_env` (set for ambient-chain backends whose spec keys are all optional),
    else the required-credential env names."""
    desc = make_adapter(slug).descriptor
    if desc.live_gate_env:
        return list(desc.live_gate_env)
    names: list[str] = []
    for f in desc.credentials_spec:
        if f.required and f.env:
            names.append(f.env[0])
    return names


def skip_unless_creds(slug: str) -> None:
    """pytest.skip (with the exact missing var names) unless every gate env var for `slug` is set.
    AWS_REGION|AWS_DEFAULT_REGION are treated as interchangeable."""
    missing = []
    for var in gate_env(slug):
        alts = (
            ["AWS_REGION", "AWS_DEFAULT_REGION"]
            if var in ("AWS_REGION", "AWS_DEFAULT_REGION")
            else [var]
        )
        if not any(os.environ.get(a) for a in alts):
            missing.append(var)
    if missing:
        pytest.skip(f"{slug}: set {', '.join(missing)} to run the live test")


def record_if_enabled(slug: str, name: str, payload) -> None:
    """When OPENREADING_RECORD_FIXTURES=1, write the SCRUBBED payload to
    tests/fixtures/<slug>/<name>.json so a live capture can refresh the offline fixture. No-op
    otherwise (so it never runs in the default offline suite)."""
    if os.environ.get("OPENREADING_RECORD_FIXTURES") != "1":
        return
    out = _FIX_ROOT / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(scrub_fixture(payload), indent=2))


def sample_pdf_request(backend_id: str, **backend):
    """A request carrying the real bundled sample PDF (for live tests)."""
    import base64

    from openreading.testing.sample_pdf import build_sample_pdf
    from openreading.types.request import OpenReadingRequest

    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": backend_id, **backend},
        }
    )


def run_live(slug: str, adapter, req, *, capture: str = "live_capture", deadline_s: int = 300):
    """Resolve creds from the env, submit → drive → normalize against the REAL backend, assert the
    response is schema-valid, and (in record mode) capture a scrubbed fixture. Returns the response."""
    from openreading import schemas
    from openreading.credentials import build_run_context
    from openreading.router.clock import RealClock
    from openreading.router.driver import run_to_completion

    ctx = build_run_context(req, adapter.descriptor)
    clock = RealClock()
    job = adapter.submit(req, ctx)
    job = run_to_completion(
        adapter, job, ctx=ctx, deadline_ms=clock.now_ms() + deadline_s * 1000, clock=clock
    )
    resp = adapter.normalize(job, ctx, req)
    schemas.validate_response(resp.to_schema_dict())
    if job.raw is not None:
        record_if_enabled(slug, capture, job.raw.payload)
    return resp
