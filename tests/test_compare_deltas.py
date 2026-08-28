"""Content-delta view (`compare --format diffs`): per-subject "lines only I captured (others
missed)" on the guaranteed text channel — packaging-immune, the noise-free answer to "what did A
extract that B/C didn't"."""

from __future__ import annotations

from openreading.cli.app import main
from openreading.comparison.deltas import content_deltas, content_overlap
from openreading.comparison.ingest import load_subjects
from tests.fakes import make_envelope


def test_content_deltas_flags_lines_only_one_subject_has():
    a = make_envelope("a", text="shared line one\nONLY-A distinctive marker line\nshared line two")
    b = make_envelope("b", text="shared line one\nshared line two")
    c = make_envelope("c", text="shared line one\nshared line two")
    d = content_deltas(load_subjects([a, b, c]))
    assert any("ONLY-A distinctive marker" in ln for ln in d["a"]["unique"])
    assert d["b"]["unique"] == [] and d["c"]["unique"] == []


def test_content_deltas_are_packaging_immune():
    # b carries the same words inline (different packaging); a's line is NOT flagged unique.
    a = make_envelope("a", text="Region Revenue North 4400")
    b = make_envelope("b", text="the region revenue north 4400 total appears here inline")
    d = content_deltas(load_subjects([a, b]))
    assert d["a"]["unique"] == []  # content present in b (fuzzy/normalized) → not a real delta


def test_content_deltas_missed_needs_consensus_among_others():
    # c lacks a line that BOTH a and b have → c.missed; a lone unique line is NOT a "miss".
    a = make_envelope("a", text="shared\nboth A and B have this consensus line\nnoise-only-in-a")
    b = make_envelope("b", text="shared\nboth A and B have this consensus line")
    c = make_envelope("c", text="shared")
    d = content_deltas(load_subjects([a, b, c]))
    assert any("consensus line" in ln for ln in d["c"]["missed"])  # both others have it, c lacks
    assert not any("noise-only-in-a" in ln for ln in d["c"]["missed"])  # only a has it → not a miss


# --- token-coverage: packaging/reordering must NEVER fabricate a content delta ---------------


def test_flattened_table_is_not_a_false_content_miss():
    # THE bug this fix targets: A packages a whole table on ONE line; B splits it into rows. Same
    # tokens → neither a's flat line is "unique" nor does b "miss" it (line-substring matching used
    # to flag both, because A's one long line is not a contiguous substring of B's split rows).
    flat = "Date Amount 05/01 100.00 05/02 200.00 05/03 300.00 Total 600.00"
    a = make_envelope("a", text="header\n" + flat)
    b = make_envelope(
        "b", text="header\nDate Amount\n05/01 100.00\n05/02 200.00\n05/03 300.00\nTotal 600.00"
    )
    d = content_deltas(load_subjects([a, b]))
    assert d["a"]["unique"] == []  # the flat line's tokens are all present in b
    assert d["a"]["missed"] == []
    assert d["b"]["missed"] == []  # b split the same content — it missed nothing


def test_reordered_content_is_not_a_delta():
    a = make_envelope("a", text="alpha beta gamma delta epsilon")
    b = make_envelope("b", text="epsilon delta gamma beta alpha")
    d = content_deltas(load_subjects([a, b]))
    assert d["a"]["unique"] == [] and d["b"]["unique"] == []


def test_novel_tokens_are_still_flagged_unique():
    # coverage must not over-suppress: a line whose tokens are absent elsewhere is a real delta.
    a = make_envelope("a", text="common line\nZZZ9 QQQ8 WWW7 novel gibberish payload only-here")
    b = make_envelope("b", text="common line")
    d = content_deltas(load_subjects([a, b]))
    assert any("novel gibberish payload" in ln for ln in d["a"]["unique"])
    assert any("novel gibberish payload" in ln for ln in d["b"]["missed"])  # 2-way: b lacks it


def test_content_overlap_high_for_same_content_different_packaging():
    a = make_envelope("a", text="Date Amount 05/01 100.00 05/02 200.00")
    b = make_envelope("b", text="Date\nAmount\n05/01\n100.00\n05/02\n200.00")
    assert content_overlap(load_subjects([a, b])) > 0.9


def test_content_overlap_low_for_disjoint_content():
    a = make_envelope("a", text="alpha beta gamma delta")
    b = make_envelope("b", text="omega sigma tau upsilon")
    assert content_overlap(load_subjects([a, b])) < 0.1


def test_content_overlap_near_zero_when_one_subject_extracted_nothing():
    # THE bug: a subject that genuinely captured zero text (a non-OCR backend on an image-only
    # page is a real failure mode, not contrived) used to be dropped from the token-set comprehension
    # entirely, collapsing `toks` to a single element and hitting the `len(toks) < 2: return 1.0`
    # branch — reporting "identical vocabulary" for a pair where one side has nothing the other has.
    # It must instead pull the score toward 0.0: its (empty) token set still participates, and an
    # empty set intersected with anything is empty.
    a = make_envelope("a", text="invoice total four hundred dollars due on receipt")
    b = make_envelope("b", text=None, pages=[])  # zero extracted content
    assert content_overlap(load_subjects([a, b])) < 0.1


def test_content_overlap_both_empty_stays_identical():
    # unchanged: two subjects that BOTH extracted nothing agree completely — this codebase's own
    # "two empties = identical" convention elsewhere (strategies/engine.py's `_token_jaccard`,
    # which returns 1.0 when the union of two empty sets is itself empty).
    a = make_envelope("a", text=None, pages=[])
    b = make_envelope("b", text=None, pages=[])
    assert content_overlap(load_subjects([a, b])) == 1.0


# --- the superpowered `--format diffs` render (content verdict + structure) ------------------


def test_format_diffs_cli_leads_with_content_verdict_and_shows_only(tmp_path, capsys):
    import json

    fa = tmp_path / "a.json"
    fb = tmp_path / "b.json"
    fc = tmp_path / "c.json"
    fa.write_text(json.dumps(make_envelope("a", text="common\nA-ONLY unique payload\nend")))
    fb.write_text(json.dumps(make_envelope("b", text="common\nend")))
    fc.write_text(json.dumps(make_envelope("c", text="common\nend")))
    rc = main(["compare", str(fa), str(fb), str(fc), "--format", "diffs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CONTENT" in out and "ONLY a" in out
    assert "A-ONLY unique payload" in out  # the actual content is shown, plainly
    assert "DIVERGENT" in out  # a real content delta → not equivalent
    assert "PAYLOAD DIFF" not in out  # the unreadable unified-diff wall is gone


def test_format_diffs_cli_says_equivalent_when_only_packaging_differs(tmp_path, capsys):
    import json

    # same content, different packaging (flat line vs split) → CONTENT is EQUIVALENT, no false deltas.
    fa = tmp_path / "a.json"
    fb = tmp_path / "b.json"
    fa.write_text(json.dumps(make_envelope("a", text="Date Amount 05/01 100.00 05/02 200.00")))
    fb.write_text(json.dumps(make_envelope("b", text="Date\nAmount\n05/01\n100.00\n05/02\n200.00")))
    rc = main(["compare", str(fa), str(fb), "--format", "diffs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EQUIVALENT" in out


def test_format_diffs_cli_never_pairs_divergent_with_full_shared_score(tmp_path, capsys):
    import json

    # closes the actual user-visible contradiction (not just the underlying content_overlap number):
    # one side has real content, the other extracted nothing → the render must not claim "100%
    # shared" on the same line as DIVERGENT.
    fa = tmp_path / "a.json"
    fb = tmp_path / "b.json"
    fa.write_text(json.dumps(make_envelope("a", text="invoice total four hundred dollars due")))
    fb.write_text(json.dumps(make_envelope("b")))  # zero extracted content
    rc = main(["compare", str(fa), str(fb), "--format", "diffs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DIVERGENT" in out
    assert "content shared by all: 1.00" not in out
