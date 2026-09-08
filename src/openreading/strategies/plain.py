"""Plain — the eleven-word simple-strategies dialect, and its desugar to advanced longhand.

A strategy says which backends run, in what order or together, and when to move on. Plain lets
you write that in eleven words you can hold in your head. It is a dialect, not a second engine:
the loader (`openreading.strategies.loader.parse_config_raw`) calls `desugar_config` after the
JSON-Schema gate (`schemas/strategy-config.v0.4.json`) and before `StrategyConfig.model_validate`,
so the engine, traces, replay, keep-best, caller-scope pruning, and `strategy validate` see only
the canonical five-node longhand the engine already runs. `openreading strategy show <name>` prints
a user strategy's body AS WRITTEN — a Plain body prints Plain, a preset prints its vendored
longhand (`openreading.cli.app._strategy_body_as_written` re-reads the source file, because the
loaded config holds only desugared longhand); `show <name> --longhand` prints the canonical tree
either dialect became, and `openreading strategy normalize` prints every strategy in the config
that way (`cmd_strategy_normalize` → `normalize_config`, the `docker compose config` analog).
That printout is the graduation path to the advanced grammar: paste it back, add the one
advanced key you need, and there is no cliff.

The whole language
==================

6 structure keys — one obvious spelling per thing you want:

    try: [a, b, c]     run in order; move on when one fails or the result can't be trusted
    race: [a, b]       run at once; the first success wins, the rest are cancelled
    compare: [a, b]    run at once; keep the objectively better result
    escalate_when:     when to move on — any listed reason fires (keys always OR)
    then: x            where to send the document when `compare` can't trust the winner
    max_time: "2m"     never take longer than this, for this strategy

4 criteria — the only words `escalate_when` understands. They are judgments, not knobs:

    looks_bad          openreading's own quality probe distrusts the result — garbled text,
                       near-empty pages, or an image-only page the backend couldn't read
    low_confidence     the backend itself reported low confidence (`0.7` instead of `true`
                       tunes the bar; `true` = 0.6)
    missing: [a, b]    a field you asked for didn't come back
    disagree           the compared backends produced materially different output
                       (`compare` only; `true` = 0.3)

0 reserved words. Every rung names a backend or another strategy, so a Plain file says what it
runs by reading it. (`auto` once meant "openreading's best remaining pick"; it is refused now, and
`_resolve_item` below says so in the words a reader needs.)

That is the entire surface. No signal catalog, no thresholds with units, no wrappers or
combinators, no `on_error`, no `route:`/`when:` (per-doc-type behavior in Plain is separate named
strategies the caller picks), no `pick:`/`on_win:`/`require:`, no LLM blocks. Reach for the
advanced grammar the day you need one of those.

Grammar
=======

A strategy body is Plain iff it is one of:

1. a bare string — one backend (`invoices: reducto`);
2. a bare list — shorthand for `try:` with nothing else (`fallback: [reducto, docling]`);
3. a map whose keys are a subset of {try, race, compare, escalate_when, then, max_time}, with
   these value shapes (closed):

* `try`: a list of items, or a single item. An item is a backend id, or the name of another
  strategy or preset in scope.
* `race` / `compare`: a list of 2 or more items, same item shapes.
* `escalate_when`: the scalar `looks_bad` (the only legal scalar), or a non-empty flat map with
  keys among {looks_bad, low_confidence, missing, disagree}:
  - `looks_bad: true` — all members at defaults; or a member map (below). Member maps OVERLAY
    the defaults: unlisted members keep theirs, `off` disables one, `true` enables one at its
    default threshold, a number sets the threshold.
  - `low_confidence: true | <0..1>` (`true` = 0.6).
  - `missing: [<field-name>, ...]` — a non-empty list of bare strings.
  - `disagree: true | <0..1>` (`true` = 0.3). Legal only in a `compare:` body.
* `then`: exactly one item (backend id or strategy name). It must not repeat an item of
  the `compare:` list — the escape hatch must be a different backend; a repeat would replay the
  loser's cached result and be a guaranteed no-op.
* `max_time`: a unit-suffixed duration string (`"90s"`, `"2m"`). A bare number is a schema
  rejection at load, located by the schema-error formatter.

Key-combination rules (schema-enforced at load): exactly one of `try`/`race`/`compare` per map
body; `escalate_when` is legal with `try` and `compare`, never `race`; `then` requires
`compare`; `escalate_when` with `compare` requires `then`. One more rule is desugar-time, not
schema: `escalate_when` on a single-item `try` is a `ConfigError` raised here by `_desugar_try`
(nowhere to escalate to) — the v0.2 schema accepts that shape.

Durations, `version: 1`, the top-level `policy:` / `limits:` / `defaults:` blocks, discovery,
precedence, and the three invocation spellings — CLI `--strategy <name>`, request
`backend.id: "strategy:<name>"`, `openreading.run(..., strategy="<name>")` — are shared file
surface, unchanged by the dialect.

The five things people want
===========================

Run backends in parallel — race the two local parsers, keep the first to finish:

```yaml
version: 1
strategies:
  quick:
    race: [pymupdf, docling]
```

Cascade on failure — try each in turn; a bare list is shorthand for `try`:

```yaml
version: 1
strategies:
  fallback: [reducto, azure-document-intelligence, docling]
```

Cost tiers — cheap first, climb to a stronger backend only when the result can't be trusted:

```yaml
version: 1
strategies:
  main:
    try: [pymupdf, docling, reducto]
    escalate_when: looks_bad
```

Escalate on your own criteria — any listed reason moves the document up a tier:

```yaml
version: 1
strategies:
  invoices:
    try: [reducto, anthropic-claude]
    escalate_when:
      missing: [invoice_number, total_amount]
      low_confidence: 0.8
```

Compare two, route to a third — the superpower. Run both, keep the better; if even the winner
can't be trusted, send it on:

```yaml
version: 1
strategies:
  contracts:
    compare: [docling, aws-textract]
    then: reducto
```

With no `escalate_when`, `compare` + `then` means "if the two disagree or the winner looks bad,
go to `then`" — the sentence a developer means when they write those three lines. Write your own
`escalate_when` only to change that (an explicit one replaces the default entirely).

Combining criteria — `escalate_when` keys always OR (any reason to distrust => move on):

```yaml
version: 1
strategies:
  careful:
    try: [pymupdf, reducto]
    escalate_when:
      looks_bad: true
      low_confidence: 0.7
```

Cost tiers are `try` + ordering + `looks_bad` — deliberately a pattern, not a construct;
`cost_saver` (`openreading.strategies.presets`) remains the zero-config spelling. Example names
must avoid the four reserved preset names (`cost_saver`, `max_accuracy`, `fast`,
`offline_first`); a strategy under one of those names is rejected at the first normalize
(`NormalizeError`; the message is quoted in `openreading.strategies.presets`).

What `looks_bad` checks
=======================

`looks_bad` is not an LLM and not a vibe — it is openreading's own reference-free quality probe
(`openreading.strategies.signals`), evaluated in-process when a rung returns: deterministic,
milliseconds, free, no network. It is a verdict on *this backend's attempt*, never on the
document: a scanned contract is not "bad", but a text-layer parse *of* a scan is — and the gate
firing is exactly what routes that scan to a backend that can read it.

    member               default  fires when
    garbled              on       the text is mojibake / control-character soup: score =
                                  0.4·replacement/control-char ratio + 0.35·non-wordlike-token
                                  ratio + 0.25·non-ASCII-run ratio, capped at 1.0; fires > 0.3
    empty_pages          0.2      more than this fraction of pages came back near-empty
                                  (under 25 extracted chars)
    no_text_from_images  on       the input has image-only pages (no text layer — pypdf
                                  analysis of the PDF bytes) AND this attempt got almost no
                                  text out of them
    min_text_per_page    off      mean extracted chars per page fall below this (100 is the
                                  field-tested scan cut)

`escalate_when: looks_bad` turns them all on at their defaults. To tune, name the members —
unlisted ones keep their defaults, `off` disables one, a number sets a threshold. The two
spellings side by side (a fragment, not a strategy body):

```yaml
escalate_when: looks_bad          # scalar: all members on, defaults above
escalate_when:
  looks_bad:                      # map: the dev decides what goes into it
    empty_pages: 0.3
    no_text_from_images: off
```

In a strategy:

```yaml
version: 1
strategies:
  scans:
    try: [pymupdf, reducto]
    escalate_when:
      looks_bad:
        empty_pages: 0.3
        no_text_from_images: off
```

(`off` unquoted is YAML `false`; `_compile_looks_bad` tests `is not False`.)

The scan member is a conjunction, by design. The raw `scanned_pages_detected` signal is computed
from the *input bytes*, so alone it would fire even on a backend that successfully OCR'd the scan
— an input-side fact, not a verdict on the attempt. Plain therefore compiles `no_text_from_images`
to the one-level combinator `all_of: [scanned_pages_detected: true, chars_per_page_below: N]`
(N = the active `min_text_per_page` value, default 100): a good OCR of a scan passes; a
text-layer parse of a scan escalates. Zero new signals, legal longhand — a deliberate improvement
over the advanced `default` bundle's bare input-side member.

Parameterization boundary: members and thresholds yes, formula internals no. The garble mixing
weights and the 25-char near-empty cut are field-tested constants, not knobs — pick a target
escalation rate and let `openreading calibrate` (`openreading.strategies.calibrate`) derive the
`empty_pages` / `min_text_per_page` thresholds over your own corpus.

Input types: only the scan member is PDF-specific; `garbled`, `empty_pages`, `min_text_per_page`,
and `low_confidence` are computed over the normalized *response* and bind on any input. A member
that cannot apply is skipped and traced as skipped (the missing-signal law — never guessed, never
silently fired), so `looks_bad` stays meaningful on every input type and never quietly becomes a
PDF-only feature.

Ownership split: confidence is *not* a `looks_bad` member — it is the backend's self-assessment,
so it is its own word (`low_confidence`). `looks_bad` (defaults) + `low_confidence: true` matches
the advanced `default` bundle member-for-member, with the scan member made result-aware.

Desugaring (normative equivalences)
===================================

    Plain                                  advanced longhand
    try: [a, b, c]                         steps: [{backend: a}, {backend: b}, {backend: c}]
    race: [a, b]                           parallel: [...], pick: fastest, on_win: cancel
    compare: [a, b]                        parallel: [...], pick: best, require: all
    compare + then: x                      steps: [{parallel..., pick: best, require: all,
                                                    escalate_if: <gate>}, {backend: x}]
    escalate_when: ...                     per-step escalate_if on every rung except the last
    max_time: "2m"                         budget: {max_duration: "2m"} on the body root
    looks_bad: true                        any_of: [all_of: [scanned_pages_detected: true,
                                                     chars_per_page_below: 100],
                                                    garbled: true, empty_pages_over: 0.2]
    looks_bad: {empty_pages: F}            overlay: empty_pages_over: F, others at defaults
    looks_bad: {min_text_per_page: N}      overlay: adds chars_per_page_below: N; the scan pair
                                           uses N as its threshold
    looks_bad: {no_text_from_images: off}  overlay: scan pair omitted (gate becomes a flat map)
    low_confidence: true / F               confidence_below: 0.6 / F
    missing: [a, b]                        fields_required: [a, b]
    disagree: true / F                     disagreement_over: 0.3 / F
    looks_bad + low_confidence: true       the `default` bundle, member-for-member, scan-aware
    <strategy or preset name>              {use: <name>}

Emitted trees are canonical (normalize fixed points): list items are `{backend: …}` maps, no
`escalate_if: off`, and the gate is a flat OR map unless the scan member forces a one-level
`any_of`. The compiled tree for `contracts` above — what `strategy show contracts --longhand`
prints:

```yaml
steps:
  - parallel:
      - { backend: docling }
      - { backend: aws-textract }
    pick: best
    require: all
    escalate_if:                        # the compiled default gate (one any_of — keys OR)
      any_of:
        - disagreement_over: 0.3
        - all_of:                       # the result-aware scan member
            - scanned_pages_detected: true
            - chars_per_page_below: 100
        - garbled: true
        - empty_pages_over: 0.2
  - { backend: reducto }                # then: — final rung, ungated
```

`ADVANCED_TO_PLAIN` is the inverse table (compiled predicate → Plain word); `strategy validate`
uses it to re-phrase issues on a Plain strategy in the vocabulary the author wrote.

Semantics
=========

`try` — cascade. With no `escalate_when` it desugars to `{steps: [...]}` with no gates: advance
on failure only, stop on first success. Error behavior is baked in and unspellable — transient
errors advance, a corrupt document (`invalid_input`) fails the whole chain, missing credentials
skip the rung — the engine's `on_error` defaults, which Plain cannot alter. With `escalate_when`
the compiled gate is attached per step to every leaf and parallel rung except the last (emitted
directly, never via cascade-level distribution — deterministic). A `try` item that references
another strategy runs that strategy's own rules; the outer `escalate_when` does not apply to it
and `strategy validate` says so — loud, never silent. The final rung is never gated: keep-best,
`on_quality_exhausted: best_effort`, and honest `warnings[]` are the inherited defaults.

`race` — first *successful* response wins; losers cancelled; a cancelled loser's call still
reached the vendor, and the trace records it. `escalate_when` beside `race` is a load error, but a generic one: the shape is
rejected by `additionalProperties: false` on the schema's `plain_race_body` and surfaced by the
loader as the located schema error ("invalid config at 'strategies/<name>': {...} is not valid
under any of the given schemas"). The schema's `description` ("a race keeps the first success;
use compare for a quality check") is never printed — the design asked for a bespoke message; the
loader ships none. The advanced grammar's silent gates-ignored-under-`pick: fastest` trap still
becomes an immediate, if terse, fix.

`compare` — alone: run all, keep the objectively better by the engine's deterministic composite
score, never an LLM judge in Plain. With `then`: a two-rung cascade with the gate *on the
parallel step*, evaluated against the comparison winner. A step gate on a `parallel` node is
evaluated on the winner's response, with leaf-step semantics. The gate retains that result as
best-so-far when it fires, then advances to the next step
(`openreading.strategies.engine._eval_cascade`). Without this rule an `escalate_if` written
beside `parallel` would parse and then do nothing. Nested-cascade, `use:`, route and decide
steps stay gate-less, because their own rules govern them. Distributing a parent gate onto a
nested cascade would stop `normalize` being a fixed point (D-v3-7). `compare` accepts 3+ items;
`disagree` is then the worst pairwise disagreement.
If fewer than two non-shadow branches finish, `disagree` cannot bind (skipped, missing-signal
law) and the quality criteria still gate the survivor. The design's "degraded compare is never
silent" `compare_degraded` warning (internal/design/simple-strategies.md §6.3) is designed, not
shipped: nothing in `src/` emits it, so a degraded compare is visible only through the attempt
trail (the missing branch's `error(…)` / `skipped(…)` / `deadline_pruned` record) and the
skipped `disagreement_over` gate row, never through `warnings[]`.

`disagree` — the `disagreement_over` predicate, defined on `pick: best` parallel nodes only
(`strategy validate` errors elsewhere; advanced files may spell it directly). Computed in
`engine._resolve_parallel` as 1 − token-set Jaccard of the guaranteed text channel over the
finished non-shadow branches (worst pair for 3+), attached to the winner's `SignalSnapshot` so
the step gate sees it through the ordinary probe/`evaluate_gate` path, and recorded on the
winning attempt unconditionally (`Attempt.disagreement`, calibration telemetry). Inlined rather
than imported from the comparison package — the leaf-isolation invariant forbids `strategies/`
importing `comparison/`.

Time — `max_time` has one placement: `budget.max_duration` on the body root, one pool for the
whole strategy (the duel and the `then:` rung share it), clamping under `limits:` as always
(children clamp, never extend). On deadline, keep-best returns the best retained result as
`Deficient`, never silence. A quality exhaustion (every rung gated) carries the
`quality_below_threshold` warning, and a deadline overrun carries `budget_exhausted` instead. The
two are separate codes so each cause is countable on its own, and because escalating to a stronger
backend is the right answer to the first and the worst answer to the second. `budget_exhausted` is
also the error class (`PlanExhaustedError`) when the deadline ended the walk with nothing
retained.

Missing signals and `missing:` — a criterion the backend can't report doesn't fire, is traced as
skipped, and is never guessed; no `on_missing` wrapper exists in Plain. `missing: [name]` is a
key lookup in the response's `typed_fields` map — the developer's own names from their
`extraction_schema`, normalized identically across every backend, which is why the criterion is
portable across rungs. Missing = key absent OR value null/empty-string. Top-level names only; the
advanced alias form (`{name, aliases}`) and dotted paths are out of Plain.

Names — a list or `then:` item that is not a registered backend id resolves as a strategy or
preset name → `{use: <name>}`. A name matching both a backend id and a strategy is an error at the
reference site (rename instruction). Duplicate ids in one list are an error, in every key: a rung
that repeats an earlier one replays a result already in hand.

The boundary
============

Per strategy body, purely syntactic, decided at load: a body is Plain iff it matches the grammar
above. Any other key (`steps:`, `parallel:`, `pick:`, `route:`, `decide:`, `on_error:`,
`judge:`, `budget:`, `escalate_if:`, `extends:`, `use:`, `with:`, `intent:`, `label:`, …) makes
the body advanced — full grammar, untouched. Mixing the vocabularies in one body is a schema
error, and a generic one: the design (internal/design/simple-strategies.md §7) asked for a
bespoke message that prints the body's compiled longhand to fork ("this strategy mixes Plain
(`try:`) and advanced (`pick:`) keys — fork this longhand or drop the advanced key: …"); what
ships is the loader's located schema error ("invalid config at 'strategies/<name>': {...} is not
valid under any of the given schemas"), the same gap as `escalate_when` beside `race` above.
The two dialects coexist in one `strategies:` map and reference each other by name in
both directions. `strategy validate` badges each strategy `dialect: plain` or
`dialect: advanced (first advanced key: pick)` — the bare key, no trailing colon
(`first_advanced_key`, rendered by `openreading.cli.app._dialect_badge`).

Two disciplines are load-bearing:

* Strict no-op on advanced. Only a dict body carrying `try`/`race`/`compare` is rewritten
  (`_is_plain_map`). Bare strings/lists and advanced map bodies pass through structurally
  identical (existing normalization rules own bare bodies; they are merely classified `plain`
  for the badge); a file with no Plain map bodies round-trips unchanged with zero `rewritten`
  entries. `tests/test_strategy_plain.py` proves this mechanically over every docs config,
  every preset, and every normalize-expansion input, using a classification predicate
  independent of the desugar (`is_plain_dialect`) so a classification bug cannot scope its own
  assertion out.
* Canonical output. Emitted trees are schema-valid normalize fixed points; desugaring the output
  again is a strict no-op (compiled trees are advanced); desugar is deterministic.

The Plain-vs-advanced differential proof on real bytes is `scripts/strategy_smoke.py` (run by
`make verify`): the advanced `local_ocr` cascade (`steps: [pymupdf, tesseract]`,
`escalate_if: default`) and its Plain spelling (`try: [pymupdf, tesseract]`,
`escalate_when: looks_bad`) both run on the shared scanned-PDF fixture, and the script asserts
the SAME attempt/category trail, chosen backend, and outcome — while the fired predicates
legitimately differ (the scan pair vs the bundle's bare `scanned_pages_detected`, plus the
bundle's `confidence_below`, evaluated-but-skipped on pymupdf).

Validation catalog
==================

Three enforcement points; Plain moves none of the existing world-consistency checks.

Schema (load; generic located schema errors, no bespoke messages): body key outside the six
(also the mixing case) · none or two of `try`/`race`/`compare` · `race`/`compare` with fewer
than 2 items · `then` without `compare` · `escalate_when` beside `race` · `compare` +
`escalate_when` without `then` · `escalate_when` scalar other than `looks_bad` · empty
`escalate_when` map · unknown criterion key · unknown `looks_bad` member · threshold outside its
domain · `missing:` empty or non-string entries · `then:` with more than one target · `max_time`
bare number.

Desugar-time (this module, `ConfigError`, Plain vocabulary, Elm doctrine — locate, explain,
suggest the fix; name resolution is the loader's one dependency on the adapter registry):
unknown name (with nearest-name suggestion) · name matching both a backend id and a strategy ·
duplicate id in one list · a rung naming `auto` · `disagree` outside a
`compare` body · `escalate_when` on a single-item `try` · `then:` repeating a `compare:` item.

`strategy validate` (world-consistency, `openreading.strategies.validate`): unbindable gate — an
error, re-phrased in Plain vocabulary ("the low_confidence check can never fire on 'pymupdf' — it
reports no confidence; add a criterion that works everywhere, e.g. `looks_bad: true`") ·
`missing:` on a rung whose backend cannot produce `typed_fields` — a warning ("missing:
'pymupdf' cannot produce typed fields, so this criterion fires on every document — this rung
will always escalate") · `escalate_when` on a `try` whose item is a strategy reference (carried
as `PlainInfo.warnings`, computed here because only the original body can detect it) ·
`disagreement_over` on a step whose node is not a `pick: best` parallel.
Validator issues raised on a Plain body post-desugar are mapped back through `ADVANCED_TO_PLAIN`
so the user never debugs vocabulary they did not write.

Trace and `explain`
===================

Compiled gates carry provenance out-of-band, never in the emitted YAML (the compiled tree stays
schema-valid longhand): `desugar_config` returns, per Plain strategy, `PlainInfo.provenance` =
`{node path → {predicate → Plain word}}`, carried on `LoadedConfig`, threaded through the
compiled plan into the engine's walk context, and consulted when gate records are minted — an
additive `source` field on `openreading.strategies.trace.GateRecord` (absent for
advanced-authored gates). `openreading explain` groups gate rows by source word for Plain
strategies; advanced traces render exactly as before. Replay, the idempotency cache, and the
attempt trail are untouched — the compiled tree is an ordinary longhand tree. The rows under
the source word are the COMPILED predicate names (`scanned_pages_detected`,
`chars_per_page_below`, `garbled`, `empty_pages_over`) with `obs=… thr=…` and `FIRED` / `ok` /
`skipped` — provenance maps every `looks_bad` predicate to that single word, so Plain member
names (`no_text_from_images`, `empty_pages`) never appear in a `GateRecord` or in `explain`, and
`cmd_explain` prints no closing `result:` line. The gate rows below are what the shared
scanned-PDF fixture produces (the attempt line's timing is illustrative):

    root.steps[0]    pymupdf      quality_escalated            410ms  $0
          looks_bad
            scanned_pages_detected   obs=True thr=True  FIRED
            chars_per_page_below     obs=0.0 thr=100  FIRED
            garbled                  obs=None thr=True  skipped
            empty_pages_over         obs=1.0 thr=0.2  FIRED

`strategy validate` also shows each config as written with a one-line plain-English summary
(`openreading.strategies.describe`) and explains the judgment words that appeared:

      main: dialect: plain
          try: [pymupdf, docling, reducto]
          escalate_when: looks_bad
          max_time: 2m
        → Tries pymupdf, then docling, then reducto, moving on when a step fails, or the
          result looks bad. Stops after 2m.
      contracts: dialect: plain
          compare: [docling, aws-textract]
          then: reducto
        → Runs docling and aws-textract at once and keeps the better result; if they
          disagree or the winner looks bad, sends the document to reducto.

      what the words mean:
        looks bad       checks unread scans, garbled text, and near-empty pages. For exact defaults and
                        overrides: openreading help gates
        disagree        worst pairwise text-token difference among successful branches (true: 0.3); not a field
                        or table comparison

Guardrails you get for free
===========================

* Every attempt on the record. `orchestration.attempts[]` names every backend that ran, winners
  and losers alike, so you can count the calls a run made. It carries no price: the totals it used
  to publish as `usage.cost_usd` were built from per-vendor rates core could not verify. `usage`
  reports what a backend consumed in its own unit.
* Every backend is visible in the Plain body. Server API-key scope can prune that written list,
  while `policy.backends` only supplies defaults outside Plain.
* Never silent. If everything gates, you get the best result kept so far with honest
  `warnings[]`, never a fabricated answer.

Design decisions, and the failure each avoids
=============================================

* Judgments, not measurements. Plain criteria are verdicts; anything with a unit or an exotic
  threshold lives in advanced. The advanced surface (~60 constructs, a 16-predicate catalog with
  wrappers and combinators, two gate systems, a 10-class error taxonomy) is expert-shaped; a
  developer could not hold it in their head after one page.
* Verdicts on the attempt, never on the document — the scan-member conjunction above. The bare
  input-side signal would escalate a scan that was OCR'd perfectly.
* Determinism by exclusion. No LLM construct exists in Plain (`judge:`, `decide:`, `review_if`,
  `decider:` are advanced-only), so a Plain file behaves identically on every deployment; the
  env-gated "config that silently does nothing" trap (`OPENREADING_LLM_DECIDER` unset) is
  unrepresentable.
* Every Plain construct desugars to existing longhand — one engine, one validator, one trace,
  one replay path. The only engine changes Plain brought were the step gate on parallel steps
  and the `disagreement_over` signal, both available to advanced files too.
* The grammar lives in `strategy-config.v0.4.json`. A
  released schema is byte-frozen, so it is changed by cutting a new version. The change is
  additive, the config `version` const stays 1, and v0.2 validates every v0.1 config.
* Presets stay vendored in longhand; they carry `intent:`, which Plain cannot spell. The docs
  pair each with its Plain near-equivalent (`openreading.strategies.presets`).
* No dollar ceiling, and no dollars at all. Prices change too often for anything core writes down
  to stay true, so the engine quotes none and enforces no spending wall. `budget:` carries
  `max_duration` and `max_attempts`, and `validate` refuses any file that declares
  `max_attempts`, because no engine code reads it. `budget_exhausted` therefore always means
  the time deadline.
* No pre-parse scan router: the cheap first rung *is* the free scan detector. No auto-tiering by
  descriptor price: ordering stays explicit, in the order you wrote.

Non-goals: no route/decide/judge/review/on_error/granularity/extends/with/shadow/hedge in Plain
(all advanced, unchanged); no renaming of advanced constructs; no dotted-path or alias forms in
`missing:`; no changes to invocation, discovery, precedence, caller scope, or budget laws.
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass
from typing import Any

from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.strategies.loader import ConfigError
from openreading.strategies.presets import PRESET_NAMES

_PLAIN_BODY_KEYS = frozenset({"try", "race", "compare", "escalate_when", "then", "max_time"})
_DISCRIMINATORS = ("try", "race", "compare")

# looks_bad member defaults (§5). Values: True=on at default, False('off' in YAML)=disabled,
# number=threshold. The scan member is a conjunction (input looks scanned AND this attempt got
# almost no text out), compiled to an all_of pair sharing the min_text_per_page threshold.
_LOOKS_BAD_DEFAULTS = {
    "garbled": True,
    "empty_pages": 0.2,
    "no_text_from_images": True,
    "min_text_per_page": False,
}
_DEFAULT_MIN_TEXT = 100  # the field-tested scan cut; the scan pair's threshold when not tuned
_DEFAULT_EMPTY_PAGES = 0.2
_DEFAULT_LOW_CONFIDENCE = 0.6
_DEFAULT_DISAGREE = 0.3

# The inverse of the §10 equivalence table: a compiled advanced predicate -> the Plain word it
# came from. `strategy validate` uses this to re-phrase issues on a Plain strategy in the four-word
# vocabulary the author actually wrote (§7, §8).
ADVANCED_TO_PLAIN = {
    "fields_required": "missing",
    "confidence_below": "low_confidence",
    "page_confidence_below": "low_confidence",
    "scanned_pages_detected": "looks_bad",
    "chars_per_page_below": "looks_bad",
    "garbled": "looks_bad",
    "empty_pages_over": "looks_bad",
    "disagreement_over": "disagree",
}


def first_advanced_key(body: Any) -> str | None:
    """The first key of a map body that is not a Plain key — names why a body is `advanced` for the
    dialect badge (§7). None for bare strings/lists and pure-Plain bodies."""
    if isinstance(body, dict):
        for key in body:
            if key not in _PLAIN_BODY_KEYS:
                return key
    return None


@dataclass(frozen=True)
class PlainInfo:
    """Per-strategy dialect classification, carried on LoadedConfig for the badge (P3) and gate
    provenance (P4). `rewritten`, `provenance`, and `warnings` are set only for Plain *map* bodies.
    `warnings` are the desugar-computed §8 rows that need the original Plain body — `strategy
    validate` surfaces them (path is relative to the strategy root)."""

    dialect: str  # "plain" | "advanced"
    rewritten: bool
    provenance: dict[str, dict[str, str]] | None  # {node path: {predicate: Plain word}}
    warnings: tuple[tuple[str, str], ...] = ()  # (relative node path, message)


def desugar_config(raw: dict) -> tuple[dict, dict[str, PlainInfo]]:
    """Rewrite Plain map bodies to canonical longhand and classify every strategy.

    Returns (config, info). The config is structurally identical to `raw` when it holds no Plain
    map bodies (§7). Raises ConfigError on a desugar-time §8 violation.
    """
    strategies = raw.get("strategies")
    if not isinstance(strategies, dict):
        return raw, {}
    strategy_names = set(strategies)
    known_names = BUILTIN_ADAPTERS.keys() | strategy_names | set(PRESET_NAMES)

    out_strategies: dict[str, Any] = {}
    info: dict[str, PlainInfo] = {}
    for name, body in strategies.items():
        if _is_plain_map(body):
            desugared, provenance, warnings = _desugar_body(name, body, strategy_names, known_names)
            out_strategies[name] = desugared
            info[name] = PlainInfo("plain", True, provenance or None, tuple(warnings))
        else:
            out_strategies[name] = body
            info[name] = PlainInfo("plain" if is_plain_dialect(body) else "advanced", False, None)

    return {**raw, "strategies": out_strategies}, info


def is_plain_dialect(body: Any) -> bool:
    """A body is Plain-dialect iff it is a bare string, a bare list, or a map whose keys are a
    subset of the seven Plain keys (§7). Independent of desugar internals — used for the badge
    and as the T1 classification predicate."""
    if isinstance(body, (str, list)):
        return True
    if isinstance(body, dict):
        return bool(body) and set(body) <= _PLAIN_BODY_KEYS
    return False


def _is_plain_map(body: Any) -> bool:
    """A Plain map body: a dict carrying exactly one of try/race/compare. The schema (P0) has
    already guaranteed the rest of the shape by the time desugar runs."""
    return isinstance(body, dict) and bool(set(body) & set(_DISCRIMINATORS))


# Each node desugar returns (canonical longhand, gate provenance, warnings) where warnings is a
# list of (relative node path, message) — the §8 rows that only the original Plain body can detect.
_Desugared = tuple[dict, dict[str, dict[str, str]], list[tuple[str, str]]]


def _desugar_body(
    name: str, body: dict, strategy_names: set[str], known_names: set[str]
) -> _Desugared:
    if "try" in body:
        return _desugar_try(name, body, strategy_names, known_names)
    if "race" in body:
        return _desugar_race(name, body, strategy_names, known_names)
    return _desugar_compare(name, body, strategy_names, known_names)


# --------------------------------------------------------------------------- node desugars


def _desugar_try(
    name: str, body: dict, strategy_names: set[str], known_names: set[str]
) -> _Desugared:
    raw_items = body["try"]
    items = raw_items if isinstance(raw_items, list) else [raw_items]
    _check_no_dup(name, "try", items)

    escalate_when = body.get("escalate_when")
    gate: dict | None = None
    predicates: dict[str, str] = {}
    if escalate_when is not None:
        if len(items) == 1:
            raise ConfigError(
                f"strategies.{name}.try: escalate_when needs somewhere to escalate to, but 'try' "
                f"has a single rung. Add another backend, or drop escalate_when"
            )
        _reject_disagree(name, escalate_when)
        gate, predicates = _compile_gate(escalate_when)

    steps: list[Any] = []
    provenance: dict[str, dict[str, str]] = {}
    warnings: list[tuple[str, str]] = []
    for idx, item in enumerate(items):
        node = _resolve_item(name, item, strategy_names, known_names, at=f"try[{idx}]")
        is_last = idx == len(items) - 1
        if gate is not None and not is_last:
            if "use" in node:  # a strategy reference runs its own rules; the outer gate can't apply
                warnings.append(
                    (
                        f"steps[{idx}]",
                        f"escalate_when does not apply to the strategy reference {item!r}, "
                        f"which runs its own rules. Gate a backend rung instead",
                    )
                )
            else:
                node = {**node, "escalate_if": copy.deepcopy(gate)}
                provenance[f"steps[{idx}]"] = dict(predicates)
        steps.append(node)

    out: dict[str, Any] = {"steps": steps}
    _attach_budget(out, body)
    return out, provenance, warnings


def _desugar_race(
    name: str, body: dict, strategy_names: set[str], known_names: set[str]
) -> _Desugared:
    items = body["race"]
    _check_no_dup(name, "race", items)
    branches = [
        _resolve_item(name, it, strategy_names, known_names, at=f"race[{i}]")
        for i, it in enumerate(items)
    ]
    out: dict[str, Any] = {"parallel": branches, "pick": "fastest", "on_win": "cancel"}
    _attach_budget(out, body)
    return out, {}, []


def _desugar_compare(
    name: str, body: dict, strategy_names: set[str], known_names: set[str]
) -> _Desugared:
    items = body["compare"]
    _check_no_dup(name, "compare", items)
    branches = [
        _resolve_item(name, it, strategy_names, known_names, at=f"compare[{i}]")
        for i, it in enumerate(items)
    ]

    then = body.get("then")
    if then is None:
        # compare alone: run all, keep the objectively better; no routing.
        out: dict[str, Any] = {"parallel": branches, "pick": "best", "require": "all"}
        _attach_budget(out, body)
        return out, {}, []

    if then in items:
        raise ConfigError(
            f"strategies.{name}.then: {then!r} is also a compare rung. The escape hatch must be "
            f"a different backend than the two being compared"
        )
    explicit = body.get("escalate_when")
    # the compare default: route when the branches disagree OR the winner looks bad (§6.3).
    escalate_when = {"disagree": True, "looks_bad": True} if explicit is None else explicit
    gate, predicates = _compile_gate(escalate_when)

    parallel_step = {"parallel": branches, "pick": "best", "require": "all", "escalate_if": gate}
    then_node = _resolve_item(name, then, strategy_names, known_names, at="then")
    out = {"steps": [parallel_step, then_node]}
    _attach_budget(out, body)
    return out, {"steps[0]": predicates}, []


# --------------------------------------------------------------------------- item resolution


def _resolve_item(
    name: str,
    item: str,
    strategy_names: set[str],
    known_names: set[str],
    *,
    at: str,
) -> dict:
    if item == "auto":
        # `auto` asked the engine to pick from vendor claims it could not verify, and it is gone.
        # A Plain rung names a backend or a strategy; the deployment's own preference order lives
        # in `policy.backends`, which is where a caller states it once for every run. Kept as its
        # own branch rather than falling through to "unknown name" because a reader who wrote
        # `auto` needs to be told what replaces it, not that it does not exist.
        raise ConfigError(
            f"strategies.{name}.{at}: 'auto' is no longer a rung. Name a backend, or set the "
            f"deployment's order once in policy.backends"
        )

    is_backend = item in BUILTIN_ADAPTERS
    is_strategy = item in strategy_names or item in PRESET_NAMES
    if is_backend and is_strategy:
        raise ConfigError(
            f"strategies.{name}.{at}: {item!r} is both a backend id and a strategy name. "
            f"Rename the strategy so the reference is unambiguous"
        )
    if is_backend:
        return {"backend": item}
    if is_strategy:
        return {"use": item}

    suggestion = difflib.get_close_matches(item, known_names, n=1)
    hint = f" (did you mean {suggestion[0]!r}?)" if suggestion else ""
    raise ConfigError(f"strategies.{name}.{at}: {item!r} is not a known backend or strategy{hint}")


def _check_no_dup(name: str, key: str, items: list) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            raise ConfigError(
                f"strategies.{name}.{key}: {item!r} appears twice, and each rung must be distinct"
            )
        seen.add(item)


def _attach_budget(out: dict, body: dict) -> None:
    budget: dict[str, Any] = {}
    if "max_time" in body:
        budget["max_duration"] = body["max_time"]
    if budget:
        out["budget"] = budget


# --------------------------------------------------------------------------- gate compilation


def _reject_disagree(name: str, escalate_when: Any) -> None:
    if isinstance(escalate_when, dict) and "disagree" in escalate_when:
        raise ConfigError(
            f"strategies.{name}.escalate_when: 'disagree' compares two backends' outputs, so it "
            f"only works inside compare: strategies. Remove it, or switch this strategy to "
            f"compare:"
        )


def _compile_gate(escalate_when: Any) -> tuple[dict, dict[str, str]]:
    """Compile an escalate_when (the scalar 'looks_bad' or a criteria map) into a gate map plus a
    {predicate: Plain word} provenance map. Keys OR; the scan member forces any_of (§6.3)."""
    if isinstance(escalate_when, str):  # the scalar form 'looks_bad'
        escalate_when = {"looks_bad": True}

    atoms: list[dict] = []
    predicates: dict[str, str] = {}
    for criterion, value in escalate_when.items():
        if criterion == "looks_bad":
            for atom in _compile_looks_bad(value):
                atoms.append(atom)
                for pk in _predicate_keys(atom):
                    predicates[pk] = "looks_bad"
        elif criterion == "low_confidence":
            atoms.append({"confidence_below": _DEFAULT_LOW_CONFIDENCE if value is True else value})
            predicates["confidence_below"] = "low_confidence"
        elif criterion == "missing":
            atoms.append({"fields_required": list(value)})
            predicates["fields_required"] = "missing"
        elif criterion == "disagree":
            atoms.append({"disagreement_over": _DEFAULT_DISAGREE if value is True else value})
            predicates["disagreement_over"] = "disagree"

    if any("all_of" in atom for atom in atoms):
        return {"any_of": atoms}, predicates
    gate: dict[str, Any] = {}
    for atom in atoms:
        gate.update(atom)
    return gate, predicates


def _compile_looks_bad(value: Any) -> list[dict]:
    """The looks_bad member overlay → gate atoms (§5, §10). Emission order is fixed:
    scan-pair, garbled, empty_pages, standalone min_text_per_page."""
    members = dict(_LOOKS_BAD_DEFAULTS)
    if isinstance(value, dict):
        members.update(value)

    min_text = members["min_text_per_page"]
    threshold = min_text if _is_number(min_text) else _DEFAULT_MIN_TEXT

    atoms: list[dict] = []
    if members["no_text_from_images"] is not False:
        atoms.append(
            {"all_of": [{"scanned_pages_detected": True}, {"chars_per_page_below": threshold}]}
        )
    if members["garbled"] is not False:
        atoms.append({"garbled": True})
    if members["empty_pages"] is not False:
        empty = members["empty_pages"]
        atoms.append({"empty_pages_over": empty if _is_number(empty) else _DEFAULT_EMPTY_PAGES})
    if min_text is not False:  # True (default cut) or a number → a standalone chars gate
        atoms.append({"chars_per_page_below": threshold})
    return atoms


def _predicate_keys(atom: dict) -> list[str]:
    if "all_of" in atom:
        return [k for sub in atom["all_of"] for k in sub]
    return list(atom)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
