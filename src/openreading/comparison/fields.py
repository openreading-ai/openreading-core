"""Dimension B — field delta over `typed_fields` (DESIGN §4B). The direct answer to "which fields
did each backend get, miss, or disagree on?". Keys are matched exactly then by normalized form;
values are compared through the equivalence ladder. Verdicts and findings are deterministic.
"""

from __future__ import annotations

import re
from typing import Any

from .equivalence import equivalence_tier
from .ingest import Subject


def _norm_key(k: str) -> str:
    return re.sub(r"[^0-9a-z]", "", k.casefold())


def _field_value(entry: Any) -> Any:
    """A typed_fields entry is `{value, type, confidence, …}`; be lenient if a raw scalar slipped
    in."""
    if isinstance(entry, dict):
        return entry.get("value")
    return entry


def _field_conf(entry: Any) -> float | None:
    if isinstance(entry, dict):
        c = entry.get("confidence")
        return float(c) if isinstance(c, int | float) else None
    return None


def _as_str(v: Any) -> str | None:
    return None if v is None else str(v)


def field_section(
    subjects: list[Subject], caps: dict[str, dict[str, bool]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (`fields` section, findings). One row per distinct (normalized) key; `by_subject`
    records presence/value/confidence/equivalence for every subject."""
    # Group keys by normalized form, keeping the first-seen original as the display key. Iterate
    # subjects then keys in subject order for a deterministic canonical key and row order.
    order: list[str] = []
    canonical: dict[str, str] = {}
    for s in subjects:
        for key in s.typed_fields:
            nk = _norm_key(key)
            if nk not in canonical:
                canonical[nk] = key
                order.append(nk)

    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for nk in order:
        display = canonical[nk]
        # Resolve each subject's entry for this normalized key (exact key wins, else any match).
        present: list[tuple[str, Any]] = []  # (label, value)
        by_subject: dict[str, dict[str, Any]] = {}
        for s in subjects:
            entry = _match_entry(s.typed_fields, nk)
            val = None if entry is _MISSING else _field_value(entry)
            # An explicitly-`null` value is incomparable (equivalence_tier(None, x) is always
            # None — its documented "these are incomparable" contract, not "these disagree"), so
            # it gets the identical treatment as a key that never appeared at all: excluded from
            # `present`/the equivalence machinery, flowing into `capable_absent`/`field_missed`
            # instead of a false `field_value_conflict` (BL-109/Jin).
            if val is None:
                by_subject[s.label] = {
                    "present": False,
                    "value": None,
                    "confidence": None,
                    "equivalence": None,
                    "capable": caps[s.label]["fields"],
                }
            else:
                present.append((s.label, val))
                by_subject[s.label] = {
                    "present": True,
                    "value": _as_str(val),
                    "confidence": _field_conf(entry),
                    "equivalence": None,  # filled below (complete-linkage vs. every other present)
                    "capable": True,  # it produced the field
                }

        reference = present[0][1] if present else None
        for label, val in present:
            # Complete-linkage, not single-reference: report a real tier only when `val` agrees
            # with EVERY other present value, not just the arbitrary first one (`reference`). A
            # signal-less value can equivalence_tier-match two present values that do not match
            # each other, so a subject that only agrees with `reference` can still sit inside a
            # row-level disagreement — this must show `null`, not a false "matched" (BL-52/Jin).
            complete = all(
                equivalence_tier(val, other_val) is not None
                for other_label, other_val in present
                if other_label != label
            )
            by_subject[label]["equivalence"] = (
                ("exact" if val is reference else equivalence_tier(reference, val))
                if complete
                else None
            )

        # Mutual agreement among ALL present values, not just each vs. `present[0]` — a
        # signal-less value can equivalence_tier-match two present values that do not match each
        # other (BL-45/Jin), so anchoring on a single reference is not transitive.
        all_equiv = all(
            equivalence_tier(va, vb) is not None
            for i, (_, va) in enumerate(present)
            for _, vb in present[i + 1 :]
        )
        capable_absent = [
            s.label
            for s in subjects
            if not by_subject[s.label]["present"] and caps[s.label]["fields"]
        ]

        if len(present) == 1:
            verdict = "unique"
        elif all_equiv:
            verdict = "partial" if capable_absent else "agree"
        else:
            verdict = "disagree"

        rows.append({"key": display, "verdict": verdict, "by_subject": by_subject})

        if verdict == "disagree":
            findings.append(
                {
                    "code": "field_value_conflict",
                    "severity": "major",
                    "page": None,
                    "field": display,
                    "bbox": None,
                    "subjects": sorted(label for label, _ in present),
                    "detail": _conflict_detail(display, present),
                }
            )
        for label in sorted(capable_absent):
            findings.append(
                {
                    "code": "field_missed",
                    "severity": "warn",
                    "page": None,
                    "field": display,
                    "bbox": None,
                    "subjects": [label],
                    "detail": f"{label} did not extract {display!r}, which "
                    f"{len(present)} other subject(s) did",
                }
            )

    return {"rows": rows}, findings


_MISSING = object()


def _match_entry(typed_fields: dict[str, Any], nk: str) -> Any:
    for key, entry in typed_fields.items():
        if _norm_key(key) == nk:
            return entry
    return _MISSING


def _conflict_detail(display: str, present: list[tuple[str, Any]]) -> str:
    parts = ", ".join(f"{label}={_field_value_repr(val)}" for label, val in present)
    return f"{display}: {parts}"


def _field_value_repr(val: Any) -> str:
    return "∅" if val is None else repr(str(val))
