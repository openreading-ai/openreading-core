"""Three stances, one report shape (DESIGN §6). Symmetric (default) adds a `consensus` section at
N≥3; `--baseline` signs every field delta against one subject; `--truth` scores each subject with
the shared evals scorer (the bridge to the eval harness, not a second scoring system — L5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openreading.evals.scorers import score

from .equivalence import equivalence_tier
from .fields import _field_value, _norm_key
from .ingest import CompareInputError, Subject, load_subjects


def _field_keys(subjects: list[Subject]) -> list[tuple[str, str]]:
    """Ordered (normalized_key, display_key) across all subjects, first-seen display wins."""
    order: list[str] = []
    canonical: dict[str, str] = {}
    for s in subjects:
        for key in s.typed_fields:
            nk = _norm_key(key)
            if nk not in canonical:
                canonical[nk] = key
                order.append(nk)
    return [(nk, canonical[nk]) for nk in order]


def _value_for(subject: Subject, nk: str) -> tuple[bool, Any]:
    """`(present, value)` for `subject`'s entry at normalized key `nk`. An explicitly-`null`
    value is treated the same as a key that never appeared: `present=False` — it is incomparable
    (equivalence_tier(None, x) is always None, "incomparable" not "disagree"), so both
    `consensus_section` and `baseline_section` must exclude it from clustering/matching rather
    than reading it as a genuine outlier or a "differ" against a real value (BL-109)."""
    for key, entry in subject.typed_fields.items():
        if _norm_key(key) == nk:
            val = _field_value(entry)
            return val is not None, val
    return False, None


def consensus_section(
    subjects: list[Subject], caps: dict[str, dict[str, bool]]
) -> dict[str, Any] | None:
    """N≥3 only: per-field majority value (strict > half of field-capable subjects) and the
    subjects that disagree with it. A row is emitted for every field at least one subject
    reported, whether or not a cluster reaches majority — a genuine N-way split, a clean tie, or
    one subject reporting what the others missed is exactly the outcome this section exists to
    surface, so it gets `has_majority: False` / `majority_value: None` / every present subject
    listed in `outliers`, not silent omission from the report (BL-68)."""
    if len(subjects) < 3:
        return None
    n_capable = sum(1 for s in subjects if caps[s.label]["fields"])
    rows: list[dict[str, Any]] = []
    for nk, display in _field_keys(subjects):
        present = [(s.label, _value_for(s, nk)[1]) for s in subjects if _value_for(s, nk)[0]]
        if not present:
            continue
        # cluster present values by equivalence; the largest cluster is the candidate majority.
        # Complete-linkage (must match EVERY existing cluster member, not just cl[0]) — a
        # signal-less value can bridge to two values that do not agree with each other, so
        # single-linkage against the first member alone is not transitive (BL-45).
        clusters: list[list[tuple[str, Any]]] = []
        for label, val in present:
            for cl in clusters:
                if all(equivalence_tier(v, val) is not None for _, v in cl):
                    cl.append((label, val))
                    break
            else:
                clusters.append([(label, val)])
        largest = max(clusters, key=len)
        has_majority = len(largest) > n_capable / 2
        majority_labels = {label for label, _ in largest} if has_majority else set()
        outliers = sorted(label for label, _ in present if label not in majority_labels)
        rows.append(
            {
                "key": display,
                "majority_value": (
                    str(largest[0][1]) if has_majority and largest[0][1] is not None else None
                ),
                "has_majority": has_majority,
                "majority_count": len(largest),
                "capable": n_capable,
                "outliers": outliers,
            }
        )
    return {"fields": rows}


def baseline_section(subjects: list[Subject], baseline_label: str) -> dict[str, Any]:
    """Sign field deltas against one subject: for each other subject, is a field match / differ /
    missing (baseline had it, they didn't) / extra (they had it, baseline didn't).

    Caveat: this only signs each subject's delta against the baseline — it never claims that two
    non-baseline subjects agree with each other (that's `consensus_section`'s job, at N>=3)."""
    base = next(s for s in subjects if s.label == baseline_label)
    others = [s for s in subjects if s.label != baseline_label]
    rows: list[dict[str, Any]] = []
    for nk, display in _field_keys(subjects):
        b_present, b_val = _value_for(base, nk)
        by_subject: dict[str, str] = {}
        for s in others:
            present, val = _value_for(s, nk)
            if b_present and present:
                by_subject[s.label] = (
                    "match" if equivalence_tier(b_val, val) is not None else "differ"
                )
            elif b_present and not present:
                by_subject[s.label] = "missing"
            elif not b_present and present:
                by_subject[s.label] = "extra"
        rows.append(
            {
                "key": display,
                "baseline_value": None
                if not b_present
                else (None if b_val is None else str(b_val)),
                "baseline_present": b_present,
                "by_subject": by_subject,
            }
        )
    return {"baseline": baseline_label, "fields": rows}


def truth_section(subjects: list[Subject], truth: dict[str, Any]) -> dict[str, Any]:
    """Per-subject evals score against the `expected` dict — precision/recall/F1, text similarity,
    table grid — laid out side by side (L5: the evals scorer verbatim, no second system). When
    `truth` names none of the scorer's five dimensions (most simply `{}`), `score()` returns an
    honest `overall: None` for every subject rather than a false-perfect `1.0` — surfaced here
    unchanged, since this section is a pass-through of the scorer's own dict (BL-79)."""
    return {
        "dimensions": sorted(truth.keys()),
        "by_subject": {s.label: score(s.response, truth) for s in subjects},
    }


def resolve_baseline(baseline: Any, subjects: list[Subject]) -> str:
    """A baseline is either an existing subject label or an extra response (dict/path) appended as
    a new subject. Returns the baseline subject's label; may append to `subjects`."""
    if isinstance(baseline, str) and any(s.label == baseline for s in subjects):
        return baseline
    extra = load_subjects([baseline, baseline])[0]  # validate one response via the shared loader
    label = extra.backend_id
    if any(s.label == label for s in subjects):
        label = f"{label}#baseline"
    appended = Subject(label=label, response=extra.response, source="file")
    subjects.append(appended)
    return label


def load_truth(truth: Any) -> dict[str, Any]:
    if isinstance(truth, dict):
        return truth
    try:
        return json.loads(Path(truth).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise CompareInputError(f"cannot read truth JSON from {truth!r}: {exc}") from exc
