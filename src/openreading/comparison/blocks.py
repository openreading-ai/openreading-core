"""Dimension D, the structural delta. Per page, align blocks (align.py) and classify:
matched (with type/position conflicts and confidence gaps), block_missed (any capable subject
lacking a block another capable subject has, majority or not — BL-71), block_unique (only one
capable subject has it). Subjects with no blocks are `not_capable` here and excluded — a
`blocks_unavailable` warning, never fabricated geometry. Tables get a grid-shape check via the
shared `evals.scorers.table_grid`.

Two-subject alignment is symmetric via `align_pair`. For N>2 subjects, alignment is anchored on
the first capable subject (D-v4-13). Full N-way clustering with cross-granularity merge is not
built, and `unaligned_ratio` surfaces how much the heuristic left unmatched. The four dimensions
and this one's place among them are stated in the `openreading.comparison` docstring.
"""

from __future__ import annotations

from typing import Any

from openreading.evals.scorers import response_tables

from .align import IOU_MIN, MERGE_LOOKAHEAD, METHOD, TAU_TEXT, align_pair, normalize_block_text
from .ingest import Subject

CONF_GAP_MIN = 0.2  # matched blocks whose confidences differ by at least this → confidence_gap


def _pages(resp: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for page in (resp.get("document") or {}).get("pages") or []:
        pn = page.get("page_number")
        if pn is not None:
            out[int(pn)] = page.get("blocks") or []
    return out


def _block_type(block: dict[str, Any]) -> str:
    return str(block.get("type") or "other")


def _excerpt(block: dict[str, Any], limit: int = 60) -> str:
    """A one-line text excerpt of a block for a finding's snippet — whitespace collapsed, truncated
    so the report is readable without opening the source responses."""
    text = " ".join((block.get("text") or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _conf_suffix(block: dict[str, Any]) -> str:
    c = block.get("confidence")
    return f" (confidence {c:.2f})" if isinstance(c, int | float) else ""


def _subject_text_on_page(resp: dict[str, Any], pn: int) -> str:
    """The subject's own text for a page, normalized for presence-testing: the flat text channel
    (which spans the whole document — the C2-complete projection) plus this page's text and block
    texts."""
    doc = resp.get("document") or {}
    parts = [doc.get("text") or ""]
    for page in doc.get("pages") or []:
        if page.get("page_number") == pn:
            parts.append(page.get("text") or "")
            for b in page.get("blocks") or []:
                parts.append(b.get("text") or "")
    return normalize_block_text(" ".join(parts))


def _content_present(block: dict[str, Any], resp: dict[str, Any], pn: int) -> bool:
    """Is this block's text present in the subject's own page text? If so the 'miss' is a
    packaging difference, because the words are there and only the segmentation differs. It is not
    content loss, so `_missed` reports it under the informational `structure` code rather than
    `block_missed`."""
    needle = normalize_block_text(block.get("text") or "")
    return bool(needle) and needle in _subject_text_on_page(resp, pn)


def blocks_section(
    subjects: list[Subject], caps: dict[str, dict[str, bool]]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (`blocks` section, findings, warnings, `alignment` object).

    `alignment.capable_subjects` (BL-58) is how many subjects are jointly block-capable —
    `caps[label]["blocks"]` (capabilities.py) alongside this function's own "did it actually
    produce a block" check, the same shape `field_section` already uses for `caps[label]["fields"]`.
    `_headline` gates the `table_cells` channel on this count instead of recomputing an
    independent, weaker per-subject check that ignores this module's own "capable" concept.

    An unmatched anchor block classifies one of two ways: `block_unique` when only the anchor
    has it (a lone singleton), or `block_missed` — once for every other capable subject not in
    `present` — in every other case (BL-71). That covers a strict majority same as before, but
    also a genuine multi-subject split that is neither a majority nor a singleton (e.g. 2-of-4,
    a clean 3-of-6 tie): the content is still absent for whichever capable subjects aren't in
    `present`, regardless of whether the ones that do have it clear half. Only true unanimity —
    `present` already covers every capable subject — is silent, correctly.
    """
    pages_by = {s.label: _pages(s.response) for s in subjects}
    capable = [s for s in subjects if caps[s.label]["blocks"] and any(pages_by[s.label].values())]
    findings: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for s in subjects:
        if s not in capable:
            warnings.append(
                {
                    "code": "blocks_unavailable",
                    "subject": s.label,
                    "detail": f"{s.label} produced no blocks; excluded from the structural delta",
                }
            )

    total_blocks = 0
    matched_instances: set[tuple[str, int, int]] = set()
    page_summaries: list[dict[str, Any]] = []

    if len(capable) >= 2:
        anchor = capable[0]
        others = capable[1:]
        all_pages = sorted({p for s in capable for p in pages_by[s.label]})

        # page-count mismatch across capable subjects
        counts = {s.label: len(pages_by[s.label]) for s in capable}
        if len(set(counts.values())) > 1:
            findings.append(
                {
                    "code": "page_count_mismatch",
                    "severity": "warn",
                    "page": None,
                    "field": None,
                    "bbox": None,
                    "subjects": sorted(counts),
                    "detail": f"page counts differ: {counts}",
                }
            )

        for s in capable:
            total_blocks += sum(len(b) for b in pages_by[s.label].values())

        for pn in all_pages:
            anchor_blocks = pages_by[anchor.label].get(pn, [])
            # for each anchor block index → set of subject labels that matched it (anchor included)
            anchor_present: list[set[str]] = [{anchor.label} for _ in anchor_blocks]
            page_matched = 0

            for other in others:
                other_blocks = pages_by[other.label].get(pn, [])
                matches, _unmatched_a, unmatched_b = align_pair(anchor_blocks, other_blocks)
                for m in matches:
                    for ai in m.a.indices:
                        anchor_present[ai].add(other.label)
                        matched_instances.add((anchor.label, pn, ai))
                    for bi in m.b.indices:
                        matched_instances.add((other.label, pn, bi))
                    page_matched += 1
                    _classify_match(m, anchor, other, anchor_blocks, other_blocks, pn, findings)
                # other-subject blocks that matched no anchor block → unique to that subject
                for bi in unmatched_b:
                    findings.append(_unique(other.label, pn, other_blocks[bi]))

            # anchor blocks: a lone singleton is block_unique; every other capable subject not
            # in `present` is block_missed (BL-71) — majority or not. A genuine multi-subject
            # split that clears neither bucket (e.g. 2-of-4, a clean 3-of-6 tie) is still "some
            # capable subject doesn't have this block" for whoever isn't in `present`; true
            # unanimity (`present` covers every capable subject) leaves nobody `not in present`,
            # so the loop below degrades to a no-op and nothing fires.
            for ai, present in enumerate(anchor_present):
                if len(present) == 1:
                    findings.append(_unique(anchor.label, pn, anchor_blocks[ai]))
                else:
                    for s in capable:
                        if s.label not in present:
                            packaging = _content_present(anchor_blocks[ai], s.response, pn)
                            findings.append(
                                _missed(
                                    s.label,
                                    pn,
                                    anchor_blocks[ai],
                                    len(present),
                                    packaging=packaging,
                                )
                            )

            page_summaries.append({"page": pn, "matched": page_matched})

        _table_findings(capable, findings)

    aligned = len(matched_instances)
    alignment = {
        "method": METHOD,
        "tau_text": TAU_TEXT,
        "iou_min": IOU_MIN,
        "merge_lookahead": MERGE_LOOKAHEAD,
        "capable_subjects": len(capable),  # BL-58: threaded out to `_headline`'s table_cells gate
    }
    if total_blocks:
        alignment["unaligned_ratio"] = round((total_blocks - aligned) / total_blocks, 4)
    # else: nothing was attempted (fewer than two subjects were block-capable) — BL-58: omit the
    # ratio rather than default to 0.0, which would misread as "perfectly aligned". The ratio is
    # an honesty signal, so a value it never measured must not be invented.
    return {"pages": page_summaries}, findings, warnings, alignment


def _classify_match(m, anchor, other, anchor_blocks, other_blocks, pn, findings) -> None:
    a_block = anchor_blocks[m.a.indices[0]]
    b_block = other_blocks[m.b.indices[0]]
    subs = sorted((anchor.label, other.label))
    snippet = _excerpt(a_block) or _excerpt(b_block)
    if m.a.btype != m.b.btype and "mixed" not in (m.a.btype, m.b.btype):
        findings.append(
            {
                "code": "type_conflict",
                "severity": "warn",
                "page": pn,
                "field": None,
                "bbox": None,
                "subjects": subs,
                "detail": f"{anchor.label} calls it {m.a.btype}, {other.label} calls it {m.b.btype}",
                "snippet": snippet,
            }
        )
    if m.iou is not None and m.iou < IOU_MIN:
        findings.append(
            {
                "code": "position_conflict",
                "severity": "warn",
                "page": pn,
                "field": None,
                "bbox": None,
                "subjects": subs,
                "detail": f"same text, different position (IoU {m.iou:.2f} < {IOU_MIN})",
                "snippet": snippet,
            }
        )
    ca, cb = a_block.get("confidence"), b_block.get("confidence")
    if isinstance(ca, int | float) and isinstance(cb, int | float) and abs(ca - cb) >= CONF_GAP_MIN:
        findings.append(
            {
                "code": "confidence_gap",
                "severity": "info",
                "page": pn,
                "field": None,
                "bbox": None,
                "subjects": subs,
                "detail": f"confidence {anchor.label}={ca:.2f} vs {other.label}={cb:.2f}",
                "snippet": snippet,
            }
        )


def _unique(label: str, pn: int, block: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "block_unique",
        "severity": "info",
        "page": pn,
        "field": None,
        "bbox": None,
        "subjects": [label],
        "detail": f"only {label} has this {_block_type(block)} block{_conf_suffix(block)}",
        "snippet": _excerpt(block) or None,
    }


def _missed(
    label: str, pn: int, block: dict[str, Any], present: int, *, packaging: bool = False
) -> dict[str, Any]:
    if packaging:
        # Content is present in the subject's own text, so this is a granularity difference and
        # not a content miss. It gets the dedicated informational `structure` code, which the
        # comparison-report v0.2 finding enum carries.
        return {
            "code": "structure",
            "severity": "info",
            "page": pn,
            "field": None,
            "bbox": None,
            "subjects": [label],
            "detail": (
                f"{label} packages this {_block_type(block)} block's text differently. The content "
                f"is present in {label}'s text, so this is a granularity difference, not a "
                f"content miss"
            ),
            "snippet": _excerpt(block) or None,
        }
    return {
        "code": "block_missed",
        "severity": "warn",
        "page": pn,
        "field": None,
        "bbox": None,
        "subjects": [label],
        "detail": f"{label} missed a {_block_type(block)} block {present} other subject(s) have",
        "snippet": _excerpt(block) or None,
    }


def _table_findings(capable: list[Subject], findings: list[dict[str, Any]]) -> None:
    grids = {s.label: response_tables(s.response) for s in capable}
    counts = {label: len(g) for label, g in grids.items()}
    if len(set(counts.values())) > 1:
        findings.append(
            {
                "code": "table_shape_mismatch",
                "severity": "warn",
                "page": None,
                "field": None,
                "bbox": None,
                "subjects": sorted(counts),
                "detail": f"table counts differ: {counts}",
            }
        )
        return
    # equal counts: compare each table's shape across subjects (anchor vs others)
    anchor = capable[0].label
    for idx, grid in enumerate(grids[anchor]):
        shape = (len(grid), max((len(r) for r in grid), default=0))
        for s in capable[1:]:
            g = grids[s.label][idx]
            other_shape = (len(g), max((len(r) for r in g), default=0))
            if other_shape != shape:
                findings.append(
                    {
                        "code": "table_shape_mismatch",
                        "severity": "warn",
                        "page": None,
                        "field": None,
                        "bbox": None,
                        "subjects": sorted((anchor, s.label)),
                        "detail": f"table {idx} shape {anchor}={shape} vs {s.label}={other_shape}",
                    }
                )
