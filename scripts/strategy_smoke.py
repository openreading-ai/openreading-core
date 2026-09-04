"""The end-to-end offline strategy smoke that `make verify` runs (harness H7, §15 T7).

Generate the scanned fixture (image-only, no text) and run it through TWO spellings of the same
cascade: the advanced `local_ocr` (`steps: [pymupdf, tesseract]`, `escalate_if: default`) and its
Plain equivalent (`try: [pymupdf, tesseract]`, `escalate_when: looks_bad`). The scanned PDF trips
the local parse's quality gate in both, so each must produce a schema-valid orchestration-carrying
response with a fired first-rung gate — and the two must agree on the attempt/category/chosen
trail. The gate *predicates* legitimately differ, because the Plain `looks_bad` uses the
result-aware scan pair from internal/design/simple-strategies.md §5 and omits the bundle's
`confidence_below`. The *behavior* must not differ.

- if the system `tesseract` binary is present, rung 2 OCRs the scan → a real, live, local
  escalation (zero fixtures, zero network);
- if not, keep-best returns the degraded pymupdf result (`quality_below_threshold`).

Both paths are green on any machine — and Plain vs advanced agree either way.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_CONFIG = (
    "version: 1\n"
    "strategies:\n"
    "  local_ocr:\n    steps: [pymupdf, tesseract]\n    escalate_if: default\n"
    "  local_ocr_plain:\n    try: [pymupdf, tesseract]\n    escalate_when: looks_bad\n"
)


def main() -> int:
    from openreading import api, schemas
    from openreading.testing.sample_pdf import build_scanned_pdf

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "scan.pdf").write_bytes(build_scanned_pdf())
        (tmp / "openreading.yaml").write_text(_CONFIG)
        os.chdir(tmp)  # discovery finds ./openreading.yaml
        doc = str(tmp / "scan.pdf")

        def run_and_trail(name: str):
            result = api.run(doc, strategy=name)
            schemas.validate_response(result)  # schema-valid, orchestration allowed
            orch = result.get("orchestration")
            assert orch, f"{name}: strategy run produced no orchestration block"
            attempts = orch["attempts"]
            first = attempts[0]
            fired = [g["predicate"] for g in first.get("gates", []) if g.get("fired")]
            assert first["backend"] == "pymupdf", (
                f"{name}: expected pymupdf first, got {first['backend']}"
            )
            assert fired, (
                f"{name}: expected a quality gate to fire on the scan; gates={first.get('gates')}"
            )
            trail = [(a["backend"], a["category"]) for a in attempts]
            return trail, orch["chosen_backend"], orch["outcome"], fired

        adv_trail, adv_chosen, adv_outcome, adv_fired = run_and_trail("local_ocr")
        pln_trail, pln_chosen, pln_outcome, pln_fired = run_and_trail("local_ocr_plain")

    # T7: the two dialects must agree on behavior, even though their gate predicates differ.
    assert adv_trail == pln_trail, f"attempt trails differ: advanced={adv_trail} plain={pln_trail}"
    assert adv_chosen == pln_chosen, f"chosen backend differs: {adv_chosen} vs {pln_chosen}"
    assert adv_outcome == pln_outcome, f"outcome differs: {adv_outcome} vs {pln_outcome}"

    escalated = adv_outcome == "ok" and adv_chosen == "tesseract"
    print(
        f"strategy-smoke: OK. Advanced and Plain agree: pymupdf gated, chosen={adv_chosen} "
        f"outcome={adv_outcome} ({'tesseract escalation' if escalated else 'keep-best degraded'})"
    )
    print(json.dumps({"trail": adv_trail, "advanced_fired": adv_fired, "plain_fired": pln_fired}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
