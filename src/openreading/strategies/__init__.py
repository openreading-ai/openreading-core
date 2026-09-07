"""OpenReading Strategies — the optional `openreading.yaml` orchestration layer.

A **strategy** is a named recipe for which backends run — in what order or in parallel — and
when to move on. You describe it once in `openreading.yaml`; the file draws rails (eligible
actions, quality thresholds, budgets), and any executor walks the same tree inside those rails
and leaves the same auditable trace. Today the only executor is the deterministic engine, and
the optional LLM decider is a declared seam with no wire adapter behind it yet. The
full config grammar (every node kind, key, predicate, and validation rule) is the docstring of
`openreading.strategies.model`; this docstring is the map. Why the design looks this way — the
prior-art survey behind the five node kinds, the gate polarity, and the sub-Turing choice — is
internal/research/strategies/prior-art.md; the decisions are internal/decisions/DECISIONS.md
(D-v3-*). Doc contract: every fenced YAML block in this package's docstrings must pass the
world-consistency half of `strategy validate` (`desugar_config` + `validate_config`; the
docs-truth test, `tests/test_docs_truth.py`; D-v3-22 — details in `openreading.strategies.model`).

Start here: write one file
--------------------------

Most people never need the full grammar. Plain is eleven words covering the five things people
actually want, which are running backends in parallel, cascading on failure, cost tiers,
escalating on simple criteria, and compare-and-route. Write `./openreading.yaml` and describe
the run the way you would say it out loud:

```yaml
version: 1
strategies:
  cheap_first:
    try: [pymupdf, reducto]     # free and local first
    escalate_when: looks_bad    # climb only when the cheap result cannot be trusted
```

`looks_bad` is the Plain criterion for a result that shows scanned pages with almost no text,
garbled characters, or too many empty pages. Invoke that strategy three equivalent ways:

    openreading parse loan.pdf --strategy cheap_first                           # CLI
    { "document": { "path": "loan.pdf" }, "backend": { "id": "strategy:cheap_first" } }  # wire
    openreading.run("loan.pdf", strategy="cheap_first")                         # Python

If `pymupdf`'s result passes the gate, that is your answer and it cost nothing. If the gate
fires, that result is retained as best-so-far and `reducto` runs. If every rung gates, you get
the best retained result with honest `warnings[]`. If every rung fails outright and nothing
was retained, you get `PlanExhaustedError` with the full attempt trail, never silence and
never a fabricated status (the keep-best law, `openreading.strategies.engine`).

Plain desugars into the five-node grammar the rest of this page describes, so everything below
is what your file becomes rather than a second system. `openreading strategy show cheap_first
--longhand` prints the exact tree. The advanced dialect writes the same cascade with `steps:`
and an explicit gate map, and the built-in `escalate_if: default` bundle is wider than
`escalate_when: looks_bad`, because it also fires on low reported confidence.

Two invariants this package exists to keep
------------------------------------------

**No file ⇒ byte-identical.** Absent a config file, every request takes exactly the legacy code
path and the response is byte-identical to a run without this package. The package is not even
imported on that path: `openreading.api` inlines the `strategy:` prefix check, reads the file
through `openreading.config` (which imports nothing from here), and builds the STRATEGY half of
it lazily, ONLY for `auto` / `strategy:` requests. A named-backend run importing nothing from
`openreading.strategies` is proven in a subprocess test. The reason: an operator who never wrote
a YAML must be able to upgrade without any behavior change.

**Compliance is never widened.** The 3-stage router's compliance/capability filter prunes the
tree BEFORE execution (`openreading.strategies.prune`); constraints from the request
and the file's `policy:` block union most-restrictive-wins; `compliance` is not a catchable
`on_error` class (naming it is a load-time error); every decision point (gate band, `decide`,
judge) enumerates its candidates first and any decider — engine or LLM — selects from that list.
`intent:` prose guides choices under the ceiling and can never move it. The reason: a strategy
file is authored far from the compliance posture it runs under, and no rule, prose, or model
output may re-admit a backend the posture dropped.

Discovery (first hit wins; sources are never merged)
----------------------------------------------------

1. Explicit: CLI `--config PATH`, Python `openreading.run(config=...)`.
2. The `OPENREADING_CONFIG` environment variable.
3. `./openreading.yaml` (or `.yml`) in the working directory — CLI and Python API ONLY.
4. Nothing found → no config; the legacy path.

**The server reads the env var only** (`config.discover(allow_cwd=False)`): a long-running
service must never change behavior because a YAML landed in its cwd. No home-directory
discovery. A found-but-broken file raises `ConfigError` — an explicitly requested config that
cannot load is an error, never a silent fall-through.

Invocation: `backend.id: "strategy:<name>"` (a documented reserved prefix of the free-string
`backend.id`, no wire-schema change — D-v3-2), CLI `--strategy <name>` / `--no-strategy`,
Python `openreading.run(..., strategy="<name>")`; `strategy:none` forces the legacy path even
when `defaults.strategy` opts `auto` traffic in. Precedence: request wire fields → CLI flags →
config `defaults:` → built-ins. `limits:` binds every strategy-engaged run and never a
direct-named request.

The map — what each module documents
------------------------------------

- `model` — the config grammar reference: file shape, invocation/precedence, the five node
  kinds (leaf / cascade / parallel / route / decide) and the `use:` reference, gates and the
  missing-signal law, `on_error` taxonomy and the normative classifier, budgets/limits, the
  shorthand→longhand rules, `defaults.advanced`, the validation catalog, the Plain summary.
- `loader` — discovery, `yaml.safe_load`-only parsing (D-v3-1), schema validation, Plain
  desugar, `LoadedConfig` provenance (source hash, `plain_info`).
- `normalize` — shorthand → canonical longhand (a fixed point), `extends` resolution, gate
  distribution (D-v3-7), `DEFAULT_BUNDLE`, the named-strategy library.
- `presets` — the four vendored presets (`cost_saver`, `max_accuracy`, `fast`, `offline_first`).
- `plain` — the Plain dialect desugar (`try` / `race` / `compare` / `then` / `escalate_when` /
  `max_time`; criteria `looks_bad` / `low_confidence` / `missing` / `disagree`) — not a second
  engine; strict no-op on advanced bodies. Full grammar: internal/design/simple-strategies.md.
- `validate` — world-consistency checks the schema cannot express (unknown backends and refs,
  cycles, `otherwise` membership, gate bindability per backend descriptor, parallel same-backend
  collisions) and the warning set, located by node path (D-v3-8). Run by `strategy validate`
  only. The run path loads the schema and compiles the tree, and it never calls
  `validate_config` (`model` §9).
- `prune` — the compile pipeline: route once, normalize, prune compliance-dropped leaves,
  union the file `policy:` into the effective compliance (D-v3-12); `CompiledPlan`.
- `facts` — pre-parse route facts (`doc_type`, `mime`, page/size probes, `filename_matches`,
  `compliance`, `sample_percent`); an uncomputable fact means "rule doesn't match", never error.
- `signals` — the reference-free quality probe (Tier-1 engine-computed, Tier-2 envelope-reported)
  and `evaluate_gate` (gate maps OR; `any_of`/`all_of`; the missing-signal law).
- `engine` — what running a tree means: the Outcome algebra, cascade/parallel/route/decide
  evaluation order, keep-best, hedge/shadow/drain, `pick: merge`, page granularity, the attempt
  trail (every rung, loser, shadow, judge and decider call is recorded), the concurrency
  contract, `classify_error`, `run_strategy`.
- `decider` — the LLM decision layer: two-key enablement (file `decider:` block AND the
  `OPENREADING_LLM_DECIDER` env; no request field can enable it), the per-request compliance gate
  on the decider/judge backend, engine defaults for every decision point, the downgrade
  taxonomy (`decider_downgraded`), deterministic decision ids, replay.
  No LLM is called today. No shipped surface constructs a `DeciderPort`, so an enabled decider
  resolves every decision point to its engine default and traces `decider_downgraded:
  unavailable`. The wire adapter is designed, not built.
- `trace` — attempt records and the `orchestration` block (closed attempt-category vocabulary).
- `calibrate` — `openreading calibrate`: derive gate thresholds from a labeled sample; it
  proposes, never rewrites the file.
- `describe` — the one-sentence plain-English summary printed under each `strategy validate`
  badge.

CLI surface: `openreading strategy show|list|validate|normalize|plan`, `openreading explain`,
`openreading replay --trace`, `openreading calibrate`; `parse --strategy / --no-strategy /
--config`. `strategy validate` exits 3 on any error, 0 otherwise (warnings never fail); a
`ConfigError` is exit 3 in every `strategy` subcommand.


The tour: one line to full tree
-------------------------------

Each stage is real, minimal, and valid on its own. Start at the top; stop wherever your problem
stops.

1. One-liner preset. Opt traffic that names no backend into a built-in strategy:

```yaml
version: 1
defaults:
  strategy: cost_saver
```

Applies only when the request says `auto` and names no strategy; `strategy:none` on any request
forces the legacy path.

2. Named cascade, default gates. The same file in the advanced dialect, with the built-in gate
bundle (`normalize.DEFAULT_BUNDLE`) in place of a Plain criterion:

```yaml
strategies:
  cheap_first:
    steps: [pymupdf, reducto]
    escalate_if: default
```

3. Tuned thresholds. Replace the bundle with your own gate map — keys OR (any reason to distrust
⇒ move on):

```yaml
strategies:
  scan_aware:
    steps: [pymupdf, docling, reducto]
    escalate_if:
      chars_per_page_below: 100
      garbled: true
      confidence_below: { value: 0.7, on_missing: skip }
```

4. Parallel / race. Run branches concurrently; first successful response wins, losers are
cancelled (`pick: best` instead waits for the branches to succeed, compares them, and keeps the
objectively better result):

```yaml
strategies:
  fast_pdf:
    parallel:
      - docling
      - reducto
    pick: fastest
```

5. Route. Dispatch on pre-parse facts — keys inside one `when:` AND; `default:` is mandatory:

```yaml
strategies:
  by_type:
    route:
      rules:
        - when: { doc_type: [invoice, bank_statement] }
          use: strategy:tables_heavy
      default: strategy:cheap_first
```

6. Full tree. Nodes nest: a cascade rung can be a parallel node, a route target can be a named
strategy, and budgets clamp everything beneath them:

```yaml
strategies:
  full:
    budget: { max_duration: 2m }
    steps:
      - backend: pymupdf
        escalate_if: default
      - parallel: [docling, reducto]
        pick: best
```

Where you started from: a bare YAML list IS a cascade. `[pymupdf, reducto]` normalizes to
`{steps: [...], escalate_if: off}` — exactly the legacy serial fallback chain, with quality gates
always explicit. The legacy behavior is a point in this design, not a second engine.

The five node types
-------------------

A strategy is a node; a node is one of five map forms, discriminated by exactly one key (plus the
reference form `use: <name>`). The grammar is closed and deliberately sub-Turing — no loops, no
variables, no expression language.

- leaf (`backend:`) — Run one backend (or `auto` = router's pick among still-eligible,
  not-yet-attempted).
- cascade (`steps:`) — Serial escalation: run in order; gates decide accept vs escalate; errors
  advance per `on_error`.
- parallel (`parallel:`) — Fan-out: run children concurrently; `pick:` selects the result (race /
  best / merge).
- route (`route:`) — Conditional dispatch over pre-parse facts; first-match rules + mandatory
  `default:`.
- decide (`decide:`) — Explicit decision point among listed sub-nodes; engine takes `otherwise:`, an
  enabled LLM decider may choose.

Four built-in presets ship as ordinary strategies under reserved names: `cost_saver`,
`max_accuracy`, `fast`, `offline_first`. `openreading strategy show cost_saver` dumps the exact
YAML for forking — by copying it: `extends:` is designed, but the v0.2 schema rejects the key in
a file (`model` §2.8).

FAQ
---

- **Is the config file required?** No. No file ⇒ no change.
- **What happens to my existing `routing.fallback`?** Without a strategy engaged, it behaves as
  always: listed ids move to the front of the eligible chain and the remaining eligible backends
  still follow (formally it desugars to `{steps: [your ids, then the rest of the eligible set in
  score order], escalate_if: off}`). If a strategy IS engaged, the strategy wins and the list is
  ignored with a `strategy_overrides_fallback` warning.
- **Does an LLM have to be involved?** No. Every construct has mandatory engine semantics —
  `review_default` for gray bands, `otherwise:` for decide nodes, a deterministic composite score
  for `pick: best`. The LLM decider is a declared seam, not a shipped call. Today every decision
  point resolves to its engine default and the trace says `decider_downgraded: unavailable`.
  When the wire adapter lands it will be opt-in by two keys, and every decider failure will
  still fall back to the engine default.
- **Can a strategy weaken compliance?** Never — see the second invariant above.
- **How do I debug why a fallback fired — or didn't?** The trace records everything: every
  attempt with its category, and for gate events each predicate's observed value vs. threshold,
  fired or not (`orchestration.attempts[]`, decision records). `openreading explain` walks a run's
  decisions; engine runs replay exactly, and LLM-mode runs replay via `openreading replay
  --trace`.
- **What does it use?** What you let it: `budget:` on any node and the operator-level `limits:`
  ceiling bound the time a walk gets before dispatch, and `orchestration.attempts[]` names ALL of
  them — winners, losers, shadows, judges, deciders — so you can count the calls a run made. No
  price: core carries no rates, so join those counts to your own
  provider invoice.
- **Can I keep using plain fallback lists?** Yes — a bare list is a valid strategy body and means
  exactly the legacy chain. Add `escalate_if: default` the day you want quality-based
  escalation; nothing else changes.

Public surface
--------------

- `load_config` / `LoadedConfig` / `ConfigError` / `resolve_strategy` / `STRATEGY_PREFIX` /
  `strip_strategy_prefix` + the `StrategyConfig` model and its deployment blocks
- `normalize_strategy` / `normalize_config` / `normalize_node` / `build_library` /
  `DEFAULT_BUNDLE` / `NormalizeError` + `PRESETS` / `PRESET_NAMES`
- `validate_config` / `ValidationIssue` — world-consistency checks
- `probe` / `evaluate_gate` / `garble_score` / `SignalSnapshot` / `GateResult` /
  `PredicateResult` — the signal probe + gate evaluation
- `compute_facts` / `evaluate_when` / `Facts` — route facts
- `compile_strategy` / `CompiledPlan` — the pruned tree
- `run_strategy` / `StrategyResult` / `Outcome` / `classify_error` — the engine
"""

from __future__ import annotations

from openreading.strategies.engine import (
    Outcome,
    StrategyResult,
    classify_error,
    run_strategy,
)
from openreading.strategies.facts import Facts, compute_facts, evaluate_when
from openreading.strategies.loader import (
    STRATEGY_PREFIX,
    ConfigError,
    LoadedConfig,
    load_config,
    resolve_strategy,
    strip_strategy_prefix,
)
from openreading.strategies.model import (
    Advanced,
    CircuitBreaker,
    DeciderConfig,
    DeciderLLM,
    Defaults,
    Limits,
    StrategyConfig,
)
from openreading.strategies.normalize import (
    DEFAULT_BUNDLE,
    NormalizeError,
    build_library,
    normalize_config,
    normalize_node,
    normalize_strategy,
)
from openreading.strategies.presets import PRESET_NAMES, PRESETS
from openreading.strategies.prune import CompiledPlan, compile_strategy
from openreading.strategies.signals import (
    GateResult,
    PredicateResult,
    SignalSnapshot,
    evaluate_gate,
    garble_score,
    probe,
)
from openreading.strategies.validate import ValidationIssue, validate_config

__all__ = [
    "DEFAULT_BUNDLE",
    "PRESET_NAMES",
    "PRESETS",
    "STRATEGY_PREFIX",
    "Advanced",
    "CircuitBreaker",
    "ConfigError",
    "Defaults",
    "CompiledPlan",
    "DeciderConfig",
    "DeciderLLM",
    "Facts",
    "GateResult",
    "Limits",
    "LoadedConfig",
    "NormalizeError",
    "Outcome",
    "PredicateResult",
    "SignalSnapshot",
    "StrategyConfig",
    "StrategyResult",
    "ValidationIssue",
    "build_library",
    "classify_error",
    "compile_strategy",
    "compute_facts",
    "evaluate_gate",
    "evaluate_when",
    "garble_score",
    "load_config",
    "normalize_config",
    "normalize_node",
    "normalize_strategy",
    "probe",
    "resolve_strategy",
    "run_strategy",
    "strip_strategy_prefix",
    "validate_config",
]
