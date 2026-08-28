"""Pre-parse facts for `route:` dispatch (spec §3.1, signals.md §2).

Facts are computed once per document before the walk. A `when:` predicate over a fact that
cannot be computed (e.g. `pages_over` on a non-PDF byte stream) makes its rule **not match** and
is traced `fact_unavailable` — never an error; `default:` is the guaranteed floor (spec §2.5).
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from openreading.types.request import OpenReadingRequest

_MB = 1024 * 1024


@dataclass
class Facts:
    doc_type: str | None = None
    mime: str | None = None
    page_count: int | None = None
    size_mb: float | None = None
    filename: str | None = None
    compliance: dict[str, Any] = field(
        default_factory=dict
    )  # effective compliance (defaults filled)
    content_hash: str | None = None

    def sample_bucket(self) -> float | None:
        if self.content_hash is None:
            return None
        return (int(self.content_hash[:8], 16) % 10_000) / 100.0  # [0, 100)


@dataclass(frozen=True)
class FactRecord:
    key: str
    expected: Any
    observed: Any
    status: str  # "match" | "no_match" | "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact": self.key,
            "expected": self.expected,
            "observed": self.observed,
            "status": self.status,
        }


@dataclass
class WhenResult:
    matched: bool
    predicates: list[FactRecord]


def doc_bytes(req: OpenReadingRequest) -> bytes | None:
    d = req.document
    if d.bytes_base64:
        try:
            return base64.b64decode(d.bytes_base64)
        except Exception:  # noqa: BLE001
            return None
    return None


def page_count(data: bytes | None) -> int | None:
    if not data or data[:5] != b"%PDF-":
        return None
    try:
        import io

        from pypdf import PdfReader

        return max(1, len(PdfReader(io.BytesIO(data)).pages))
    except Exception:  # noqa: BLE001
        return None


def compute_facts(
    req: OpenReadingRequest, effective_compliance: dict[str, Any] | None = None
) -> Facts:
    """Compute every available fact for `req`. `effective_compliance` (the post-union request ∪
    --policy ∪ file `policy:` set, from compile) feeds the compliance fact; absent it falls back
    to the request's own compliance block."""
    d = req.document
    data = doc_bytes(req)
    compliance = effective_compliance
    if compliance is None:
        compliance = req.compliance.model_dump() if req.compliance else {}
    return Facts(
        doc_type=(req.routing.doc_type_hint if req.routing else None),
        mime=d.mime_type,
        page_count=page_count(data),
        size_mb=(len(data) / _MB if data else None),
        filename=d.filename,
        compliance=compliance,
        content_hash=(hashlib.sha256(data).hexdigest() if data else None),
    )


def _listify(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def evaluate_when(when: dict[str, Any], facts: Facts) -> WhenResult:
    """Evaluate a route `when:` map. Keys AND; `any_of` ORs its sub-maps. A fact that cannot be
    computed makes the whole rule not match (its predicate is recorded `unavailable`)."""
    records: list[FactRecord] = []
    all_match = True
    for key, val in when.items():
        if key == "any_of":
            subs = [evaluate_when(w, facts) for w in val]
            for s in subs:
                records.extend(s.predicates)
            all_match = all_match and any(s.matched for s in subs)
            continue
        matched, observed, status = _eval_fact(key, val, facts)
        records.append(FactRecord(key, val, observed, status))
        all_match = all_match and matched
    return WhenResult(all_match, records)


def _eval_fact(key: str, expected: Any, facts: Facts) -> tuple[bool, Any, str]:
    def result(observed: Any, matched: bool) -> tuple[bool, Any, str]:
        if observed is None:
            return False, None, "unavailable"
        return matched, observed, ("match" if matched else "no_match")

    if key == "doc_type":
        return result(facts.doc_type, facts.doc_type in _listify(expected))
    if key == "mime":
        return result(facts.mime, facts.mime in _listify(expected))
    if key == "pages_over":
        return result(
            facts.page_count, facts.page_count is not None and facts.page_count > expected
        )
    if key == "pages_under":
        return result(
            facts.page_count, facts.page_count is not None and facts.page_count < expected
        )
    if key == "size_over_mb":
        return result(facts.size_mb, facts.size_mb is not None and facts.size_mb > expected)
    if key == "size_under_mb":
        return result(facts.size_mb, facts.size_mb is not None and facts.size_mb < expected)
    if key == "filename_matches":
        obs = facts.filename
        return result(obs, obs is not None and re.search(expected, obs) is not None)
    if key == "sample_percent":
        bucket = facts.sample_bucket()
        return result(bucket, bucket is not None and bucket < expected)
    if key == "compliance":
        # nested {field: value}; always "available" (the compliance block carries defaults)
        matched = all(facts.compliance.get(f) == v for f, v in expected.items())
        return (
            matched,
            {f: facts.compliance.get(f) for f in expected},
            ("match" if matched else "no_match"),
        )
    # unknown fact key (schema should have rejected it) — never matches
    return False, None, "unavailable"
