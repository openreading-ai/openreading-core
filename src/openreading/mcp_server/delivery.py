"""Fit complete documents against the SDK's serialized response, including escaping.

The default budget is 1,000,000 UTF-8 bytes for a complete JSON-RPC response and newline.
This is an operator delivery setting, not an import limit or a universal host maximum.
The actual request ID participates in measurement. The SDK serialization is mirrored here;
real stdio tests bind this calculation to the bytes written by the pinned MCP SDK.

An accepted result may enter context or become a host-created file. Core guarantees neither.
An oversized result becomes a local export receipt, never truncated normalized content.
No network transfer, host filesystem staging, or code execution occurs in this module.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from mcp import types

from openreading.artifacts.delivery import (
    CompleteResult,
    DeliveryReceipt,
    FileResult,
    document_content,
    save_export,
    warning_summary,
)
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.service import ArtifactService

DEFAULT_RESPONSE_BYTES = 1_000_000


def validate_delivery_config(budget: int, root: Path | None) -> None:
    if type(budget) is not int or budget < 4096:
        raise ValueError("Document response bytes must be an integer of at least 4096.")
    if root is not None and (not root.is_absolute() or ".." in root.parts):
        raise ValueError("Document exports require an absolute directory without parent traversal.")


def tool_result(payload: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json_bytes(payload).decode())], isError=False
    )


def response_bytes(result: types.CallToolResult, request_id: str | int) -> int:
    message = types.JSONRPCMessage(
        types.JSONRPCResponse(
            jsonrpc="2.0",
            id=request_id,
            result=result.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    )
    return len(message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")) + 1


def deliver_document(
    service: ArtifactService,
    artifact_id: str,
    *,
    mode: str,
    budget: int,
    root: Path | None,
    request_id: str | int,
) -> types.CallToolResult:
    manifest, passages, response = service.store.load_document(artifact_id)
    content = document_content(manifest, passages, response)
    data = json_bytes(content)
    receipt = DeliveryReceipt(
        artifact_id=manifest.artifact_id,
        display_name=manifest.display_name,
        content_bytes=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        page_count=manifest.page_count,
        passage_count=manifest.passage_count,
        parser_warnings=warning_summary(response),
    ).wire()
    if mode == "auto":
        candidate = tool_result(CompleteResult(**receipt, content=content).wire())
        if response_bytes(candidate, request_id) <= budget:
            return candidate
    destination = root if root is not None else service.config.artifact_root / "exports"
    # Build and measure the receipt before writing, so an undeliverable path creates no export.
    path = destination / service.store.grant / (receipt["content_sha256"] + ".json")
    fallback = tool_result(
        FileResult(
            **receipt,
            local_path=str(path),
            next_action="The complete JSON is saved on the local computer, not delivered to this chat. Use an existing authorized local-file tool if this mode has access. Otherwise ask the user to attach this exported JSON; uploading sends it to the assistant host. A local path alone does not give a cloud sandbox access. Focused search and exact reads remain available.",
        ).wire()
    )
    if response_bytes(fallback, request_id) > budget:
        raise ArtifactError("response_too_large")
    save_export(destination, service.store.grant, data)
    return fallback
