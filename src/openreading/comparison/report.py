"""Report assembly. Compose the per-dimension sections into one versioned, schema-valid report.
Deterministic: fixed subject order, findings sorted by a fixed key, every report validated against
comparison-report.v0.2.json before it is returned. v0.2 adds the content-first `headline`, the
`structure` finding code, and the non-determinism rule. The `openreading.comparison` docstring's
"The report" section states what each of those means.
"""

from __future__ import annotations

from typing import Any

from openreading.schemas import validate_comparison_report

from .blocks import blocks_section
from .capabilities import resolve_capabilities
from .facts import subject_dict, subject_facts
from .fields import field_section
from .ingest import Subject
from .stances import baseline_section, consensus_section, truth_section
from .text import text_section

_SEVERITY_RANK = {"major": 0, "warn": 1, "info": 2}

# The non-determinism rule. The response carries NO determinism signal, and output_paradigm is
# not a reliable proxy (nuextract is typed_fields/markdown, open-ocr is token_stream), so this is
# a documented constant keyed on backend.id. A content finding that involves one of these
# subjects caps at informational, because run-to-run drift is indistinguishable from a real
# difference.
_NON_DETERMINISTIC = frozenset({"anthropic-claude", "google-gemini", "qwen-vl", "nuextract"})
# Codes that assert CONTENT equivalence (as opposed to structure/packaging/cost).
_CONTENT_CODES = frozenset(
    {"text_divergence", "field_value_conflict", "block_missed", "table_shape_mismatch"}
)


def _nondeterministic_labels(subjects: list[Subject]) -> list[str]:
    return sorted(s.label for s in subjects if s.backend_id in _NON_DETERMINISTIC)


def _cap_nondeterministic(findings: list[dict[str, Any]], nd_labels: list[str]) -> None:
    """Downgrade content findings that involve a non-deterministic subject to informational."""
    nd = set(nd_labels)
    for f in findings:
        if (
            f["code"] in _CONTENT_CODES
            and f.get("severity") in ("warn", "major")
            and (set(f.get("subjects") or []) & nd)
        ):
            f["severity"] = "info"
            f["detail"] += (
                " (info: involves a non-deterministic or generative subject, similarity only)"
            )


def _has_table(subject: Subject) -> bool:
    for page in (subject.response.get("document") or {}).get("pages") or []:
        for b in page.get("blocks") or []:
            if b.get("type") == "table":
                return True
    return False


def _headline(
    subjects: list[Subject],
    findings: list[dict[str, Any]],
    nd_labels: list[str],
    alignment: dict[str, Any],
) -> dict[str, Any]:
    """Content-first headline: equivalence on the guaranteed channels, computed from the content
    findings. C1/C2 removed OpenReading-introduced text divergence, so a remaining difference is
    attributable to the engines."""
    codes = {f["code"] for f in findings}
    channels: dict[str, Any] = {
        "text": {
            "agreement": "diverge" if "text_divergence" in codes else "agree",
            "score": None,
            "detail": None,
        }
    }
    if any(s.typed_fields for s in subjects):
        fdiff = {"field_value_conflict", "field_missed"} & codes
        channels["typed_fields"] = {
            "agreement": "partial" if fdiff else "agree",
            "score": None,
            "detail": None,
        }
    if any(_has_table(s) for s in subjects):
        # BL-58: `_has_table` alone only asks whether SOME subject produced a table — it says
        # nothing about whether a table comparison actually ran. `table_shape_mismatch` is only
        # ever emitted when blocks_section found >=2 jointly block-capable subjects
        # (alignment.capable_subjects); below that, the finding was structurally incapable of
        # firing, so "agree" would be unverified, not genuine agreement. Reuse the typed_fields
        # channel's own precedent for "one side capable, didn't produce" instead of inventing a
        # new vocabulary word: "partial".
        if alignment.get("capable_subjects", 0) < 2:
            table_agreement = "partial"
        else:
            table_agreement = "diverge" if "table_shape_mismatch" in codes else "agree"
        channels["table_cells"] = {
            "agreement": table_agreement,
            "score": None,
            "detail": None,
        }
    agreements = {c["agreement"] for c in channels.values()}
    verdict = (
        "equivalent"
        if agreements == {"agree"}
        else "divergent"
        if agreements == {"diverge"}
        else "mixed"
    )
    return {"verdict": verdict, "channels": channels, "nondeterministic_subjects": nd_labels}


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_RANK.get(f["severity"], 9),
            f["code"],
            f.get("field") or "",
            f.get("page") if f.get("page") is not None else -1,
            ",".join(f.get("subjects") or []),
        ),
    )


def _facts_findings(subjects: list[Subject]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in subjects:
        f = subject_facts(s)
        if f["blocks"] == 0 and f["chars"] == 0 and f["fields"] == 0:
            out.append(
                {
                    "code": "empty_output",
                    "severity": "warn",
                    "page": None,
                    "field": None,
                    "bbox": None,
                    "subjects": [s.label],
                    "detail": f"{s.label} produced no text, blocks, or fields",
                }
            )
    return out


def _propagated_warnings(subjects: list[Subject]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in subjects:
        codes = {str(w.get("code")) for w in (s.response.get("warnings") or [])}
        if "confidence_unavailable" in codes:
            out.append(
                {
                    "code": "confidence_unavailable",
                    "subject": s.label,
                    "detail": f"{s.label} reports it cannot produce per-element confidence",
                }
            )
    return out


def build_report(
    subjects: list[Subject],
    *,
    baseline_label: str | None = None,
    truth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caps, cap_warnings = resolve_capabilities(subjects)

    fields_sec, field_findings = field_section(subjects, caps)
    text_sec, text_findings = text_section(subjects)
    blocks_sec, block_findings, block_warnings, alignment = blocks_section(subjects, caps)

    raw_findings = [
        *field_findings,
        *text_findings,
        *block_findings,
        *_facts_findings(subjects),
    ]
    nd_labels = _nondeterministic_labels(subjects)
    _cap_nondeterministic(raw_findings, nd_labels)  # the non-determinism rule
    findings = _sort_findings(raw_findings)

    warnings: list[dict[str, Any]] = [
        *cap_warnings,
        *block_warnings,
        *_propagated_warnings(subjects),
    ]
    if len({_canonical(s.response) for s in subjects}) == 1:
        warnings.append(
            {
                "code": "subjects_identical",
                "subject": None,
                "detail": "all subjects are byte-identical responses",
            }
        )

    report: dict[str, Any] = {
        "schema_version": "0.2",
        "mode": "pairwise" if len(subjects) == 2 else "nway",
        "subjects": [subject_dict(s) for s in subjects],
        "alignment": alignment,
        "headline": _headline(  # content-first summary
            subjects, findings, nd_labels, alignment
        ),
        "fields": fields_sec,
        "text": text_sec,
        "blocks": blocks_sec,
        "findings": findings,
        "warnings": warnings,
    }
    consensus = consensus_section(subjects, caps)
    if consensus is not None:
        report["consensus"] = consensus
    if baseline_label is not None:
        report["baseline"] = baseline_section(subjects, baseline_label)
    if truth is not None:
        report["truth"] = truth_section(subjects, truth)

    validate_comparison_report(report)
    return report


def _canonical(resp: dict[str, Any]) -> str:
    import json

    return json.dumps(resp, sort_keys=True, ensure_ascii=False)
