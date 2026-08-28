"""Human renderings of a comparison report (DESIGN §8). Delta-first: agreement is hidden by
default — the user asked for the diff. `explain` reuses `render_table`. Pure string builders over
the report dict; no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from openreading.derive.tables import _esc_pipe


def _md_row(cells: Sequence[str]) -> str:
    """One GFM table row. Every cell goes through the same escape the table renderer uses, so a
    literal `|` in a detail or snippet can never open a column."""
    return "| " + " | ".join(_esc_pipe(c) for c in cells) + " |"


def _fmt_cost(c: Any) -> str:
    return f"${c:.4f}" if isinstance(c, int | float) else "-"


def _fmt_ms(m: Any) -> str:
    return f"{int(m)}ms" if isinstance(m, int | float) else "-"


def render_table(report: dict[str, Any], *, show_agreements: bool = False) -> str:
    lines: list[str] = []
    subs = report["subjects"]
    lines.append(f"COMPARE — {len(subs)} subjects ({report['mode']})")
    lines.append("")

    # scoreboard
    hdr = f"{'SUBJECT':<20}{'TYPE':<16}{'PAGES':>6}{'BLOCKS':>7}{'CHARS':>7}{'FIELDS':>7}{'COST':>10}{'TIME':>8}"
    lines.append(hdr)
    for s in subs:
        f = s["facts"]
        lines.append(
            f"{s['label']:<20}{str(s['backend'].get('type') or '-'):<16}"
            f"{f['pages']:>6}{f['blocks']:>7}{f['chars']:>7}{f['fields']:>7}"
            f"{_fmt_cost(f['cost_usd']):>10}{_fmt_ms(f['duration_ms']):>8}"
        )

    # content-first headline (§9 tranche 2): equivalence on the guaranteed channels
    hl = report.get("headline")
    if hl:
        chans = "  ".join(f"{k}:{v['agreement']}" for k, v in (hl.get("channels") or {}).items())
        lines += ["", f"CONTENT: {str(hl.get('verdict', '')).upper()}  ({chans})"]
        if hl.get("nondeterministic_subjects"):
            lines.append(
                f"  non-deterministic (similarity-only): "
                f"{', '.join(hl['nondeterministic_subjects'])}"
            )

    # text similarity (pairwise headline)
    matrix = report.get("text", {}).get("matrix")
    if matrix and len(subs) == 2:
        lines += ["", f"text similarity: {matrix[0][1]:.2f}"]
    al = report.get("alignment")
    if al:
        # BL-58: `unaligned_ratio` is omitted (not 0.0) when fewer than two subjects were block-
        # capable — nothing was attempted, so render that honestly instead of "0.00" (perfectly
        # aligned).
        ratio = al.get("unaligned_ratio")
        ratio_str = f"{ratio:.2f}" if isinstance(ratio, int | float) else "n/a"
        lines.append(f"block alignment: {al['method']}  unaligned={ratio_str}")

    # findings (deltas only)
    findings = report.get("findings", [])
    lines += ["", f"FINDINGS ({len(findings)})"]
    if not findings:
        lines.append("  (none — subjects agree on every compared dimension)")
    for fnd in findings:
        loc = fnd.get("field") or (f"p{fnd['page']}" if fnd.get("page") is not None else "")
        loc = f" {loc}" if loc else ""
        snip = f'  "{fnd["snippet"]}"' if fnd.get("snippet") else ""
        lines.append(
            f"  [{fnd['severity']:>5}] {fnd['code']}{loc}  "
            f"{{{', '.join(fnd['subjects'])}}}  — {fnd['detail']}{snip}"
        )

    if show_agreements:
        agree = [r for r in report.get("fields", {}).get("rows", []) if r["verdict"] == "agree"]
        lines += ["", f"AGREEMENTS ({len(agree)} fields)"]
        for r in agree:
            lines.append(f"  {r['key']}")

    warnings = report.get("warnings", [])
    if warnings:
        lines += ["", "WARNINGS"]
        for w in warnings:
            who = f" [{w['subject']}]" if w.get("subject") else ""
            lines.append(f"  {w['code']}{who}")

    return "\n".join(lines)


def render_markdown(report: dict[str, Any], *, show_agreements: bool = False) -> str:
    subs = report["subjects"]
    out: list[str] = [f"# Comparison — {len(subs)} subjects ({report['mode']})", ""]
    out += [
        _md_row(["subject", "type", "pages", "blocks", "chars", "fields", "cost", "time"]),
        "|---|---|--:|--:|--:|--:|--:|--:|",
    ]
    for s in subs:
        f = s["facts"]
        out.append(
            _md_row(
                [
                    s["label"],
                    str(s["backend"].get("type") or "-"),
                    str(f["pages"]),
                    str(f["blocks"]),
                    str(f["chars"]),
                    str(f["fields"]),
                    _fmt_cost(f["cost_usd"]),
                    _fmt_ms(f["duration_ms"]),
                ]
            )
        )
    findings = report.get("findings", [])
    out += ["", f"## Findings ({len(findings)})", ""]
    if not findings:
        out.append("_No differences on any compared dimension._")
    else:
        out += [
            _md_row(["severity", "code", "where", "subjects", "detail"]),
            "|---|---|---|---|---|",
        ]
        for fnd in findings:
            where = fnd.get("field") or (f"p{fnd['page']}" if fnd.get("page") is not None else "")
            snip = f' — "{fnd["snippet"]}"' if fnd.get("snippet") else ""
            out.append(
                _md_row(
                    [
                        fnd["severity"],
                        fnd["code"],
                        where,
                        ", ".join(fnd["subjects"]),
                        f"{fnd['detail']}{snip}",
                    ]
                )
            )
    return "\n".join(out)


def _kinds_phrase(lines: list[str]) -> str:
    """The line-kind character of a delta side ('mostly readable text' vs 'mostly filler') — turns
    a mute count into a judgment. Deterministic, no LLM (characterize.py)."""
    from openreading.comparison.characterize import characterize, phrase

    return phrase(characterize(lines))


def _clip(s: str, n: int = 140) -> str:
    """Collapse whitespace (tabs → spaces) and truncate — one readable line, never a wall."""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render_diffs(
    subjects: list[Any],
    report: dict[str, Any],
    *,
    baseline_label: str | None = None,  # reserved; the view is symmetric per-subject
    limit: int = 30,
) -> str:
    """`--format diffs`: the at-a-glance "what actually differs, and does it matter" view. Four
    sections, separating CONTENT (did anyone miss real text/values — the alarming axis) from
    STRUCTURE (how the same content is packaged — tables, types, granularity — the expected axis):

    ① CONTENT     — token-coverage delta over the text channel; leads with an equivalence verdict,
                    so packaging/reordering never reads as a content miss (deltas.py).
    ② TABLES      — each table's grid shape per subject + who flattened it (structure.py).
    ③ TYPES       — blocks the subjects label differently (report type_conflict findings).
    ④ GRANULARITY — block counts (who over-fragments).

    Field deltas follow when typed fields disagree."""
    from openreading.comparison.deltas import content_deltas, content_overlap
    from openreading.comparison.structure import table_deltas

    facts = {s["label"]: s["facts"] for s in report["subjects"]}
    npages = max((f.get("pages") or 0 for f in facts.values()), default=0)
    sep = " vs " if len(subjects) == 2 else " · "
    lines: list[str] = [f"DIFF — {sep.join(s.label for s in subjects)}   ({npages} page(s))"]

    # ① CONTENT — the packaging-immune "did anyone miss real text/values?" axis
    deltas = content_deltas(subjects)
    overlap = content_overlap(subjects)
    has_delta = any(d["unique"] or d["missed"] for d in deltas.values())
    lines += ["", "① CONTENT — real text/values either side missed"]
    if not has_delta:
        lines.append(f"   ✔ EQUIVALENT   content shared by all: {overlap:.2f}  ·  0 real misses")
    else:
        lines.append(f"   ✗ DIVERGENT   content shared by all: {overlap:.2f}")
        for s in subjects:
            d = deltas[s.label]
            if not (d["unique"] or d["missed"]):
                continue
            lines.append(f"   {s.label}:")
            for header, items, mark in (
                (
                    f"     MISSED — {len(d['missed'])} line(s) others have that {s.label} lacks:",
                    d["missed"],
                    "-",
                ),
                (
                    f"     ONLY {s.label} — {len(d['unique'])} line(s) no other backend captured"
                    + (f" — {_kinds_phrase(d['unique'])}" if d["unique"] else "")
                    + ":",
                    d["unique"],
                    "+",
                ),
            ):
                if not items:
                    continue
                lines.append(header)
                for ln in items[:limit]:
                    lines.append(f"        {mark} {_clip(ln)}")
                if len(items) > limit:
                    lines.append(f"        … {len(items) - limit} more")

    # ② TABLES — how the SAME cells were reconstructed (grid vs flattened)
    td = table_deltas(subjects)
    lines += ["", f"② TABLES — {len(td['tables'])} table(s)"]
    if len(set(td["counts"].values())) > 1:
        lines.append(f"   table counts differ: {td['counts']}")
    flat_any: set[str] = set()
    grid_any: set[str] = set()
    for t in td["tables"]:
        loc = f"p{t['page']} " if t.get("page") else ""
        shape_str = "  ".join(
            f"{lbl} {r}×{c}" + (" (flat)" if lbl in t["flat"] else "")
            for lbl, (r, c) in t["shapes"].items()
        )
        label = t["label"] or f"table {t['index']}"
        lines.append(f"   {loc}{label:<28} {shape_str}")
        flat_any |= t["flat"]
        grid_any |= {lbl for lbl, (r, _) in t["shapes"].items() if r >= 2}
    if flat_any:
        grids = ", ".join(sorted(grid_any - flat_any)) or "others"
        lines.append(
            f"   → reconstructed grids: {grids}   ·   "
            f"flattened to one row: {', '.join(sorted(flat_any))}"
        )
    if not td["tables"]:
        lines[-1] = "② TABLES — none detected"

    # ③ TYPES — same block, different semantic label
    tconf = [f for f in report.get("findings", []) if f["code"] == "type_conflict"]
    lines += ["", f"③ TYPES — {len(tconf)} block(s) labeled differently"]
    for f in tconf[:limit]:
        snip = f'"{_clip(f["snippet"], 40)}"  ' if f.get("snippet") else ""
        lines.append(f"   {snip}{f['detail']}")
    if len(tconf) > limit:
        lines.append(f"   … {len(tconf) - limit} more")
    if not tconf:
        lines[-1] = "③ TYPES — none differ"

    # ④ GRANULARITY — who over-fragments
    blocks = {lbl: (f.get("blocks") or 0) for lbl, f in facts.items()}
    lines += ["", "④ GRANULARITY"]
    if any(blocks.values()):
        lines.append("   " + "  ·  ".join(f"{lbl} {n} blocks" for lbl, n in blocks.items()))
        lo = min(v for v in blocks.values() if v)
        hi_lbl = max(blocks, key=lambda k: blocks[k])
        if lo and blocks[hi_lbl] >= 2 * lo:
            lines.append(
                f"   → {hi_lbl} is ~{blocks[hi_lbl] // lo}× more granular (often ≈1 block/cell)"
            )
    else:
        lines[-1] = "④ GRANULARITY — n/a (no block structure)"

    # FIELD DELTAS — typed-field disagreements (when present)
    conflicts = [
        r for r in report["fields"]["rows"] if r["verdict"] in ("disagree", "partial", "unique")
    ]
    if conflicts:
        lines += ["", "FIELD DELTAS"]
        for r in conflicts:
            vals = ", ".join(
                f"{lbl}={bs['value']}" for lbl, bs in r["by_subject"].items() if bs["present"]
            )
            lines.append(f"   {r['key']} [{r['verdict']}]  {vals}")
    return "\n".join(lines)
