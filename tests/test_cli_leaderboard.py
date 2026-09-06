"""CLI coverage for `openreading leaderboard` (BL-160) — mirrors
tests/test_cli_replay_calibrate.py's shape for `calibrate`: arg handling, backend-id validation,
--format table/json, and (AC-6) the same coded-exit taxonomy `compare`/`calibrate` already use.

AC-6's four taxonomy-member tests inject the exception at the seam `cmd_leaderboard` actually
calls (`openreading.evals.leaderboard.run_leaderboard`), the SAME documented technique
tests/test_cli.py already uses for `cmd_compare`'s fan-out ComplianceRefused and
tests/test_cli_replay_calibrate.py / tests/test_cli.py use for `cmd_calibrate`'s ComplianceRefused
— because, like those, a per-case backend fault never actually escapes evals.runner.run_case (it
is always turned into a scored CaseResult, per AC-1/AC-2), so there is no organic trigger; this
proves the CLI's OWN coded-exit mapping is correct and stays correct, independent of whether
today's implementation happens to exercise it."""

from __future__ import annotations

import json

import pytest

from openreading.cli import main
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import (
    ComplianceRefused,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)

pytest.importorskip("fitz", reason="pymupdf not installed")

SAMPLE = "src/openreading/evals/sample"


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


# ---- basic CLI wiring: arg handling, formats ---------------------------------------------------


def test_leaderboard_cli_table_format_is_the_default(capsys):
    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf,tesseract"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "dataset:" in out and "rank" in out
    assert "{" not in out.splitlines()[0]  # not JSON


def test_leaderboard_cli_json_format_is_a_schema_valid_report(capsys):
    from openreading import schemas

    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf,tesseract", "--format", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    schemas.validate_leaderboard_report(report)
    assert {b["backend_id"] for b in report["backends"]} == {"pymupdf", "tesseract"}


def test_leaderboard_cli_unknown_backend_exits_2(capsys):
    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf,not-a-real-backend"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[leaderboard]" in err and "not-a-real-backend" in err
    # The reader is typing a comma-separated list of ids, so the message names the ones that
    # exist, the way `backends --check` always has.
    assert "known:" in err and "pymupdf" in err


def test_leaderboard_cli_fewer_than_two_backends_exits_2(capsys):
    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[leaderboard]" in err and "at least two" in err
    # the message states the fix, not only the problem
    assert "--backends" in err and "--all-ready" in err


def test_leaderboard_cli_missing_dataset_exits_3(capsys, tmp_path):
    rc = main(["leaderboard", str(tmp_path / "nope"), "--backends", "pymupdf,tesseract"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "[leaderboard]" in err
    assert "Traceback" not in err


def test_leaderboard_cli_malformed_policy_block_exits_3_without_a_traceback(capsys, tmp_path):
    bad_config = tmp_path / "openreading.yaml"
    bad_config.write_text("version: 1\npolicy: {require_locall: true}\n")
    rc = main(
        [
            "leaderboard",
            SAMPLE,
            "--backends",
            "pymupdf,tesseract",
            "--config",
            str(bad_config),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "[leaderboard]" in err
    assert "did you mean 'require_local'" in err
    assert "Traceback" not in err


# ---- AC-6: the CLI leaderboard command exits through the same coded, per-taxonomy-member exits --
# ---- (RetryableError / TerminalError / UnsupportedFeatureError / ComplianceRefused) that ---------
# ---- calibrate and compare already use — never a bare exit-1 crash. ------------------------------


def _raise(exc: Exception):
    def _inner(*args, **kwargs):
        raise exc

    return _inner


@pytest.mark.parametrize(
    "exc",
    [
        RetryableError("simulated rate-limit exhaustion"),
        TerminalError("simulated can't-run-at-all failure"),
        UnsupportedFeatureError("simulated unsupported feature", feature="ocr"),
        ComplianceRefused("nothing is compliant here", constraint="no_compliant_backend"),
    ],
    ids=["retryable", "terminal", "unsupported_feature", "compliance_refused"],
)
def test_leaderboard_cli_taxonomy_member_exits_3_clean(exc, capsys, monkeypatch):
    import openreading.evals.leaderboard as leaderboard_mod

    monkeypatch.setattr(leaderboard_mod, "run_leaderboard", _raise(exc))

    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf,tesseract"])

    assert rc == 3
    out, err = capsys.readouterr()
    assert out == ""  # no partial/misleading report on the failure path
    assert "[leaderboard]" in err
    assert str(exc) in err
    assert "Traceback" not in err
    assert type(exc).__name__ not in err  # clean [label] line, not the exit-1 crash-handler shape


# ---- the human table must carry what the schema calls required to read a mean (C12, A30) -------
#
# `_render_leaderboard_table` is a pure function of a BenchmarkReport, so these build the report
# directly: the cases that matter (a backend that never scored, a tie, a mutual failure) cannot be
# produced from the shipped one-case sample, and inventing backends to produce them would test the
# runner rather than the renderer.


def _report(backends, cases, path="ds", case_names=None):
    from openreading.types.leaderboard import (
        BenchmarkReport,
        LeaderboardBackend,
        LeaderboardCase,
        LeaderboardDataset,
    )

    names = case_names if case_names is not None else [c["name"] for c in cases]
    return BenchmarkReport(
        dataset=LeaderboardDataset(path=path, case_count=len(names), case_names=names),
        backends=[LeaderboardBackend(**b) for b in backends],
        cases=[LeaderboardCase(**c) for c in cases],
    )


def _backend(bid, rank, mean, n_cases, n_scored, errors=0, nd=False, dims=None):
    return {
        "backend_id": bid,
        "rank": rank,
        "mean_score": mean,
        "n_cases": n_cases,
        "n_scored": n_scored,
        "errors": errors,
        "cost_per_doc": 0.0,
        "non_deterministic": nd,
        "dimensions": dims or {},
    }


def test_table_distinguishes_a_never_scored_backend_from_a_genuine_zero():
    # C12: both rows carry mean_score 0.0. Only n_scored separates "scored 0.0 on every case it
    # ran" from "never scored a case", and the schema marks n_scored required for reading a mean.
    # `errors` does not separate them: a case with no recognized `expected` dimension is unscored
    # without erroring, so the never-scored row can show errors 0 too.
    from openreading.cli.app import _render_leaderboard_table

    out = _render_leaderboard_table(
        _report(
            [
                _backend("scored-zero", 1, 0.0, 2, 2, dims={"text_contains": 0.0}),
                _backend("never-ran", 2, 0.0, 2, 0),
            ],
            [
                {
                    "name": "a",
                    "winner": "scored-zero",
                    "scores": {"scored-zero": 0.0, "never-ran": None},
                },
                {
                    "name": "b",
                    "winner": "scored-zero",
                    "scores": {"scored-zero": 0.0, "never-ran": None},
                },
            ],
        )
    )
    # columns: rank, backend, mean, scored, cost/doc, errors, dimensions...
    scored_row = next(ln for ln in out.splitlines() if "scored-zero" in ln).split()
    never_row = next(ln for ln in out.splitlines() if "never-ran" in ln).split()
    assert (scored_row[2], scored_row[3]) == ("0.000", "2/2")  # a measured zero, over 2 cases
    assert (never_row[2], never_row[3]) == ("—", "0/2")  # 0/0 is not a measurement, so no number
    assert "scored" in out.splitlines()[2]  # the column is named in the header


def test_table_marks_a_non_deterministic_backend():
    from openreading.cli.app import _render_leaderboard_table

    out = _render_leaderboard_table(
        _report(
            [_backend("generative", 1, 0.9, 1, 1, nd=True), _backend("plain", 2, 0.5, 1, 1)],
            [{"name": "a", "winner": "generative", "scores": {"generative": 0.9, "plain": 0.5}}],
        )
    )
    assert "non-deterministic" in next(ln for ln in out.splitlines() if "generative" in ln)
    assert "non-deterministic" not in next(ln for ln in out.splitlines() if " plain" in ln)


def test_per_case_block_names_no_winner_on_a_tie_or_a_mutual_failure():
    # A30: the JSON `winner` breaks ties alphabetically, a rule stated only inside the schema. The
    # human block must not present that tiebreak as a result: a reader tallying this block would
    # otherwise read 4-0 where the honest tally is 1 win, 1 tie, 1 mutual failure, 1 no result.
    from openreading.cli.app import _render_leaderboard_table

    out = _render_leaderboard_table(
        _report(
            [_backend("aaa", 1, 0.5, 4, 3), _backend("bbb", 2, 0.5, 4, 3)],
            [
                {"name": "win", "winner": "bbb", "scores": {"aaa": 0.2, "bbb": 0.9}},
                {"name": "tie", "winner": "aaa", "scores": {"aaa": 1.0, "bbb": 1.0}},
                {"name": "both_zero", "winner": "aaa", "scores": {"aaa": 0.0, "bbb": 0.0}},
                {"name": "neither", "winner": None, "scores": {"aaa": None, "bbb": None}},
            ],
        )
    )
    lines = {ln.strip().split(":")[0]: ln for ln in out.splitlines() if ln.startswith("  ")}
    assert "winner=bbb" in lines["win"]
    assert "tie" in lines["tie"] and "winner=" not in lines["tie"]
    assert "winner=" not in lines["both_zero"]
    assert "winner=" not in lines["neither"]
    # and the tally, so the block cannot be read as a 4-0 sweep
    tally = next(ln for ln in out.splitlines() if "tally" in ln)
    assert "1 win" in tally and "1 tie" in tally
