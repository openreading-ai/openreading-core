"""Phase B (Manifest v0.6) — the `parse` CLI batch surface, OFFLINE (pymupdf, plus a keyless hosted
backend that never gets past the credential check).

M2 envelope selection (single file → response.v0.3, byte-identical to v0.5; directory / glob / >=2
args → batch-result), exit code 4 for a partial batch, --save-dir per-item output, the flags the
batch dispatch must not drop, and the stderr channel: per-item errors and the cost preflight."""

from __future__ import annotations

import json

from openreading import api, schemas
from openreading.cli.app import main
from openreading.testing.sample_pdf import build_sample_pdf


def _pdf(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(build_sample_pdf())
    return p


def test_single_file_stays_a_response_envelope(tmp_path, capsys):
    # M2: one explicit file → the v0.5 single-document behavior, NOT a batch
    f = _pdf(tmp_path / "a.pdf")
    rc = main(["parse", str(f), "--backend", "pymupdf"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert d["schema_version"] == "0.3" and "items" not in d  # a response, not a batch envelope
    assert d["backend"]["id"] == "pymupdf"


def test_directory_produces_one_batch_envelope(tmp_path, capsys):
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "sub" / "b.pdf")
    rc = main(["parse", str(d), "--backend", "pymupdf"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["schema_version"] == "0.2" and "items" in env  # batch-result envelope
    assert env["summary"]["succeeded"] == 2 and env["status"]["state"] == "succeeded"
    schemas.validate_batch_result(env)


def test_two_file_args_trigger_batch(tmp_path, capsys):
    a, b = _pdf(tmp_path / "a.pdf"), _pdf(tmp_path / "b.pdf")
    rc = main(["parse", str(a), str(b), "--backend", "pymupdf"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0 and "items" in env and env["summary"]["total"] == 2


def test_partial_batch_exits_4(tmp_path, capsys):
    d = tmp_path / "c"
    _pdf(d / "good.pdf")
    (d / "bad.pdf").write_bytes(b"this is not a pdf at all")  # pymupdf raises → item failed
    rc = main(["parse", str(d), "--backend", "pymupdf"])
    env = json.loads(capsys.readouterr().out)
    assert env["status"]["state"] == "partial"
    assert (env["summary"]["succeeded"], env["summary"]["failed"]) == (1, 1)
    assert rc == 4  # partial → exit 4


def test_save_dir_writes_per_item_responses(tmp_path, capsys):
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "sub" / "b.pdf")
    outdir = tmp_path / "saved"
    rc = main(["parse", str(d), "--backend", "pymupdf", "--save-dir", str(outdir)])
    assert rc == 0
    assert (outdir / "a.pdf.json").exists() and (outdir / "sub" / "b.pdf.json").exists()
    schemas.validate_response(json.loads((outdir / "a.pdf.json").read_text()))  # full response each


def test_stdout_is_pure_json_progress_on_stderr(tmp_path, capsys):
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "b.pdf")
    main(["parse", str(d), "--backend", "pymupdf"])
    cap = capsys.readouterr()
    json.loads(cap.out)  # stdout parses as exactly one JSON document (no progress noise)
    assert "[1/2]" in cap.err or "[2/2]" in cap.err  # progress went to stderr


def test_every_item_reaches_the_progress_counter_on_stderr(tmp_path, capsys):
    # Tier 2/1 (BL-147): the [N/total] stderr counter must reach total and print a line per item.
    # Skipping is gone, so the third file is dispatched and fails on the backend's own terms
    # rather than being counted without ever running.
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "b.pdf")
    (d / "c.docx").write_bytes(b"not really a docx")  # pymupdf refuses it first-hand
    rc = main(["parse", str(d), "--backend", "pymupdf"])
    cap = capsys.readouterr()
    env = json.loads(cap.out)
    # Exit 4 is the documented "batch parse: partial, some items failed". A directory holding a
    # file the backend cannot read used to exit 0 because the file was skipped without ever being
    # tried; it is now attempted, fails honestly, and the exit code says so.
    assert env["status"]["state"] == "partial" and rc == 4
    assert env["summary"]["failed"] == 1 and "skipped" not in env["summary"]
    assert "[3/3]" in cap.err  # counter reaches total
    assert "c.docx" in cap.err  # the failing item still gets its own stderr line


def test_empty_directory_produces_empty_batch_warning_and_stderr_line(tmp_path, capsys):
    # Tier 3: a real, empty directory used to exit 1 with zero stderr output and no `warnings` key
    # at all — indistinguishable from a hang or a crash from the terminal.
    d = tmp_path / "empty"
    d.mkdir()
    rc = main(["parse", str(d), "--backend", "pymupdf"])
    cap = capsys.readouterr()
    env = json.loads(cap.out)
    assert rc == 1  # unchanged: 0 succeeded ⇒ failed ⇒ exit 1 (no exit-code change)
    assert env["summary"]["total"] == 0
    assert [w["code"] for w in env.get("warnings", [])] == ["empty_batch"]
    assert cap.err.strip() != ""  # no longer silent
    assert "no source resolved to a document to process" in cap.err


def test_hidden_file_only_directory_produces_empty_batch_warning(tmp_path, capsys):
    # A directory whose only content is a dotfile (e.g. .DS_Store) resolves to zero sources the
    # same way an empty directory does — a common real-world shape, not just an edge case.
    d = tmp_path / "c"
    d.mkdir()
    (d / ".DS_Store").write_bytes(b"junk")
    rc = main(["parse", str(d), "--backend", "pymupdf"])
    cap = capsys.readouterr()
    env = json.loads(cap.out)
    assert rc == 1
    assert env["summary"]["total"] == 0
    assert [w["code"] for w in env.get("warnings", [])] == ["empty_batch"]
    assert "no source resolved to a document to process" in cap.err


def test_batch_unknown_strategy_puts_the_hint_on_stderr(tmp_path, capsys):
    # Per-item isolation (M6) turns this caller error into a failed item, so the batch exits 1
    # rather than the single-document 2 — the hint must still land on stderr instead of hiding
    # inside the stdout envelope, which is usually redirected to a file.
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    rc = main(["parse", str(d), "--strategy", "no_such_strategy"])
    cap = capsys.readouterr()
    assert rc == 1
    assert "unknown_strategy" in cap.err and "unknown strategy 'no_such_strategy'" in cap.err
    assert json.loads(cap.out)["status"]["state"] == "failed"  # stdout is still the envelope


_RACE = """\
version: 1
strategies:
  duo:
    parallel: [reducto, aws-textract]
    pick: best
"""
CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ " * 4


def test_batch_keep_candidates_reaches_every_item(tmp_path, capsys, monkeypatch):
    # A race is the only node shape that retains a loser. Racing two REAL local branches is not an
    # option: PyMuPDF's table finder flips a process-global glyph-height flag, so two concurrent
    # pymupdf branches corrupt span geometry for the rest of the session. Scripted backends keep
    # the race hermetic and the winner deterministic (garbled text loses).
    from tests.fakes import ScriptedBackend, scripted_registry

    monkeypatch.setattr(
        api,
        "build_registry",
        lambda: scripted_registry(
            ScriptedBackend("reducto", text=GARBLED),
            ScriptedBackend("aws-textract", text=CLEAN),
        ),
    )
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    cfg = tmp_path / "openreading.yaml"
    cfg.write_text(_RACE)
    argv = ["parse", str(d), "--strategy", "duo", "--config", str(cfg)]

    assert main([*argv, "--keep-candidates"]) == 0
    item = json.loads(capsys.readouterr().out)["items"][0]
    assert item["response"]["backend"]["id"] == "aws-textract"
    cands = item["response"]["orchestration"]["candidates"]
    assert [c["backend"] for c in cands] == ["reducto"]  # the completed loser
    schemas.validate_response(cands[0]["response"])  # a full envelope, not a stub

    assert main(argv) == 0  # control: retention is the flag's doing, not the strategy's
    assert (
        "candidates"
        not in json.loads(capsys.readouterr().out)["items"][0]["response"]["orchestration"]
    )


def test_preflight_warns_before_a_big_hosted_batch(tmp_path, capsys, monkeypatch):
    # >10 live items on a hosted backend arms the scope preflight; with no key every item then
    # fails offline at the credential check, so nothing here touches the network.
    for var in ("REDUCTO_API_KEY", "OPENREADING_REDUCTO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "c"
    for i in range(11):
        _pdf(d / f"f{i:02d}.pdf")
    main(["parse", str(d), "--backend", "reducto"])
    err = capsys.readouterr().err
    assert "[preflight] 11 items on hosted backend reducto" in err
    assert "11 call(s) on your own key" in err


# --- BL-84: --jobs floor/ceiling on the CLI surface --------------------------------------


def test_batch_jobs_floor_clamps_to_one_and_still_completes(tmp_path, capsys):
    # The original repro: `--jobs 0` used to run the batch fine (jobs<=1 is the serial path) but
    # then crash on the OUTSIDE-the-try/except schema-validate call, after every document had
    # already succeeded — a bare traceback and zero bytes on stdout. It must now clamp cleanly.
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "b.pdf")
    rc = main(["parse", str(d), "--backend", "pymupdf", "--jobs", "0"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["request"]["jobs"] == 1  # corrected value, not the raw 0
    assert env["summary"]["succeeded"] == 2


def test_batch_jobs_over_ceiling_is_a_clean_coded_exit_not_a_bare_traceback(tmp_path, capsys):
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    rc = main(["parse", str(d), "--backend", "pymupdf", "--jobs", "5000000"])
    cap = capsys.readouterr()
    assert rc == 2  # same coded-exit bucket as the existing --max-items guard
    assert "jobs" in cap.err and "max-jobs" in cap.err
    assert cap.out == ""  # nothing printed — no partial/garbled envelope on stdout


def test_batch_max_jobs_flag_narrows_the_ceiling(tmp_path, capsys):
    # --max-jobs mirrors --max-items's own "sane default + explicit override" shape; a caller who
    # narrows it gets rejected at a lower bar than the built-in default.
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    rc = main(["parse", str(d), "--backend", "pymupdf", "--jobs", "5", "--max-jobs", "2"])
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""


def test_batch_jobs_within_a_raised_max_jobs_still_runs(tmp_path, capsys):
    # BL-94: this test's name always claimed to exercise a RAISED ceiling, but its body narrowed
    # one instead (--max-jobs 2, well under the default 32) — the one scenario the name actually
    # promised (--jobs above the default 32-worker ceiling, permitted only because --max-jobs
    # raises it to match) was never run, so BatchRequestEcho's own structural Field(le=32)
    # crashing that exact input went uncaught by a test with this exact name.
    d = tmp_path / "c"
    _pdf(d / "a.pdf")
    _pdf(d / "b.pdf")
    rc = main(["parse", str(d), "--backend", "pymupdf", "--jobs", "33", "--max-jobs", "33"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["request"]["jobs"] == 33
    assert env["summary"]["succeeded"] == 2


# --- the two stderr advisories: what a run will do, and what --jobs actually did -----------


def test_preflight_line_quotes_no_price_at_all(tmp_path, capsys, monkeypatch):
    """The line used to read `12 items → hosted backend reducto (~$0.015-$0.06/page-equiv each)`.

    Every number in it came from `descriptor.cost`, a rate card this package had written down and
    could not verify, so the advisory presented a guess in the same breath as a real item count.
    What it names now is what core knows before a byte is read: how many calls leave this machine,
    to whom, and on whose key."""
    for var in ("REDUCTO_API_KEY", "OPENREADING_REDUCTO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "c"
    for i in range(12):
        _pdf(d / f"f{i:02d}.pdf")
    main(["parse", str(d), "--backend", "reducto"])
    err = capsys.readouterr().err
    preflight = [ln for ln in err.splitlines() if ln.startswith("[preflight]")]
    assert preflight, "the advisory still fires for a big hosted batch"
    assert all("$" not in ln for ln in preflight)
    assert "12 call(s) on your own key" in err  # the count, which is a fact
    assert "more if a document is paged" in err  # ... and why it is a floor


def test_jobs_above_a_backend_cap_says_so_on_stderr(tmp_path, capsys):
    # tesseract's descriptor caps platform concurrency at 4 (CPU-bound local OCR), so `--jobs 16`
    # silently became `request.jobs: 4` with nothing on stderr: the caller had no way to learn the
    # knob they turned did nothing. .txt items are skipped at intake, so no OCR subprocess runs.
    d = tmp_path / "c"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        (d / f"f{i}.txt").write_text("x")
    main(["parse", str(d), "--backend", "tesseract", "--jobs", "16"])
    err = capsys.readouterr().err
    assert "--jobs 16 requested" in err
    assert "tesseract caps platform concurrency at 4" in err


def test_jobs_within_a_backend_cap_prints_no_concurrency_notice(tmp_path, capsys):
    # The common case must stay quiet: --jobs at or under the cap, and a backend declaring no cap
    # at all, both print nothing. An advisory that fires on every run is an advisory nobody reads.
    d = tmp_path / "c"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        (d / f"f{i}.txt").write_text("x")
    main(["parse", str(d), "--backend", "tesseract", "--jobs", "4"])  # exactly at the cap
    assert "caps platform concurrency" not in capsys.readouterr().err

    e = tmp_path / "p"
    _pdf(e / "a.pdf")
    _pdf(e / "b.pdf")
    main(["parse", str(e), "--backend", "pymupdf", "--jobs", "16"])  # declares no cap
    assert "caps platform concurrency" not in capsys.readouterr().err
