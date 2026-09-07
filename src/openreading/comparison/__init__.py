"""Compare — the cross-backend delta layer.

Every backend extracts slightly differently, which is the observation this package exists for.
Because every result is the same response envelope, comparison is a pure function over N
schema-valid responses: ``compare([response, ...], *, baseline=None, truth=None) ->
ComparisonReport``, a read-only, deterministic, offline function that classifies every field,
block and run fact as agreeing, disagreeing, missed, unique, or honestly not-capable. No
adapter, router, engine or response-schema change was needed to add it.

Laws (non-negotiable)
---------------------
- L1 purity: compare never executes, retries, cancels or bills. Acquiring responses and comparing
  them are separate acts, and the library function is always pure. One surface composes them as
  sugar, the CLI's ``compare <doc> --backends`` fan-out. That surface spends, and the report
  builder never does.
- L2 adjacency (leaf): the adjacency rule is "this package + one CLI verb + one pure server
  endpoint; deleting the package changes nothing else". The load-bearing half holds as shipped:
  nothing in ``router/`` or ``strategies/`` imports it (``strategies/plain.py`` only names it in
  a docstring), and it imports no adapter/router/engine execution path. Its cross-package
  imports are the vendored validators (``openreading.schemas``), the registry (a lazy, local
  ``make_adapter(id).descriptor`` read -- any failure means "unknown", never a raise),
  ``openreading.evals.scorers``, the corpus-report models in ``openreading.types.batch`` and the
  GFM cell escape ``openreading.derive.tables._esc_pipe``. The "one verb + one endpoint" half is
  wider than the spec says: beyond ``openreading compare`` / ``POST /v1/compare`` and the
  ``openreading.compare`` re-export, ``cli explain`` renders a report via ``render_table``,
  ``evals.leaderboard`` reads ``report._NON_DETERMINISTIC``, ``testing.conformance`` (C11)
  reuses ``align.token_similarity``, so deleting the package would break those too. None of them
  feeds a run (L3). The single schema integration point
  (``orchestration.candidates[]``) is an opt-in additive key inside a block that is already
  ``additionalProperties: true``.
- L3 no influence: report output never feeds routing, ranking, gating or ``pick: best``. ``pick``
  decides during a run; compare explains after it. No feedback loop is built here and none is
  ambient, so a comparison can never widen the compliance-eligible set.
- L4 determinism: same inputs => byte-identical report. No timestamps, randomness, network or
  LLM; fixed iteration order (page asc, reading_order asc, subjects in given order); fixed
  documented thresholds. Determinism is what makes drift detection on top of it sound.
- L5 one metric stack: ``evals.scorers`` owns metrics (``text_similarity``, ``field_prf``,
  ``table_grid``, ``canonical_text``); compare imports, never re-implements. Truth mode IS the
  evals scorer applied per subject -- no second scoring system to drift.
- L6 honesty: a backend that cannot produce a dimension (per descriptor and its response
  ``warnings[]``, e.g. ``confidence_unavailable``) is ``not_capable``, never ``missed``; the
  report carries a warning instead of a fabricated value. Compare states what differs and
  crowns no winner unless the user supplies a baseline or truth; consensus is labelled consensus.
- L7 zero new deps: stdlib only (``difflib``, ``json``, ``math``) -- no numpy/pandas/rapidfuzz.

Naming: the verb/function is ``compare``; the package is ``comparison`` because a package named
``compare`` would be shadowed by the ``openreading.compare()`` function exported from the top-level
``__init__`` (attribute-vs-submodule collision). Output = "comparison report"; the two-subject
rendering = "delta view".

Acquisition (three modes; all reduce to the first)
--------------------------------------------------
a. Offline files (the primitive): ``openreading compare a.json b.json [c.json ...]`` -- responses
   from any surface, machine or time. Subject ``source`` = ``file``.
b. Fan-out sugar: ``openreading compare doc.pdf --backends a,b,c`` runs N independent DIRECT
   parses (plain driver per backend: no strategy engine, gates, keep-best or re-ranking), then
   compares. Fan-out is SERIAL: deterministic subject order and one hosted call in flight, so a
   wide ``--all-ready`` (every backend the readiness table reports ready) cannot stampede provider
   rate limits; there is no concurrency flag. ``--save-dir DIR`` writes ``DIR/<backend-id>.json``
   so mode (a) can replay the comparison forever. ``--deadline`` applies to every fanned-out
   backend exactly as ``parse --deadline`` does. Hosted backends are called once each and the
   scoreboard shows each subject's ``usage`` counters, never a price; fan-out is an explicit user
   act, so there is no budget machinery. ``source`` = ``fanout``.
c. From a strategy run (the one integration point): ``parse --keep-candidates`` (Python param
   ``keep_candidates=True`` on ``run``/``run_request``/``run_strategy``; server: a
   ``"keep_candidates": true`` key in the ``POST /v1/parse`` body) retains every completed
   non-winner branch's full normalized envelope under ``orchestration.candidates[]`` as
   ``{backend, node, category, response}``; ``openreading compare --from resp.json`` then takes
   winner + candidates as subjects (``source`` = ``candidate``). Default off (payload bloat); a
   direct, non-strategy run is byte-identical with or without the flag. Compliance: candidates
   only ever hold output from backends the run was already allowed to execute -- retention
   widens nothing. Attempts that errored before normalization have nothing to retain.

Subjects may repeat a backend id (same backend, two runs -- the drift check). Labels
disambiguate (``ingest.load_subjects``): the first subject with a backend id keeps the bare id; a
colliding subject is labelled ``<id> (<backend.version>)`` when it carries a version and that
label is not already taken (aggregators hide the engine in ``backend.version``, so id+version keeps
the engines distinguishable), otherwise ``<id>#N`` by input order. An empty backend id falls back
to the file stem only when a Python caller passed a path (the CLI hands over parsed dicts in every
mode, so the stem never appears there), else the literal ``subject``.

The four dimensions
-------------------
A. Run facts -> the scoreboard (always available, pure envelope reads, never degraded): per
   subject ``status.state``, ``usage.duration_ms``, ``usage.pages_processed``,
   page/block/char counts, ``typed_fields`` count, warning codes,
   ``backend.type`` and ``output_paradigm``.
B. Field delta (``typed_fields``): union of keys, one row per key with per-subject value,
   confidence and presence. Key matching: exact, then normalized (casefold, strip non-alnum).
   Value-equivalence ladder -- first matching tier wins, deterministic, a fixed tested table,
   not configurable and never an LLM: ``exact`` -> ``normalized`` (whitespace/case) -> ``money``
   (``$4,400.00`` == ``4400.00 USD``) -> ``number`` (``1,234`` == ``1234``) -> ``date`` (ISO-8601
   vs common formats). Row verdicts: ``agree`` | ``disagree`` | ``partial`` | ``unique`` |
   ``not_capable``. Answers "which fields got missed" directly.
C. Text delta: ONE canonical-text derivation reused from evals (``scorers.canonical_text``:
   ``document.text`` -> ``document.markdown`` -> blocks concatenated in reading order).
   Pairwise token similarity via ``difflib.SequenceMatcher``; N-way = the pairwise matrix plus
   per-pair unique-char counts. ``--format diff`` renders a unified diff for two subjects.
D. Structural delta (blocks + tables): per page, align blocks (below), then classify: matched
   (``type_conflict`` when one says ``title`` and another ``text``; ``confidence_gap`` on matched
   pairs), ``block_missed`` (some but not all capable subjects have it -- majority or not; a 2-of-4
   split or a 3-of-6 tie is ``block_missed`` for the subjects lacking it exactly like a strict
   majority), ``block_unique`` (a lone singleton). Tables: grid-shape comparison plus cell-level
   equivalence via ``scorers.table_grid``. Subjects with no blocks at all (markdown-only
   backends) are ``not_capable`` for D, excluded from its math, and get a ``blocks_unavailable``
   warning -- never fabricated geometry.

Alignment (``openreading.comparison.align``)
--------------------------------------------
Backends disagree on segmentation, not just content (one paragraph vs three lines; paradigms
``block_tree`` / ``element_list`` / ``block_graph``). Pages align by ``page_number``; a count
mismatch is finding ``page_count_mismatch`` and unpaired pages compare against nothing (all their
blocks unique). Blocks align text-first, geometry-validated (text exists for every block, bboxes
may not):

1. normalize block text: casefold, collapse whitespace, strip punctuation;
2. pairwise token-similarity scores; accept greedily by descending score, subject order as the
   deterministic tie-break, floor ``TAU_TEXT = 0.60``;
3. when both sides carry canonical ``[0,1]`` top-left bboxes, IoU validates: a text match with
   same-page IoU < ``IOU_MIN = 0.30`` is demoted to a ``position_conflict`` finding, and IoU breaks
   score ties;
4. granularity merge: before matching, on the finer side greedily concatenate runs of adjacent
   same-type blocks (reading order, lookahead ``MERGE_LOOKAHEAD = 4``) when the concatenation
   scores strictly higher against a coarser block than any constituent; merged groups match as
   one and provenance keeps constituent indices;
5. unmatched blocks become ``block_missed`` / ``block_unique`` per dimension D.

Thresholds are module constants, not flags. Threshold flags are not built, so retune them
against evidence in code rather than per run.
The report's ``alignment`` object records ``{method: "text_first/v1", tau_text, iou_min,
merge_lookahead, capable_subjects}`` plus ``unaligned_ratio`` whenever alignment was attempted,
so a consumer can judge how far to trust dimension D; ``unaligned_ratio`` is a first-class honesty
signal, not a failure. When fewer than two subjects are block-capable the key is OMITTED rather
than defaulted to ``0.0`` (which would misread as "perfectly aligned"); human renderers print
``n/a`` for it. Two subjects
align pairwise-symmetric (exact, order-invariant); N > 2 anchors on the first block-capable
subject and aligns every other against it (DECISIONS D-v4-13: symmetric N-way clustering that
also honours the granularity merge is materially more complex, so for N > 2 block-level
order-invariance holds only up to the anchor; facts/fields/text stay order-invariant for all N).

Stances (one report shape)
--------------------------
- Symmetric (default): no subject privileged. At N >= 3 a ``consensus`` section gives per-field
  majority values and names outliers; majority = strict (> N/2 of CAPABLE subjects). A field with
  no majority still gets a row: ``has_majority: false``, ``majority_value: null``, every present
  subject in ``outliers``.
- Baseline: ``--baseline <label|path>`` (a path is added as an extra subject) signs every field
  delta against one subject -- match / differ / missing / extra.
- Truth: ``--truth golden.json`` (the evals dataset ``expected`` shape) runs ``scorers.score()``
  verbatim per subject -- field precision/recall/F1, text similarity, table grid -- side by side.

The report -- ``comparison-report.v0.2.json``
--------------------------------------------
A vendored, versioned JSON Schema in ``openreading.schemas`` (``validate_comparison_report``);
every report is validated before it is returned. Top level: ``schema_version`` ("0.2"),
``mode`` (``pairwise`` iff exactly 2 subjects, else ``nway``), ``subjects[]`` (``label``,
``backend``, ``source: file|fanout|candidate``, ``facts``), ``alignment``, ``headline``,
``fields.rows[]`` (``key``, ``verdict``, ``by_subject.<label>.{present, value, confidence,
equivalence}``), ``text`` (``matrix`` in subject order + ``pairs[]`` with ``similarity`` and
``a_only_chars``/``b_only_chars``), ``blocks.pages[]`` (``page``, ``matched``, ``findings``),
``findings[]`` (``code``, ``severity: info|warn|major``, ``page``, ``field``, ``bbox``,
``subjects``, ``detail``, and for structural findings a ``snippet`` -- a short excerpt of the block
text plus its confidence, so an OCR error reads as one without opening the source responses),
``warnings[]``, and stance-dependent ``consensus`` / ``baseline`` / ``truth``. ``findings[]`` is
the flat, severity-sorted view of everything the dimensional sections detail.

Finding codes (closed set): ``field_value_conflict`` ``field_missed`` ``text_divergence``
``block_missed`` ``block_unique`` ``structure`` (packaging/granularity difference, not content
loss) ``type_conflict`` ``position_conflict`` ``table_shape_mismatch`` ``confidence_gap``
``page_count_mismatch`` ``empty_output``.
Warning codes: ``blocks_unavailable``; ``descriptor_unavailable`` (backend id unknown to the
local registry => capability checks fall back to observed output, stated openly: ``fields`` is
whether the subject actually produced ``typed_fields``, and ``blocks`` / ``confidence`` are ASSUMED
capable so genuine misses still surface -- an unknown backend is never quietly ``not_capable``);
``confidence_unavailable`` (propagated); ``subjects_identical``. With a known descriptor,
``fields`` = produced ``typed_fields`` OR the descriptor claims key-value / custom-schema
extraction (``openreading.comparison.capabilities``).

``headline`` is the content-first summary: per guaranteed channel (``text``, ``typed_fields`` when
any subject has them, ``table_cells`` when any subject has a table) an ``agreement`` of
``agree`` / ``diverge`` / ``partial``, rolled into ``verdict`` ``equivalent`` (all agree) /
``divergent`` (all diverge) / ``mixed``. ``table_cells`` is ``partial`` when fewer than two
subjects are block-capable, because ``table_shape_mismatch`` could not have fired and "agree"
would be unverified. Non-determinism rule: the envelope carries no determinism signal and
``output_paradigm`` is not a reliable proxy, so ``report._NON_DETERMINISTIC`` names the generative
backends by id; a content finding involving one of them caps at ``info`` (run-to-run drift is
indistinguishable from a real difference) and the labels are listed in
``headline.nondeterministic_subjects``.

Deliberately ignored: ``chunks``, ``backend_raw``, per-page provenance of page-granularity
strategy runs (each envelope is one subject, whole). Reports embed extracted content x N
subjects -- the same sensitivity as responses, multiplied; community reports are local files.

Surfaces
--------
CLI (``openreading compare``)::

    openreading compare r1.json r2.json [r3.json ...]        # mode (a): pure, offline
    openreading compare doc.pdf --backends a,b,c            # mode (b): serial fan-out
    openreading compare doc.pdf --all-ready --save-dir out/ # every ready backend, saved
    openreading compare --from resp.json                    # mode (c): winner + candidates
      [--baseline <label|path>] [--truth golden.json] [--deadline SECS]
      [--format json|table|diff|diffs|md]                   # default json (schema-valid)
      [--show-agreements]                                   # human formats hide agreements

Delta-first: human formats show only differences by default -- the user asked for the delta,
agreement is noise (``--show-agreements`` restores it). ``--format diff`` (singular) is the 2-way
git-style unified text diff plus differing field rows, valid for exactly two subjects.
``openreading explain report.json`` dispatches on report shape (``subjects`` + ``fields`` +
``findings``) and renders a saved report like a strategy trace; ``--format table|md`` is sugar
over the same renderer (``openreading.comparison.render``).

Exit codes: ``0`` report produced; ``1`` a fanned-out backend raised an unexpected error; ``2``
misuse (< 2 subjects or backends, unknown backend, fan-out over more than one document,
``diff`` with != 2 subjects, mixed batch + single subjects); ``3`` fan-out cannot run (missing
credentials, retry exhaustion, unsupported feature -- same semantics as ``parse``); ``5``
unreadable or schema-invalid inputs, or ``--from`` on a run that kept no candidates (the error
names the fix: re-run with ``--keep-candidates``).

``--format diffs`` (plural, ``render.render_diffs``) answers "what actually differs, and does it
matter?" by separating the two axes a raw finding list conflates -- content (did anyone miss
real text?) from structure (how the same content is packaged). Four sections:

1. CONTENT: a packaging-immune token-coverage verdict (``EQUIVALENT`` / ``DIVERGENT`` + a
   "shared by all" overlap score). A line counts as present in a subject when >= 80% of its
   tokens appear there (``deltas._COVER``), so a backend that flattens a table onto one line vs
   one that splits it into rows is not a false miss, and reordering never fabricates a delta.
2. TABLES: each table's grid shape per subject + who flattened it (``structure.table_deltas``).
3. TYPES: blocks the subjects label differently (``type_conflict`` findings).
4. GRANULARITY: block counts -- who over-fragments (about one block per cell).

Field deltas follow when typed fields disagree. A long finding list collapses to one screen.

Python: ``openreading.compare(inputs, *, baseline=None, truth=None) -> dict`` (dicts or paths)
joins ``run``/``route`` in the top-level ``__all__``. There is no fan-out in the Python API; a
Python user composes ``run()`` calls with their own concurrency and error handling -- there is
no second execution API to maintain. Fan-out exists only as CLI sugar (L1).

Server: ``POST /v1/compare`` with ``{"responses": [...], "baseline"?, "truth"?}`` returns the
report. It is pure (L1) and never executes a backend. Fan-out over the JSON API is not offered.

Corpus compare -- two batch runs
--------------------------------
When EVERY subject is a batch-result envelope (from ``parse <dir>``), ``compare`` switches to
corpus mode (``openreading.comparison.corpus``): documents pair across runs by identity
precedence ``relpath`` -> ``filename`` -> ``sha256``; each paired document gets its own
``comparison-report.v0.2`` and the verdicts roll up into the vendored ``corpus-report.v0.1.json``.
Per-document verdict: ``equivalent`` / ``divergent`` / ``mixed`` (from the document headline) or
``unpaired`` (present in some runs, not all -- surfaced, never a crash); pairs are ordered by
identity key. ``--format table|md`` = one verdict line per document + rollup. ``--format diffs``
is value-first: rollup, one line per document, and under each divergent one the actual content
each backend captured that the other missed (``content_deltas.unique``, token-coverage matched),
capped at about 8 lines per side with a "... N more" tail -- values, not counts or structure,
because that is what exposes an OCR misread or a lopsided values split at a glance. Structure
(tables/types/granularity) stays in ``--format table`` and the single-pair four-section view: a
corpus-wide four-section diff is a wall. The footer prints a ``jq`` one-liner to dump one
document's full text; ``parse <dir> --save-dir`` per backend then ``compare outA/<doc>.json
outB/<doc>.json --format diffs`` drills one document. ``--format diff`` is rejected in corpus mode
and mixing batch-result and single-response subjects is a usage error (both exit 2).

Candidate retention scope (DECISIONS D-v4-14)
---------------------------------------------
``--keep-candidates`` captures every completed non-winner branch at the parallel/race/ensemble/
merge collection point (raced losers, judged losers, merge sources, shadows) -- the constructs
whose purpose is to run N backends and keep one. Superseded serial-cascade rungs are NOT retained:
threading capture through every leaf/rung of the engine walk is materially more invasive, and the
primary "compare what the run discarded" case is the race/ensemble. Cascade rungs still expose
their trail via ``orchestration.attempts[]``. The flag is an explicit Python parameter, never a
request-schema field: the vendored request schema is ``additionalProperties: false``, so the
server pops ``keep_candidates`` from the body before validation.

What compare is NOT
-------------------
- not a picker (``parallel: pick: best`` decides during a run; compare explains after);
- not a scorer-against-truth (the ``evals`` harness is; truth mode is the bridge that imports it);
- not a router input (L3 -- no ambient feedback loop);
- not a quality oracle (symmetric mode reports difference, not correctness -- bring a baseline
  or a golden);
- not a storage/history system (this package compares what you hand it, statelessly).

The private company repo builds on top of this package, never instead of it. It holds a run
store with drift detection keyed by document hash, backend and adapter version. It holds a
visual bbox-overlay delta view, disagreement-driven labelling (disagreements are the
highest-value annotation targets and grow golden datasets), an LLM equivalence judge that
stays inert without an explicit operator gate, and alignment calibration against adjudicated
corpora.

Deliberately deferred (recorded so they are not relitigated ad hoc): chunk-level comparison;
page-provenance-aware comparison of ``granularity: page`` runs; threshold flags; ``compare``
inside a strategy YAML (a ``compare:`` node would be orchestration, not observability, and would
arrive via the strategies spec process); streaming/incremental comparison; cross-document
comparison (same backend, different docs -- evals territory).

Design authority: internal/decisions/DECISIONS.md (D-v4-13, D-v4-14).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .ingest import CompareInputError, Subject, load_subjects
from .report import build_report

__all__ = ["CompareInputError", "Subject", "compare", "load_subjects"]


def compare(
    inputs: Iterable[Any],
    *,
    baseline: Any = None,
    truth: Any = None,
) -> dict[str, Any]:
    """Compare N response envelopes (dicts or paths to response JSON) and return a schema-valid
    ComparisonReport. Pure — never runs, retries, cancels, or bills anything.

    `baseline` (a subject label or an extra response — dict or path) signs every field delta
    against one subject; `truth` (an evals `expected` dict or path) scores each subject with the
    shared evals scorer. Symmetric (neither given) adds a consensus section at N≥3.
    """
    from .stances import load_truth, resolve_baseline

    subjects = load_subjects(inputs)
    baseline_label = resolve_baseline(baseline, subjects) if baseline is not None else None
    truth_expected = load_truth(truth) if truth is not None else None
    return build_report(subjects, baseline_label=baseline_label, truth=truth_expected)
