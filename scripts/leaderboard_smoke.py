"""BL-160 — the end-to-end offline leaderboard smoke (joins `make verify`).

Run the REAL `openreading leaderboard` CLI command (through `openreading.cli.main`, the same
entry point a shell invocation reaches) over the one dataset this repo ships
(`src/openreading/evals/sample`, the deterministic built-in sample PDF — no binary fixture
needed) across two local, no-network, no-key backends: pymupdf and tesseract. Assert the printed
`--format json` output is a schema-valid BenchmarkReport with both backends ranked and a non-empty
per-case table.

Deliberately does NOT branch on whether the `tesseract` system binary happens to be installed
(unlike compare-smoke/strategy-smoke, which must): `run_leaderboard` reuses `evals.runner.run_case`
unchanged, which turns a missing-binary failure into that backend's own scored, error-carrying
case (never a crash) — so this smoke is green either way, and a genuinely missing tesseract binary
just means tesseract's row shows real errors instead of real scores, which is itself the harness
working correctly, not a reason to special-case the assertion.
"""

from __future__ import annotations

import contextlib
import io
import json


def main() -> int:
    from openreading import schemas
    from openreading.cli import main as cli_main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli_main(
            [
                "leaderboard",
                "src/openreading/evals/sample",
                "--backends",
                "pymupdf,tesseract",
                "--format",
                "json",
            ]
        )
    assert rc == 0, f"leaderboard exited {rc}, stdout={out.getvalue()!r}"

    report = json.loads(out.getvalue())
    schemas.validate_leaderboard_report(report)

    ranked_ids = {b["backend_id"] for b in report["backends"]}
    assert ranked_ids == {"pymupdf", "tesseract"}, ranked_ids
    assert report["cases"], "leaderboard produced an empty per-case table"
    assert report["dataset"]["case_count"] == len(report["cases"])

    best = report["backends"][0]
    print(
        f"leaderboard-smoke: OK — {len(report['backends'])} backend(s) ranked over "
        f"{report['dataset']['case_count']} case(s); best={best['backend_id']} "
        f"mean={best['mean_score']:.3f} cost/doc={best['cost_per_doc']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
