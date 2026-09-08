"""The end-to-end offline leaderboard smoke, which `make verify` runs (BL-160).

This runs the real `openreading leaderboard` CLI command through `openreading.cli.main`, the same
entry point a shell invocation reaches. It runs over the one dataset this repository ships, the
deterministic built-in sample PDF at `src/openreading/evals/sample`, so no binary fixture is
needed. Two local backends do the work, pymupdf and tesseract, with no network and no key. The
assertion is that the printed `--format json` output is a schema-valid BenchmarkReport, that both
backends are ranked, and that the per-case table is not empty.

This smoke deliberately does not branch on whether the `tesseract` system binary is installed,
unlike the compare and strategy smokes, which must. `run_leaderboard` reuses `evals.runner.run_case`
unchanged, and that turns a missing-binary failure into a scored, error-carrying case for that
backend rather than a crash. The smoke is therefore green either way. A genuinely missing binary
just means tesseract's row shows real errors instead of real scores, which is the harness working
correctly rather than a reason to special-case the assertion.
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
        f"leaderboard-smoke: OK. {len(report['backends'])} backend(s) ranked over "
        f"{report['dataset']['case_count']} case(s); best={best['backend_id']} "
        f"mean={best['mean_score']:.3f} errors={best['errors']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
