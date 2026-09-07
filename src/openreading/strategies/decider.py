"""The decision layer: the seam every declared decision point resolves through.

This module is the normative contract for the optional LLM decider and LLM-as-judge: where they may
act, exactly what they see, exactly what they may return, and how every failure mode degrades to
the deterministic engine. The file grammar that declares decision points is
`openreading.strategies.model` (+ `schemas/strategy-config.v0.2.json`); the walk that consults them
is `openreading.strategies.engine` (`_resolve_decision_point`, `_decide`, `_select_best`,
`_pairwise_judge`, `_replay_decision`). Nothing here adds a node type, a signal, or an action the
engine does not already have. The `decider.md §n` citations in this module's and the engine's
comments name the numbered sections of this docstring, which absorbed that design record.
Decisions: internal/decisions/DECISIONS.md D-v3-15, D-v3-17, D-v3-18, D-v3-24.

Status
------
Plumbing (two-key gate, per-request compliance gate, engine defaults, downgrade taxonomy, one-shape
decision record), the executor contract (strict single-tool schema `build_decider_tool`, engine
re-validation `revalidate_action`, the `DeciderPort`/`JudgePort` seams and verdict types,
`decider_call`/`judge_call` metering, the pairwise judge), `mask_fields`, `openreading replay
--trace` and the H6 canary test are all implemented and exercised offline by in-process fakes.
The production wire adapter (the real Messages-API call behind the ports) is NOT built: no shipped
surface constructs a port, so today every enabled + eligible decision point resolves to its engine
default traced `decider_downgraded: unavailable` (D-v3-15). Its design is
internal/design/decider-executor.md; its one law (DE-1, "no upgrade-triggered spend") is that a
deployment resolving `unavailable` today must keep doing so after upgrading until the operator
flips an explicit arm switch — env gate + `decider:` block + an installed key are NOT enough.

§1 One tree, two walkers
------------------------
The engine compiles the strategy file into one normalized tree (config hash recorded), prunes it
against the request's compliance-eligible set, and walks it. Walking is deterministic except at
declared decision points, of which there are exactly three kinds (§2). Every decision point carries
mandatory, schema-enforced engine semantics (`review_default` on a gate band, `otherwise:` on a
decide node, the deterministic composite score behind a judge), so a deployment with no LLM
anywhere runs 100% of valid configs and loses nothing but the optional judgment band — the
no-gimmick guarantee: the LLM is a mode of three specific decisions, never a prerequisite.

Deterministic constructs always outrank the decider (the Colang priority rule: deterministic flows
win when they match; the LLM fills declared gaps at a declared lower priority):
- `route.rules` resolve by first-match before any `decide` node in `default:` is reached.
- `escalate_if` fires before `review_if` is consulted; the decider is asked only inside the gray
  band the config explicitly declared.

Enablement is two-key, and never a wire field. The decider runs only when BOTH hold:

1. The strategy file carries a `decider:` block:

   ```yaml
   decider:
     llm:
       backend: anthropic-claude     # required — a registry backend slug
       timeout: 5s                   # per-decision deadline; expiry → engine default (§3.4)
       send_document_content: false  # default false — §6
       mask_fields: []               # typed_field names stripped from signals — §5
   ```

2. The operator sets `OPENREADING_LLM_DECIDER=1` (`DECIDER_ENV`; truthy tokens `1/true/yes/on`,
   off tokens `""/0/false/no/off`). Any other value is a backend id: it both enables the decider
   and overrides the file's `decider.llm.backend` (and a judge's backend, §1).

Either key alone leaves the engine in charge: a `decider:` block without the env gate resolves
every decision point to its engine default, traced `decider_downgraded: env_disabled`. There is no
per-request wire field that can enable the decider — enablement is a deployment posture, the same
split as `allow_unverified_compliance`, so a caller can never talk a service into consulting an LLM
its operator did not deploy. The verdict (`resolve_decider_status` → `DeciderStatus`) is computed
ONCE per walk at `run_strategy` entry and binds every decision point in the request identically
(D-v3-15); `env` is `os.environ` unless overridden via `run_strategy(env=...)` for tests.

The judge is gated the same way, with one difference: a `judge:` block on a `pick: best` parallel
node carries its own `backend`, `intent` (≤200 chars), `excerpt_chars` (default 4000) and
`timeout` (the spec suggested a 30s default, longer than a decision's 5s because judging reads
excerpts; neither default is encoded — the schema declares no default for either `timeout`, and
`_pairwise_judge` never reads the key, §3.3 rail 4), so a top-level `decider:` block is NOT
required for judge-only configs — but the env gate IS: the two-key rule covers gate bands, decide
nodes, and judges. The judge gate is resolved per node (`resolve_judge_status`) because its backend
lives on the node. An ungated judge downgrades to the engine composite score, traced
`decider_downgraded: env_disabled`.

§2 The three decision points
----------------------------
| # | Point     | Where it arises                     | Action set                 |
|---|-----------|-------------------------------------|----------------------------|
| a | Gate band | `review_if:` on a cascade step      | `{accept, escalate}`       |
| b | Decide    | a `decide:` node                    | the `among:` list          |
| c | Judge     | `judge:` on a `pick: best` parallel | completed candidates, pairs|

Engine semantics (mandatory, schema-enforced): (a) `review_default` (default `escalate`);
(b) take `otherwise:`; (c) the deterministic composite score.
Route is NOT a decision point — it is deterministic first-match (`POINTS`).

§2.1 Gate band. A cascade step whose result passed `escalate_if` but matched `review_if` is a
decision point with actions `{accept, escalate}`. The engine resolves it to `review_default`, whose
default is `escalate` — false-escalation costs cents, false-acceptance costs correctness. An
enabled decider chooses instead, seeing the signals that fired the band; the spec also promised it
the latency of escalating, but no latency figure reaches the `DecisionPoint` today (§3.1, §6).
(`confidence_below` on a backend that reports no confidence binds only with
`on_missing: escalate` — `_predicate_binds`; the schema default `on_missing: skip` does not make it
bindable. `validate` raises its unbindable-gate error only when EVERY predicate of the gate fails to
bind (`_check_gate_bindable`), so a `confidence_below` beside a Tier-1 predicate passes silently.)

```yaml
steps:
  - backend: pymupdf
    escalate_if: { chars_per_page_below: 100, garbled: true }   # hard gate — decider never consulted
    review_if:   { confidence_below: { value: 0.85, on_missing: escalate } }   # gray band; pymupdf
                                                                # reports no confidence → escalate
    review_default: escalate                                    # the engine's answer, always present
  - reducto
```

§2.2 Decide node. The explicit "choose a sub-tree" point. The engine builds the candidate list
from `among:` (post-pruning; candidate names are the among targets' labels, unique by a
normalize-time invariant) plus `otherwise`; engine mode dispatches `otherwise:`; an enabled decider
picks one candidate. The chosen child's Outcome is the decide's Outcome.

```yaml
decide:
  among: [strategy:tables_heavy, strategy:text_light]
  otherwise: strategy:text_light        # mandatory — the engine's answer
intent: >
  Choose tables_heavy for financial layouts with dense grids;
  text_light for prose-dominant documents.
```

§2.3 Judge. A `pick: best` parallel node with a `judge:` block replaces the engine's deterministic
comparator with LLM-as-judge (§4). Absent, ineligible, or failing judge → the engine composite
score, traced `decider_downgraded`. The judge selects among completed candidates; it never produces
content. With zero or one successful candidate there is nothing to judge: the engine comparator
runs and no `judge` decision record is written.

§3 The decider contract
-----------------------
§3.1 The `DecisionPoint` record — the complete input the decider receives, and the only input:

```jsonc
{
  "decision_id": "dp_5f2c9a…",                           // deterministic digest (§5)
  "point": "gate_band",                                  // gate_band | decide | judge
  "node_path": "strategies.invoices.steps[0].review_if",
  "label": "cheap_rung_review",                          // the node's label: (defaults to path)
  "intent": "Escalate when key invoice amounts look unreliable.",   // prose from the config
  "signals": { "confidence_below": { "observed": 0.81, "threshold": 0.85 } },
                                    // keyed by the firing gate operator; observed values, masked §5
  "candidates": ["accept", "escalate"],                  // the closed action space, engine-built
  "engine_default": "escalate",
  "budget_remaining": { "duration_ms": 71000 }           // remaining time only; no cost pool
}
```

`candidates` is the closed action space enumerated by the engine (the Not Diamond candidate-list
contract: the caller supplies the list, the router only ever returns a member of it). Gate-band
`signals` are keyed by the firing gate OPERATOR carrying `{observed, threshold}` rather than a bare
signal name — the operator encodes the comparison direction, so no fragile suffix-stripping table
is needed (D-v3-15). The spec's `document_excerpt` (populated under `send_document_content: true`)
is designed, not shipped: `DecisionPoint` has no such field and no engine code reads the flag,
which exists only as a declared `DeciderLLM` key (§6). Per-candidate descriptions /
`est_added_latency` are not built today — the spec's richer candidate objects, ancestor intent and
route facts are all narrowed by the code; §6 lists each gap.

§3.2 The strict tool invocation. The decider model is invoked with a single tool and a forced
strict tool choice (`tool_choice: {"type": "any"}` + `strict: true`), so schema conformance is a
decoding guarantee — `build_decider_tool(candidates)`:

```jsonc
{
  "name": "decide",
  "strict": true,
  "input_schema": {
    "type": "object",
    "properties": {
      "action":    { "enum": ["accept", "escalate"] },   // == the candidate list, exactly
      "rationale": { "type": "string", "maxLength": 280 }
    },
    "required": ["action", "rationale"],
    "additionalProperties": false
  }
}
```

The `action` enum IS the candidate list — an out-of-set choice is a type-level impossibility, not
a prompt-level hope. The engine re-validates the returned action anyway (`revalidate_action`,
belt and suspenders): `None` → `refusal`, out-of-set → `malformed`; a port that raises →
`malformed`. The `rationale` is bounded prose for the trace, never an instruction to the engine.

§3.3 The hard rails, enforced by construction, not by prompt:
1. Candidates are built post-pruning. Compliance-dropped backends were removed from the tree
   before execution, so they never appear in `candidates`; the decider cannot choose what it
   cannot see.
2. Decider calls are recorded, not enforced. Each call that RETURNS lands as a `decider_call`
   attempt (judge calls as `judge_call`) in the trail, so a reader can count them. A
   `DeciderPort.decide` that raises is caught (`malformed`) BEFORE the attempt is recorded, so a
   raising call leaves nothing in the trail; only its downgrade shows on the decision record. The
   port used to report a `cost_usd` per call and the engine summed it into `usage.cost_usd`. Both
   are gone with the rest of core's money: the caller's own provider
   bill is where a decider's spend is visible.
3. Compliance is never visible (cited as "rail 4" in engine comments). Pruning happens upstream;
   no compliance constraint, `DropReason`, or dropped backend appears in any `DecisionPoint`. The
   decider decides quality/latency trade-offs; it has no compliance surface to reason about,
   correctly or otherwise.
4. The engine owns metering and re-validation; the port is thin and untrusted (D-v3-17). This
   keeps trace, budget and compliance out of the port entirely. Ports are plain synchronous
   Protocol methods run via `asyncio.to_thread` so a slow or hanging implementation cannot stall
   sibling coroutines (parallel branches, the coordinated clock); a per-call deadline is not yet
   applied by the engine (deferred to the wire adapter), so `timeout` is reserved, not yet fired.

§3.4 The downgrade taxonomy — failure resolves to the engine, never to failure. Any decider failure
resolves the decision point to its engine default with the reason traced as
`decider_downgraded: <reason>` (`DOWNGRADE_REASONS`, closed):

- `timeout` — no decision within `decider.llm.timeout` (the spec's 5s default is not encoded:
  `DeciderLLM.timeout` defaults to `None`, and no engine path applies the deadline or emits
  this reason today — §3.3 rail 4).
- `malformed` — output fails schema validation or engine re-validation (§3.2), or the port raised.
- `refusal` — the model declines to choose (`action: None`).
- `compliance` — the decider/judge backend was dropped by the request's compliance filter (§3.5).
- `scope_denied` — the decider/judge backend is outside the CALLER's backend allow-list. A
  `decider:`/`judge:` backend is CALLED, with the operator's vendor key, so the same per-token
  ceiling that bounds every dispatched backend bounds it. Checked ahead of `compliance` because
  the caller can act on it, and reported instead of it when both apply.
- `env_disabled` — a `decider:` (or `judge:`) block exists but `OPENREADING_LLM_DECIDER` is not
  set (§1).
- `unavailable` — enabled and eligible, but no executor port is wired into this runtime; also
  the backend id being unknown to the registry (`_backend_eligible` maps the registry `KeyError`
  here). `validate` does NOT check `decider.llm.backend` / `judge.backend` against the registry
  — its unknown-backend error runs only on leaf `backend:` nodes and cascade steps — so a
  misspelled decider/judge backend passes `validate` and first surfaces as a run-time
  `unavailable`.
- `trace_missing` — replay mode only: a decision point has no logged choice in the trace, or the
  logged choice is no longer a valid candidate for this run (§5).
- `otherwise_pruned` — a `decide:` node's own `otherwise:` was pruned by compliance while at least
  one `among:` survivor remained; `openreading.strategies.prune` substituted the first-listed
  survivor as the new default (D-v3-24).

`otherwise_pruned` is not a decider failure: it fires whenever dispatch actually resolves to the
substituted `otherwise`, independent of whether any `decider:`/`judge:` block is configured or
enabled — pruning, not decision-making, drives it — and never clobbers a real decider-level
downgrade already on the record. Engine-mode priority for a configured block: env off →
`env_disabled`; backend compliance-dropped → `compliance`; no port → `unavailable`; a config with
no block at all is pure engine mode with no annotation. An LLM outage can never fail a parse: the
decision point degrades, the walk continues, the trace says why.

§3.5 The decider is itself a compliance-checked backend. `decider.llm.backend` (and
`judge.backend`) names an ordinary registry backend, resolved like any other. Per request it passes
`Router.check_eligible` against the EFFECTIVE (request ∪ policy) compliance BEFORE any decision
point runs (`_backend_eligible`). If it is dropped (`require_local` and the decider is hosted;
`no_train_on_data` and the model trains on inputs), every decision point in that request resolves
via engine semantics, traced `decider_downgraded: compliance`, and the port is never called. An LLM
that trains on data can therefore never see a `no_train_on_data` document — structurally, not
contractually. Compliance is never widened here.

The same backend is checked against the CALLER's allow-list on the same pass, ahead of compliance,
traced `decider_downgraded: scope_denied`. A `decider:`/`judge:` backend is one that gets CALLED,
with the operator's vendor key, so the per-token ceiling that bounds every dispatched backend has
to bound it too. Nothing spends on this path today (no port is wired into any shipped surface, so
the status resolves to `unavailable` before a call), which is exactly why the gate is here now:
the day the wire executor lands, this would otherwise be a backend a scoped request reaches with
no check at all.

§4 LLM-as-judge
---------------
The judge compares completed `pick: best` candidates. Orchestration lives in the ENGINE, not the
port (D-v3-17): `JudgePort.compare` judges ONE ordered pair and returns a positional winner.
- Pairwise, both orderings. Position bias in pairwise LLM judging is measured and severe, so every
  comparison is two calls — A-then-B and B-then-A — with candidates identified only as positional
  labels (`JudgeCandidate.label` "A"/"B"), sources hidden. Inconsistent verdicts across the two
  orderings count as a tie.
- Ties break deterministically and identically to the engine comparator: cheaper backend
  first-listed (lower branch index). The spec asks for ties to be
  traced as ties; the engine does not: `_judge_pair` returns `_tie_break_pair(a, b, ctx)` with no
  annotation, and the one `judge` record (`_record_judge`) carries no tie field, so a tie-broken
  winner is indistinguishable in the trace from a consistent verdict.
- The judge selects; it never synthesizes. The winner is a real backend's output with provenance
  intact (never-fabricate). Combining outputs is `pick: merge`'s job (D-v3-19).
- What the judge reads: a capped excerpt (`judge.excerpt_chars`, default 4000) of each candidate's
  text plus its typed_fields (JSON-safe `{value, confidence}` view, masked per §5), and the
  `judge.intent` criteria prose. Never the raw request, never a backend id, never compliance
  context; the operator's trace still names the chosen/eligible backends.
- More than two candidates: single-elimination against the current best, in listed order — n−1
  pairs, 2·(n−1) calls, bounded cost. More than three candidates is a `validate` warning.
- Judge calls are recorded attempts, category `judge_call`.
- Downgrade: a judge that is ungated (`env_disabled`), compliance-ineligible (`compliance`,
  §3.5), portless (`unavailable`), or absent from a replay trace (`trace_missing`) yields the
  engine's deterministic composite score, traced `decider_downgraded`; the LLM path and every
  status-level downgrade write exactly ONE `judge` decision record (`eligible` = the successful
  candidates' backend ids, `chosen` = the winner's backend id). No `judge:` block, or ≤1
  successful candidate, is plain engine comparison with no record (§2.3). A `compare` that
  RAISES is not caught today: unlike `_decide`, `_judge_pair` has no `except`, so the exception
  propagates out of the walk instead of degrading to the composite — the spec's "failing judge →
  engine score" holds for the status-level reasons only, not yet for a crashing port.

§5 Decision trace, masking & replay
-----------------------------------
Every DECISION POINT (§2) — engine-, LLM- or trace-resolved — appends ONE record, one format
(`decision_record`), to `orchestration.decisions[]`: the OPA decision-log shape (decision id +
input + result + config version, maskable, replayable) applied to strategy walks. The
deterministic route (`_eval_route`) appends to the SAME list in its own shape — `{point: "route",
node_path, decider: "engine", chosen: <rule index | "default">, rules: [...]}`, with no
decision_id / label / eligible / config_hash / strategy / downgraded — so a consumer of
`decisions[]` must branch on `point` before reading the fields below:

```jsonc
{
  "decision_id": "dp_5f2c9a…",              // deterministic digest of (config_hash, node_path, seq)
  "node_path": "strategies.invoices.steps[0].review_if",
  "label": "cheap_rung_review",
  "point": "gate_band",                     // gate_band | decide | judge
  "eligible": ["accept", "escalate"],
  "chosen": "escalate",
  "decider": "engine",                      // engine | llm | trace
  "downgraded": null,                       // a §3.4 reason when the engine answered instead
  "signals": { "confidence_below": { "observed": 0.81, "threshold": 0.85 } },   // omitted if empty
  "config_hash": "sha256:…",
  "strategy": "invoices",
  // llm-only fields, added by the executor path when the port reports them:
  "model_id": "claude-…",
  "rationale": "Amounts table extracted cleanly; header confidence dip only."
}
```

`decision_id` (`decision_id()`) is a deterministic digest `"dp_" + sha256(config_hash|node_path|
seq)[:26]`, NOT a time-based ULID: a wall-clock id would break the determinism law and make trace
replay non-exact (D-v3-15). `seq` is a walk-global counter on the `Trace` (`next_dp_seq`) that only
gate-band / decide / judge points draw from — the deterministic route record does not. Same config
+ same inputs ⇒ same `decision_id`, so replay keys on it reproducibly.

Masking. `decider.llm.mask_fields` names typed_fields whose values are stripped
(`mask_typed_fields`) ONCE, when the engine builds `dp.signals` / `JudgeCandidate.typed_fields`,
so both sinks share one rule: the port sees the masked input and the record is built from the same
masked input — a masked value can reach neither an LLM prompt nor the trace (D-v3-18). The one list
governs decider and judge alike; a judge-only config (no `decider:` block) has no mask list. No
trace fixtures are committed (traces are generated in-memory in tests, nothing to scrub on disk);
the H6 canary test proves a planted secret never reaches any decider input nor
`json.dumps(orchestration)`.

Replay (`openreading replay --trace t.json`, `run_strategy(replay=<decisions>)`):
- Engine runs replay exactly: same config hash + same inputs ⇒ same decision records. The CLI
  checks this at the WHOLE-TRACE level, once, at load time, before any decision point is
  consulted: a trace carrying a `config_hash` that differs from the freshly-compiled one is refused
  outright, naming `config_hash` — a trace recorded under one configuration or compliance posture
  must not silently replay into a run compiled under another. A trace with no `config_hash` (older
  or hand-built) has nothing to compare and falls through to per-decision `trace_missing`.
- Replay is a decision MODE, not a live port (D-v3-18): the engine indexes `decision_id → record`
  and each decision point short-circuits to its logged `chosen` (`decider: "trace"`) before the
  live-decider path runs. It never invokes `DeciderPort`/`JudgePort` — there is nothing to call
  offline; the seam shared with a live run is the `decision_id` identity. A point absent from the
  trace, or whose logged choice is no longer a valid candidate, takes the engine default traced
  `trace_missing`. A judge point replays by its logged WINNER BACKEND, not by re-running the
  pairwise comparisons — the source-blind port cannot be replayed by identity; the outcome is the
  same.
- Replay bypasses the two-key env gate and the compliance filter — it sends nothing to an LLM, so
  both are moot; this keeps replay fully offline and deployment-independent.
- `replay=[]` is a valid EMPTY trace (mode on, `trace_missing` everywhere), distinct from
  `replay=None` (mode off) — the distinction is by identity, not truthiness, so an empty trace can
  never silently disable replay.
- Divergence between two live LLM runs is attributable: `model_id` (+ a prompt hash once the wire
  adapter reports one) say whether the model or the prompt changed. Two executors may choose
  differently within the same rails — that is the design, not a leak: rails bind identically;
  choices inside gray bands may differ and are always traced.

§6 What the LLM sees, and what it never sees
--------------------------------------------
Sees (via the `DecisionPoint`) — each item as the spec states it, then what the engine builds
today, because the code narrows the spec in three places and the gap must stay visible:
- `intent:` prose — spec: the decision point's node AND its ancestors, in scope order; today: the
  node's own `intent` only (`_eval_decide` passes `node.get("intent")`, the gate band
  `step.get("intent")`; no ancestor walk exists).
- Signals — spec: the route facts AND the observed quality signals for this document
  (post-masking); today: the firing gate operators' `{observed, threshold}` plus masked
  typed_fields only; `ctx.facts` (the route facts) never enter a `DecisionPoint`.
- The remaining time budget — `duration_ms`, present only when the walk has a deadline
  (`_budget_snapshot`); no cost pool.
- The candidate list — spec: with per-candidate descriptions and latency estimates; today: bare
  action names (§3.1).

Never sees: raw secrets or credentials (they never appear in any strategy artifact, so there is
nothing to leak); compliance constraints, drop reasons, or dropped backends (§3.3); document
content — the spec lets `decider.llm.send_document_content: true` opt in, but today the flag is
declared (`DeciderLLM`, schema default false) and never read, and `DecisionPoint` has no excerpt
field, so the decider reasons over signals ABOUT the document, not the document, unconditionally.

The law that governs all of it: prose guides choices under the rails; it never moves a rail.
`intent:` text is the decider's semantic payload and the UI's display text; budgets, thresholds,
eligibility, and compliance bind both executors identically, and no rationale, however persuasive,
changes what the engine will dispatch.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from openreading.credentials import EnvCredentialBroker
from openreading.router.registry import Registry
from openreading.router.router import Router, RouterConfig
from openreading.strategies.model import DeciderLLM
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest

# The env key of the second enablement key (decider.md §1). Truthy → decider enabled; a backend-id
# value both enables it and overrides `decider.llm.backend`.
DECIDER_ENV = "OPENREADING_LLM_DECIDER"
_ENABLE_TOKENS = frozenset({"1", "true", "yes", "on"})
_DISABLE_TOKENS = frozenset({"", "0", "false", "no", "off"})

# The closed downgrade taxonomy (decider.md §3.4). `unavailable` (D-v3-15) names the 14.1 state and
# the lasting case "decider enabled + eligible but no LLM executor is wired into this runtime".
DOWNGRADE_REASONS = frozenset(
    {
        "timeout",  # 14.2 — no decision within decider.llm.timeout
        "malformed",  # 14.2 — output fails schema / engine re-validation
        "refusal",  # 14.2 — the model declines to choose
        "compliance",  # the decider backend was dropped by the request compliance filter (§3.5)
        "env_disabled",  # a decider is configured but OPENREADING_LLM_DECIDER is not set (§1)
        "trace_missing",  # 14.3 — replay: a decision point has no logged choice
        "unavailable",  # enabled + eligible, but no LLM executor deployed in this runtime (D-v3-15)
        "scope_denied",  # the decider/judge backend is outside the CALLER's allow-list. Distinct
        # from "compliance": that one's fix is the policy, this one's is the token's allow-list.
        "otherwise_pruned",  # BL-54 — a decide node's own otherwise: was pruned by compliance and
        # dispatch resolved through the substituted among[0] survivor (integration.md §2c), not the
        # operator's configured default
    }
)

# The three decision points (decider.md §2). Route is NOT here — it is deterministic first-match.
POINTS = frozenset({"gate_band", "decide", "judge"})


@dataclass(frozen=True)
class DeciderStatus:
    """The once-per-walk verdict on whether the LLM decider may act (decider.md §1, §3.5). When
    `mode == "engine"` every decision point resolves to its engine default; `reason` (when set) is
    the `decider_downgraded` annotation each record carries."""

    mode: str  # "engine" | "llm"
    reason: str | None = None  # a DOWNGRADE_REASONS member when mode == "engine" and a block exists
    backend: str | None = None  # the resolved decider backend id (file value or env override)


@dataclass
class DecisionPoint:
    """The complete, and only, input a decider receives at a decision point (decider.md §3.1). Built
    post-pruning (compliance). Carries no compliance surface (rail 4)."""

    decision_id: str
    point: str  # a POINTS member
    node_path: str
    label: str
    candidates: list[str]  # the closed action space, enumerated by the engine
    engine_default: str  # the mandatory, always-present deterministic answer
    intent: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    budget_remaining: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionVerdict:
    """What a decider executor returns for one decision point. `action` is a candidate the model
    chose (or `None` = declined → `refusal`). The engine records the call as a `decider_call`
    attempt in the trail (decider.md §3.3).
    `rationale` is bounded trace prose, never an instruction to the engine."""

    action: str | None
    model_id: str | None = None
    rationale: str | None = None


class DeciderPort(Protocol):
    """The 14.2 LLM executor seam for gate-band / decide points. `decide` performs the strict
    single-tool invocation (`build_decider_tool`) and returns a `DecisionVerdict`; the engine
    re-validates the action against the candidate list (`revalidate_action`) and records the call
    as an attempt — the port is never trusted blindly (decider.md §3.2)."""

    def decide(self, dp: DecisionPoint) -> DecisionVerdict: ...


@dataclass
class JudgeCandidate:
    """One completed `pick: best` candidate as the judge sees it (decider.md §4): a positional
    label, a capped excerpt, and typed_fields. The source backend is HIDDEN — the judge compares
    content, never provenance, so it cannot prefer a source by name."""

    label: str  # "A" | "B" — positional only
    excerpt: str
    typed_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class JudgeVerdict:
    """One pairwise judgment. `winner` is a positional label ("A"/"B"). The engine records each
    call as a `judge_call` attempt."""

    winner: str  # "A" | "B"
    model_id: str | None = None


class JudgePort(Protocol):
    """The 14.2 LLM-as-judge seam. `compare` judges ONE ordered pair; the engine runs it twice
    (both orderings) and resolves position bias / ties deterministically (decider.md §4). Sources
    are hidden behind the positional labels — the port never receives a backend id."""

    def compare(self, a: JudgeCandidate, b: JudgeCandidate, intent: str | None) -> JudgeVerdict: ...


def build_decider_tool(candidates: list[str]) -> dict[str, Any]:
    """The strict single-tool schema (decider.md §3.2): the `action` enum IS the candidate list, so
    an out-of-set choice is a decoding impossibility, not a prompt-level hope. Invoked with
    `tool_choice: any`, `strict: true`. The engine re-validates the returned action anyway."""
    return {
        "name": "decide",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"enum": list(candidates)},
                "rationale": {"type": "string", "maxLength": 280},
            },
            "required": ["action", "rationale"],
            "additionalProperties": False,
        },
    }


def revalidate_action(action: str | None, candidates: list[str]) -> tuple[str | None, str | None]:
    """Engine re-validation of a decider's returned action (decider.md §3.2, belt-and-suspenders).
    Returns (valid_action, downgrade_reason): a valid member → (action, None); `None` → (None,
    "refusal"); an out-of-set value → (None, "malformed")."""
    if action is None:
        return None, "refusal"
    if action not in candidates:
        return None, "malformed"
    return action, None


def decision_id(config_hash: str, node_path: str, seq: int) -> str:
    """A deterministic decision id: same config + same walk position ⇒ same id, so a trace replay
    (14.3) is exact. Not a time-based ULID — determinism (execution.md §6) forbids wall-clock ids
    (D-v3-15, patches decider.md §3.1/§5)."""
    digest = hashlib.sha256(f"{config_hash}|{node_path}|{seq}".encode()).hexdigest()
    return "dp_" + digest[:26]


def mask_typed_fields(fields: Mapping[str, Any], mask_fields: list[str]) -> dict[str, Any]:
    """Strip the named typed_fields (decider.llm.mask_fields, decider.md §5). This one rule feeds
    BOTH sinks — the values are stripped before the decider/judge sees them AND before they are
    logged — so a masked field's value can never reach an LLM prompt nor the trace."""
    if not mask_fields:
        return dict(fields)
    drop = set(mask_fields)
    return {k: v for k, v in fields.items() if k not in drop}


def _env_decider_backend(env: Mapping[str, str], file_backend: str) -> tuple[bool, str]:
    """Read the env key. Returns (enabled, effective_backend). A backend-id value both enables and
    overrides the file backend (decider.md §1)."""
    raw = env.get(DECIDER_ENV)
    if raw is None:
        return False, file_backend
    token = raw.strip()
    if token.lower() in _DISABLE_TOKENS:
        return False, file_backend
    if token.lower() in _ENABLE_TOKENS:
        return True, file_backend
    return True, token  # an explicit backend-id override


def _backend_eligible(
    backend: str,
    req: OpenReadingRequest,
    registry: Registry,
    router_config: RouterConfig,
    broker: EnvCredentialBroker | None = None,
) -> str | None:
    """Compliance-check a decider/judge backend against the effective (request ∪ policy) compliance
    (decider.md §3.5), BEFORE any decision point runs. Returns a downgrade reason ("compliance" |
    "unavailable") or None when eligible. Compliance is never widened here.

    `broker` is the walk's own credential broker. Stage 1 resolves a container backend's endpoint
    through it, so gating with a different broker would check an endpoint this walk never dispatches
    to."""
    try:
        Router(registry, router_config, broker=broker).check_eligible(req, backend)
    except ScopeRefused:
        return "compliance"
    except KeyError:
        # Unknown backend id: `validate` never checks the decider or judge backend against
        # the registry, so a misspelling first surfaces here. Treat it as unavailable.
        return "unavailable"
    return None


def resolve_decider_status(
    *,
    decider: DeciderLLM | None,
    req: OpenReadingRequest,
    registry: Registry,
    router_config: RouterConfig,
    env: Mapping[str, str],
    port: DeciderPort | None,
    backend_allowlist: frozenset[str] | None = None,
    broker: EnvCredentialBroker | None = None,
) -> DeciderStatus:
    """The two-key enablement + compliance gate, computed once per walk (§3.5: the verdict binds
    every decision point in the request identically). Priority: no block → pure engine; block but
    env off → env_disabled; block + env but backend compliance-dropped → compliance; enabled +
    eligible but no executor wired → unavailable; otherwise → llm."""
    if decider is None:
        return DeciderStatus("engine")  # pure engine mode; decisions carry no downgrade annotation

    enabled, backend = _env_decider_backend(env, decider.backend)
    if not enabled:
        return DeciderStatus("engine", "env_disabled", backend)

    if backend_allowlist is not None and backend not in backend_allowlist:
        # Ahead of the compliance gate below so the reported reason is the one the caller can act
        # on, and because scope is the narrower, later subtraction (strategies.prune uses the same
        # precedence). Downgrades to engine mode rather than raising: a decision point resolving
        # to its engine default is this taxonomy's answer to every decider failure, and refusing
        # the whole walk over an out-of-scope JUDGE would be a bigger hammer than the caller's
        # scope asks for — the backends that actually process the document are bounded elsewhere.
        return DeciderStatus("engine", "scope_denied", backend)
    reason = _backend_eligible(backend, req, registry, router_config, broker)
    if reason is not None:
        return DeciderStatus("engine", reason, backend)
    if port is None:
        # enabled + eligible, but the LLM executor is not deployed in this runtime (14.1 state).
        return DeciderStatus("engine", "unavailable", backend)
    return DeciderStatus("llm", None, backend)


def resolve_judge_status(
    *,
    judge_backend: str,
    req: OpenReadingRequest,
    registry: Registry,
    router_config: RouterConfig,
    env: Mapping[str, str],
    port: JudgePort | None,
    backend_allowlist: frozenset[str] | None = None,
    broker: EnvCredentialBroker | None = None,
) -> DeciderStatus:
    """The judge's enablement + compliance gate for one `pick: best` node. Unlike the decider, the
    judge needs no top-level `decider:` block (its backend is on the `judge:` block, decider.md
    §1), but the SAME two-key env gate + compliance filter apply. `env` may carry a backend-id
    override, which also retargets the judge backend."""
    enabled, backend = _env_decider_backend(env, judge_backend)
    if not enabled:
        return DeciderStatus("engine", "env_disabled", backend)
    if backend_allowlist is not None and backend not in backend_allowlist:
        # Ahead of the compliance gate below so the reported reason is the one the caller can act
        # on, and because scope is the narrower, later subtraction (strategies.prune uses the same
        # precedence). Downgrades to engine mode rather than raising: a decision point resolving
        # to its engine default is this taxonomy's answer to every decider failure, and refusing
        # the whole walk over an out-of-scope JUDGE would be a bigger hammer than the caller's
        # scope asks for — the backends that actually process the document are bounded elsewhere.
        return DeciderStatus("engine", "scope_denied", backend)
    reason = _backend_eligible(backend, req, registry, router_config, broker)
    if reason is not None:
        return DeciderStatus("engine", reason, backend)
    if port is None:
        return DeciderStatus("engine", "unavailable", backend)
    return DeciderStatus("llm", None, backend)


def decision_record(
    dp: DecisionPoint,
    chosen: str,
    decider_label: str,
    downgraded: str | None,
    *,
    config_hash: str,
    strategy: str,
) -> dict[str, Any]:
    """Build the one-shape decision record appended to `orchestration.decisions[]` (decider.md
    §5). LLM-only fields (model_id, prompt_hash, rationale) are added by the 14.2 executor."""
    rec: dict[str, Any] = {
        "decision_id": dp.decision_id,
        "node_path": dp.node_path,
        "label": dp.label,
        "point": dp.point,
        "eligible": list(dp.candidates),
        "chosen": chosen,
        "decider": decider_label,  # "engine" | "llm" | "trace"
        "config_hash": config_hash,
        "strategy": strategy,
        "downgraded": downgraded,  # a DOWNGRADE_REASONS member, or None
    }
    if dp.signals:
        rec["signals"] = dp.signals
    return rec
