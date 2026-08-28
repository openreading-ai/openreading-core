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


def test_leaderboard_cli_fewer_than_two_backends_exits_2(capsys):
    rc = main(["leaderboard", SAMPLE, "--backends", "pymupdf"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[leaderboard]" in err and "at least two" in err


def test_leaderboard_cli_missing_dataset_exits_3(capsys, tmp_path):
    rc = main(["leaderboard", str(tmp_path / "nope"), "--backends", "pymupdf,tesseract"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "[leaderboard]" in err
    assert "Traceback" not in err


def test_leaderboard_cli_unreadable_policy_exits_3_without_a_traceback(capsys, tmp_path):
    bad_policy = tmp_path / "policy.json"
    bad_policy.write_text("{not valid json")
    rc = main(
        [
            "leaderboard",
            SAMPLE,
            "--backends",
            "pymupdf,tesseract",
            "--policy",
            str(bad_policy),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "[leaderboard]" in err
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
