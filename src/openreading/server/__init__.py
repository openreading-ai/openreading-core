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
    A request completed, including partial batches, job submissions, and job reads.
    Job state determines whether processing is still running.
400
    The body or request shape is invalid, a batch or compare count exceeds its limit, or a
    compare body exceeds ``OPENREADING_MAX_COMPARE_BYTES``.
401
    Authentication is enabled and the bearer token is missing or invalid.
403
    The matched API key cannot access the requested backend.
404
    A named backend or job identifier is unknown.
413
    The raw request body exceeds ``OPENREADING_MAX_BODY_BYTES`` on any endpoint, or an
    uploaded file or its metadata exceeds its own limit.
422
    The backend cannot provide a requested feature.
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

File uploads
------------
You can send client files to ``/v1/parse``, ``/v1/route``, and ``/v1/jobs`` using
``multipart/form-data``. Existing JSON requests retain their request and response schemas.
For example, curl's ``-F 'file=@report.docx'`` reads the file on the calling machine.
Each backend reads the formats its descriptor claims in ``openreading.adapters``.

Send exactly one binary ``file`` part and one UTF-8 JSON ``request`` text part.
Both parts are required in either order, and the request field must have no filename.
The request requires ``backend`` and accepts the endpoint's existing options.
A null backend keeps default routing for parse and route, while jobs requires a named backend.
Only parse accepts the existing ``keep_candidates`` extension for retaining alternate responses.
An optional ``document`` object permits ``mime_type`` and ``password`` alone.
Source fields and ``document.filename`` are refused even when null, because the upload supplies them.

``openreading.server.uploads`` converts the file into the existing in-memory document representation.
The filename becomes a basename after removing both directory separator styles.
Empty names, dot names, control characters, and names exceeding 255 UTF-8 bytes are refused.
Explicit MIME metadata wins over a specific part header, followed by shared byte and filename inference.
An absent or generic binary header allows inference, and unknown types remain unknown.
Uploads require no ``OPENREADING_SERVER_PATH_ROOT`` because that variable gates server filesystem reads.

File content is limited to 100 MiB, metadata to 1 MiB, and complete request bodies to
``OPENREADING_MAX_BODY_BYTES`` (150 MiB by default). Declared and streamed overflows return 413.
Temporary spools close before dispatch, including malformed input, cancellation, and disconnect paths.
The normalized request retains file bytes for execution, including pending jobs after upload cleanup.
Base64 adds roughly one third to file size, while parsing and adapters can allocate further copies.
The body limit bounds individual requests and does not impose a global concurrency or memory budget.

Authentication precedes decoding, and existing API-key backend scope checks precede dispatch.
Upload storage failures return sanitized 500 envelopes without temporary paths or document passwords.
Multipart parts with duplicate names fail, while duplicate JSON keys keep Python's last-value behavior.
``/docs`` and ``/openapi.json`` describe both encodings from the vendored request schema.
Folder traversal belongs on the client, as demonstrated by ``scripts/upload_folder.py``.
Neither multipart batch intake nor archive extraction is provided by these endpoints.
"""

from __future__ import annotations

from openreading.server.app import ServerConfigError, create_app

# The CLI catches this startup error and renders one tagged line instead of a traceback.
__all__ = ["ServerConfigError", "create_app"]
