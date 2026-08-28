"""Offline tests for `scripts/run_test_suite.py` — BL-69's retry-once wrapper around `pytest
--cov`, which closes the gap `BL-61`'s before-the-run `rm -f .coverage .coverage.*` leaves open: a
second, genuinely concurrent `pytest --cov` invocation writing an incompatible sibling coverage
data file *mid-run*, past the point the Makefile's own sweep can reach.

These tests exercise the module's decision logic and file-clearing helper directly, at pytest
speed, with no real pytest subprocess and no real INTERNALERROR reproduced — matching this item's
own acceptance criteria, which allows the harder-to-synthesize end-to-end retry behavior to be
verified manually and recorded in the implementation receipt instead (mirroring `BL-61`'s own
precedent for the same difficulty). What *is* fully asserted here, automatically, is the
selectivity the acceptance criteria requires outright: this must retry for pytest-cov's own
combine()-time schema-mismatch DataError and that failure alone, never for a normal red test, a
`--cov-fail-under` breach, or any other INTERNALERROR.

`TestMain` (BL-72) closes the remaining gap: `main`'s own retry-once orchestration — the actual
control flow `make test`/`make verify`/CI run through this module for — had no automated coverage
at all. Each case stubs `run_pytest_once` (and, where call order is the thing under test,
`clear_stray_coverage_data_files`) via `monkeypatch.setattr`; still no real pytest subprocess, no
real coverage state touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is a plain directory, not an installed package — load the module by path, matching
# tests/test_check_backlog_merged.py's own precedent for the same situation.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_test_suite.py"
_spec = importlib.util.spec_from_file_location("run_test_suite", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rts
_spec.loader.exec_module(rts)


# The exact two-line INTERNALERROR shape this item's own finding reproduced live (abridged: the
# real traceback has more frames above this, which is irrelevant to detection).
_BRANCH_VS_STATEMENT = (
    "1868 passed, 16 deselected, 1 warning in 67.26s\n"
    "INTERNALERROR> Traceback (most recent call last):\n"
    'INTERNALERROR>   File ".../pytest_cov/engine.py", line 275, in finish\n'
    "INTERNALERROR>     self.cov.combine()\n"
    'INTERNALERROR>   File ".../coverage/sqldata.py", line 726, in update\n'
    "INTERNALERROR>     raise DataError(\n"
    "INTERNALERROR> coverage.exceptions.DataError: Can't combine branch coverage data with "
    "statement data\n"
)
_STATEMENT_VS_BRANCH = _BRANCH_VS_STATEMENT.replace(
    "Can't combine branch coverage data with statement data",
    "Can't combine statement coverage data with branch data",
)


class TestIsStrayCombineFailure:
    def test_true_for_branch_vs_statement_signature_at_exit_3(self) -> None:
        assert rts.is_stray_combine_failure(3, _BRANCH_VS_STATEMENT) is True

    def test_true_for_statement_vs_branch_signature_at_exit_3(self) -> None:
        assert rts.is_stray_combine_failure(3, _STATEMENT_VS_BRANCH) is True

    def test_false_for_clean_pass(self) -> None:
        assert rts.is_stray_combine_failure(0, "1868 passed in 42.00s\n") is False

    def test_false_for_a_normal_red_test_even_at_the_same_exit_code_as_a_coincidence(self) -> None:
        # Exit 1 is a normal test failure's own exit code; must never be treated as retryable even
        # if a test's own assertion text happens to mention the exact DataError wording.
        output = (
            "FAILED tests/test_x.py::test_thing - AssertionError: Can't combine branch coverage "
            "data with statement data\n1 failed, 1867 passed in 40.00s\n"
        )
        assert rts.is_stray_combine_failure(1, output) is False

    def test_false_for_cov_fail_under_breach(self) -> None:
        # pytest_cov/plugin.py's own session.testsfailed += 1 path — exit 1, never 3.
        output = (
            "1868 passed in 42.00s\n"
            "ERROR: Coverage failure: total of 88 is less than fail-under=91.0000\n"
        )
        assert rts.is_stray_combine_failure(1, output) is False

    def test_false_for_internalerror_that_is_not_this_dataerror(self) -> None:
        # Some other plugin's own internal crash — must not be silently retried either.
        output = (
            "INTERNALERROR> Traceback (most recent call last):\n"
            'INTERNALERROR>   File ".../someplugin/hooks.py", line 12, in pytest_sessionfinish\n'
            "INTERNALERROR>     raise RuntimeError('unrelated plugin bug')\n"
            "INTERNALERROR> RuntimeError: unrelated plugin bug\n"
        )
        assert rts.is_stray_combine_failure(3, output) is False

    def test_false_for_a_different_dataerror_variant_at_exit_3(self) -> None:
        # coverage/sqldata.py's own "Conflicting file tracer name" DataError — a genuine
        # coverage-file corruption signal this item must not mask, not a droppable schema clash.
        output = (
            "INTERNALERROR> Traceback (most recent call last):\n"
            'INTERNALERROR>   File ".../coverage/sqldata.py", line 783, in update\n'
            "INTERNALERROR>     raise DataError(\n"
            "INTERNALERROR> coverage.exceptions.DataError: Conflicting file tracer name for "
            "'foo.py': 'a' vs 'b'\n"
        )
        assert rts.is_stray_combine_failure(3, output) is False

    def test_false_for_exit_3_with_no_internalerror_marker(self) -> None:
        # Defensive: the exit code alone is never sufficient without the marker text too.
        assert (
            rts.is_stray_combine_failure(
                3, "Can't combine branch coverage data with statement data\n"
            )
            is False
        )


class TestClearStrayCoverageDataFiles:
    def test_removes_dot_coverage_and_suffixed_siblings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".coverage").write_text("main")
        (tmp_path / ".coverage.hostname.123.456").write_text("worker")
        (tmp_path / ".coverage.strayhost.999.abc").write_text("stray")

        rts.clear_stray_coverage_data_files()

        assert not (tmp_path / ".coverage").exists()
        assert not (tmp_path / ".coverage.hostname.123.456").exists()
        assert not (tmp_path / ".coverage.strayhost.999.abc").exists()

    def test_leaves_unrelated_files_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".coveragerc").write_text("[run]\n")
        (tmp_path / "other.txt").write_text("keep me")

        rts.clear_stray_coverage_data_files()

        assert (tmp_path / ".coveragerc").exists()
        assert (tmp_path / "other.txt").exists()

    def test_is_a_no_op_when_nothing_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rts.clear_stray_coverage_data_files()  # must not raise — mirrors `rm -f`'s own tolerance


class TestMain:
    """BL-72: `main`'s own retry-once control flow, stubbed at the `run_pytest_once` boundary —
    pure-Python, deterministic, sub-millisecond; no real pytest subprocess or coverage state."""

    def test_non_retryable_success_runs_pytest_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run_pytest_once(argv: list[str]) -> tuple[int, str]:
            calls.append(list(argv))
            return (0, "5 passed\n")

        monkeypatch.setattr(rts, "clear_stray_coverage_data_files", lambda: None)
        monkeypatch.setattr(rts, "run_pytest_once", fake_run_pytest_once)

        assert rts.main([]) == 0
        assert len(calls) == 1

    def test_retryable_stray_failure_is_rescued_by_a_clean_second_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        outcomes = iter([(3, _BRANCH_VS_STATEMENT), (0, "5 passed\n")])

        def fake_clear() -> None:
            events.append("clear")

        def fake_run_pytest_once(argv: list[str]) -> tuple[int, str]:
            events.append("run")
            return next(outcomes)

        monkeypatch.setattr(rts, "clear_stray_coverage_data_files", fake_clear)
        monkeypatch.setattr(rts, "run_pytest_once", fake_run_pytest_once)

        assert rts.main([]) == 0
        # Order-sensitive: clear_stray_coverage_data_files must run before EACH attempt, not
        # merely twice at some unspecified point.
        assert events == ["clear", "run", "clear", "run"]

    def test_retryable_stray_failure_then_a_genuine_red_test_reports_the_real_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact gap this item's own finding demonstrated live (main() mutated to
        # unconditionally `return 0`): a genuinely-failed retry must never be laundered into a
        # false success.
        outcomes = iter([(3, _BRANCH_VS_STATEMENT), (1, "1 failed, 4 passed\n")])

        monkeypatch.setattr(rts, "clear_stray_coverage_data_files", lambda: None)
        monkeypatch.setattr(rts, "run_pytest_once", lambda argv: next(outcomes))

        assert rts.main([]) == 1

    def test_non_retryable_failure_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run_pytest_once(argv: list[str]) -> tuple[int, str]:
            calls.append(list(argv))
            return (1, "1 failed, 4 passed\n")

        monkeypatch.setattr(rts, "clear_stray_coverage_data_files", lambda: None)
        monkeypatch.setattr(rts, "run_pytest_once", fake_run_pytest_once)

        assert rts.main([]) == 1
        assert len(calls) == 1
