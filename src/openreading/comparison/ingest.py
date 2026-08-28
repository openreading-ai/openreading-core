"""Subject ingestion (DESIGN §3). Turn N inputs — response dicts or paths to response JSON — into
labeled `Subject`s, validated against the response schema. Pure: no execution, no network."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openreading.schemas import validate_response


class CompareInputError(ValueError):
    """An input was not a schema-valid response, or fewer than two subjects were supplied. The CLI
    maps this to exit code 5 (DESIGN §8)."""


@dataclass
class Subject:
    """One thing being compared: a response envelope plus its provenance and display label."""

    label: str
    response: dict[str, Any]
    source: str = "file"  # file | fanout | candidate
    # populated lazily by properties below
    _tf: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def backend_id(self) -> str:
        return str((self.response.get("backend") or {}).get("id") or "unknown")

    @property
    def backend_type(self) -> str | None:
        t = (self.response.get("backend") or {}).get("type")
        return str(t) if t is not None else None

    @property
    def output_paradigm(self) -> list[str] | None:
        op = (self.response.get("backend") or {}).get("output_paradigm")
        return list(op) if isinstance(op, list) else None

    @property
    def typed_fields(self) -> dict[str, Any]:
        return self.response.get("typed_fields") or {}


def _load_one(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if isinstance(item, str | Path):
        p = Path(item)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompareInputError(f"cannot read response JSON from {item!r}: {exc}") from exc
    raise CompareInputError(f"unsupported input {type(item).__name__}; expected dict or path")


def _label_for(resp: dict[str, Any], item: Any) -> str:
    bid = str((resp.get("backend") or {}).get("id") or "").strip()
    if bid:
        return bid
    if isinstance(item, str | Path):
        return Path(item).stem
    return "subject"


def load_subjects(inputs: Iterable[Any], *, sources: list[str] | None = None) -> list[Subject]:
    """Validate and label N inputs into Subjects. `sources` (optional, from the CLI which knows the
    acquisition mode) stamps each subject's provenance; defaults to 'file'. Labels are the backend
    id, disambiguated with #2/#3… on collision in input order."""
    items = list(inputs)
    if len(items) < 2:
        raise CompareInputError("compare needs at least two subjects")

    subjects: list[Subject] = []
    seen: dict[str, int] = {}
    used: set[str] = set()
    for i, item in enumerate(items):
        resp = _load_one(item)
        try:
            validate_response(resp)
        except Exception as exc:  # noqa: BLE001 — normalize every schema failure to one input error
            raise CompareInputError(
                f"input #{i + 1} is not a schema-valid response: {exc}"
            ) from exc
        base = _label_for(resp, item)
        seen[base] = seen.get(base, 0) + 1
        if seen[base] == 1:
            label = base
        else:
            # §9 hazard: aggregators (open-ocr, ~100 engines behind one id) hide the engine in
            # backend.version — key colliding subjects on id+version so the engines stay
            # distinguishable. The (ver)-derived label is computed from `ver` alone, so a THIRD
            # (or later) subject sharing the identical (base, ver) pair as an earlier one would
            # otherwise recompute the byte-identical label — check every label already handed out
            # (`used`), not only whether ver itself is absent, and fall back to the #N counter
            # form on any collision, not only a missing version.
            ver = str((resp.get("backend") or {}).get("version") or "").strip()
            candidate = f"{base} ({ver})" if ver else None
            label = candidate if candidate and candidate not in used else f"{base}#{seen[base]}"
        used.add(label)
        src = sources[i] if sources and i < len(sources) else "file"
        subjects.append(Subject(label=label, response=resp, source=src))
    return subjects
