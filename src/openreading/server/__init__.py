"""Expose the local HTTP process started by ``openreading serve``.

``POST /v1/parse`` accepts the vendored request schema and returns the vendored response schema.
A null backend resolves ``policy.backends`` from ``openreading.yaml``, then falls back to
``pymupdf``. A named backend runs directly after API key scope checks. Strategy identifiers run
through ``openreading.strategies``.

``POST /v1/route`` returns a router plan without executing it. ``POST /v1/compare`` compares
existing responses. ``POST /v1/batch`` accepts explicit document objects plus four shared request
fields: outputs, extraction schema, features, and pages. Batch items fail independently.

``POST /v1/jobs`` starts an in-memory job. ``GET /v1/jobs/{job_id}`` advances or reads it, while
``DELETE /v1/jobs/{job_id}`` drops its local record. The job store is bounded by count and terminal
age. It is process-local and does not use the run ledger.

HTTP status codes
-----------------
200
    A request completed, including partial batch results and job reads.
202
    A job was accepted and remains in progress.
400
    The body or request shape is invalid, or a configured size limit was exceeded.
401
    Authentication is enabled and the bearer token is missing or invalid.
403
    The matched API key cannot access the requested backend.
404
    A named backend or job identifier is unknown.
413
    The request body exceeds its configured byte limit.
422
    The request fails vendored schema validation.
424
    Execution cannot start because required credentials or dependencies are missing.
429
    The in-memory job store has reached its configured limit.
500
    An unexpected internal failure occurred.
502
    A backend returned a terminal provider failure.
503
    A backend returned a retryable failure or the resolved chain produced no response.
504
    Execution exceeded its deadline.

API keys come from ``OPENREADING_API_KEYS``. Optional backend scopes come from
``OPENREADING_API_KEY_SCOPES`` and can only narrow access. Backend credentials remain in the
server process environment. Request bodies never carry credential values.

The process binds to ``127.0.0.1:8787`` by default. CORS is disabled unless the operator supplies
an origin. Body, batch, compare, and job-store limits are defined and documented in
``openreading.server.app``. Import ``create_app`` to construct the ASGI application.
"""

from __future__ import annotations

from openreading.server.app import ServerConfigError, create_app

# The CLI catches this startup error and renders one tagged line instead of a traceback.
__all__ = ["ServerConfigError", "create_app"]
