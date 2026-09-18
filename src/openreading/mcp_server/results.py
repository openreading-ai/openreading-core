"""Deliver complete retained results with measured MCP envelopes and lossless continuation.

The response budget includes the actual JSON-RPC request identifier and string escaping.
Auto mode returns complete content when it fits, otherwise a verified local export receipt.
File mode always exports. Fragment mode uses the existing document JSON Pointer subdivision.
For example, a string at /payload/document/text keeps contiguous Unicode code-point offsets.
Cursors bind result identity, input grant, content digest, reply budget and format revision.
Each continuation reloads and verifies retained bytes, so a cursor grants no additional authority.

Every fragment is measured once against its enclosing message before packing a reply.
No fragment-count ceiling limits delivery. Oversized indivisible keys refuse without truncation.
A file receipt is measured before publication; an undeliverable receipt creates no export.
Local paths establish neither host access nor cloud upload, and content remains untrusted.
"""

from __future__ import annotations

from pathlib import Path

from mcp import types

from openreading.artifacts.delivery import save_export
from openreading.artifacts.document import _plan
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import CompleteResult, FileResult, FragmentResult
from openreading.artifacts.results import RetainedResults
from openreading.artifacts.search import _binding, _cursor, _offset
from openreading.mcp_server.delivery import response_bytes, tool_result, validate_delivery_config


def deliver_result(
    store: RetainedResults,
    result_id: str,
    *,
    mode: str,
    budget: int,
    root: Path | None,
    request_id: str | int,
    cursor: str | None = None,
) -> types.CallToolResult:
    validate_delivery_config(budget, root)
    if mode not in {"auto", "file", "fragments"}:
        raise ValueError("Unknown result delivery mode")
    if cursor is not None and mode != "fragments":
        raise ArtifactError("invalid_cursor")
    content = store.load(result_id)
    receipt = store.receipt(result_id, content)
    if mode == "fragments":
        wire = content.wire()
        plan = _plan(wire, budget)
        binding = _binding(
            ["result", "0.1", store.store.grant, result_id, receipt.content_sha256, budget]
        )
        start = _offset(cursor, binding, len(plan))
        envelope = {
            **receipt.wire(),
            "delivery": "fragments",
            "fragment_start": start,
            "fragment_count": len(plan),
            "fragments": [],
            "next_cursor": None,
        }
        empty_size = response_bytes(tool_result(envelope), request_id)
        size, accepted, continuation = empty_size, 0, None
        values = []
        for index in range(start, len(plan)):
            fragment = plan[index].materialize(wire)
            delta = (
                response_bytes(tool_result({**envelope, "fragments": [fragment]}), request_id)
                - empty_size
            )
            size += delta + bool(values)
            if size > budget:
                break
            values.append(fragment)
            token = _cursor(binding, index + 1) if index + 1 < len(plan) else None
            cursor_delta = (
                response_bytes(tool_result({**envelope, "next_cursor": token}), request_id)
                - empty_size
            )
            if size + cursor_delta <= budget:
                accepted, continuation = len(values), token
        if not accepted:
            raise ArtifactError("response_too_large")
        result = tool_result(
            FragmentResult(
                **{**envelope, "fragments": values[:accepted], "next_cursor": continuation}
            ).wire()
        )
        if response_bytes(result, request_id) > budget:
            raise ArtifactError("response_too_large")
        return result
    data = json_bytes(content.wire())
    if mode == "auto" and len(data) <= budget:
        candidate = tool_result(CompleteResult(**receipt.wire(), content=content).wire())
        if response_bytes(candidate, request_id) <= budget:
            return candidate
    destination = root if root is not None else store.store.config.artifact_root / "exports"
    path = destination / store.store.grant / (receipt.content_sha256 + ".json")
    result = tool_result(
        FileResult(
            **receipt.wire(),
            local_path=str(path),
            next_action="Complete JSON is saved on the local computer. Use an authorized local-file tool if available, or retrieve fragments with openreading_get_result. Attaching the export uploads content to the assistant host. A path alone does not establish access or complete reading.",
        ).wire()
    )
    if response_bytes(result, request_id) > budget:
        raise ArtifactError("response_too_large")
    save_export(destination, store.store.grant, data)
    return result
