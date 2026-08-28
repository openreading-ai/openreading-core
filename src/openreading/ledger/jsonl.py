"""`JsonlJournal` — the core `Journal` implementation: one JSONL file per run, one line per
`StepResult` (internal/design/ledger.md §6, §11). Append-only (L4): a second write under the same key
is a follow-on record, never a replacement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openreading.ledger.step import StepRef, StepResult


class JsonlJournal:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Ledger T3 (§4.1/§7.4): a per-run monotonic append counter, held on this instance — "one
        # instance per run today" (plan §4.1, `api.py`'s own construction). Seeded from whatever is
        # already on disk (a resume-mode journal opens the SAME file a fresh run already wrote to),
        # so a resumed process's own counter picks up where the original left off rather than
        # restarting at 0 and re-numbering already-journaled records.
        self._seq_by_run: dict[str | None, int] = {}
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = obj.get("run_id")
                    if rid is not None:
                        self._seq_by_run[rid] = self._seq_by_run.get(rid, 0) + 1

    def append(self, result: StepResult) -> StepResult:
        seq = self._seq_by_run.get(result.run_id, 0)
        self._seq_by_run[result.run_id] = seq + 1
        stamped = result.model_copy(update={"journal_seq": seq})
        line = stamped.model_dump_json(exclude_none=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return stamped

    def get(self, ref: StepRef) -> list[StepResult]:
        """Every record at `ref`, in append order. A truncated trailing line — the realistic
        signature of a crash mid-`append` (Phase C round-1 F3) — is skipped, not fatal: it must not
        cost every earlier, perfectly valid record in the same file its readability."""
        if not self._path.exists():
            return []
        out: list[StepResult] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    obj.get("run_id") == ref.run_id
                    and obj.get("step_path") == ref.step_path
                    and obj.get("step_seq") == ref.step_seq
                ):
                    out.append(StepResult.model_validate(obj))
        return out
