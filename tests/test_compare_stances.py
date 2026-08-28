"""M3b — the three stances (DESIGN §6): symmetric consensus (N≥3), baseline signing, truth
scoring — plus the cost_outlier finding."""

from __future__ import annotations

import itertools
import json

from openreading.comparison import compare
from tests.fakes import make_envelope

# --- consensus (symmetric, N ≥ 3) -------------------------------------------------------


def test_consensus_absent_for_two_subjects() -> None:
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$5"})
    assert "consensus" not in compare([a, b])


def test_consensus_majority_and_outlier() -> None:
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": "100 USD"})  # equivalent to a → majority of 2/3
    c = make_envelope("c", fields={"Total": "$999"})  # the outlier
    report = compare([a, b, c])
    assert "consensus" in report
    row = next(r for r in report["consensus"]["fields"] if r["key"] == "Total")
    assert row["has_majority"] is True
    assert row["majority_count"] == 2
    assert row["outliers"] == ["c"]


# --- N-way transitivity (BL-45) ------------------------------------------------------


def test_consensus_never_false_agreement_for_named_code_triple() -> None:
    # "100 USD" / "100 EUR" / "100" are three genuinely different values (two real, differing
    # currencies plus a signal-less bare number) — no permutation of who is compared first may
    # cluster all three into one false "everyone agrees" majority.
    values = {"p": "100 USD", "q": "100 EUR", "r": "100"}
    for order in itertools.permutations(values):
        envs = [make_envelope(label, fields={"Total": values[label]}) for label in order]
        report = compare(envs)
        row = next((r for r in report["consensus"]["fields"] if r["key"] == "Total"), None)
        assert not (row is not None and row["majority_count"] == 3 and row["outliers"] == [])


def test_symbol_and_bare_triple_never_falsely_agrees() -> None:
    # Joint gate (BL-45): "$100" and "€100" are genuinely different currencies once normalized to
    # ISO codes, and "100" is signal-less. Checked across the two orderings where the ambiguous
    # bare value sorts first — proves the money-tier fix and the aggregation fix hold
    # together, not just individually.
    #
    # The bare value genuinely equivalence-matches EITHER currency on its own (BL-36, unchanged),
    # so complete-linkage clustering correctly forms a real 2-of-3 majority (bare + whichever
    # currency it's compared against first) with the other currency as a genuine outlier — that is
    # not the bug. The bug this item fixes is the OLD single-linkage behavior: a false, UNANIMOUS
    # 3-of-3 majority with zero outliers, silently pairing two different currencies with each
    # other through the bare value as a bridge. So the invariant to prove is "never a false
    # unanimous agreement with an empty outlier list" (matching this item's own acceptance
    # criteria), not "never any majority at all."
    values = {"dollar": "$100", "euro": "€100", "bare": "100"}
    ambiguous_value_first = [
        ("bare", "dollar", "euro"),
        ("bare", "euro", "dollar"),
    ]
    for order in ambiguous_value_first:
        envs = [make_envelope(label, fields={"Total": values[label]}) for label in order]
        report = compare(envs)
        row = next(r for r in report["fields"]["rows"] if r["key"] == "Total")
        assert row["verdict"] == "disagree"
        consensus_row = next(
            (r for r in report["consensus"]["fields"] if r["key"] == "Total"), None
        )
        assert consensus_row is not None
        assert consensus_row["majority_count"] == 2
        assert consensus_row["capable"] == 3
        assert consensus_row["outliers"] != []
        assert "dollar" in consensus_row["outliers"] or "euro" in consensus_row["outliers"]


def test_disagree_row_never_shows_full_by_subject_agreement() -> None:
    # BL-52: `all_equiv` (driving the row's own `verdict`) has been complete-linkage since BL-45,
    # but `by_subject[label]["equivalence"]` stayed anchored to `present[0]` alone — so a
    # "disagree" row could show every subject as "matched" (each individually equivalence_tier
    # -matches the arbitrary first-listed reference, even though not each other). Reuses the same
    # dollar/euro/bare fixture as `test_symbol_and_bare_triple_never_falsely_agrees` above, across
    # every subject ordering, so the invariant holds regardless of who happens to be listed first.
    values = {"dollar": "$100", "euro": "€100", "bare": "100"}
    for order in itertools.permutations(values):
        envs = [make_envelope(label, fields={"Total": values[label]}) for label in order]
        report = compare(envs)
        row = next(r for r in report["fields"]["rows"] if r["key"] == "Total")
        assert row["verdict"] == "disagree"
        assert any(bs["equivalence"] is None for bs in row["by_subject"].values())


# --- no-consensus outcomes (BL-68) ---------------------------------------------------


def test_consensus_row_present_for_three_way_split_with_no_bridging_value() -> None:
    # Three genuinely different values — no pair equivalence-matches under any tier (not exact,
    # not normalized, not money/number/date), so no cluster can bridge to a majority. Before this
    # fix, the field silently vanished from `consensus.fields` entirely; the no-majority outcome
    # is exactly the case this section exists to surface, so the row must still appear.
    a = make_envelope("a", fields={"Status": "Approved"})
    b = make_envelope("b", fields={"Status": "Pending"})
    c = make_envelope("c", fields={"Status": "Rejected"})
    report = compare([a, b, c])
    row = next((r for r in report["consensus"]["fields"] if r["key"] == "Status"), None)
    assert row is not None  # the row used to be omitted entirely — this is the bug itself
    assert row["has_majority"] is False
    assert row["majority_value"] is None
    assert row["majority_count"] == 1
    assert row["capable"] == 3
    assert sorted(row["outliers"]) == ["a", "b", "c"]


def test_consensus_for_one_of_capable_subjects_reported_field_others_missed() -> None:
    # Extends test_compare_core.py::test_partial_when_capable_subject_absent's own fixture (three
    # subjects; a and b agree on Total; c is field-capable — it reported Other — but missed Total
    # entirely) to assert on `consensus`, where today that fixture only asserts on `fields.rows`.
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$5"})
    c = make_envelope("c", fields={"Other": "z"})  # capable (produced a field), missed Total
    report = compare([a, b, c])

    # Total: a real majority still stands (a and b agree); the absent-but-capable c is not folded
    # into outliers merely for not having reported the field at all.
    total = next(r for r in report["consensus"]["fields"] if r["key"] == "Total")
    assert total["has_majority"] is True
    assert total["majority_value"] == "$5"
    assert total["majority_count"] == 2
    assert total["capable"] == 3
    assert total["outliers"] == []

    # Other: the reverse shape — one of three capable subjects reported a field the other two
    # missed entirely. No cluster can reach a majority of 3, so (as above) the no-majority outcome
    # must be explicit rather than the row vanishing from the report.
    other = next(r for r in report["consensus"]["fields"] if r["key"] == "Other")
    assert other["has_majority"] is False
    assert other["majority_value"] is None
    assert other["majority_count"] == 1
    assert other["capable"] == 3
    assert other["outliers"] == ["c"]


def test_consensus_two_null_reporters_never_land_in_outliers() -> None:
    # BL-109: two subjects that both explicitly report null for the same field are the clearest
    # possible non-conflict — before the fix, equivalence_tier(None, x) being always-None meant
    # neither null value ever clustered with anything, not even with each other (equivalence_tier
    # (None, None) is also None, the ladder's own "incomparable" contract), so both landed in
    # outliers exactly like a genuine disagreement would. The fix excludes null-but-present values
    # from this field's present/outlier machinery entirely, so b and c disappear from the row's
    # outliers together rather than being scattered into it.
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": None})
    c = make_envelope("c", fields={"Total": None})
    report = compare([a, b, c])
    row = next(r for r in report["consensus"]["fields"] if r["key"] == "Total")
    assert "b" not in row["outliers"]
    assert "c" not in row["outliers"]
    assert row["outliers"] == ["a"]  # the lone real value has no majority of its own to join
    assert row["capable"] == 3


# --- baseline ---------------------------------------------------------------------------


def test_baseline_null_value_reads_missing_not_differ() -> None:
    # BL-109 mirror: a baseline's real value against another subject's explicit null must read
    # "missing" — the same treatment an omitted key already gets correctly — never "differ", which
    # is the identical wording a genuine value mismatch produces.
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": None})
    report = compare([a, b], baseline="a")
    row = next(r for r in report["baseline"]["fields"] if r["key"] == "Total")
    assert row["by_subject"]["b"] == "missing"


def test_baseline_by_label() -> None:
    a = make_envelope("reducto", fields={"Total": "$5", "Date": "2024-01-01"})
    b = make_envelope("pymupdf", fields={"Total": "$5"})  # missing Date
    report = compare([a, b], baseline="reducto")
    assert report["baseline"]["baseline"] == "reducto"
    total = next(r for r in report["baseline"]["fields"] if r["key"] == "Total")
    date = next(r for r in report["baseline"]["fields"] if r["key"] == "Date")
    assert total["by_subject"]["pymupdf"] == "match"
    assert date["by_subject"]["pymupdf"] == "missing"


def test_baseline_extra_value() -> None:
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$5", "Bonus": "z"})
    report = compare([a, b], baseline="a")
    bonus = next(r for r in report["baseline"]["fields"] if r["key"] == "Bonus")
    assert bonus["by_subject"]["b"] == "extra"


def test_baseline_as_extra_response() -> None:
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$9"})
    golden = make_envelope("golden", fields={"Total": "$5"})
    report = compare([a, b], baseline=golden)  # appended as a subject
    assert report["baseline"]["baseline"] == "golden"
    assert len(report["subjects"]) == 3


# --- truth ------------------------------------------------------------------------------


def test_truth_scores_each_subject() -> None:
    a = make_envelope("a", fields={"Total": "$5"}, text="hello world")
    b = make_envelope("b", fields={"Total": "$9"}, text="hello there")
    report = compare([a, b], truth={"typed_fields": {"Total": "$5"}, "text": "hello world"})
    assert set(report["truth"]["by_subject"]) == {"a", "b"}
    # a matches the golden Total exactly; b does not → a scores higher on field recall
    fa = report["truth"]["by_subject"]["a"]["dimensions"]["field_prf"]["f1"]
    fb = report["truth"]["by_subject"]["b"]["dimensions"]["field_prf"]["f1"]
    assert fa > fb


def test_truth_from_path(tmp_path) -> None:
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$5"})
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"typed_fields": {"Total": "$5"}}))
    report = compare([a, b], truth=str(golden))
    assert report["truth"]["dimensions"] == ["typed_fields"]


def test_truth_with_empty_golden_scores_none_not_false_perfect() -> None:
    # An empty golden.json names none of the five scored dimensions — an ordinary "not labeled
    # yet" shape, not a claim that every subject is perfect (BL-79). Two subjects with
    # genuinely, mutually-disagreeing garbage text must both come back honestly unscored.
    a = make_envelope("a", text="totally garbled unreadable output nothing like the source")
    b = make_envelope("b", text="a completely different garble, disagreeing with a entirely")
    report = compare([a, b], truth={})
    assert report["truth"]["by_subject"]["a"]["overall"] is None
    assert report["truth"]["by_subject"]["b"]["overall"] is None


def test_truth_all_correct_with_tables_expected_empty_scores_overall_one_not_half() -> None:
    # BL-95: two subjects, both genuinely correct on every checked axis — the needle text is
    # present and neither hallucinated a table (truth's tables: [] correctly asserts "none
    # expected"). Before the fix, the tables branch fell through to 0.0 regardless, dragging a
    # perfect response's overall down to 0.5 via truth_section's real compare --truth path.
    a = make_envelope("a", text="hello world")
    b = make_envelope("b", text="hello world, again")
    report = compare([a, b], truth={"text_contains": ["hello world"], "tables": []})
    assert report["truth"]["by_subject"]["a"]["overall"] == 1.0
    assert report["truth"]["by_subject"]["b"]["overall"] == 1.0


# --- cost_outlier -----------------------------------------------------------------------


def test_cost_outlier_finding() -> None:
    a = make_envelope("a", text="x", cost_usd=0.01)
    b = make_envelope("b", text="x", cost_usd=0.01)
    c = make_envelope("c", text="x", cost_usd=0.50)  # ≫ 3× the others' mean
    report = compare([a, b, c])
    outlier = next(f for f in report["findings"] if f["code"] == "cost_outlier")
    assert outlier["subjects"] == ["c"]


def test_no_cost_outlier_when_comparable() -> None:
    a = make_envelope("a", text="x", cost_usd=0.01)
    b = make_envelope("b", text="x", cost_usd=0.012)
    assert not any(f["code"] == "cost_outlier" for f in compare([a, b])["findings"])
