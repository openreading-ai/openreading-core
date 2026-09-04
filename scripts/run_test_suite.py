"""Run the offline pytest suite with the coverage floor, retrying once — and only once — when
pytest-cov's own `combine()` step hits a stray, schema-incompatible coverage data file written
*mid-run* by a second, genuinely concurrent `pytest --cov` invocation on this same shared checkout
(BL-69).

This script clears any stale `.coverage(.*)` file already at rest before it starts (BL-61), so that
sweep can only ever guard the before-the-run case. pytest-cov's own `Central.finish()`
(`pytest_cov/engine.py`) calls `Coverage.combine()` unconditionally at the end of every `--cov`
run, after every real test has already run; if a sibling `.coverage.*` file uses the opposite
schema from this repo's own (`pyproject.toml`'s `[tool.coverage.run]` sets `branch = true`
repo-wide, so anything statement-mode found on disk is foreign), `coverage`'s own
`CoverageData.update()` (`coverage/sqldata.py`) raises `DataError` — "Can't combine branch coverage
data with statement data" or "Can't combine statement coverage data with branch data", both raised
with `slug="cant-combine"` — from a call site inside `combine_parallel_data()`
(`coverage/data.py`) that only guards the sibling file's own `.read()`, not this `.update()` call.
Uncaught, it propagates all the way out through pytest's own `pytest_runtestloop` hookwrapper as
pytest's own INTERNALERROR (exit code 3) — independent of whether every real test just passed.

This runs the real invocation once, streaming its output live exactly as a bare `pytest` call
would. Only when it exits 3 with that exact signature in its own output does this sweep the stray
data file(s) again and retry the whole invocation once. Any other failure — a red test (exit 1), a
`--cov-fail-under` breach (also exit 1: `pytest_cov/plugin.py`'s own `session.testsfailed += 1`
path, never exit 3), or an INTERNALERROR that is not this specific DataError — is never retried:
this script exits with pytest's own real exit code, unaltered, so a genuine failure is exactly as
loud as it was before BL-69.

This script takes no opinion on *which* pytest args to run — the Makefile passes its full, real
argument list (`-m "not live" --cov=openreading ... --cov-fail-under=91`) through unchanged, so
the coverage floor keeps exactly one source of truth for `tests/test_docs_freshness.py` to check
(`Makefile`'s own `--cov-fail-under=NN`), not a second, driftable copy buried in this file.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO

# pytest's own internal-error exit status (`_pytest/config/__init__.py`'s `ExitCode.INTERNAL_ERROR`).
_INTERNAL_ERROR_EXIT_CODE = 3

# The two exact `coverage` DataError messages this script guards (`coverage/sqldata.py`'s
# `CoverageData.update()`) — a schema-incompatible sibling data file, never a real assertion
# failure, a --cov-fail-under breach, or a genuine coverage-file corruption (e.g. a "Conflicting
# file tracer name" DataError, which this must NOT swallow).
_COMBINE_SCHEMA_MISMATCH_SIGNATURES = (
    "Can't combine branch coverage data with statement data",
    "Can't combine statement coverage data with branch data",
)


def clear_stray_coverage_data_files() -> None:
    """Remove any coverage data file at rest, CWD-relative (BL-61)."""
    for path in (Path(".coverage"), *Path().glob(".coverage.*")):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def is_stray_combine_failure(returncode: int, combined_output: str) -> bool:
    """True only for pytest-cov's combine()-time INTERNALERROR on an incompatible sibling file.

    Deliberately narrow — an internal error for any other reason, or a normal red test / coverage-
    floor breach (both exit 1, never 3), must never read as retryable (BL-69's own acceptance
    criteria: "must not swallow any other DataError variant or mask a genuine coverage-file
    corruption").
    """
    if returncode != _INTERNAL_ERROR_EXIT_CODE:
        return False
    if "INTERNALERROR" not in combined_output:
        return False
    return any(sig in combined_output for sig in _COMBINE_SCHEMA_MISMATCH_SIGNATURES)


def _pump(pipe: IO[str], dest: IO[str], sink: list[str]) -> None:
    """Forward each line from `pipe` to `dest` live, while also collecting it into `sink`."""
    for line in pipe:
        dest.write(line)
        dest.flush()
        sink.append(line)


def run_pytest_once(pytest_args: Sequence[str]) -> tuple[int, str]:
    """Run the real pytest invocation, streaming stdout/stderr live exactly as a bare `pytest`
    call would, while also capturing both (interleaved) for signature detection."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *pytest_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    combined: list[str] = []
    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, combined)),
        threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, combined)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    proc.wait()
    return proc.returncode, "".join(combined)


def main(argv: Sequence[str]) -> int:
    clear_stray_coverage_data_files()  # BL-61: clear anything already at rest.
    returncode, output = run_pytest_once(argv)
    if is_stray_combine_failure(returncode, output):
        print(
            "run_test_suite.py: pytest-cov's combine() hit a concurrent, incompatible coverage "
            "data file (BL-69). Clearing stray data and retrying once",
            file=sys.stderr,
        )
        clear_stray_coverage_data_files()
        returncode, _ = run_pytest_once(argv)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
