"""Return the entire retained normalized result as lossless, byte-bounded JSON fragments.

``get_document`` reads no parser output beyond the verified retained response. The configured
import profile decides which channels exist. Parser warnings and partial status remain visible.
Completing pagination proves transport completeness, not recognition of every printed character.
The normalized ``backend_raw`` envelope is excluded. No ranking or relationship inference runs.

The content object contains ``response``, ``page_origins``, ``evidence``, and import ``warnings``.
Response retains the normalized schema, including optional tables, fields, children and usage.
Evidence maps returned IDs to their existing block spans without repeating passage text.
Read those IDs with ``openreading_read`` to obtain exact citation quotes. Unmeasured origins
remain absent, rather than being promoted to native text.

Fragments address this content with JSON Pointer paths. An empty path names the whole object.
A value fragment assigns its complete value, including nulls and empty containers. If a container
does not fit, its empty value precedes its children in array order or sorted object-key order.
Oversized strings use contiguous ``start:end`` Unicode code-point spans and a total ``length``.
For example, text at ``/response/document/text`` is concatenated only with that same path.
Pointer tokens escape tilde as ``~0`` and slash as ``~1``. No text or field name is shortened.

Consume fragments in order through ``next_cursor: null``. ``fragment_start`` and ``fragment_count``
detect skipped pages, while ``content_sha256`` hashes canonical ``json_bytes`` of the whole content.
Replies take the longest fragment prefix fitting the byte cap, without a separate fragment-count
ceiling. Packing counts each fragment once and includes the actual continuation-token bytes.
Cursors bind the artifact, content hash, payload cap and format revision. They contain no paths
and convey no access rights. Each request revalidates the store, without reparsing the source.
Each service keeps at most one pagination plan, limited to 8 MiB of serialized plan metadata.
Plans contain paths, container markers and character spans, without extracted values or raw payloads.
Verified manifest hashes bind reuse. Larger plans are computed again rather than retained.
An indivisible field name that exceeds the cap returns ``response_too_large`` instead of losing it.
The ``document-tool.v0.1.json`` family owns the request, response and error contracts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from openreading.artifacts.constants import MAX_CURSOR_CHARS
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import (
    ArtifactId,
    ArtifactManifest,
    Digest,
    ErrorEnvelope,
    Passage,
    WireModel,
    json_bytes,
)
from openreading.artifacts.search import _binding, _cursor, _offset


class DocumentRequest(WireModel):
    artifact_id: ArtifactId
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)


class ValueFragment(WireModel):
    path: str
    value: JsonValue


class TextFragment(WireModel):
    path: str
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    length: int = Field(ge=1)


class DocumentResult(WireModel):
    schema_version: Literal["0.1"] = "0.1"
    scope: Literal["retained_normalized_response"] = "retained_normalized_response"
    artifact_id: ArtifactId
    display_name: str
    content_sha256: Digest
    fragment_start: int = Field(ge=0)
    fragment_count: int = Field(ge=1)
    fragments: list[ValueFragment | TextFragment] = Field(min_length=1)
    next_cursor: str | None = Field(default=None, max_length=MAX_CURSOR_CHARS)

    def wire(self) -> dict:
        # A fragment's explicit null is document content, not an absent optional field.
        return self.model_dump(mode="json")


DocumentPayload = Annotated[
    DocumentRequest | DocumentResult | ErrorEnvelope,
    Field(title="OpenReading Document Tool v0.1"),
]

PLAN_CACHE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FragmentPlan:
    path: str
    kind: str
    start: int = 0
    end: int = 0
    length: int = 0

    def materialize(self, content: dict) -> dict:
        if self.kind in {"object", "array"}:
            return {"path": self.path, "value": {} if self.kind == "object" else []}
        value = content
        for encoded in self.path.split("/")[1:]:
            token = encoded.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        if self.kind == "text":
            return {
                "path": self.path,
                "text": value[self.start : self.end],
                "start": self.start,
                "end": self.end,
                "length": self.length,
            }
        return {"path": self.path, "value": value}


class DocumentCache:
    def __init__(self):
        self.entry: tuple[str, str, list[FragmentPlan]] | None = None


def _plan(content: dict, cap: int) -> list[FragmentPlan]:
    plan = []
    for part in _fragments(content, "", (cap - 1024) // 2):
        if "text" in part:
            plan.append(
                FragmentPlan(part["path"], "text", part["start"], part["end"], part["length"])
            )
        else:
            kind = "object" if part["value"] == {} else "array" if part["value"] == [] else "value"
            plan.append(FragmentPlan(part["path"], kind))
    return plan


def _fragments(value: JsonValue, path: str, cap: int) -> Iterator[dict]:
    whole = {"path": path, "value": value}
    if len(json_bytes(whole)) <= cap:
        yield whole
    elif isinstance(value, (dict, list)):
        empty = {"path": path, "value": {} if isinstance(value, dict) else []}
        if len(json_bytes(empty)) > cap:
            raise ArtifactError("response_too_large")
        yield empty
        items = sorted(value.items()) if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _fragments(child, f"{path}/{token}", cap)
    elif isinstance(value, str):
        start = 0
        while start < len(value):
            low, high = 0, min(len(value) - start, cap)
            while low < high:
                size = (low + high + 1) // 2
                part = {
                    "path": path,
                    "text": value[start : start + size],
                    "start": start,
                    "end": start + size,
                    "length": len(value),
                }
                if len(json_bytes(part)) <= cap:
                    low = size
                else:
                    high = size - 1
            if low == 0:
                raise ArtifactError("response_too_large")
            yield {
                "path": path,
                "text": value[start : start + low],
                "start": start,
                "end": start + low,
                "length": len(value),
            }
            start += low
        if not value:
            raise ArtifactError("response_too_large")
    else:
        raise ArtifactError("response_too_large")


def get_document(
    manifest: ArtifactManifest,
    passages: list[Passage],
    response: dict,
    cursor: str | None,
    cap: int,
    *,
    cache: DocumentCache | None = None,
) -> DocumentResult:
    content = {
        "response": {key: value for key, value in response.items() if key != "backend_raw"},
        "page_origins": manifest.page_origins,
        "evidence": [
            {key: value for key, value in passage.wire().items() if key not in {"text", "bbox"}}
            for passage in passages
        ],
        "warnings": manifest.warnings,
    }
    # Reserve room for the fixed envelope and a continuation token before subdividing values.
    if cap < 2048:
        raise ArtifactError("response_too_large")
    key = _binding([manifest.wire(), cap])
    entry = cache.entry if cache is not None else None
    if entry is not None and entry[0] == key:
        _, digest, plan = entry
    else:
        digest = hashlib.sha256(json_bytes(content)).hexdigest()
        plan = _plan(content, cap)
        if cache is not None:
            size = len(json_bytes([(p.path, p.kind, p.start, p.end, p.length) for p in plan]))
            # One assignment publishes a complete plan when retrieval calls overlap.
            cache.entry = (key, digest, plan) if size <= PLAN_CACHE_BYTES else None
    binding = _binding(["document", "0.1", manifest.artifact_id, digest, cap])
    start = _offset(cursor, binding, len(plan))

    def construct(values, continuation):
        return DocumentResult(
            artifact_id=manifest.artifact_id,
            display_name=manifest.display_name,
            content_sha256=digest,
            fragment_start=start,
            fragment_count=len(plan),
            fragments=values,
            next_cursor=continuation,
        )

    first = plan[start].materialize(content)
    size = len(json_bytes(construct([first], None).wire())) - len(json_bytes(first))
    values = []
    accepted = 0
    continuation = None
    for index in range(start, len(plan)):
        value = first if index == start else plan[index].materialize(content)
        size += len(json_bytes(value)) + bool(values)
        if size > cap:
            break
        values.append(value)
        token = _cursor(binding, index + 1) if index + 1 < len(plan) else None
        # Count each fragment once instead of repeatedly serializing an oversized prefix.
        # A final null cursor can fit after an intermediate continuation token did not.
        if size - len(b"null") + len(json_bytes(token)) <= cap:
            accepted = len(values)
            continuation = token
    if not accepted:
        raise ArtifactError("response_too_large")
    return construct(values[:accepted], continuation)
