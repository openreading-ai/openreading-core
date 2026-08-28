"""H8 — the end-to-end offline compare smoke (joins `make verify`).

Parse the bundled sample PDF through pymupdf live, then compare it against a second real backend:
tesseract live when the system binary is present (a genuine cross-backend delta), else a small
deterministic fixture envelope so the smoke is green on any machine. Assert the report is
schema-valid, is pairwise, and surfaces at least one finding (the two backends really do differ).
Fully offline (local backends), zero network — safe inside the `verify` gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def _fixture_envelope() -> dict[str, Any]:
    """A deterministic stand-in second subject when the tesseract binary is absent — a markdown-only
    transcription that deliberately differs from pymupdf's block output."""
    return {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "fixture-ocr", "type": "oss_library"},
        "document": {"text": "OpenReading Test Document (a deliberately different transcription)."},
    }


def main() -> int:
    from openreading import api, compare, schemas
    from openreading.testing.sample_pdf import build_sample_pdf
    from openreading.testing.tesseract_probe import tesseract_ocr_works

    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "sample.pdf"
        pdf.write_bytes(build_sample_pdf())
        a = api.run(str(pdf), backend="pymupdf")
        if tesseract_ocr_works():
            b = api.run(str(pdf), backend="tesseract")
            second = "tesseract (live)"
        else:
            b = _fixture_envelope()
            second = "fixture (no tesseract binary)"

    report = compare([a, b])
    schemas.validate_comparison_report(report)
    assert report["mode"] == "pairwise", report["mode"]
    findings = report["findings"]
    assert findings, "pymupdf and the second backend produced no differences at all"

    sim = report["text"]["matrix"][0][1]
    codes = sorted({f["code"] for f in findings})
    print(
        f"compare-smoke: OK — pymupdf × {second}, schema-valid report, "
        f"{len(findings)} findings ({', '.join(codes)}), text similarity {sim:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
