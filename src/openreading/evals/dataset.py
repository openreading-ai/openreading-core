"""Eval dataset layout. A dataset is a directory of case subdirectories, each holding a single
`case.json`:

    {
      "name": "loan_page1",
      "input": {"builtin_sample": true, "pages": [1]},   // or "path", "bytes_base64", or "url"
      "backend": {"operation": "..."},                    // optional per-case backend overrides
      "compliance": {"require_local": true},              // optional per-case compliance (BL-112)
      "expected": { "text_contains": [...], "tables": [[...]], "typed_fields": {...} }
    }

`input` takes exactly one of four forms. `builtin_sample` resolves to the generated 2-page test
PDF (openreading.testing.sample_pdf), so a committed dataset needs no binary. `path` names a file
beside `case.json`, which is what real datasets ship. `bytes_base64` inlines the document, and
`url` passes a URL straight through to the backend.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvalCase:
    name: str
    request_body: dict[str, Any]
    expected: dict[str, Any]
    source: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _resolve_document(input_spec: dict, case_dir: Path) -> dict:
    if input_spec.get("builtin_sample"):
        from openreading.testing.sample_pdf import build_sample_pdf

        return {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
            "filename": "sample.pdf",
        }
    if "bytes_base64" in input_spec:
        return {
            "bytes_base64": input_spec["bytes_base64"],
            "mime_type": input_spec.get("mime_type", "application/pdf"),
        }
    if "path" in input_spec:
        p = (case_dir / input_spec["path"]).resolve()
        if not p.is_relative_to(case_dir.resolve()):
            raise ValueError(f"case input path escapes case_dir: {input_spec['path']!r}")
        return {
            "bytes_base64": base64.b64encode(p.read_bytes()).decode(),
            "mime_type": input_spec.get("mime_type", "application/pdf"),
            "filename": p.name,
        }
    if "url" in input_spec:
        return {
            "url": input_spec["url"],
            "mime_type": input_spec.get("mime_type", "application/pdf"),
        }
    raise ValueError(
        f"case input must set builtin_sample | bytes_base64 | path | url: {input_spec!r}"
    )


def load_case(case_json: Path, *, backend_id: str) -> EvalCase:
    spec = json.loads(case_json.read_text())
    case_dir = case_json.parent
    document = _resolve_document(spec.get("input", {}), case_dir)
    body: dict[str, Any] = {
        "document": document,
        "backend": {"id": backend_id, **spec.get("backend", {})},
    }
    if "pages" in spec.get("input", {}):
        body["pages"] = {"ranges": [{"start": p, "end": p} for p in spec["input"]["pages"]]}
    if "outputs" in spec:
        body["outputs"] = spec["outputs"]
    if "extraction_schema" in spec:
        body["extraction_schema"] = spec["extraction_schema"]
    if "compliance" in spec:
        # BL-112: forward a per-case compliance requirement into request_body so
        # calibrate_strategy's per-case Router.check_eligible gate has a real request-level
        # channel to see (previously always None — evals/dataset.py never forwarded this key).
        body["compliance"] = spec["compliance"]
    return EvalCase(
        name=spec.get("name", case_dir.name),
        request_body=body,
        expected=spec.get("expected", {}),
        source=case_json,
        meta=spec.get("meta", {}),
    )


def load_dataset(dataset_dir: str | Path, *, backend_id: str) -> list[EvalCase]:
    root = Path(dataset_dir)
    cases = [load_case(cj, backend_id=backend_id) for cj in sorted(root.glob("*/case.json"))]
    if not cases:
        raise FileNotFoundError(f"no */case.json cases under {root}")
    return cases
