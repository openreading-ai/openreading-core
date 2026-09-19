"""Expose static authorized descriptors and isolated, explicitly requested diagnostics.

Discovery constructs only authorized adapters and reads descriptors without health checks or credential resolution.
An empty authority returns an empty catalog. Oversized catalogs refuse; request one backend to reduce the reply.
For example, allowing pymupdf does not expose reducto metadata or permit its diagnostic worker to start.

Readiness and liveness use execution_process ownership, private environment and process-group cleanup without acquiring documents.
Readiness has a thirty-second wall-clock bound; liveness allows its requested probe timeout plus ten seconds for startup and validation.
The parent verifies authorization before creating scratch and the child repeats it before adapter construction.
Successful cleanup removes the per-attempt scratch directory, but its empty execution/<grant> parent can remain.
Abrupt parent loss can leave per-attempt scratch requiring operator removal.
Cleanup denial retains the attempt handle in the caller until the error propagates; no background retry is implied.
Local termination does not prove remote cancellation, and this process boundary is not an operating-system sandbox.

Replies preserve complete reports up to 65536 JSON bytes. No field is silently truncated to fit.
General dispatch reserves a conservative envelope for that ceiling before starting diagnostic work.
For example, a very small response budget refuses before any liveness probe, even when its eventual report might fit.
Negative readiness and measured liveness outcomes are successful diagnostic reports, not tool execution failures.
"""

from __future__ import annotations

import json
import shutil
import time

from openreading.adapters.registry import make_adapter
from openreading.artifacts.intake import directory
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.store import safe_read
from openreading.mcp_server.diagnostic_worker import MAX_DIAGNOSTIC_BYTES
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.mcp_server.execution_process import ExecutionAttempt, ExecutionError
from openreading.schemas import validate_liveness_report
from openreading.types.diagnostic_tool import (
    BackendCatalog,
    CatalogRequest,
    LivenessRequest,
    ReadinessReport,
    ReadinessRequest,
)
from openreading.types.liveness import LivenessReport


def authorize(authority: ExecutionConfig, backend: str) -> None:
    """Check the independent operator scope before adapter construction or credential access."""
    if backend not in authority.allowed_backends:
        raise ExecutionRefused("scope_denied")


def describe(authority: ExecutionConfig, arguments: dict) -> dict:
    request = CatalogRequest.model_validate(arguments)
    if request.backend is not None:
        authorize(authority, request.backend)
    names = [request.backend] if request.backend is not None else sorted(authority.allowed_backends)
    result = BackendCatalog(backends=[make_adapter(name).descriptor for name in names]).wire()
    if len(json_bytes(result)) > MAX_DIAGNOSTIC_BYTES:
        raise ArtifactError("response_too_large")
    return result


class DiagnosticAttempt(ExecutionAttempt):
    """Reuse execution ownership without acquiring a source, publishing a result or creating a job."""

    def check_backend(self, operation: str, arguments: dict) -> dict:
        model = {"readiness": ReadinessRequest, "liveness": LivenessRequest}[operation]
        request = model.model_validate(arguments)
        authorize(self.authority, request.backend)
        if self._started:
            raise ExecutionError("execution_failed")
        self._started = True
        timeout = request.timeout_s if isinstance(request, LivenessRequest) else 20.0
        expires = time.monotonic() + timeout + 10.0

        def observe():
            if time.monotonic() >= expires:
                raise ExecutionError("timeout")

        packet = json_bytes(
            {
                "operation": operation,
                "backend": request.backend,
                "timeout_s": timeout,
                "allowed_backends": sorted(self.authority.allowed_backends),
            }
        )
        with directory(self.root.parent, create=True):
            pass
        self.root.mkdir(mode=0o700)
        try:
            self._run_child(packet, "openreading.mcp_server.diagnostic_worker", observe)
            observe()
            payload = json.loads(safe_read(self.root / "diagnostic.json", MAX_DIAGNOSTIC_BYTES))
            if payload == {"error": {"code": "response_too_large"}}:
                raise ArtifactError("response_too_large")
            if operation == "liveness":
                validate_liveness_report(payload)
            result = (
                LivenessReport.model_validate(payload).to_schema_dict()
                if operation == "liveness"
                else ReadinessReport.model_validate(payload).wire()
            )
            reported = result["backend"] if operation == "liveness" else result["readiness"]["slug"]
            if reported != request.backend:
                raise ExecutionError("execution_failed")
            return result
        finally:
            # Leave scratch intact if process ownership cannot be safely closed.
            self.close()
            shutil.rmtree(self.root)
