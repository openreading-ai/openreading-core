"""Corpus compare: compare two or more batch-result runs document by document.

Documents pair across runs by identity precedence relpath → filename → sha256; each paired document
reuses the existing comparison-report v0.2 (build_report), unchanged; a document present in some
runs but not all is `unpaired`, not a crash. Renders a per-document verdict table and, for `diffs`,
a value-first view: under each divergent doc, the actual content each subject captured that the
other(s) missed (capped) — not counts, not structure. Structure (table shapes, types, granularity)
lives in `--format table` and the single-pair drill; a corpus-wide four-section diff is a wall."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal, cast

from openreading.comparison.ingest import Subject
from openreading.comparison.report import build_report

Verdict = Literal["equivalent", "divergent", "mixed", "unpaired"]


def is_batch_envelope(obj: Any) -> bool:
    """A batch-result envelope (vs a single response.v0.3): it carries an `items` list and a
    `summary` object."""
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("items"), list)
        and isinstance(obj.get("summary"), dict)
    )


def _doc_key(source: dict) -> str:
    return source.get("relpath") or source.get("filename") or source.get("sha256") or ""


@dataclass
class CorpusPair:
    key: str
    source: dict[str, Any]  # {relpath, filename, sha256}
    responses: dict[str, dict | None]  # run label -> response envelope (None if not comparable)
    report: dict | None  # comparison-report v0.2 (paired docs only)
    verdict: Verdict  # equivalent | divergent | mixed | unpaired


def _index(batch: dict) -> dict[str, dict]:
    """key → succeeded item (only succeeded items carry a comparable response)."""
    out: dict[str, dict] = {}
    for item in batch.get("items") or []:
        if item.get("state") == "succeeded" and item.get("response"):
            key = _doc_key(item.get("source") or {})
            if key:
                out[key] = item
    return out


def corpus_pairs(batches: list[dict], labels: list[str]) -> list[CorpusPair]:
    """Pair documents across `batches` (aligned with `labels`) by identity. Paired docs (present as
    a succeeded item in EVERY run) get a comparison-report v0.2 + its headline verdict; the rest are
    `unpaired`. Deterministic order: sorted by identity key."""
    indexes = [_index(b) for b in batches]
    keys = sorted({k for idx in indexes for k in idx})
    pairs: list[CorpusPair] = []
    for key in keys:
        present = [idx.get(key) for idx in indexes]
        responses: dict[str, dict | None] = {}
        for i, lbl in enumerate(labels):
            item = present[i]
            responses[lbl] = item["response"] if item is not None else None
        anchor = next((p for p in present if p is not None), None)
        src = (anchor or {}).get("source", {})
        source = {k: src.get(k) for k in ("relpath", "filename", "sha256")}
        report: dict | None
        verdict: Verdict
        if all(item is not None for item in present):
            subjects = []
            for i, item in enumerate(present):
                assert (
                    item is not None
                )  # guaranteed by all(...) above; narrows for the type checker
                subjects.append(Subject(label=labels[i], response=item["response"]))
            report = build_report(subjects)
            verdict = cast(Verdict, (report.get("headline") or {}).get("verdict") or "mixed")
        else:
            report, verdict = None, "unpaired"
        pairs.append(
            CorpusPair(key=key, source=source, responses=responses, report=report, verdict=verdict)
        )
    return pairs


def _finding_tally(pairs: list[CorpusPair]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for p in pairs:
        for f in (p.report or {}).get("findings") or []:
            code = f.get("code")
            if code:
                tally[code] = tally.get(code, 0) + 1
    return tally


def corpus_report_dict(
    pairs: list[CorpusPair],
    batches: list[dict],
    labels: list[str],
    sources: list[str] | None = None,
) -> dict:
    """Assemble the schema-valid corpus-report.v0.1 dict from paired documents."""
    from openreading.schemas import validate_corpus_report
    from openreading.types.batch import (
        CorpusDocument,
        CorpusDocumentSource,
        CorpusReport,
        CorpusRollup,
        CorpusSubject,
    )

    subjects = [
        CorpusSubject(
            label=labels[i],
            source=(sources[i] if sources and i < len(sources) else None),
            backend_tally=(batches[i].get("summary") or {}).get("backends") or {},
        )
        for i in range(len(labels))
    ]
    documents = [
        CorpusDocument(source=CorpusDocumentSource(**p.source), verdict=p.verdict, report=p.report)
        for p in pairs
    ]
    rollup = CorpusRollup(
        documents=len(pairs),
        equivalent=sum(1 for p in pairs if p.verdict == "equivalent"),
        divergent=sum(1 for p in pairs if p.verdict == "divergent"),
        mixed=sum(1 for p in pairs if p.verdict == "mixed"),
        unpaired=sum(1 for p in pairs if p.verdict == "unpaired"),
        by_finding_code=_finding_tally(pairs),
    )
    report = CorpusReport(subjects=subjects, documents=documents, rollup=rollup).to_schema_dict()
    validate_corpus_report(report)
    return report


def _loc(pair: CorpusPair) -> str:
    return (
        pair.source.get("relpath")
        or pair.source.get("filename")
        or pair.source.get("sha256")
        or "?"
    )


def _rollup_line(pairs: list[CorpusPair]) -> str:
    n = len(pairs)
    eq = sum(1 for p in pairs if p.verdict == "equivalent")
    dv = sum(1 for p in pairs if p.verdict == "divergent")
    mx = sum(1 for p in pairs if p.verdict == "mixed")
    up = sum(1 for p in pairs if p.verdict == "unpaired")
    return f"{n} document(s): {eq} equivalent · {dv} divergent · {mx} mixed · {up} unpaired"


def render_corpus_table(pairs: list[CorpusPair], labels: list[str]) -> str:
    """One verdict line per document + the rollup, and a finding tally across paired docs."""
    lines = [f"CORPUS COMPARE: {' vs '.join(labels)}", _rollup_line(pairs), ""]
    for p in pairs:
        lines.append(f"  [{p.verdict:>10}] {_loc(p)}")
    tally = _finding_tally(pairs)
    if tally:
        lines += ["", "FINDINGS (across paired documents)"]
        for code, n in sorted(tally.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {n:>4}  {code}")
    return "\n".join(lines)


_VERDICT_MARK = {"equivalent": "✔", "divergent": "✗", "mixed": "~", "unpaired": "?"}


def _clip(text: str, width: int = 100) -> str:
    """One line, intra-line whitespace collapsed, clipped to width — a value never breaks layout."""
    s = " ".join(str(text).split())
    return s if len(s) <= width else s[: width - 1] + "…"


def _doc_values(
    pair: CorpusPair, labels: list[str], limit: int
) -> tuple[float, list[str], dict[str, Counter]]:
    """The value diff for one divergent document: the actual content lines each subject captured that
    the other(s) missed (`content_deltas.unique`, token-coverage matched so packaging never fakes a
    delta), capped at ``limit`` per side, each side's header annotated with its line-kind character
    ("mostly readable text" vs "mostly filler"). Returns (content-overlap, rendered lines, per-side
    kind counts — the corpus story's raw material). No structure."""
    from openreading.comparison.characterize import characterize, phrase
    from openreading.comparison.deltas import content_deltas, content_overlap

    resp: dict[str, dict] = {lbl: r for lbl in labels if (r := pair.responses.get(lbl)) is not None}
    present = list(resp)
    subjects = [Subject(label=lbl, response=resp[lbl]) for lbl in present]
    ov = content_overlap(subjects)
    dl = content_deltas(subjects)

    out: list[str] = []
    kinds: dict[str, Counter] = {}
    for lbl in present:
        uniq = dl[lbl]["unique"]
        if not uniq:
            continue
        kinds[lbl] = characterize(uniq)
        out.append(f"      only {lbl} captured ({len(uniq)}) — {phrase(kinds[lbl])}:")
        out += [f"        + {_clip(ln)}" for ln in uniq[:limit]]
        if len(uniq) > limit:
            out.append(f"        … {len(uniq) - limit} more")
    if not out:  # divergent, yet identical text → the difference is purely structural
        out.append(
            "      (same text on both sides — differs only in structure; see --format table)"
        )
    return ov, out, kinds


def _summary_table(rows: list[tuple], labels: list[str]) -> list[str]:
    """The scannable per-document summary: verdict, shared score, and each side's unique-line count
    with its character — the whole corpus at a glance before the line-level details. Rows are
    (mark, loc, verdict, ov | None, kinds_by_label)."""
    from openreading.comparison.characterize import phrase

    def cell(kinds: dict[str, Counter], lbl: str) -> str:
        c = kinds.get(lbl)
        return f"{sum(c.values())} · {phrase(c)}" if c else "—"

    w = 34  # per-side column width
    out = [
        "  "
        + f"{'document':<31}{'shared':>6}   "
        + "".join(f"{('only ' + lbl):<{w}}" for lbl in labels).rstrip()
    ]
    for mark, loc, verdict, ov, kinds in rows:
        shared = f"{ov:.2f}" if ov is not None else "—"
        if verdict == "divergent":
            sides = "".join(f"{_clip(cell(kinds, lbl), w - 2):<{w}}" for lbl in labels).rstrip()
        else:
            sides = verdict
        out.append(f"  {mark} {loc:<29}{shared:>6}   {sides}")
    return out


def render_corpus_diffs(pairs: list[CorpusPair], labels: list[str], *, limit: int = 8) -> str:
    """Value-first, three layers: the story (corpus-level judgment), the per-document summary table
    (verdict · shared · each side's unique count + character), then under each DIVERGENT document
    the actual content each subject captured that the other(s) missed (capped at ``limit`` per
    side). No structure — that is `--format table` and the single-pair drill. The footer shows how
    to dump one document's full raw text when the capped sample isn't enough."""
    from openreading.comparison.characterize import corpus_story

    detail_lines: list[str] = []
    table_rows: list[tuple] = []
    totals: dict[str, Counter] = {lbl: Counter() for lbl in labels}
    for p in pairs:
        if p.verdict == "divergent" and p.report is not None:
            ov, block, kinds = _doc_values(p, labels, limit)
            table_rows.append((_VERDICT_MARK["divergent"], _loc(p), "divergent", ov, kinds))
            detail_lines.append(
                f"  {_VERDICT_MARK['divergent']} {_loc(p):<28} divergent · {ov:.2f} shared"
            )
            detail_lines += block
            for lbl, c in kinds.items():
                totals[lbl] += c
        else:
            table_rows.append((_VERDICT_MARK.get(p.verdict, "·"), _loc(p), p.verdict, None, {}))

    lines = [
        f"CORPUS DIFFS: {' vs '.join(labels)}",
        "the values each captured that the other missed · content channel · packaging-immune",
        _rollup_line(pairs),
        "",
    ]
    story = corpus_story(totals)
    if story:
        first, *rest = story.replace(". ", ".\n").splitlines()
        lines += [f"the story: {first}", *rest, ""]
    lines += _summary_table(table_rows, labels)
    if detail_lines:
        lines += ["", "details (the lines behind the counts):"]
        lines += detail_lines
    lines += [
        "",
        f"showing ≤{limit} lines per side — dump one document's full text with:",
        "  jq -r '.items[]|select(.source.relpath==\"NAME\").response.document.text' RUN.json",
    ]
    return "\n".join(lines)
