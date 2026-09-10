"""Deterministic lexical retrieval and byte-bounded exact reads over verified passages.

Queries match original Unicode letter/number spans after case folding. Ranking prefers
more distinct terms, then physical page and source position. Excerpts retain original
code-point offsets even when case folding expands a character, such as German sharp S.
Cursors bind the request and next offset. They convey no authority or filesystem path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from typing import TypeVar

from openreading.artifacts.constants import (
    MAX_CURSOR_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_QUERY_CHARS,
    MAX_READ_PASSAGES,
    MAX_SEARCH_HITS,
)
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import (
    ArtifactManifest,
    Passage,
    ReadResult,
    SearchHit,
    SearchResult,
    WireModel,
    json_bytes,
)

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
Result = TypeVar("Result", bound=WireModel)


def _binding(request: list) -> str:
    return hashlib.sha256(json_bytes(request)).hexdigest()


def _cursor(binding: str, offset: int) -> str:
    return (
        base64.urlsafe_b64encode(json_bytes({"v": 1, "request": binding, "offset": offset}))
        .decode()
        .rstrip("=")
    )


def _offset(cursor: str | None, binding: str, length: int) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > MAX_CURSOR_CHARS or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError("Invalid encoding")
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            set(value) != {"v", "request", "offset"}
            or value["v"] != 1
            or value["request"] != binding
            or type(value["offset"]) is not int
            or not 0 < value["offset"] < length
        ):
            raise ValueError("Invalid cursor binding")
        return value["offset"]
    except (ValueError, TypeError, KeyError):
        raise ArtifactError("invalid_cursor") from None


def _fit(
    items: list,
    start: int,
    limit: int,
    binding: str,
    cap: int,
    construct: Callable[[list, str | None], Result],
) -> Result:
    end = min(len(items), start + limit)
    while end >= start:
        cursor = _cursor(binding, end) if end < len(items) else None
        result = construct(items[start:end], cursor)
        if len(json_bytes(result.wire())) <= cap and (end > start or not items):
            return result
        end -= 1
    raise ArtifactError("response_too_large")


def search(
    manifest: ArtifactManifest,
    passages: list[Passage],
    query: str,
    limit: int,
    cursor: str | None,
    cap: int,
) -> SearchResult:
    if (
        not isinstance(query, str)
        or len(query) > MAX_QUERY_CHARS
        or type(limit) is not int
        or not 1 <= limit <= MAX_SEARCH_HITS
    ):
        raise ValueError("Query or limit is outside the tool contract")
    terms = list(dict.fromkeys(m.group().casefold() for m in TOKEN.finditer(query)))
    matches = []
    for passage in passages:
        tokens = [(m.group().casefold(), m.start()) for m in TOKEN.finditer(passage.text)]
        found = [term for term in terms if any(token == term for token, _ in tokens)]
        if not found:
            continue
        first = min(offset for token, offset in tokens if token in found)
        start = max(0, first - 60)
        end = min(len(passage.text), start + MAX_EXCERPT_CHARS)
        matches.append(
            (
                len(found),
                passage,
                SearchHit(
                    evidence_id=passage.evidence_id,
                    page=passage.page,
                    text_origin=passage.text_origin,
                    matched_terms=found,
                    excerpt_start=start,
                    excerpt_end=end,
                    excerpt=passage.text[start:end],
                ),
            )
        )
    matches.sort(
        key=lambda item: (-item[0], item[1].page, item[1].block_index, item[1].segment_index)
    )
    hits = [item[2] for item in matches]
    binding = _binding(["search", manifest.artifact_id, terms, limit])
    start = _offset(cursor, binding, len(hits))
    return _fit(
        hits,
        start,
        limit,
        binding,
        cap,
        lambda values, continuation: SearchResult(
            artifact_id=manifest.artifact_id,
            query=query,
            hits=values,
            next_cursor=continuation,
            warnings=[] if hits else ["no_matches"],
        ),
    )


def read(
    manifest: ArtifactManifest,
    passages: list[Passage],
    evidence_ids: list[str],
    cursor: str | None,
    cap: int,
) -> ReadResult:
    if not 1 <= len(evidence_ids) <= MAX_READ_PASSAGES or len(set(evidence_ids)) != len(
        evidence_ids
    ):
        raise ValueError("Read requires one to eight unique evidence identifiers")
    by_id = {p.evidence_id: p for p in passages}
    if any(identifier not in by_id for identifier in evidence_ids):
        raise ArtifactError("evidence_not_found")
    ordered = [by_id[identifier] for identifier in evidence_ids]
    binding = _binding(["read", manifest.artifact_id, evidence_ids])
    start = _offset(cursor, binding, len(ordered))
    return _fit(
        ordered,
        start,
        len(ordered),
        binding,
        cap,
        lambda values, continuation: ReadResult(
            artifact_id=manifest.artifact_id,
            display_name=manifest.display_name,
            passages=values,
            next_cursor=continuation,
        ),
    )
