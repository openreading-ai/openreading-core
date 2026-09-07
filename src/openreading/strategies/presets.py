"""The four vendored built-in presets, and the strategy cookbook (executable truth).

Presets are ordinary strategies pre-registered under reserved names (`PRESET_NAMES`). They are
held here as raw nodes; `openreading.strategies.normalize` expands them like any user strategy.
Contract:

* `openreading strategy show <name>` prints a preset's exact vendored longhand for forking (a
  user strategy prints as written, and Plain prints Plain). `--longhand` prints the canonical
  tree for either, and `openreading strategy normalize` prints the whole config's strategies in
  canonical longhand (`normalize_config`). Forking a preset means pasting that printout into
  your own file under a new name. `extends:` is designed but not accepted in a file
  (`openreading.strategies.model` §2.8).
* A user config that defines a strategy under a preset's name is rejected the first time it is
  normalized — `strategy validate`, `show --longhand`, `strategy normalize`, or a run — by
  `build_library` in `openreading.strategies.normalize`, a `NormalizeError`: "strategy
  'cost_saver' collides with a built-in preset. Rename it, then run `openreading strategy show
  cost_saver` to copy the preset body into your file". The loader's schema gate and desugar pass
  do not reject it, and neither does a bare `strategy show <name>`: that path
  (`openreading.cli.app._strategy_body_as_written`) re-reads the source file and prints the body
  without normalizing, so the collision surfaces only on the normalizing paths. Silent shadowing
  would make one name mean two trees depending on which file loaded.
* `escalate_if: default` is `DEFAULT_BUNDLE` in `openreading.strategies.normalize` —
  `{scanned_pages_detected: true, garbled: true, empty_pages_over: 0.2, confidence_below: 0.6}`,
  copied onto every cascade step except the last. `confidence_below` is inapplicable on a backend
  that reports no confidence and is traced as skipped (`signal_unavailable`), never guessed.
* Presets stay vendored in the advanced grammar and are never re-spelled in Plain: each carries
  an `intent:` line (a live semantic surface for deciders and UIs) that Plain deliberately cannot
  spell. The Plain near-equivalents below share each preset's structure (node kind, backend
  order, pick) — `tests/test_strategy_plain.py` proves it — but the gates differ: `intent:` is
  dropped, and Plain's `looks_bad` scan member is result-aware (`openreading.strategies.plain`)
  where the bundle's `scanned_pages_detected` is a bare input-side fact. Hence "≈", not "=".

    cost_saver    ≈ try: [pymupdf, docling, aws-textract]
                    escalate_when: {looks_bad: true, low_confidence: true}
    max_accuracy  ≈ try: [aws-textract, azure-document-intelligence] + the same escalate_when
    offline_first ≈ try: [pymupdf, tesseract, docling] + the same escalate_when
    fast          ≈ race: [pymupdf, tesseract]

`offline_first` orders local backends only, but *enforcement* of "never leave the machine" is
`policy: { backends: [...] }` — the allow-list lives outside the tree and a strategy can never
widen it.

Cookbook
========

Twelve complete examples of `openreading.yaml`. Every fenced YAML block below is checked by
`tests/test_docs_truth.py`. The test parses each block and runs the world-consistency half of
`strategy validate`, so an edit that breaks a cross-reference fails `make verify`. It does not
run the loader's JSON-Schema gate, which is the separate check on the file's grammar. Fragments
showing only `strategies:` belong in a file with `version: 1` at the top. Invoke a named
strategy with `backend.id: "strategy:<name>"`, CLI `--strategy <name>`, or
`openreading.run(..., strategy="<name>")`. Where a shorter Plain spelling exists
(`openreading.strategies.plain`) it follows the longhand; it desugars to that longhand.
Execution laws cited here live in `openreading.strategies.engine`; the grammar in
`schemas/strategy-config.v0.2.json` and `openreading.strategies.normalize`.

1. The headline: free local first, paid rung only when quality demands it
--------------------------------------------------------------------------

Try pymupdf (free, milliseconds) first; escalate to reducto only when the cheap result looks bad.

```yaml
version: 1

strategies:
  cheap_first:
    steps: [pymupdf, reducto]
    escalate_if: default
```

Plain spelling (`looks_bad` is the always-available half of the `default` bundle; add
`low_confidence: true` to match it member-for-member):

```yaml
version: 1
strategies:
  cheap_first:
    try: [pymupdf, reducto]
    escalate_when: looks_bad
```

What happens: normalization copies the `default` bundle onto the pymupdf step (the final step
stays ungated). On a digital PDF pymupdf passes and is accepted — reducto never runs at all.
On a scan `scanned_pages_detected` fires: the pymupdf result is retained as `Deficient` (attempt
category `quality_escalated`), reducto runs and is accepted, and the response carries a
`quality_escalated` warning. The bundle's `confidence_below: 0.6` is skipped on pymupdf.

The same strategy with tuned thresholds:

```yaml
strategies:
  cheap_first:
    steps:
      - backend: pymupdf
        escalate_if:
          chars_per_page_below: 100      # keys OR: any reason to distrust => move on
          garbled: true
          scanned_pages_detected: true
      - reducto
```

All three tuned predicates are Tier-1 (engine-computed, always available), so this gate can
never be unbindable. When any fires, pymupdf is retained as `Deficient` (`quality_escalated`)
and reducto — the ungated final step — runs and is accepted.

2. Run two hosted backends and keep the better output
-----------------------------------------------------

```yaml
strategies:
  hosted_duel:
    parallel:
      - reducto
      - azure-document-intelligence
    pick: best
```

Plain spelling (`compare` keeps the objectively better):

```yaml
strategies:
  hosted_duel:
    compare: [reducto, azure-document-intelligence]
```

What happens: both branches launch immediately, each dispatching on the ONE per-request adapter
instance `build_registry()` made — never a fresh instance per branch; siblings never share a
credential-bound client only because validation forbids two branches naming the same backend.
`pick: best` waits for all non-shadow branches (default `require: all`), then the engine's
composite score
compares candidates. With no gates on this node the score basis is the default-bundle Tier-1
predicates evaluated over each candidate (comparison is never 0/0); remaining ties break by the
first-listed candidate. The loser records `judged_lost`, because its call reached the vendor
whether or not it won.

The judged variant — a `judge:` block on the same node makes the comparison LLM-judged:

```yaml
    pick: best
    judge:
      backend: anthropic-claude
      intent: Prefer complete, arithmetically consistent line-item tables.
      excerpt_chars: 4000
```

The judge is itself a backend: it must pass the request's stage-1 compliance filter, its call is
recorded as a `judge_call` attempt with its billed cost, and comparison is pairwise in both
orderings. Like every decision point it requires the operator's `OPENREADING_LLM_DECIDER` env
gate (`openreading.strategies.decider`); an ungated, unavailable, or ineligible judge downgrades
to the engine score, the decision record carrying a `downgraded: <reason>` field (the spec calls
this `decider_downgraded`; no warning code or record value of that name is ever emitted) — the
parse never fails because a judge did.

3. Race locals for speed
------------------------

Lowest possible latency; both candidates are free local libraries.

```yaml
strategies:
  fast_local:
    parallel:
      - pymupdf
      - docling
    pick: fastest
    on_win: cancel
```

Plain spelling:

```yaml
strategies:
  fast_local:
    race: [pymupdf, docling]
```

What happens: the first branch to return a *successful* response wins; the loser is cancelled
and recorded as `raced_lost`. Per-branch quality gates are not evaluated in a race — wrap the
race as a gated cascade step for a quality floor (the gate then evaluates the race winner).
Simultaneous completions tie-break by branch index.

4. Failure routing: hosted chain with a local floor
---------------------------------------------------

Transient hosted failures advance down the chain, a corrupt document fails fast, and a local
backend is the floor.

```yaml
strategies:
  resilient:
    steps:
      - reducto
      - azure-document-intelligence
      - docling                      # local floor: no credentials, no network
    escalate_if: off                 # pure failure routing; no quality gates
    on_error:
      invalid_input: fail            # a corrupt doc fails on every backend; never burn the chain
      transient: next                # timeout + rate_limited + provider_error advance
```

What happens: a reducto timeout records `error(timeout)` and advances; azure succeeding makes
the cascade `Ok` with a `fallback_used` warning (recovery). A corrupt document classifies as
`invalid_input` and stops the strategy with the full trail (`TerminalError` at the root).
`missing_credentials` is not routable: an un-credentialed rung is a `skipped(missing_credentials)`
attempt that advances automatically. With `escalate_if: off` this is the serial fallback chain
plus an error map. (`off` unquoted is YAML `false`; the grammar accepts both spellings —
DECISIONS D-v3-22 widened the schema rather than force users to quote the natural form.)

5. Doc-type conditional routing
-------------------------------

Invoices and bank statements on a table-focused strategy, very large PDFs on a big-document
strategy, everything else on a general default.

```yaml
strategies:
  tables_heavy: { steps: [docling, reducto], escalate_if: { table_sanity_below: 0.7, zero_blocks: true } }
  big_docs:     { steps: [pymupdf, aws-textract], escalate_if: default }
  general:      { steps: [pymupdf, reducto], escalate_if: default }

  front_door:
    route:
      rules:
        - when: { doc_type: [invoice, bank_statement] }   # keys AND within one rule
          use: strategy:tables_heavy
        - when: { pages_over: 200, mime: application/pdf }
          use: strategy:big_docs
      default: strategy:general                            # mandatory floor
```

What happens: facts are computed once, before evaluation; rules are first-match-wins, and *all*
rules are still evaluated for the trace (shadowed-rule debugging). A request with
`routing.doc_type_hint: invoice` dispatches to `tables_heavy`. When `doc_type` cannot be
computed that rule simply does not match — its fact record in the route decision reads
`status: "unavailable"` (the spec's name for this, `fact_unavailable`, is never recorded;
`openreading.strategies.facts`) — and `default:` catches the document: an uncomputable fact is
never an error.

6. Hedged start: primary plus a delayed second bet
--------------------------------------------------

reducto runs alone unless it is slow, at which point aws-textract fires as a hedge.

```yaml
strategies:
  hedged:
    parallel:
      - reducto
      - backend: aws-textract
        start_after: 45s             # hedge: launches only if no winner after 45s
    pick: fastest
    on_win: cancel
```

What happens: reducto launches at t=0. If it returns within 45s textract never launches, is
never added to the attempted set, and never costs anything; if not, textract fires. Two spec
promises here are designed, not shipped: the `hedged_start` warning has no emitter (a hedge
launch adds no warning), and a reducto *failure* before 45s does NOT shortcut the remaining
delay — the hedge sleeps the full `start_after` on the shared clock (parallel law 2 in
`openreading.strategies.engine`); only the node resolving first cancels a still-parked hedge. A
hedge whose delay would land past the node deadline is `deadline_pruned`. If both run and
reducto wins, the textract attempt is still recorded (`raced_lost`) because its call reached
AWS, and the response never blocks on the loser's cancellation.

7. Budget-capped best-effort cascade ending in `auto`
-----------------------------------------------------

A hard time wall, an escalation ladder, and "let the router pick something untried" last.

```yaml
strategies:
  best_effort:
    budget: { max_duration: 2m }
    on_quality_exhausted: best_effort    # the default; shown for clarity
    escalate_if: default                 # copied onto every step except the last
    steps:
      - pymupdf
      - docling
      - aws-textract                     # a named rung: every rung names what it runs
```

Plain spelling (`max_time` is the wall; `best_effort` keep-best is the inherited default):

```yaml
strategies:
  best_effort:
    try: [pymupdf, docling, aws-textract]
    escalate_when: looks_bad
    max_time: "2m"
```

What happens: the cascade-level gate is copied onto pymupdf and docling; the final `auto` rung
stays ungated and takes the router's stage-3 best among backends not yet attempted in this walk
(none left → `Err(exhausted)`, reason `no_untried_backend`). On exhaustion — the ladder ran out
or the `max_duration` deadline ended the walk — keep-best returns the best retained `Deficient`
result — ties keep the EARLIEST-retained rung, the comparison being strict-greater (engine Law
4; the cookbook's highest-rung-index tiebreak is not implemented) — carrying
`budget_exhausted` when the deadline ended the walk and `quality_below_threshold` when the ladder
ran out (`engine.run_strategy`): the two are separate codes because escalating to a stronger
backend answers the second and is exactly wrong for the first. `budget_exhausted` is also an
error class: nothing retained and the deadline ended the walk → `Err(budget_exhausted)`; nothing
retained otherwise → `Err(exhausted)`; both raise `PlanExhaustedError`.

8. Compliance-constrained cascade with a guaranteed local floor
---------------------------------------------------------------

PHI: only BAA-covered or fully-local backends may see the document, and nothing may train on
it. Compliance lives in `policy:`, outside the tree.

```yaml
version: 1

policy:
  backends: [pymupdf, tesseract, aws-textract]

strategies:
  phi_pipeline:
    steps:
      - pymupdf                      # local: the document never leaves this machine
      - aws-textract                 # hosted rung with a BAA path
      - reducto                      # pruned in deployments where its claims aren't verified
      - docling                      # local floor
    escalate_if: default
```

Plain spelling — `policy:` is shared with Plain, so only the cascade changes:

```yaml
version: 1
policy:
  backends: [pymupdf, tesseract, aws-textract]
strategies:
  phi_pipeline:
    try: [pymupdf, aws-textract, reducto, docling]
    escalate_when: looks_bad
```

What happens: the file's `policy:` unions into every request's compliance block
(most-restrictive-wins, DECISIONS D-v3-12), and the 3-stage router prunes the tree *before*
execution. A hosted rung whose
BAA/no-train posture is not verified is dropped up front: the cascade simply has one fewer rung,
recorded in `orchestration.dropped[]` with the router's `DropReason`; nothing can re-admit it,
and naming compliance in `on_error` is a load-time error. `strategy validate` warns statically
about steps unreachable under the file's own `policy:`. If *every* rung were pruned: terminal
`no_compliant_backend` — never a silent downgrade.

9. Audit sampling: shadow a premium backend on 5% of traffic
------------------------------------------------------------

On a deterministic 5% sample, run aws-textract alongside the standard pipeline — recorded for
calibration, never allowed to win.

```yaml
strategies:
  standard: { steps: [pymupdf, reducto], escalate_if: default }

  audited:
    parallel:
      - use: standard
      - backend: aws-textract
        shadow: true                 # runs, fully recorded, can never win
    pick: best

  entry:
    route:
      rules:
        - when: { sample_percent: 5 }
          use: strategy:audited
      default: strategy:standard
```

What happens: `sample_percent` buckets by sha256 of the document bytes — deterministic per input,
so the same document always lands in the same bucket and replays agree with the idempotency
cache. The shadow branch is excluded from `pick` and always drained (category `shadow`); its
full response lands in the trace. Shadow-vs-winner comparison over
time is the calibration feed for tuning gate thresholds.

10. Gray-band review plus an explicit decision point
----------------------------------------------------

Invoice field extraction: missing required fields open a review band rather than a hard gate,
and the escalation target is a choice between two named strategies.

```yaml
strategies:
  precise_tables: { steps: [docling, reducto], escalate_if: { table_sanity_below: 0.7 } }
  llm_extract:    { steps: [anthropic-claude], escalate_if: off }

  invoices:
    steps:
      - backend: reducto
        operation: extract
        review_if:                                   # gray band, not a hard gate
          fields_required: [invoice_number, total_amount]
          field_confidence_below: 0.8
        review_default: escalate                     # engine semantics of the band
      - decide:
          among: [strategy:precise_tables, strategy:llm_extract]
          otherwise: strategy:precise_tables         # engine mode always takes this
        intent: Photographed receipts do better on the LLM extractor; born-digital
          invoices do better on the table pipeline.
```

What happens: `fields_required` fires on absence by definition — a missing or empty
`invoice_number` puts the result in the review band, a decision point with actions
`{accept, escalate}`. The engine resolves it to `review_default` (category `review_escalated`,
result retained); an enabled LLM decider may choose `accept` instead. The second step is a
`decide` node: candidates are enumerated first; engine mode takes `otherwise:`, LLM mode picks
one candidate guided by `intent:` — steering, never widening — and any decider failure resolves
to `otherwise:`, the decision record carrying `downgraded: <reason>` (the spec's
`decider_downgraded`; never a warning code). `validate` says up front that `review_if` with no
decider configured always resolves to `review_default`.

11. Page-level escalation for big scanned documents
---------------------------------------------------

A 300-page filing has 12 scanned exhibit pages; only those pages should hit the premium rung.

```yaml
strategies:
  big_scans:
    granularity: page                # gates evaluate per page; only failing pages escalate
    steps:
      - backend: pymupdf
        escalate_if:
          chars_per_page_below: 100
          scanned_pages_detected: true
      - reducto
```

What happens: pymupdf parses the whole document; gates evaluate per page. The 288 digital pages
are accepted; the 12 failing pages escalate to reducto as `pages.ranges`, so the premium rung
bills 12 pages, not 300. The response is stitched with per-page provenance —
`orchestration.pages[] {page, backend}` plus an additive `source_backend` on each response page.
PDF input only; a rung lacking native page-range support forces document granularity for that
rung (`validate` warns).

12. The maximal composition — everything at once
------------------------------------------------

One file exercising the whole grammar: an operator ceiling, a compliance-fact route, a cascade
nesting a hedged judged parallel, an `auto` leaf, an error map, shadow sampling, a deployment
default.

```yaml
version: 1

policy:
  backends: [pymupdf, tesseract, aws-textract]

limits:                              # operator ceiling on every strategy-engaged run;
  max_duration_per_doc: 10m          #   binds strategies, not direct-named requests

defaults:
  strategy: front_door               # traffic naming no backend runs front_door

strategies:
  base_cascade:
    budget: { max_duration: 4m }                       # node budget; children clamp, never extend
    on_quality_exhausted: best_effort                  # exhaustion returns keep-best, not an error
    on_error:                                          # governs all steps; step maps override key-by-key
      invalid_input: fail                              # corrupt doc: stop, don't burn rungs
      transient: next                                  # timeout/rate_limited/provider_error advance
    escalate_if: default                               # the §4.4 bundle, copied onto every
                                                       #   non-final step (§7 rule 5)
    steps:
      - backend: pymupdf                               # free local first
      - label: hosted_duel                             # stable trace identity across reorders
        parallel:
          - reducto                                    # primary, launches at t=0
          - backend: google-document-ai
            start_after: 30s                           # pure stagger under pick: best (the node
                                                       #   can't resolve early, so this branch
                                                       #   always launches; a true winner-cancels
                                                       #   hedge needs pick: fastest — spec §2.3)
        pick: best
        judge:                                         # presence makes the comparison LLM-judged
          backend: anthropic-claude                    # must pass the request's compliance filter
          intent: Prefer complete tables with arithmetically consistent totals.
        on_win: cancel                                 # losers cancelled; billed cost still recorded
      - aws-textract                                   # a named rung

  tables_heavy:                                        # a second cascade written out in full:
    budget: { max_duration: 6m }                       #   `extends:` is designed, not a file key
    escalate_if: default                               #   (§2.8), so a fork is a copy
    steps: [pymupdf, reducto]

  local_only:                                          # nothing leaves the machine
    { steps: [pymupdf, docling], escalate_if: default }

  audited:
    parallel:
      - use: base_cascade                              # a whole strategy as one branch
      - backend: azure-document-intelligence
        shadow: true                                   # recorded, never wins, always drained
    pick: best

  front_door:
    route:                                             # facts computed once, pre-parse
      rules:
        - when: { doc_type: [invoice, bank_statement] }
          use: strategy:tables_heavy
        - when: { sample_percent: 2 }                  # deterministic content-hash bucket
          use: strategy:audited
      default: strategy:base_cascade                   # mandatory floor
```

What happens: a request with no named backend routes through `front_door`. An invoice takes
`tables_heavy`, a second cascade with a
bigger duration budget. Inside `base_cascade`: pymupdf, then the hedged judged duel inside the
cascade's 4m budget inside the operator's 10m ceiling (children clamp, never extend; `limits:`
binds strategy-engaged runs only, never a direct-named request), then an `auto` rung that can
only pick a backend the walk has not touched — the attempted set spans rungs, branches, shadows,

Reading the trace
=================

Every strategy-engaged response carries an `orchestration` block (`openreading.strategies.trace`):
`strategy`, the attempt trail `attempts[]` — one record per backend run with `node`, `category`
(`succeeded`, `quality_escalated`, `raced_lost`, `judged_lost`, `shadow`, `judge_call`, …),
`duration_ms`, and `gates[]` where each evaluated predicate carries `threshold`,
`observed`, `fired`, and `skipped: signal_unavailable` when it could not bind — and
`decisions[]`: one record per decision point (gate bands, decide nodes, judges) plus one
`point: "route"` record per route node evaluated, carrying every rule's `matched` flag and fact
records (`engine._eval_route`). Example 1 has no decision points and no route, so its
`decisions[]` is empty and the whole story lives on the attempts; its final step carries
`gates: []` because no gate is copied onto a final step. The CLI (`cmd_explain` in
`openreading.cli.app`) renders the same trail for humans — a header, one line per attempt, and
under a gated attempt one `obs=… thr=…` row per predicate marked `FIRED` / `ok` / `skipped`;
there is no closing `result:` line and warnings are not printed (they stay on `warnings[]`).
The shape, with the observed values the shared scanned-PDF fixture produces:

    $ openreading explain response.json
    strategy cheap_first  →  reducto (ok)
      root.steps[0]    pymupdf      quality_escalated            410ms  $0
          scanned_pages_detected     obs=True thr=True  FIRED
          garbled                    obs=None thr=True  skipped
          empty_pages_over           obs=1.0 thr=0.2  FIRED
          confidence_below           obs=None thr=0.6  skipped
      root.steps[1]    reducto      succeeded                   2900ms  $0.0600

Every row answers the two operational questions — *why did money get spent* and *why didn't
something run* (skipped, pruned, or never reached). Both executors leave the same records; the
trace is also the input to `openreading replay --trace`.
"""

from __future__ import annotations

from openreading.strategies.model import RawNode

PRESETS: dict[str, RawNode] = {
    "cost_saver": {
        # The third rung used to be `auto`, which asked the router to pick from vendor claims.
        # Every rung names a backend now, so a reader can see what a preset will actually run.
        "intent": "Local parse first; escalate to a hosted backend only on bad quality.",
        "steps": ["pymupdf", "docling", "aws-textract"],
        "escalate_if": "default",
    },
    "max_accuracy": {
        "intent": "A hosted backend first; second opinion from another when quality gates fire.",
        "steps": ["aws-textract", "azure-document-intelligence"],
        "escalate_if": "default",
    },
    "fast": {
        "intent": "Lowest latency: race the local parsers, keep the first success.",
        "parallel": ["pymupdf", "tesseract"],
        "pick": "fastest",
        "on_win": "cancel",
    },
    "offline_first": {
        "intent": "Never leave the machine. Enforcement belongs to policy: { backends: [pymupdf, tesseract] }.",
        "steps": ["pymupdf", "tesseract", "docling"],
        "escalate_if": "default",
    },
}

PRESET_NAMES: frozenset[str] = frozenset(PRESETS)
