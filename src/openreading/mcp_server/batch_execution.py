"""Execute explicit granted requests through the shared serial batch runner.

Every request is authorized before any source is acquired, including later items in the batch.
Each item uses its own ExecutionAttempt, which reauthorizes and checks copied bytes in the child.
The caller holds one execution slot for the batch; serial items preserve that concurrency bound.
Duplicate requests remain separate items in input order.
Empty input retains a batch with status.state=failed and the warning code empty_batch.
Job state=succeeded means publication only, including publication of an empty batch with a failed aggregate status.

The shared runner distinguishes returned responses from raised failures, without rewriting provider status.
For example, a returned failed response is a succeeded batch item whose response still says failed.
Item failures expose fixed execution codes, never arbitrary provider exception messages or local paths.
Cancellation, deadlines and denied process cleanup abort the batch instead of starting further items.
No partial batch is published after interruption; completed private attempt files remain for operator cleanup.
Source hashes cover successful returned responses and are also attached to their individual source records.
No document-size cap, retry, native provider batch call or remote cancellation guarantee is added.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import ResultContent, ResultProvenance
from openreading.artifacts.store import Store
from openreading.batch.runner import run_batch
from openreading.batch.sources import ResolvedSource, format_of
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.mcp_server.execution_process import ExecutionAttempt, ExecutionError
from openreading.types.batch import BatchRequestEcho, SourceRef
from openreading.types.execution_job import ExecutionFaultCode


class ItemFailure(Exception):
    """Expose only a fixed execution code through the shared runner's backend_code seam."""

    def __init__(self, code: str):
        self.backend_code = code if code in ExecutionFaultCode.__args__ else "execution_failed"
        super().__init__(self.backend_code)


def authorize_batch(authority: ExecutionConfig, value: dict) -> list[dict]:
    """Snapshot authorized shared requests without acquiring source files or resolving credentials."""
    if (
        not isinstance(value, dict)
        or set(value) != {"requests"}
        or not isinstance(value["requests"], list)
    ):
        raise ExecutionRefused("invalid_request")
    return [json.loads(authority.authorize(item).request_json) for item in value["requests"]]


def execute_batch(
    store: Store,
    authority: ExecutionConfig,
    value: dict,
    *,
    environment: Mapping[str, str],
    check: Callable[[], None],
    on_attempt: Callable[[int, Path], None],
) -> ResultContent:
    """Retain shared batch semantics while keeping acquisition and execution within existing grants."""
    requests = authorize_batch(authority, value)
    sources = [
        ResolvedSource(
            SourceRef(
                filename=item["document"].get("filename") or Path(item["document"]["path"]).name,
                format=format_of(item["document"].get("filename") or item["document"]["path"]),
                path=item["document"]["path"],
                relpath=item["document"]["path"],
                mime_type=item["document"].get("mime_type"),
            )
        )
        for item in requests
    ]
    indexes = {id(source): index for index, source in enumerate(sources)}
    hashes: list[str] = []
    adapters: dict[str, str | None] = {}
    fatal: list[ExecutionError] = []

    def run_one(source: ResolvedSource, _idempotency_key: str | None) -> dict:
        try:
            check()
            index = indexes[id(source)]
            attempt = ExecutionAttempt(store, authority, environment=environment)
            try:
                on_attempt(index, attempt.root)
                content = attempt.run(requests[index], check=check)
            finally:
                attempt.close()
            hashes.extend(content.provenance.source_sha256)
            if len(content.provenance.source_sha256) == 1:
                source.ref.sha256 = content.provenance.source_sha256[0]
            for backend, version in content.provenance.adapters.items():
                adapters[backend] = (
                    version if backend not in adapters or adapters[backend] == version else None
                )
            return content.payload
        except (ExecutionError, ExecutionRefused, ArtifactError) as error:
            stop = {
                "cancelled": ExecutionError("cancelled"),
                "timeout": ExecutionError("timeout"),
                "os_permission_denied": ExecutionError("os_permission_denied"),
            }.get(error.code)
            if stop is not None:
                fatal.append(stop)
            raise ItemFailure(error.code) from None
        except Exception:
            raise ItemFailure("execution_failed") from None

    def progress(*_args):
        if fatal:
            raise fatal[0]
        check()

    check()
    result = run_batch(
        sources,
        run_one=run_one,
        jobs=1,
        request_echo=BatchRequestEcho(
            jobs=1, source_args=[item["document"]["path"] for item in requests]
        ),
        on_progress=progress,
    )
    check()
    return ResultContent(
        kind="batch_result",
        payload=result.to_schema_dict(),
        provenance=ResultProvenance(
            request_sha256=hashlib.sha256(json_bytes({"requests": requests})).hexdigest(),
            config_sha256=authority.fingerprint,
            core_version=importlib.metadata.version("openreading"),
            core_commit=None,
            adapters=adapters,
            source_sha256=hashes,
            subjects={},
        ),
    )
