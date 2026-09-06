"""Pydantic mirror of `strategy-config.v0.2.json`, and the reference for what `openreading.yaml`
may contain — every node kind, every key, every gate predicate, and every validation rule.

A **strategy** is a named recipe for which backends run — in what order or in parallel — and
when to move on. The file draws rails (eligible actions, thresholds, budgets); any executor — the
deterministic engine or an LLM decider — walks the same tree inside those rails and leaves the
same auditable trace. Three laws frame everything below:

1. **No file ⇒ no change.** Absent a strategy file, every request takes exactly the legacy code
   path; the strategy layer is not even imported.
2. **Compliance is outside the tree.** The 3-stage router's compliance/capability filter prunes
   the tree BEFORE execution. No strategy, rule, prose, or LLM can re-admit a dropped backend.
   Compliance is never a catchable error class.
3. **Deciders choose; they never widen.** Every decision point enumerates its candidates first;
   any decider selects from that list. Budgets, thresholds, and compliance are the ceiling —
   `intent:` prose guides choices under the ceiling and can never move it.

The DSL is deliberately sub-Turing: no `Next:` pointers, no loops, no variables, no expression
language. If a future version ever adds expressions it will be a single existing language (CEL)
behind a single `expr:` key; nothing in the shipped grammar depends on it.

DECISIONS D-v3-6: the vendored JSON Schema is the STRICT grammar authority for the node tree
(same posture as DECISIONS D4: where a pydantic model and the JSON Schema could drift, the schema wins).
The loader validates the raw file against the schema BEFORE constructing these models. This
module types the file's top-level shape and the deployment blocks (`policy`/`limits`/`decider`/
`defaults`); the `strategies` map holds schema-validated raw nodes (`RawNode`). The rich,
recursive typed node IR — Leaf/Cascade/Parallel/Route/Decide and the gate/budget models — lives
with the normalizer (`openreading.strategies.normalize`), the stage that actually consumes it
(shorthand→longhand, `extends`, copy-down). Keeping node typing there, not here, avoids a fragile
recursive smart-union that would re-encode the grammar the schema already owns.

Companions: `openreading.strategies` (package map, discovery, quickstart); `.engine` (what
running a tree means: evaluation order, keep-best, cancellation, cost accounting, cache laws);
`.signals` (the full signal catalog); `.facts` (route facts); `.decider` (the LLM decider and its
downgrade taxonomy); `.plain` (the Plain dialect desugar); `.validate`; `.normalize`; `.presets`;
`.calibrate`. Rationale: internal/decisions/DECISIONS.md (D-v3-*),
internal/design/simple-strategies.md, internal/design/decider-executor.md; why the design looks
this way — the prior-art survey — is internal/research/strategies/prior-art.md. "The spec" below
means internal/archive/reference-docs-2026-08/strategies/spec.md, the design document this
grammar was written from. Where that document and the engine disagree, the engine is what ships.

Doc contract (D-v3-22; the docs-truth test, `tests/test_docs_truth.py`): every fenced YAML block
in this docstring — and in the `openreading.strategies` package, `engine`, `signals`, `decider`,
`presets`, and `plain` docstrings — is extracted and must pass `desugar_config` +
`validate_config` with no errors (the world-consistency half of `strategy validate`; the test
does not run the loader's JSON-Schema pass), so a docstring edit that breaks that contract fails
`make verify`. Config-like blocks are wrapped into a full config with referenced-but-undefined
strategy names stubbed; bare gate / error maps, a `{pick, judge}` options snippet, and the
preset-definition showcase are skipped, and a floor assertion on the block count keeps the
classifier from passing vacuously.


1. File, discovery, precedence
==============================

1.1 File shape
--------------

One file: `openreading.yaml` (`.yml` accepted; JSON accepted — the schema is JSON Schema). Parsed
with `yaml.safe_load` only (D-v3-1: a config file may never construct arbitrary Python objects;
`pyyaml` is imported lazily so the no-config path never pays the import). Top level:

```yaml
version: 1                    # required — config format version (additive evolution)

policy:                       # optional — superset of the existing --policy JSON, same flat keys
  require_baa: true           #   compliance keys → request.compliance (unioned, most-restrictive-wins)
  no_train_on_data: true
  allow_unverified_compliance: false    # deployment keys → RouterConfig, as today
  baa_tier_confirmed: [reducto]         #   tier-gated BAAs this deployment has actually signed

limits:                       # optional — operator ceilings on every strategy-engaged run (§6.4)
  max_duration_per_doc: 10m

decider:                      # optional — LLM decider configuration; inert without the env gate (decider.md)
  llm: { backend: anthropic-claude, timeout: 5s, send_document_content: false }

defaults:                     # optional — deployment defaults
  strategy: cost_saver        #   applied ONLY when backend.id == "auto" and no strategy is named
                              # `advanced:` (circuit_breaker, attempt_timeout) parses and is
                              # refused by `strategy validate` — nothing reads it (§8)

strategies:                   # the library of named strategies (§2)
  cheap_first:
    steps: [pymupdf, reducto]
    escalate_if: default
```

Secrets never appear in this file — backends resolve credentials from the environment exactly as
on the direct path (`credentials_ref` indirection only). Any key matching a credential pattern is
a `strategy validate` error (`validate._scan_secrets`) — the schema's open sub-trees (`policy`,
`with.*`) do not lock it down, so such a file still parses (§9). Under `with.*` it then runs;
under `policy:` it does not, because a secret-looking key is also an unknown policy key and
`openreading.config` refuses the whole block where the file is read, before any of it becomes a
constraint.

1.2 Discovery order (first hit wins; sources are never merged)
--------------------------------------------------------------

1. Explicit: CLI `--config PATH`, Python `openreading.run(config=...)`.
2. The `OPENREADING_CONFIG` environment variable.
3. `./openreading.yaml` in the working directory — CLI and Python API ONLY.
4. Nothing found → no config; behavior is byte-identical to the no-file path.

The server loads config only via `OPENREADING_CONFIG` (`loader.discover(allow_cwd=False)`) — a
long-running service must never change behavior because a stray YAML landed in its cwd. There is
no home-directory discovery. A found-but-broken file raises `ConfigError`: an explicitly
requested config that cannot load is an error, never a silent fall-through.

1.3 Invoking a strategy, and precedence
---------------------------------------

D-v3-2: `strategy:` is a documented reserved prefix of the already free-string `backend.id`, so
no request-schema bump. `loader.strip_strategy_prefix` is the recognizer the server uses;
`openreading.api` deliberately inlines its own `_STRATEGY_PREFIX` / `startswith` check instead,
so the no-file path never imports this package (the no-change law — package docstring).

- `backend.id: "reducto"` (any concrete id) → Strategy layer bypassed entirely — the direct path,
  including the compliance check and `ComplianceRefused`. `limits:` does not apply (§6.4).
- `backend.id: "strategy:<name>"` → Run the named strategy. Unknown name → `unknown_strategy` error
  (HTTP 400; CLI `parse` exit 2).
- `backend.id: "strategy:none"` → Force the legacy path even when `defaults.strategy` is set — the
  per-request escape hatch.
- `backend.id: "auto"` + file sets `defaults.strategy` → Run that strategy (the operator explicitly
  opted `auto` traffic in).
- `backend.id: "auto"`, no file or no `defaults.strategy` → The 3-stage router plan + serial
  executor, unchanged.
- CLI `--strategy <name>` / `--no-strategy` → Sugar for `backend.id: "strategy:<name>"` /
  `"strategy:none"`. Python: `openreading.run(..., strategy="<name>")`.
- `routing.fallback` present AND a strategy engaged → The strategy wins; the list is ignored with a
  `strategy_overrides_fallback` warning. Without a strategy, `routing.fallback` behaves exactly as
  before (formally the desugared cascade of §7 rule 7).

Precedence, highest first: **request wire fields → CLI flags → config `defaults:` → built-ins.**
Compliance is outside precedence: constraints from the request, `--policy`, and the file's
`policy:` block are **unioned, most-restrictive-wins** — constraints only ever add. D-v3-12 fixes
the union (done in `prune.compile_strategy`): the boolean PHI constraints `require_baa` /
`no_train_on_data` / `require_local` OR to True; `data_region` / `max_retention` take the
request's value if set, else the file adds it (request-wins-else-file — there is no total order
on regions, and the request is the more specific choice); deployment keys map to `RouterConfig`
(`allow_unverified_compliance` ORs; `train_optout_confirmed` / `baa_tier_confirmed` union). The
effective compliance is what prunes the tree AND what the route `compliance` facts read.

The block is refused whole (`ConfigError`, from `openreading.config.load`) if it names a key
outside `api.POLICY_KEYS` or gives one the wrong type. The schema declares this sub-object
`additionalProperties: true`, so that check is the only thing standing between a typo and a run
with no constraint: `require_locall` used to be dropped in silence, and a quoted
`allow_unverified_compliance: "false"` was truthy enough to switch the fail-closed tolerance ON.


2. The node grammar — five node types, closed, recursive
========================================================

A strategy is a **node**: one of five map forms, discriminated by exactly one key, or a
shorthand (§7):

- leaf (`backend:`) — Run one backend (or `auto` = router's stage-3 pick among still-eligible,
  not-yet-attempted backends).
- cascade (`steps:`) — Serial escalation: run steps in order; gates on each step decide accept vs
  escalate; errors advance per `on_error`.
- parallel (`parallel:`) — Fan-out: run children concurrently; `pick:` selects the result (race /
  best / merge).
- route (`route:`) — Conditional dispatch over pre-parse facts; first-match rules + mandatory
  `default:`.
- decide (`decide:`) — An explicit decision point: choose among listed sub-nodes. Engine mode takes
  `otherwise:`; LLM mode asks the decider.

Plus the **reference form** `use: <name>` (or the string `"strategy:<name>"`), valid anywhere a
node is expected. `use` cycles are `strategy validate` errors with the cycle path printed (one
that survives to run time raises `StrategyReferenceCycle`); `extends` cycles are `NormalizeError`s
(§2.8 — and `extends` itself is schema-rejected in a file).

Fields valid on any node:

- `intent:` [string ≤ 200 chars] — Prose for humans, UIs, and the LLM decider. The engine ignores
  it; prose can never relax a hard field.
- `label:` [string] — Stable node identity for UIs and traces (defaults to the node path, e.g.
  `steps[1]`; paths break on reorder, labels don't).
- `budget:` [object] — `{max_duration, max_attempts}` — §6 (`max_attempts` is accepted but not
  enforced by the engine — §6.1).
- `on_error:` [map] — Error-class routing — §5. Meaningful on cascades and their steps (§5.3).

Durations everywhere are strings with units (`"90s"`, `"2m"`, `"250ms"`; schema pattern
`^[0-9]+(ms|s|m|h)$`). A bare number is a load-time error, reported by the loader as the raw
schema message at its instance path. Inside a strategy the node `oneOf` collapses that path to
the strategy itself (`invalid config at 'strategies/x': {...} is not valid under any of the given
schemas`); a key-level path appears only at non-`oneOf` sites (`invalid config at
'limits/max_duration_per_doc': 60 is not of type 'string'`). There is no "did you mean" for
durations — nor for a misspelled Plain key, which gets the same raw node message; the package's
only such hint is for an unknown backend/strategy NAME inside a Plain `try`/`race`/`compare`
item (the desugar, §11).

2.1 Leaf (`backend:`)
---------------------

```yaml
backend: reducto
operation: extract            # optional — request.backend.operation for this leaf
version: "2025-06"            # optional pin — request.backend.version
timeout: 60s                  # accepted by the grammar; NOT read by the engine (see below)
with:                         # optional per-leaf request overrides
  features: { ocr: force }
  outputs:  { tables: html }
```

- `backend` — Registry slug, or `auto` = the router's stage-3 scoring (honoring the request's
  `optimize_for`) picks among backends that are (a) still compliance/capability-eligible and (b) not
  yet attempted anywhere in this walk — including parallel branches and quality-escalated rungs. An
  `auto` leaf can therefore never re-run a backend the walk already tried.
- `with` — Designed to shallow-merge over the request for this leaf only. Closed allow-list:
  exactly `features`, `outputs`, `pages`, `extraction_schema`; any other key is a load-time
  error — in particular `compliance`, `document`, and `backend` can never appear (a strategy may
  tune what a rung produces, never what is parsed or the compliance posture). **Designed, not
  shipped:** the grammar accepts it and `normalize` carries it into the longhand, but the engine
  never reads it — `_run_leaf` submits `ctx.req` unchanged.
- `timeout` — Designed as a per-attempt deadline for this leaf, clamped to the remaining node
  deadline (the containment rule). **Designed, not shipped:** the grammar accepts the key and
  `validate` warns when it exceeds the effective deadline ("it will be clamped"), but the engine
  never reads it — `_run_leaf` hands the driver the remaining *node* deadline
  (`deadline_ms - started`), so every leaf is bounded only by the enclosing `budget.max_duration`
  / `limits:` deadline (§6.3).
- `operation` / `version` — Designed as per-leaf `request.backend.operation` /
  `request.backend.version`. **Designed, not shipped:** accepted by the grammar, never read by
  the engine (`_step_request` takes `version` from the descriptor's `runtime.version_pin`, and
  the request's own `backend.operation` is what reaches the adapter).

Two things a leaf deliberately does NOT have:

- **No retry knobs.** Same-backend retry lives in exactly one place — the driver (DECISIONS
  D-v2-7.2). A `RetryableError` surfacing to the strategy engine means "this backend is
  exhausted → advance." The grammar cannot express a retry loop (any `retry`/`max_retries` key
  is rejected by the schema).
- **No credentials.** Skip-on-missing-credentials is inherited unchanged: an un-credentialed
  leaf is a `skipped` attempt, never an error, with no network preflight.

2.2 Cascade (`steps:`)
----------------------

```yaml
steps:
  - backend: pymupdf
    escalate_if: { chars_per_page_below: 100, garbled: true }   # hard gate — any-of (§4)
    review_if:   { confidence_below: { value: 0.85, on_missing: escalate } }   # gray band → decision
                                                                # point; pymupdf has no confidence → escalate
    review_default: escalate                                    # the engine's answer for the band (default escalate)
  - reducto
granularity: document          # document (default) | page
on_quality_exhausted: best_effort   # best_effort (default) | fail
escalate_if: default           # cascade-level gate — copied to every step except the last (§7 rule 5)
```

- `steps` [list of nodes; required] — Ordered. Each item is a leaf, any nested node, or a
  reference.
- `escalate_if` [gate map, `default`, or `off`; default `off`] — Cascade-level: normalization copies
  it onto each step lacking its own, except the final step. `default` = the built-in bundle (§4.4).
  Per-step: this step's hard gate. The unquoted YAML `off`/`no`/`false` arrives as boolean `False`,
  which the grammar accepts as "no gates" (D-v3-22 — widened the grammar rather than the docs,
  because every user writes the natural unquoted form).
- `review_if` (per step) [gate map; default absent] — The gray band: evaluated only when
  `escalate_if` did not fire. A match is a decision point with actions `{accept, escalate}`
  (`openreading.strategies.decider`).
- `review_default` (per step) [`accept` or `escalate`; default `escalate`] — The engine's resolution
  of the band (and the LLM decider's fallback). Escalate is the safe default: false-escalation costs
  cents, false-acceptance costs correctness.
- `granularity` [`document` or `page`; default `document`] — `page`: gates evaluate per page; only
  failing pages escalate to the next step (§2.7).
- `on_quality_exhausted` [`best_effort` or `fail`; default `best_effort`] — All steps ran, none
  passed its gate: return the best retained result with warnings, or fail the strategy.

Step-by-step semantics (normative evaluation order: `openreading.strategies.engine`):

1. Steps run in order. A step is a full node — a cascade step can be a `parallel` or a `use:`. A
   `parallel` step's own `escalate_if` (§2.3) is evaluated on the selected winner's response,
   with the same accept/escalate/keep-best semantics as a leaf step — this is how the "wrap the
   race in a cascade step and gate the step" idiom works. Nested-cascade and `use:` steps carry
   no step-position gate; they run their own internal rules.
2. A step that **fails** (error) routes through `on_error` (§5): `next` advances, `fail` stops.
3. A step that **succeeds** evaluates its gates against the normalized result (for a `parallel`
   step, the winner's result):
   - `escalate_if` fires → advance to the next step, RETAINING this result as best-so-far (a
     low-quality result is a signal and a safety net, never discarded).
   - otherwise `review_if` fires → a decision point with actions `{accept, escalate}`. The
     engine resolves it to `review_default` (default `escalate`); an enabled LLM decider may
     choose instead.
   - neither fires → **accept**: this result is the strategy's answer.
4. A gate on the **final** step is legal: there is nothing left to escalate to, but a fired gate
   makes the result Deficient like any other and it enters the keep-best resolution — the
   returned result may therefore be an earlier rung's higher-scoring retained result, and
   `on_quality_exhausted: fail` applies. The winner carries a `quality_below_threshold` warning
   (or `budget_exhausted`, when a deadline rather than a gate ended the walk — §6.3.3).
5. Exhaustion — the keep-best law and when `PlanExhaustedError` is raised — is the engine's
   contract: best retained result with honest warnings if anything was retained, else
   `PlanExhaustedError` with the full attempt trail; never silence, never a fabricated status.

2.3 Parallel (`parallel:`)
--------------------------

```yaml
parallel:
  - docling
  - backend: reducto
    start_after: 5s            # hedge: launch only if no winner after 5s
  - backend: aws-textract
    shadow: true               # runs, recorded, can never win (audit/calibration)
pick: best                     # fastest | best | merge
judge:                         # optional, pick: best only — presence makes the comparison LLM-judged
  backend: anthropic-claude
  intent: Prefer complete, arithmetically consistent line-item tables.
  excerpt_chars: 4000
on_win: cancel                 # cancel (default) | drain
require: all                   # pick best/merge: how many non-shadow branches must finish (int | all)
```

- `parallel` [list of nodes (≥2); required] — Branches, started concurrently subject to
  `start_after` offsets. Each branch dispatches on the ONE per-request adapter instance
  (`ctx.registry.get(backend)`) — not a fresh instance per branch; the credential-leak invariant
  holds only because two sibling branches whose subtrees can dispatch the same backend id (`auto`
  leaves excluded) are a `strategy validate` error — which is also what makes the engine's
  cache-key no-collision law hold by construction. The run path never calls `validate_config`
  (§9): an unvalidated file with such siblings loads and runs.
- `pick` [`fastest`, `best`, `merge`; required] — `fastest` = race: first branch to return a
  *successful* response wins; per-branch quality gates are NOT evaluated in a race (speed is the
  point — wrap the race in a cascade step and gate the step if you want both). `best` = wait until
  `require:` non-shadow branches (default all) have *succeeded* — or every non-shadow branch has
  resolved — then compare and select one. `merge` = ensemble (§2.4).
- `judge` [object; default absent] — Only with `pick: best`. Absent → the engine's deterministic
  composite score compares candidates. Present → LLM-as-judge (`{backend, intent, excerpt_chars:
  4000, timeout}`), pairwise both orderings. `timeout` is accepted by the grammar but reserved:
  the engine never reads it and no per-judge-call deadline fires yet (the spec's `30s` default is
  unimplemented; `.decider` §3.4) — only the enclosing node deadline bounds the walk. A judge is a
  decision point like any other: it requires the `OPENREADING_LLM_DECIDER` env gate (a `decider:`
  block is *not* required for judge-only configs), and an unavailable/ineligible/ungated judge
  downgrades to the engine score with a `decider_downgraded` trace. A judge is itself a backend and
  must pass the stage-1 compliance filter for the request.
- `on_win` [`cancel` or `drain`; default `cancel`] — Losers: `cancel` = `adapter.cancel()` + task
  cancellation. `drain` = losers finish; results land in the trace, cost in `usage`. Whether
  `cancel` actually stops vendor-side billing depends on the backend's own `cancel_supported`
  descriptor field — honest per-backend semantics, not always a cost difference from `drain`.
- `start_after` (per branch) [duration; default `0s`] — Delayed start: the branch launches after
  this delay unless the node has already resolved. Under `pick: fastest` this is a true hedge (a
  winner cancels the launch); under `best`/`merge` the node cannot resolve early, so it is a pure
  stagger — the branch always launches (barring deadline). The spec asks for a sibling's *failure*
  to shortcut the remaining delay; the engine has no such wake — a parked hedge sleeps its full
  `start_after` even after every earlier sibling has failed (`.engine`, parallel evaluation §2).
  As shipped, `start_after` takes an explicit duration and rejects `auto`. Engine-observed
  percentiles are a roadmap extension that waits on latency telemetry.
- `shadow: true` (per branch) [bool; default `false`] — The branch runs and is fully recorded but is
  excluded from `pick` and can never win; a shadow is always drained. Combine with a route rule on
  `sample_percent` for deterministic audit sampling.
- `require` [int or `all`; default `all`] — `pick: best`/`merge` only: proceed to comparison once
  this many non-shadow branches have SUCCEEDED (`ok` / `composite_ok`). A failed branch resolves
  but does not count; once no more can succeed, the comparison runs over what completed. The
  spec also ends the wait at the node deadline; the engine's `_drive_best` has no deadline check
  — the deadline bounds hedge launch and cancel dispatch, never the wait itself (`.engine`).
- `merge` [object; default absent] — `{typed_fields: vote, text: best}` — the only merge policy
  (§2.4), spelled out for readability.

2.4 `pick: merge` (ensemble), as shipped
----------------------------------------

Merges `typed_fields` only: per field, majority value across branches; tie → highest reported
confidence; still tied → cheaper backend (descriptor cost midpoint), then first-listed.
`document.text/markdown/pages` are taken wholesale from the best-scoring branch (recorded as
`merge_base` in the trace). Merged fields carry per-field provenance in the trace; a channel no
branch produced stays absent, and a merged response never fabricates confidence for values whose
source had none. Text-level alignment voting and block-structure merge are out of scope for the
shipped merge.

2.5 Route (`route:`)
--------------------

```yaml
route:
  rules:
    - when: { doc_type: [invoice, bank_statement] }      # keys AND within one rule
      use: strategy:tables_heavy
    - when: { pages_over: 200, mime: application/pdf }
      use: strategy:big_docs
    - when: { compliance: { require_local: true } }      # compliance facts are routable (nested)
      use: strategy:local_only
    - when: { sample_percent: 5 }                        # deterministic content-hash bucket
      use: strategy:audited
  default: strategy:general                              # mandatory — schema-enforced
```

This is the single canonical route shape — `rules:` + `default:` sibling keys; there is no
list-with-else form. Rules are first-match-wins, top to bottom. All rules are evaluated for the
trace even after a match (shadowed-rule debugging). `when:` predicates are a flat, enum-keyed map
over pre-parse facts (§3.1); multiple keys in one `when:` AND together; alternatives are separate
rules; `any_of:` exists for OR (§4.3); there is no NOT (facts have polar variants). The
`compliance` fact is a nested `{field: value}` map, not a dotted key (D-v3-11 — the schema's
`additionalProperties: false` on `when` rejects the dotted spelling). A fact that cannot be
computed makes the rule not match — its fact record reads `status: "unavailable"` (the spec's
`fact_unavailable`; no such literal is emitted) — never an error; `default:` is the guaranteed
floor.

LLM-assisted routing is not a route feature — it is composition: put a `decide` node in
`default:` (or in any rule's `use:`).

2.6 Decide (`decide:`)
----------------------

```yaml
decide:
  among: [strategy:tables_heavy, strategy:text_light]
  otherwise: strategy:text_light      # mandatory — the engine's (and any failed decider's) choice
intent: >                             # node-level, like on every node — the decision criteria
  Choose tables_heavy for financial layouts with dense grids;
  text_light for prose-dominant documents.
```

- `among` [list of nodes/references (≥2); required] — The enumerated candidate set. Each
  entry is any node form; references are the common case.
- `otherwise` [one member of `among`; required] — The deterministic resolution: engine mode
  always takes it; LLM mode falls back to it on any decider failure. Must be (structurally equal to)
  one of `among`'s entries — the engine default is always an action the decider could also have
  chosen.

`intent:` sits at node level and is the decision criteria — the decider's semantic payload and
the UI's display text, inert to the engine. The engine resolves a `decide` to `otherwise:`
unconditionally; an enabled LLM decider chooses one member of `among` under the
`openreading.strategies.decider` contract. Missing `otherwise:` and `among:` with fewer than two
entries are schema (load-time) errors; `otherwise:` not a member of `among:` is a `strategy
validate` error (§9). When compliance
prunes the `otherwise:` target but at least one `among:` survivor remains, the engine substitutes
the first-listed survivor and traces `decider_downgraded: otherwise_pruned` (D-v3-24 — pruning,
not decision-making, drives it, so it fires whether or not any decider is configured).

**Candidate names must be distinct.** Each `among:` entry is named by its `use:` reference, else
its leaf `backend`, else its explicit `label:`, else — for an unnamed sub-tree — its node kind
(`steps` / `parallel` / `route` / `decide`), in that order. That name is the decider's entire
action space and the key the engine dispatches on, so two entries resolving to the same name (two
unlabeled `parallel` sub-trees, say) is a `NormalizeError`, as is an entry named `otherwise` (the
engine's own fallthrough action) — raised by `normalize._check_candidate_labels`, so it surfaces
from `strategy validate` and, on the run path, at compile time (§9). Give a composite candidate a
`label:`; split duplicate leaves
into separately named strategies.

2.7 Page granularity (`granularity: page`, cascades only)
---------------------------------------------------------

Gates are evaluated per page; only failing pages escalate to the next step, sent as
`pages.ranges` where the next backend's descriptor supports ranges. Results are stitched with
per-page provenance (`orchestration.pages[] {page, backend}`; the response `pages[]` gains an
additive `source_backend` field). Constraints: PDF input; a rung whose backend lacks native
page-range support forces document granularity for that rung (validation warns).

2.8 Named strategies, `extends`, presets
----------------------------------------

`strategies:` is a flat library; deep nesting becomes named references.

**`extends:` is designed, not shipped as a file key.** The vendored v0.2 schema carries no
`extends` property on any node form. A file that declares one fails schema validation in the
loader, which prints `invalid config at 'strategies/invoices': {...} is not valid under any of
the given schemas` and exits 3 from every `strategy` subcommand. The merge itself exists as
`normalize._resolve_extends`, which tests exercise against in-memory `StrategyConfig` objects.
To reuse a preset today, run `openreading strategy show <name>` and paste the printout into your
own file.

- `extends: <name>` (the normalize-level semantics) copies another strategy (built-in presets
  included) then applies a shallow, field-level merge: a re-declared top-level clause replaces
  the base's wholesale (no deep list splicing — predictability over cleverness). Refs may extend
  refs; a cycle or an unknown base is a `NormalizeError`.
- Built-in presets are ordinary strategies pre-registered under reserved names.
  `openreading strategy show <name>` dumps the exact YAML for forking. Defining a strategy under
  a preset's name is a `NormalizeError` ("strategy 'cost_saver' collides with a built-in preset;
  use `extends: cost_saver` or rename") raised by `normalize.build_library` — reported by
  `strategy validate` and, on the run path, at compile time (`prune.compile_strategy`); the file
  itself loads.
- `use:` references resolve at execution and are preserved by name through normalization
  (stable UI identity); `extends` would resolve at normalize time into the expanded tree.

The four vendored presets (`openreading.strategies.presets`) — normative; exactly what
`strategy show` prints:

```yaml
cost_saver:            # free/cheap first; pay only when the local parse looks bad
  intent: "Local parse first; escalate to the router's best remaining pick only on bad quality."
  steps: [pymupdf, docling, auto]
  escalate_if: default

max_accuracy:          # router's best pick, escalating to its next-best on quality gates
  intent: "Best eligible backend; second opinion from the next-best when quality gates fire."
  steps: [auto, auto]          # rung 2's auto excludes rung 1's pick (attempted-set rule)
  escalate_if: default

fast:                  # race the local parsers; first success wins
  intent: "Lowest latency: race the local parsers, keep the first success."
  parallel: [pymupdf, tesseract]
  pick: fastest
  on_win: cancel

offline_first:         # local-only cascade — pair with policy require_local to ENFORCE locality
  intent: "Never leave the machine. Enforcement belongs to policy: { require_local: true }."
  steps: [pymupdf, tesseract, docling]
  escalate_if: default
```


3. Facts and signals
====================

Two catalogs, both flat, enum-keyed, primitive-valued — trivially machine-evaluable,
form-renderable, and LLM-reliable. `openreading.strategies.signals` is the full catalog with
definitions, availability, and computation notes; this section is the binding summary.

3.1 Pre-parse facts (route `when:`)
-----------------------------------

- `doc_type` [enum or list (the request's 10-value vocabulary)] — source: `routing.doc_type_hint`;
  the roadmap's auto-classifier when it lands (hint wins). If unavailable: rule doesn't match.
- `mime` [string or list] — source: `document.mime_type`. If unavailable: rule doesn't match.
- `pages_over` / `pages_under` [int] — source: page-count probe over materialized PDF bytes (pypdf —
  permissive-license, core). If unavailable: rule doesn't match + a `status: "unavailable"` fact
  record (the spec's `fact_unavailable`).
- `size_over_mb` / `size_under_mb` [number] — source: materialized byte length. If unavailable: rule
  doesn't match.
- `filename_matches` [regex string] — source: `document.filename`. If unavailable: rule doesn't
  match.
- `compliance.<field>` [bool/string] — source: the post-union effective compliance (request ∪
  `--policy` ∪ file `policy:`, most-restrictive-wins — the same constraint set that pruned the
  tree). Consequence: a file `policy:` key makes its matching fact constant for every request. If
  unavailable: always available.
- `sample_percent` [number 0–100] — source: deterministic sha256(document bytes) bucket — stable per
  input, idempotency-cache compatible. If unavailable: always available once materialized.

3.2 Post-parse quality signals (gates `escalate_if:` / `review_if:`)
--------------------------------------------------------------------

**Tier 1 — engine-computed, always available.** These exist precisely because cheap rungs
(pymupdf, docling text-layer) emit no confidence; they are computed by a reference-free quality
probe over the `NormalizedResponse` of every strategy-engaged attempt (and surfaced as trace
telemetry whether or not a gate references them — never on the no-config path, where the
no-change law binds):

- `chars_per_page_below: N` — mean extracted chars per selected page below N (~100 is the
  field-tested scan cut)
- `empty_pages_over: F` — fraction of pages with fewer than 25 chars exceeds F
- `garbled: true` / `garble_score_over: F` — composite mojibake score =
  0.4·(replacement/control-char ratio) + 0.35·(non-wordlike-token ratio) + 0.25·(non-ASCII-run
  ratio), normalized [0,1]; `garbled: true` ≈ score > 0.3
- `scanned_pages_detected: true` — any page with no text-layer text and at least one image
  (the probe does not measure image area; "no text + an image ≈ a scanned page")
- `text_source: none` — text-layer analysis on PDF inputs. The gate value enum is exactly
  `prior_ocr | none`; `digital` is what the probe reports for a text-bearing PDF, never a gate
  value (the schema rejects it at load). The probe classifies only `digital` (any page has
  extractable text) vs `none`; `prior_ocr` (an invisible OCR layer over a scan, lower-trust)
  needs render-mode / image-coverage analysis pypdf does not expose, and the naive
  "text + any image" heuristic false-positives on a born-digital page with a decorative image,
  so a `text_source: prior_ocr` gate is evaluated but never matches (D-v3-9)
- `table_sanity_below: F` — ragged-row / empty-cell composite over emitted tables
- `zero_blocks: true` — no blocks emitted
- `matches_regex: "..."` — user regex over document text
- `sample_percent: N` — same deterministic bucket as §3.1 (audit escalation)

**Cross-branch — `pick: best` parallel steps only.** `disagreement_over: F` (added in
`strategy-config.v0.2.json`; the Plain `disagree` criterion, §11) is neither Tier 1 nor Tier 2:
the engine injects it into the snapshot only when gating a `pick: best` parallel step's winner,
as the worst pairwise 1 − token-set Jaccard of `document.text` across the finished non-shadow
branches. Writing it on any other node's gate is a `strategy validate` ERROR ("disagreement_over
compares parallel branches — it only works on a `pick: best` parallel step"; Plain likewise
confines `disagree` to a `compare:` body, §11) — not an inert signal. At run time it is absent
from a single-response probe and when fewer than two branches finished, where it is an
unavailable signal — never fires, traced `signal_unavailable` (§3.3). The value is also recorded
on the winner attempt as telemetry.

**Tier 2 — backend-reported, present only when the backend's response carries them:**

- `confidence_below: F` — mean over the pages that REPORT a `pages[].confidence` (aggregation
  defined once, in the engine); the signal is absent when no page reports one
- `page_confidence_below: F` — any page's confidence below F
- `field_confidence_below: F` or `{fields: {name: F, ...}}` — `typed_fields{}.confidence` (numeric
  only; qualitative strings never coerced). The bare float applies to every extracted field; the
  `fields:` sub-key form sets per-field thresholds — `fields:` is required for the per-field map so
  the form never collides with the §3.3 `on_missing` wrapper
- `fields_required: [name, {name, aliases: []}]` — fires when a named typed_field is missing or
  empty — absence is a first-class trigger
- `doc_type_confidence_below: F` — `document.doc_type.confidence`
- `warning_code: <code>` — a `warnings[]` entry with this code is present

3.3 The missing-signal law
--------------------------

- At runtime, a predicate over an ABSENT signal is *inapplicable*: it does not fire, and the trace
  records it as skipped with reason `signal_unavailable` — never silently, never guessed.
- Per-predicate override, object form: `confidence_below: { value: 0.7, on_missing: escalate }`
  (`on_missing: skip` is the default). The spec says "any gate key"; the shipped schema offers
  the `{value, on_missing}` wrapper only on the numeric/boolean threshold keys (plus
  `field_confidence_below`'s own `{fields, on_missing}` form) — `text_source`, `sample_percent`,
  `matches_regex`, `warning_code` and `fields_required` take bare values and reject the wrapper
  at load.
  `value` and `on_missing` are reserved keys that always mean this wrapper form; the one
  predicate with its own object shape (`field_confidence_below`) keeps its per-field map under an
  explicit `fields:` sub-key precisely so the two forms never collide —
  `field_confidence_below: { fields: {total: 0.9}, on_missing: escalate }` composes both.
- `fields_required` fires on absence by definition — that is its job.
- Statically, `openreading strategy validate` reads adapter descriptors: a user-written gate
  whose predicates can ALL never bind on its step's backend (e.g. only `confidence_below` on
  `pymupdf`, whose descriptor grades `block_confidence: X`) is a `strategy validate` error with
  the fix in the message, located at the gate path (e.g. `steps[0].escalate_if`): "gate can never
  fire on 'pymupdf': none of its predicates (confidence_below) bind — pymupdf emits no confidence.
  Add an always-available signal such as chars_per_page_below or garbled, or set on_missing:
  escalate"
  (`dialect: plain` re-phrases it in the Plain vocabulary: "the ... check can never fire on
  'pymupdf' — it reports no confidence; add a criterion that works everywhere, e.g. `looks_bad:
  true`"). Two exemptions: gates sourced from the built-in `default` bundle (designed to
  degrade — Tier-1 members carry the load), and `backend: auto` leaves (no fixed descriptor;
  checked at runtime via the trace instead). The honest spelling for a review band on a
  confidence-less rung is `confidence_below: { value: 0.85, on_missing: escalate }` — escalate
  when the rung cannot report confidence (D-v3-22).


4. Gate syntax and combination rules
====================================

A **gate** is a map of quality conditions attached to a cascade step. The engine evaluates it
against that step's normalized result and decides whether to accept the result or move on.

4.1 Polarity — the one rule to memorize
---------------------------------------

- A **gate map** (`escalate_if`, `review_if`) ORs its keys: *any* listed condition fires the
  gate. Mnemonic: *any reason to distrust ⇒ move on.*
- A **route `when:` map** ANDs its keys: *all* facts must match for the rule to fire. Mnemonic:
  *all facts match ⇒ this lane.*

This asymmetry matches how people naturally read each construct; it is stated in the JSON Schema
`description` of every gate and `when:` key (editor hovers, UI labels) and shown in every trace
record.

4.2 Forms
---------

```yaml
escalate_if: default                      # the built-in bundle (§4.4)
escalate_if: off                          # no quality gate (errors still advance per §5)
escalate_if:                              # flat map — keys OR
  chars_per_page_below: 100
  garbled: true
  confidence_below: { value: 0.7, on_missing: skip }    # object form unlocks on_missing
```

4.3 One combinator level (no more)
----------------------------------

```yaml
escalate_if:
  any_of:                                 # explicit OR (same as flat keys)
    - all_of:                             # AND group
        - confidence_below: 0.8
        - text_source: prior_ocr
    - garbled: true
```

`any_of:` and `all_of:` are the two combinators; there is no `not:`. Authored gates use one
combinator level (an `any_of`/`all_of` of flat predicate maps), but the grammar and the evaluator
are recursive — a combinator may hold another (e.g. an `all_of` pair inside an `any_of`, which
the Plain `looks_bad` scan member compiles to — §11). Thresholds are validated to their domains
([0,1] for confidences and fractions) at load time.

4.4 The `default` bundle
------------------------

`escalate_if: default` expands (visibly, via `strategy normalize`; `normalize.DEFAULT_BUNDLE`) to:

```yaml
escalate_if:
  scanned_pages_detected: true
  garbled: true
  empty_pages_over: 0.2
  confidence_below: 0.6        # Tier-2 bonus: silently inapplicable on confidence-less backends
```

The load-bearing members are Tier-1 (always available), so the bundle works on every backend;
`confidence_below` adds signal where confidence exists. Bundle-sourced gates are exempt from the
unbindable-gate validation error (§3.3) — the bundle is designed to degrade. Thresholds err
toward escalation: false-escalation costs cents, false-acceptance costs correctness. The
`openreading calibrate` tool (`openreading.strategies.calibrate`) translates target escalation
rates into tuned thresholds — users pick rates and budgets; tools derive thresholds.


5. Failure routing (`on_error:`) and the error-class taxonomy
=============================================================

5.1 The closed taxonomy
-----------------------

Failure routing is by error CLASS, never HTTP status codes — backends include local libraries.
The classes map onto the existing four-exception taxonomy (`openreading.types.errors`):

- `timeout` — driver deadline exhausted / RetryableError timeout codes. Default action: `next`
- `rate_limited` — RetryableError 429-class, driver backoff already exhausted. Default action:
  `next`
- `auth` — credentials present but rejected. Default action: `next`
- `provider_error` — TerminalError provider-side / RetryableError exhausted. Default action: `next`
- `unsupported_feature` — UnsupportedFeatureError. Default action: `next`
- `invalid_input` — TerminalError classified as caller-input (corrupt doc, bad password). Default
  action: `fail` — a corrupt document fails on every backend; never burn the cascade
- `exhausted` — a composite child (parallel/cascade) ran out of children, or an `auto` leaf found no
  untried eligible backend. Default action: `next`
- `budget_exhausted` — a node's time deadline (`max_duration`) was reached. Default action: `next`
- `missing_credentials` — broker cannot resolve required env vars. Default action: always `skip` —
  not configurable, never an error
- *(compliance)* — ComplianceRefused. Default action: uncatchable — pruned before execution; naming
  it in `on_error` is a load-time error

Alias: `transient` = `timeout` + `rate_limited` + `provider_error`. `any` = every catchable
class.

**The normative classifier** (`engine.classify_error`), driven by exception type plus the
adapter's `backend_code` (the verbatim code every `AdapterError` already carries):

- `UnsupportedFeatureError`, backend_code any → `unsupported_feature`
- `MissingCredentialsError`, backend_code — → `missing_credentials` (skip)
- `RetryableError` (driver exhausted it), backend_code deadline/timeout codes, or the driver's own
  "deadline exceeded" → `timeout`
- `RetryableError` (driver exhausted it), backend_code rate-limit codes (`429`, `rate_limited`,
  `throttled`, `resource_exhausted`) → `rate_limited`
- `RetryableError` (driver exhausted it), backend_code anything else → `provider_error`
- `TerminalError`, backend_code auth codes (`auth_rejected`, `permission_denied`, `invalid_key`) →
  `auth`
- `TerminalError`, backend_code document-is-bad codes (`corrupt_document`, `invalid_password`,
  `unreadable_input`) → `invalid_input`
- `TerminalError`, backend_code this-backend-can't codes (`doc_too_large`, `page_limit_exceeded`) →
  `unsupported_feature` — a different backend with higher limits can still succeed, so it advances
- `TerminalError`, backend_code anything else (including unknown codes) → `provider_error`
- `ComplianceRefused`, backend_code — → never reaches the engine for tree backends (pruned); from a
  decider/judge eligibility check it triggers the downgrade path, not `on_error`

Adapters keep mapping native errors onto the four exception types exactly as before; the code
sets shown here are the documented `backend_code` vocabulary adapters should prefer. Unknown codes
default to `provider_error` (advance) — the conservative direction.

5.2 Form — a flat map
---------------------

```yaml
on_error:
  invalid_input: fail          # (already the default; shown for clarity)
  transient: next
  any: next                    # `any` sets the action for classes not otherwise listed
```

Actions are exactly two: `next` (advance in the enclosing serial order / lose the branch) and
`fail` (stop this strategy with the full trail). There is no `retry` action — same-backend retry
is the driver's alone — and no `goto`: recovery structure is the tree itself. Specific class keys
override `any`. `missing_credentials` and `compliance` are invalid keys (load-time error).

5.3 Where `on_error` binds
--------------------------

`on_error` is meaningful on a **cascade** (governs all its steps) and on an individual **step**
(overrides the cascade's, key by key). Inside a **parallel** node, a branch failure is
branch-terminal — recorded, never routed; the composite outcome when all branches fail is the
engine's. At the strategy root, an unhandled `fail` or exhaustion maps to the existing
exceptions (`TerminalError` / `PlanExhaustedError`) unchanged.

**Quality-below-threshold is deliberately not an error class.** Gates (§2.2) are the same
advance-the-chain state machine with a different classifier — but a gated step *retains* its
result while a failed step has none. Forcing one syntax onto both made both worse.


6. Budgets, timeouts, deadlines
===============================

6.1 The two currencies — `budget:` attaches to any node
-------------------------------------------------------

- `max_duration` — Converted once, at node entry, into an absolute deadline that bounds every
  nested node's orchestration loop and the driver calls of cascade / paged-cascade leaves
  (`deadline_ms - started`). A parallel branch's leaf is the exception as shipped: its driver
  runs with the constant `DEFAULT_DEADLINE_MS`; the node deadline bounds the branch's launch,
  cancel dispatch and drain, not the driver call itself (see `engine`).
- `max_attempts` — Designed as the total attempts that reached a backend across the subtree
  (skips, driver-internal retries and `decider_call`/`judge_call` not counted). **Designed, not
  shipped:** the grammar accepts the key and `validate` reads it for one static warning (§9), but
  the engine never reads it — `_enter_budget` consumes only `max_duration`; nothing counts
  attempts or stops dispatch on an attempt count. `max_duration` is the only enforced budget,
  and `strategy validate` **refuses** a file that declares `max_attempts` (§9) rather than
  reporting a ceiling nothing holds.

6.3 Propagation laws
--------------------

1. **Deadline:** `deadline_at = min(now + max_duration, parent.deadline_at)` at node entry; the
   absolute time is passed down. The spec additionally clamps leaf `timeout:` to the remaining
   deadline; the engine never reads leaf `timeout:` (§2.1) — the remaining node deadline is the
   only per-attempt bound the driver receives.
2. **Attempts (spec only):** the spec's law — children draw from the innermost enclosing attempt
   budget, `child_effective = min(child_declared, parent_remaining)` — is not implemented; the
   engine never reads `max_attempts` (§6.1). What ships is a refusal: `validate` errors on any
   node declaring `max_attempts`, at that node's own path. A child `max_duration` above its
   parent's is legal and gets no static warning — the deadline law clamps it at run time.
3. **Exhaustion (deadline):** stop dispatching. With a retained result and
   `on_quality_exhausted: best_effort` (the default) the node resolves deficient on that result
   and the response carries a `budget_exhausted` warning — distinct from the
   `quality_below_threshold` a gated-out exhaustion carries, because the two ask the caller for
   opposite things and a corpus run has to be able to count the deadline misses. With nothing
   retained, or `on_quality_exhausted: fail`, a deadline-ended walk resolves
   `Err(budget_exhausted)` (the error class of §5).
4. **Decider and judge calls** run inside the enclosing node deadline like any attempt, but no
   per-call deadline is applied to them yet (`.decider` §3.4); with no attempt counter in the
   engine there is nothing for them to be excluded from.

6.4 `limits:` — the operator ceiling
------------------------------------

`limits: {max_duration_per_doc}` is an implicit outermost budget wrapped around EVERY
strategy-engaged run (named strategies and `defaults.strategy` alike). No strategy, nested
budget, or LLM decider can exceed it. It does NOT apply to direct-named backend requests
(`backend.id: "reducto"`), which bypass the strategy layer entirely — the one place the wall does
not reach: **`limits:` binds strategies, not direct requests.** (D-v3-12: it is applied as the
outermost budget in `run_strategy`, the strategy-only entry point, so a direct request — which
never calls `run_strategy` — cannot see it by construction; nested budgets clamp to it.)

6.5 Composition order — a published contract, never configurable
----------------------------------------------------------------

    route/decide → [ limits → budget+deadline → cascade/parallel → driver retry
                     → per-attempt timeout → adapter call ]

The YAML expresses *which* layers exist, never their nesting order. The "per-attempt timeout"
layer is the remaining node deadline the driver is handed on each cascade leaf call (parallel
branches get `DEFAULT_DEADLINE_MS`, §6.1) — neither leaf `timeout:` (§2.1) nor
`defaults.advanced.attempt_timeout` (§8) feeds it.


7. Shorthand → longhand normalization (complete rules)
======================================================

Normalization is deterministic and shipped as a command (`openreading strategy normalize`). The
longhand tree is canonical — the JSON-serialization target for UIs and the LLM generation
contract. Every shorthand round-trips: `normalize(shorthand) = longhand`;
`normalize(longhand) = longhand`.

1. bare string `pymupdf` → `{backend: pymupdf}`
2. bare string `auto` → `{backend: auto}`
3. bare string `strategy:invoices` → `{use: invoices}`
4. a YAML list `[a, b]` → `{steps: [{backend: a}, {backend: b}]}` with `escalate_if: off` — exactly
   the legacy serial fallback chain; quality gates are always explicit
5. cascade-level `escalate_if` → copied onto each non-final step that lacks its own gate, then the
   cascade-level key is dropped (the canonical cascade carries per-step gates, never a top-level
   `escalate_if`). A nested-cascade step is SKIPPED — its `escalate_if` is that inner cascade's own
   level gate, not a step-position gate, so distributing onto it would be ambiguous and break the
   fixed point (D-v3-7: a second pass would re-distribute to the grandchildren). Leaf, reference,
   and parallel/route/decide steps distribute normally.
6. `escalate_if: default` → the §4.4 bundle
7. request `routing.fallback: [a, b]` (no strategy engaged) → `{steps: [a, b, <remaining eligible
   backends in stage-3 score order>], escalate_if: off}` — listed ids move to the front *within* the
   eligible set and the rest of the chain follows, exactly the legacy `_apply_explicit_fallback` +
   executor walk. The formal statement that the legacy chain is a point in this design, not a second
   engine
8. `extends:` → resolved at normalize time into the expanded tree (schema-rejected in a file,
   §2.8); `use:` refs stay by name
9. preset name → the vendored strategy's longhand


8. `defaults.advanced` — reserved engine knobs (accepted, not yet read)
======================================================================

The grammar and the `Advanced` / `CircuitBreaker` models below accept `defaults.advanced`, but
NO code reads it as shipped: the block is schema-validated and then ignored. Concretely —

- `circuit_breaker: {max_fails, cooldown}` — Reserved for a per-backend, process-level
  consecutive-failure breaker (design intent: 5 fails / 30s multiplicative cooldown, one half-open
  probe, an open breaker turning a leaf into a `skipped(circuit_open)` attempt that advances like
  a credential skip and is not routable via `on_error`, and an ejection floor of one so the last
  compliance-eligible backend is never benched). Not implemented: `skipped(circuit_open)` is
  reserved trace vocabulary (`openreading.strategies.trace`) the engine never emits.
- `attempt_timeout` — Reserved for a default per-attempt timeout on leaves that set none. Not
  implemented — and neither is the leaf `timeout:` it would default (§2.1): every leaf is bounded
  only by the enclosing deadline (§6).

Setting either key is inert, and inert is not harmless when the key is a safety limit: an author
writes a breaker or a per-attempt timeout precisely to bound a run that spends money at a vendor.
`strategy validate` therefore **refuses** a file declaring either key (§9), so it fails on the
bench instead of failing open in production.


9. Validation catalog
=====================

`openreading strategy validate` checks grammar (JSON Schema, in the loader) AND world-consistency
(`openreading.strategies.validate`). Errors go to stderr, warnings to stdout; it badges each
strategy `dialect: plain` / `dialect: advanced`; **exit code 3 on any error, 0 otherwise —
warnings never fail** (D-v3-8). `ConfigError` (unparseable / grammar-invalid / missing explicit
file) is exit 3 in every `strategy` subcommand. "no openreading.yaml found" is exit 3 only for
`validate`, `normalize`, and `plan`; `show` and `list` fall back to the presets alone and exit 0.

Two error tiers. The run path (`api`, the server, `prune.compile_strategy`, the engine) runs
only the first: `validate_config` is called by `strategy validate`, never by a run. The spec
called every row below "load-time"; only the first tier is.

**Grammar errors** (JSON Schema in the loader — the file will not load; `ConfigError`):

unknown predicate or fact key · missing route `default:` · `decide` missing `otherwise:` or
`among:` with fewer than two entries · threshold outside its domain · bare-number duration ·
`on_error` naming `compliance` or `missing_credentials` · `with:` containing any key outside its
closed allow-list (`features`, `outputs`, `pages`, `extraction_schema` — §2.1) · any `extends:`
key (§2.8) · the Plain desugar's own §8 violations (e.g. an unknown name in a `try`/`race`/
`compare` item, §11).

**`strategy validate` errors** (the file loads; reported only by `validate_config`. On the run
path the same defects surface later — as a `NormalizeError` at compile time, or as an ordinary
run-time attempt outcome):

unknown backend id · unknown `strategy:`/`use:` reference · `use` cycle (path printed) ·
`otherwise:` not a member of `among:` · two `among:` entries resolving to the same candidate
name, or one named `otherwise` (§2.6; `NormalizeError`) · secrets-pattern key · any of the three
unenforced guardrails — `budget.max_attempts` (§6.1), `defaults.advanced.circuit_breaker` and
`defaults.advanced.attempt_timeout` (§8) — refused rather than warned about, because each is a
safety limit and a limit that validates green while enforcing nothing is worse than none ·
sibling parallel branches whose subtrees can dispatch the same backend id (§2.3) · user-written gate whose
predicates can all never bind on its step's backend (§3.3) · `disagreement_over` on any gate
other than a `pick: best` parallel step's (§3.2) · strategy named after a built-in preset
(`NormalizeError`) · `strategy:none` used as a node.

**Warnings** (the file loads; the message says exactly what to change — the implemented set;
`openreading.strategies.validate` is normative):

shadowed route rule — a rule whose `when` exactly duplicates an earlier rule's and can never fire
(a child `max_duration` above its parent's is clamped by the deadline law without a static
warning) · leaf `timeout` exceeding the effective deadline (the message says "it will be
clamped", but the engine never reads leaf `timeout` — §2.1) · a final cascade rung that is a
`pick: fastest` node
whose branches carry their own gates (branch gates are not evaluated in a race — gate the
enclosing cascade step instead, §2.3) · `granularity: page` with a non-first rung whose backend
lacks native page-range selection (that rung re-parses the whole document, §2.7) · `review_if`
present with no decider configured (it will resolve to `review_default`; said up front) · a
`decide` node with no decider configured (it will always take `otherwise:`) · `fields_required`
(Plain `missing`) on a backend whose descriptor cannot produce typed fields (the rung always
escalates) · judged comparison with more than 3 non-shadow candidates (2·(n−1) pairwise LLM
calls — slow) · steps unreachable under the file's own `policy:` block or `--policy` ("`reducto`
is filtered out by the policy — remove it or relax the policy") · the Plain desugar's own
warnings (`openreading.strategies.plain`). There is no nested-fan-out / worst-case-attempts
warning.

Errors follow the Elm doctrine: locate (file + node path, e.g.
`strategies.cheap.steps[0].escalate_if`), explain in domain terms, suggest the fix. Line-precise
location is a documented follow-up (D-v3-8: `yaml.safe_load` discards source marks, and the
injected-`__line__` trick breaks the schema's `additionalProperties: false`; the node path already
satisfies "locate"). With `--policy p.json`, `validate` additionally flags steps that can never
run *in that compliance context*.


10. JSON Schema and editor experience
=====================================

The config schema is vendored at `src/openreading/schemas/strategy-config.v0.2.json` (the
`v0.1.json` artifact is byte-frozen; v0.2 adds the Plain body grammar and the `disagreement_over`
predicate additively — the config `version` const stays `1`) — the same schema-authority
convention as request/response/descriptor (and the same wheel force-include gotcha). Shorthands
are encoded as titled `oneOf: [string, array, object]` branches so editor completion stays clean.
For completion and inline validation in your editor, point a `# yaml-language-server: $schema=`
modeline at the vendored file. Use the path in your checkout or the copy inside the installed
package. Nothing serves the schema over HTTP, and SchemaStore carries no entry for it. Schema
`description` strings are written at tool-description quality: they are simultaneously editor
hovers and LLM-decider context, so one sentence serves two consumers.


11. The Plain dialect
=====================

**Plain** is a closed, eleven-word dialect of this file and the recommended front door
(`openreading.strategies.plain`; tutorial and full grammar: internal/design/simple-strategies.md).
It is *not a second engine*: the loader desugars a Plain body to the canonical longhand of §§2–6
before normalization, validation, and execution, so every law above binds unchanged. A strategy
body is Plain iff it is a bare string, a bare list, or a map whose keys are a subset of the six
Plain keys; any other key makes the body **advanced** (the full grammar). Both dialects coexist in
one `strategies:` map and reference each other by name. `openreading strategy validate` badges
each `dialect: plain` or `dialect: advanced`, and `strategy show <name> --longhand` prints the
compiled tree.

The surface: structure keys `try` (→ cascade), `race` (→ `parallel` + `pick: fastest`),
`compare` (→ `parallel` + `pick: best`), `then` (→ the final rung of a compare cascade),
`escalate_when` (→ per-step `escalate_if`), `max_time` (→ `budget:` on the body root); criteria
`looks_bad`, `low_confidence`, `missing`, `disagree`; and the `auto` leaf. Compiled gate
equivalences:

- `looks_bad` (defaults) → `scanned_pages_detected` + `chars_per_page_below` (an `all_of` pair),
  `garbled`, `empty_pages_over` — under one `any_of`
- `low_confidence: F` → `confidence_below: F` (`true` = 0.6)
- `missing: [names]` → `fields_required: [names]`
- `disagree: F` → `disagreement_over: F` (`true` = 0.3)

`looks_bad` is a deliberate, result-aware improvement over the `default` bundle (§4.4): its scan
member fires only when the input is image-only AND this attempt extracted almost no text (the
`all_of` pair), where the bundle uses the bare input-side `scanned_pages_detected`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A raw strategy node exactly as it appears in the file after schema validation: a bare string
# (backend id / "auto" / "strategy:<name>"), a list (cascade shorthand), or a map form. The typed
# node models arrive in normalize.py.
RawNode = str | list[Any] | dict[str, Any]


class CircuitBreaker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_fails: int | None = Field(default=None, ge=1)
    cooldown: str | None = None


class Advanced(BaseModel):
    model_config = ConfigDict(extra="forbid")

    circuit_breaker: CircuitBreaker | None = None
    attempt_timeout: str | None = None


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str | None = None
    advanced: Advanced | None = None


class DeciderLLM(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str
    timeout: str | None = None
    send_document_content: bool = False
    mask_fields: list[str] | None = None


class DeciderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: DeciderLLM | None = None


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_duration_per_doc: str | None = None


class StrategyConfig(BaseModel):
    """The parsed, schema-valid openreading.yaml. `strategies` holds raw nodes (RawNode);
    normalize.py turns each into the canonical typed longhand tree."""

    model_config = ConfigDict(extra="forbid")

    version: int
    # `policy` is a superset of the --policy JSON (compliance + RouterConfig keys); it is
    # deliberately open (additionalProperties: true in the schema) and consumed via the same
    # flat-key path as api._apply_policy / api.router_config.
    policy: dict[str, Any] | None = None
    limits: Limits | None = None
    decider: DeciderConfig | None = None
    defaults: Defaults | None = None
    strategies: dict[str, RawNode] = Field(default_factory=dict)

    def strategy_names(self) -> list[str]:
        return sorted(self.strategies)
