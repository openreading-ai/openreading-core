"""Cap a publisher corpus to a few documents, before anything bills.

A public benchmark corpus is large on purpose. ParseBench publishes 2,078 pages and ExtractBench
4,869, and a hosted backend charges for every one of them. Running the whole corpus is the last
thing you do, not the first. This module makes the first thing cheap: pick a handful of documents,
write them out as a corpus in the publisher's own on-disk format, and hand that to the publisher's
own runner. The scoring you get is the publisher's, unchanged, over fewer documents.

The two formats this reads are the publisher's, not ours, and both are read rather than assumed:

- **JSONL** (ParseBench). ``<root>/{category}.jsonl`` holds one rule per line, and a line's ``pdf``
  key is a path relative to the corpus root. Rows group by ``(category, pdf)``, so one document is
  one inference no matter how many rules assert against it. An optional
  ``<root>/expected_markdown.json`` maps the same relative paths to expected Markdown.
- **Sidecar** (ExtractBench). ``<root>/<group>/<stem>.<ext>`` beside ``<stem>.test.json``, which
  carries that document's schema and expected values.

Selection is round-robin across categories rather than the first N rows. A two-document run drawn
entirely from ``chart.jsonl`` tells you nothing about tables, and the whole point of a small run is
breadth on the way to a decision. Within a category, order is the sorted document id, so the same
``--limit`` picks the same documents every time and a rerun resumes rather than re-bills.

Nothing here downloads, scores, or calls a backend. It reads one directory and writes another.
`openreading.evals.preflight` prices the result, and `openreading.evals.official` runs it.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CorpusLayout = Literal["jsonl", "sidecar"]

# Mirrors parse_bench.test_cases.loader.SUPPORTED_EXTENSIONS. A sidecar corpus is not all PDFs.
_SOURCE_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".jfif", ".tiff", ".tif", ".webp"})
_EXPECTED_MARKDOWN = "expected_markdown.json"


class CorpusError(ValueError):
    """Raised when a prepared directory holds no documents this module can read."""


@dataclass(frozen=True)
class BenchmarkDocument:
    """One document of a publisher corpus, and where its files live."""

    doc_id: str
    group: str
    path: Path
    relative: str


@dataclass(frozen=True)
class SubsetPlan:
    """The documents a run will actually touch, and how many it left behind."""

    data_dir: Path
    layout: CorpusLayout
    documents: tuple[BenchmarkDocument, ...]
    total_available: int

    @property
    def is_complete(self) -> bool:
        """True when nothing was left behind, so the run covers the whole prepared corpus."""

        return len(self.documents) == self.total_available


def _jsonl_documents(data_dir: Path) -> list[BenchmarkDocument]:
    seen: dict[str, BenchmarkDocument] = {}
    for jsonl in sorted(data_dir.glob("*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            relative = row.get("pdf")
            if not isinstance(relative, str) or not relative:
                continue
            group = row.get("category") or jsonl.stem
            # The loader keys a case on (category, pdf) and names it "<group>/<stem>", so that is
            # the id a person reading a publisher report already sees.
            doc_id = f"{group}/{Path(relative).stem}"
            seen.setdefault(
                doc_id,
                BenchmarkDocument(
                    doc_id=doc_id,
                    group=str(group),
                    path=(data_dir / relative),
                    relative=relative,
                ),
            )
    return list(seen.values())


def _sidecar_documents(data_dir: Path) -> list[BenchmarkDocument]:
    documents: list[BenchmarkDocument] = []
    for group_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if group_dir.name.startswith(".") or group_dir.name.startswith("_"):
            continue
        for source in sorted(group_dir.iterdir()):
            if not source.is_file() or source.name.endswith(".test.json"):
                continue
            if source.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            documents.append(
                BenchmarkDocument(
                    doc_id=f"{group_dir.name}/{source.stem}",
                    group=group_dir.name,
                    path=source,
                    relative=f"{group_dir.name}/{source.name}",
                )
            )
    return documents


def discover_documents(data_dir: str | Path) -> tuple[CorpusLayout, tuple[BenchmarkDocument, ...]]:
    """Read a prepared corpus and return its layout plus every document in it, id-sorted."""

    root = Path(data_dir)
    if not root.is_dir():
        raise CorpusError(f"prepared corpus {root} is not a directory")
    # The publisher's own loader prefers the sidecar reader when both shapes are present, so this
    # detection has to agree with it or a subset would be read back differently than it was built.
    has_sidecar = any(root.rglob("*.test.json"))
    if list(root.glob("*.jsonl")) and not has_sidecar:
        layout: CorpusLayout = "jsonl"
        documents = _jsonl_documents(root)
    else:
        layout = "sidecar"
        documents = _sidecar_documents(root)
    if not documents:
        raise CorpusError(
            f"no benchmark documents found under {root}; run `openreading benchmark prepare` first"
        )
    return layout, tuple(sorted(documents, key=lambda doc: doc.doc_id))


def select_documents(
    documents: tuple[BenchmarkDocument, ...],
    *,
    limit: int,
    names: tuple[str, ...] = (),
) -> tuple[BenchmarkDocument, ...]:
    """Pick the documents to run: named ones if given, else `limit` spread across categories.

    `limit` of 0 means every document. Naming a document that the corpus does not hold is an error
    rather than a silent skip, because a typo would otherwise quietly shrink a paid run.
    """

    if names:
        by_id = {doc.doc_id: doc for doc in documents}
        by_stem: dict[str, list[BenchmarkDocument]] = {}
        for doc in documents:
            by_stem.setdefault(Path(doc.relative).stem, []).append(doc)
        chosen: list[BenchmarkDocument] = []
        for name in names:
            if name in by_id:
                chosen.append(by_id[name])
                continue
            matches = by_stem.get(name, [])
            if not matches:
                available = ", ".join(sorted(by_id)[:8])
                raise CorpusError(f"no benchmark document named {name!r}; try one of: {available}")
            chosen.extend(matches)
        # De-duplicate while keeping the order the reader typed, so the report reads back the same.
        unique: list[BenchmarkDocument] = []
        for doc in chosen:
            if doc not in unique:
                unique.append(doc)
        return tuple(unique)

    if limit < 0:
        raise ValueError("--limit cannot be negative")
    if limit == 0 or limit >= len(documents):
        return documents

    by_group: dict[str, list[BenchmarkDocument]] = {}
    for doc in documents:
        by_group.setdefault(doc.group, []).append(doc)
    picked: list[BenchmarkDocument] = []
    round_index = 0
    # Round-robin, so `--limit 2` on a four-category corpus spans two categories rather than
    # taking two charts and telling you nothing about tables.
    while len(picked) < limit:
        added = False
        for group in sorted(by_group):
            if len(picked) == limit:
                break
            group_docs = by_group[group]
            if round_index < len(group_docs):
                picked.append(group_docs[round_index])
                added = True
        if not added:  # pragma: no cover - guarded by `limit >= len(documents)` above
            break
        round_index += 1
    return tuple(sorted(picked, key=lambda doc: doc.doc_id))


def plan_subset(data_dir: str | Path, *, limit: int, names: tuple[str, ...] = ()) -> SubsetPlan:
    """Resolve a prepared corpus into the documents one run will touch."""

    root = Path(data_dir)
    layout, documents = discover_documents(root)
    return SubsetPlan(
        data_dir=root,
        layout=layout,
        documents=select_documents(documents, limit=limit, names=names),
        total_available=len(documents),
    )


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination)


def _materialize_jsonl(plan: SubsetPlan, destination: Path) -> None:
    keep = {doc.relative for doc in plan.documents}
    for jsonl in sorted(plan.data_dir.glob("*.jsonl")):
        rows = []
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("pdf") in keep:
                rows.append(line)
        # A category with nothing selected is dropped rather than written empty. The publisher
        # discovers its evaluation groups from the files present, and an empty one reports as a
        # category that scored nothing rather than a category this run did not ask for.
        if rows:
            (destination / jsonl.name).write_text("\n".join(rows) + "\n", encoding="utf-8")
    expected = plan.data_dir / _EXPECTED_MARKDOWN
    if expected.is_file():
        try:
            mapping: dict[str, Any] = json.loads(expected.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}
        trimmed = {key: value for key, value in mapping.items() if key in keep}
        if trimmed:
            (destination / _EXPECTED_MARKDOWN).write_text(
                json.dumps(trimmed, indent=2), encoding="utf-8"
            )
    for doc in plan.documents:
        _copy(doc.path, destination / doc.relative)


def _materialize_sidecar(plan: SubsetPlan, destination: Path) -> None:
    for doc in plan.documents:
        _copy(doc.path, destination / doc.relative)
        # Every sibling sharing the stem travels with it: `.test.json` carries the schema and the
        # expected values, and a corpus without it scores nothing.
        for sibling in sorted(doc.path.parent.glob(f"{doc.path.stem}.*")):
            if sibling != doc.path:
                _copy(sibling, destination / doc.group / sibling.name)


def materialize_subset(plan: SubsetPlan, destination: str | Path) -> Path:
    """Write the planned documents to `destination` in the publisher's own corpus format.

    The result is a corpus the publisher's runner reads exactly as it reads the full one, so no
    metric, report, or leaderboard behaves differently for having fewer documents in front of it.
    """

    target = Path(destination)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if plan.layout == "jsonl":
        _materialize_jsonl(plan, target)
    else:
        _materialize_sidecar(plan, target)
    return target
