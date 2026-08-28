"""Dimension A — run facts (DESIGN §4A). Pure envelope reads; never degraded. One flat record per
subject: state, timing, money, and structural counts. The scoreboard the user reads first."""

from __future__ import annotations

from typing import Any

from openreading.evals.scorers import canonical_text

from .ingest import Subject


def _blocks_of(resp: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in (resp.get("document") or {}).get("pages") or []:
        out.extend(page.get("blocks") or [])
    return out


def _char_count(resp: dict[str, Any]) -> int:
    """Length of the subject's primary text surface. Delegates to the one canonical-text
    derivation shared with evals (`evals.scorers.canonical_text`, DESIGN §4C: text → markdown →
    block text in reading order) instead of maintaining a second, independently driftable copy of
    the precedence rule (BL-136)."""
    return len(canonical_text(resp))


def subject_facts(subject: Subject) -> dict[str, Any]:
    resp = subject.response
    usage = resp.get("usage") or {}
    doc = resp.get("document") or {}
    pages = doc.get("pages") or []
    page_count = len(pages) or int(doc.get("page_count") or 0)
    warnings = resp.get("warnings") or []
    return {
        "state": str((resp.get("status") or {}).get("state") or "unknown"),
        "duration_ms": usage.get("duration_ms"),
        "cost_usd": usage.get("cost_usd"),
        "cost_basis": usage.get("cost_basis"),
        "pages": page_count,
        "blocks": len(_blocks_of(resp)),
        "chars": _char_count(resp),
        "fields": len(subject.typed_fields),
        "warning_codes": sorted(
            {str(w.get("code")) for w in warnings if w.get("code") is not None}
        ),
    }


def subject_dict(subject: Subject) -> dict[str, Any]:
    """The `subjects[]` entry: identity, provenance, and the facts scoreboard."""
    backend: dict[str, Any] = {"id": subject.backend_id, "type": subject.backend_type}
    if subject.output_paradigm is not None:
        backend["output_paradigm"] = subject.output_paradigm
    return {
        "label": subject.label,
        "backend": backend,
        "source": subject.source,
        "facts": subject_facts(subject),
    }
