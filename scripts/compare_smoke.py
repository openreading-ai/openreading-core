"""The end-to-end offline compare smoke, which `make verify` runs (H8).

This parses the bundled sample PDF through pymupdf, then compares that result against a second
real backend. The second backend is tesseract when the system binary is present, which gives a
genuine cross-backend delta. Otherwise it is a small deterministic fixture envelope, so the smoke
stays green on any machine. It asserts that the report is schema-valid, that it is pairwise, and
that it carries at least one finding, because the two backends really do differ. It uses local
backends only and makes no network call, which is what lets it sit inside the `verify` gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def _fixture_envelope() -> dict[str, Any]:
    """A deterministic stand-in second subject for when the tesseract binary is absent. It is a
    markdown-only transcription that deliberately differs from pymupdf's block output."""
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
        f"compare-smoke: OK. pymupdf vs {second}, schema-valid report, "
        f"{len(findings)} findings ({', '.join(codes)}), text similarity {sim:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
