"""Dimension C — text delta (DESIGN §4C). Each subject's canonical text (the ONE derivation, from
`evals.scorers.canonical_text`) compared pairwise with difflib. N-way = the pairwise matrix; the
two-subject case is the delta view's unified diff. Deterministic: fixed subject order, no deps
beyond stdlib difflib.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from openreading.evals.scorers import canonical_text

from .ingest import Subject

# A pair whose token similarity falls below this is flagged `text_divergence`. A documented,
# tunable constant (DESIGN §5: thresholds live in code, not flags); Enterprise calibration E5.
TEXT_DIVERGENCE_BELOW = 0.5


def _token_similarity(a: str, b: str) -> float:
    ta, tb = a.split(), b.split()
    if not ta and not tb:
        return 1.0
    return SequenceMatcher(None, ta, tb).ratio()


def _char_only_counts(a: str, b: str) -> tuple[int, int]:
    """(chars in `a` not matched in `b`, chars in `b` not matched in `a`) — a rough 'unique text'
    measure for the delta view."""
    matched = sum(size for _, _, size in SequenceMatcher(None, a, b).get_matching_blocks())
    return len(a) - matched, len(b) - matched


def _round(x: float) -> float:
    return round(x, 4)


def text_section(
    subjects: list[Subject],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (`text` section, findings). Matrix rows/cols follow subject order; `pairs` lists the
    upper triangle with per-pair unique-char counts."""
    texts = [canonical_text(s.response) for s in subjects]
    n = len(subjects)

    matrix = [[_round(_token_similarity(texts[i], texts[j])) for j in range(n)] for i in range(n)]

    pairs: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = matrix[i][j]
            a_only, b_only = _char_only_counts(texts[i], texts[j])
            pairs.append(
                {
                    "a": subjects[i].label,
                    "b": subjects[j].label,
                    "similarity": sim,
                    "a_only_chars": a_only,
                    "b_only_chars": b_only,
                }
            )
            if sim < TEXT_DIVERGENCE_BELOW:
                findings.append(
                    {
                        "code": "text_divergence",
                        "severity": "warn",
                        "page": None,
                        "field": None,
                        "bbox": None,
                        "subjects": sorted((subjects[i].label, subjects[j].label)),
                        "detail": f"{subjects[i].label} vs {subjects[j].label}: "
                        f"text similarity {sim:.2f} < {TEXT_DIVERGENCE_BELOW}",
                    }
                )

    return {"matrix": matrix, "pairs": pairs}, findings
