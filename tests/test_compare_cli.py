"""M3a — the `openreading compare` CLI verb (H7): files mode, fan-out (monkeypatched for
determinism), formats, exit codes, save-dir round-trip, and `explain` dispatch onto a report."""

from __future__ import annotations

import json
import pathlib

import pytest

from openreading.cli import app, main
from openreading.readiness import BackendReadiness
from tests.fakes import make_envelope


def _write(tmp: pathlib.Path, name: str, **kw) -> str:
    p = tmp / name
    p.write_text(json.dumps(make_envelope(**kw)))
    return str(p)


# --- files mode -------------------------------------------------------------------------


def test_files_mode_json(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a", fields={"Total": "$5"})
    b = _write(tmp_path, "b.json", backend_id="b", fields={"Total": "$9"})
    rc = main(["compare", a, b])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "0.2" and report["mode"] == "pairwise"


def test_files_mode_table(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a", fields={"Total": "$5"})
    b = _write(tmp_path, "b.json", backend_id="b", fields={"Total": "$9"})
    rc = main(["compare", a, b, "--format", "table"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMPARE" in out and "FINDINGS" in out and "field_value_conflict" in out


def test_files_mode_md(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a", fields={"Total": "$5"})
    b = _write(tmp_path, "b.json", backend_id="b", fields={"Total": "$9"})
    rc = main(["compare", a, b, "--format", "md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Comparison" in out and "## Findings" in out and "field_value_conflict" in out


def test_fewer_than_two_inputs_exit_2(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a")
    assert main(["compare", a]) == 2


def test_invalid_input_exit_5(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a")
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a response"}')
    assert main(["compare", a, str(bad)]) == 5


def test_diff_requires_two(tmp_path, capsys) -> None:
    files = [_write(tmp_path, f"{x}.json", backend_id=x) for x in ("a", "b", "c")]
    assert main(["compare", *files, "--format", "diff"]) == 2


def test_files_mode_diff_renders_header_and_field_deltas(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a", text="hello world", fields={"Total": "$5"})
    b = _write(tmp_path, "b.json", backend_id="b", text="hello there", fields={"Total": "$9"})
    rc = main(["compare", a, b, "--format", "diff"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--- a" in out and "+++ b" in out  # unified-diff header lines
    assert "FIELD DELTAS" in out
    assert "Total [disagree]" in out and "a=$5" in out and "b=$9" in out


# --- fan-out mode (api.run monkeypatched → deterministic, offline) -----------------------


@pytest.fixture
def fake_run(monkeypatch):
    def _run(source, *, backend, **kw):
        return make_envelope(
            backend_id=backend, text=f"body from {backend}", fields={"Total": "$5"}
        )

    monkeypatch.setattr(app.api, "run", _run)


def test_fanout_backends(tmp_path, capsys, fake_run) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    rc = main(["compare", str(doc), "--backends", "pymupdf,tesseract"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert {s["label"] for s in report["subjects"]} == {"pymupdf", "tesseract"}
    assert all(s["source"] == "fanout" for s in report["subjects"])


def test_fanout_all_ready(tmp_path, capsys, fake_run, monkeypatch) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")

    def fake_readiness(adapter, *, broker=None):
        slug = adapter.descriptor.id
        return BackendReadiness(
            slug=slug,
            type="oss_library",
            extra_installed=True,
            ready=slug in ("pymupdf", "tesseract"),
        )

    monkeypatch.setattr(app, "backend_readiness", fake_readiness)
    rc = main(["compare", str(doc), "--all-ready"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert {s["label"] for s in report["subjects"]} == {"pymupdf", "tesseract"}
    assert all(s["source"] == "fanout" for s in report["subjects"])


def test_fanout_unknown_backend_exit_2(tmp_path, capsys, fake_run) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    assert main(["compare", str(doc), "--backends", "pymupdf,made-up"]) == 2
    err = capsys.readouterr().err
    # The reader is typing a comma-separated list of ids, so the message names the ones that
    # exist, the way `backends --check` always has.
    assert "made-up" in err and "known:" in err and "pymupdf" in err


def test_fanout_refuses_backends_and_all_ready_together(tmp_path, capsys, fake_run) -> None:
    # --all-ready silently won, so a typed --backends list was discarded whole, an unknown id in
    # it included. Refusing is the only way the reader learns their list was ignored.
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    assert main(["compare", str(doc), "--backends", "pymupdf,tesseract", "--all-ready"]) == 2
    assert "--backends and --all-ready are alternatives" in capsys.readouterr().err


def test_fanout_save_dir_roundtrips(tmp_path, capsys, fake_run) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    out = tmp_path / "saved"
    rc = main(["compare", str(doc), "--backends", "pymupdf,tesseract", "--save-dir", str(out)])
    assert rc == 0
    saved = sorted(p.name for p in out.glob("*.json"))
    assert saved == ["pymupdf.json", "tesseract.json"]
    # mode (b) reduces to mode (a): the saved envelopes re-compare
    capsys.readouterr()
    assert main(["compare", str(out / "pymupdf.json"), str(out / "tesseract.json")]) == 0


@pytest.fixture
def fake_run_error(monkeypatch):
    def _run(source, *, backend, **kw):
        raise Exception("boom")  # bare, to exercise the `except Exception` branch specifically

    monkeypatch.setattr(app.api, "run", _run)


def test_fanout_unexpected_error_exit_1(tmp_path, capsys, fake_run_error) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    rc = main(["compare", str(doc), "--backends", "pymupdf,tesseract"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[pymupdf] error: Exception: boom" in err


# --- explain dispatch -------------------------------------------------------------------


def test_explain_renders_comparison_report(tmp_path, capsys) -> None:
    a = _write(tmp_path, "a.json", backend_id="a", fields={"Total": "$5"})
    b = _write(tmp_path, "b.json", backend_id="b", fields={"Total": "$9"})
    main(["compare", a, b])
    report = json.loads(capsys.readouterr().out)
    rpath = tmp_path / "report.json"
    rpath.write_text(json.dumps(report))
    rc = main(["explain", str(rpath)])
    assert rc == 0
    assert "COMPARE" in capsys.readouterr().out


def test_compare_fanout_over_a_directory_is_usage_not_an_errno(tmp_path, capsys):
    """`parse <folder>` works, so a reader tries `compare <folder> --backends a,b` next. It used
    to reach the adapter and come back as a raw IsADirectoryError at exit 1, naming an errno
    rather than the way through."""
    (tmp_path / "docs").mkdir()
    rc = main(["compare", str(tmp_path / "docs"), "--backends", "pymupdf,tesseract"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "is a directory" in err
    assert "openreading compare a.json b.json" in err  # the way through, not just the refusal


def test_compare_fanout_on_a_missing_document_is_usage_not_an_errno(tmp_path, capsys):
    """`parse` refuses a mistyped filename at exit 2 with a sentence, and the reason is in a
    comment there: `str(e)` on an OSError leads with an `[Errno 2]` the reader cannot use. Fan-out
    never got that handler, so the same typo came back as a raw SourceNotFoundError at exit 1."""
    rc = main(["compare", str(tmp_path / "nope.pdf"), "--backends", "pymupdf,tesseract"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nope.pdf" in err
    assert "Errno" not in err
    assert "no such file or directory" in err


def test_compare_from_cancelled_race_explains_missing_candidates(tmp_path, capsys):
    response = make_envelope("pymupdf")
    response["orchestration"] = {
        "strategy": "fast",
        "attempts": [
            {"backend": "pymupdf", "category": "succeeded"},
            {"backend": "tesseract", "category": "raced_lost", "detail": "cancelled"},
        ],
    }
    path = tmp_path / "run.json"
    path.write_text(json.dumps(response))
    assert main(["compare", "--from", str(path)]) == 5
    err = capsys.readouterr().err
    assert "cancel" in err and "completed" in err and "openreading help chaining" in err


def test_compare_from_batch_points_to_saved_item_responses(tmp_path, capsys):
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"items": [], "summary": {}, "status": {"state": "failed"}}))
    assert main(["compare", "--from", str(path)]) == 5
    err = capsys.readouterr().err
    assert "batch-result" in err and "--save-dir" in err
