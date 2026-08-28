"""The strategy engine — the async walker over a pruned strategy tree.

`run_strategy(compiled, req, *, registry, clock, ...)` evaluates a `CompiledPlan`
(`openreading.strategies.prune`) and returns `StrategyResult(response, orchestration)`. It is the
one place a strategy file's semantics turn into backend calls: cascades (`steps:`), `route:`,
`parallel:` with race / best / merge + hedge / shadow / drain, `decide:` nodes and gate bands
through the decision layer (`openreading.strategies.decider`), `use:` references, single-leaf
strategies, and `granularity: page` cascades. It consumes ONLY pruned trees: compliance and
capability filtering happened once in compile, dropped backends do not exist in the tree it sees,
so execution is structurally incapable of widening eligibility. "The eligible set" below is the
router's stage-1/2 survivors for this request, computed once, up front. Pruning collapses upward
(`prune._prune_node` returns `None`): a composite whose children all vanish — a cascade with no
rungs, a parallel with no branches, a route rule whose target is gone, a decide whose `among:`
empties — disappears and its parent re-evaluates; a route whose `default:` target is fully
pruned collapses whole (matched or not, so `_eval_route` may assume `r["default"]` exists);
the root collapsing is the terminal `no_compliant_backend` refusal before any attempt.
`strategy validate --policy` flags each leaf unreachable under the policy — a `default:` target
included — per leaf, not as a route-specific warning.

Hook point and surfaces (wiring facts)
--------------------------------------
- The strategy layer engages in exactly one place: inside `openreading.api.run_request`, after
  validation and before any backend is dispatched, and only when a strategy actually engages
  (`backend.id: "strategy:<name>"`, or `defaults.strategy` on an `auto` request). The legacy
  paths are the literal old code — the `auto` arm still calls `router.executor.execute_plan`,
  the named arm still calls the driver directly, and a named-backend run imports nothing from
  `openreading.strategies` (pinned by a subprocess test). "No file ⇒ no change" is a
  byte-identical-output test obligation, never an assumption that this engine "reduces to" the
  old behaviour through shared code. `execute_plan` stays the executor for the no-strategy `auto`
  path and for `openreading route --run`, which builds a `RoutePlan` and never reads the config.
- `strategy:` is a documented reserved prefix of the free-string `backend.id` (no request-schema
  bump); `strategy:none` forces the legacy path. Both `make_adapter` call sites in
  `openreading.api` (`build_request`, `run_request`) special-case the prefix before touching the
  registry (a bare lookup would `KeyError` → a misleading 404 `unknown_backend`) — via an inlined
  `_STRATEGY_PREFIX` / `startswith` check, NOT `loader.strip_strategy_prefix`, so a named-backend
  run never imports this package (T10); the server is what calls `strip_strategy_prefix`. Known
  prefix + unknown name → `api._run_strategy_request` raises `UnknownStrategyError` (`backend_code`
  `unknown_strategy`): HTTP 400 (a malformed ask against this deployment's config, not a missing
  resource), CLI exit 2 (same class of caller error as an unknown `--backend`, so the exit-code
  table gains no new row).
- CLI: `parse` alone takes `--strategy <name>` (sugar for the prefixed id — a separate flag
  because `--backend` is an argparse `choices=` list that would reject the prefix), `--no-strategy`
  (`strategy:none`) and `--config PATH`; `route` takes only `file --policy p.json [--run]` (plus
  the shared `--env-file`) and never consults a strategy. Subcommands (each takes `[--config
  PATH]` except `explain`, which takes only the shared `--env-file` and its positional):
  `strategy validate [--policy p.json]` — grammar + world-consistency of every strategy (with a
  policy: flag steps unreachable under it); `strategy plan <file> --strategy <name> [--policy]` —
  the normalized, PRUNED tree for this document / policy, no execution; `strategy show <name>
  [--longhand]` — the body of a named strategy or built-in preset as written (or normalized);
  `strategy normalize` — shorthand → canonical longhand; `strategy list` — named strategies +
  presets in scope; `explain <response.json>` — the orchestration block as a narrative; `replay
  <file> --trace <trace.json> [--strategy] [--policy]` — re-execute taking each logged choice;
  `calibrate <dataset> --strategy <name> [--target-escalation F] [--max-cost-per-doc $]` —
  threshold tuning from a target escalation rate (proposes, never rewrites). Python:
  `openreading.run(source, strategy=..., config=...)`.
- Server: config comes ONLY from `OPENREADING_CONFIG` (never cwd sniffing, same posture as the
  env-only `RouterConfig`). `POST /v1/parse` and `POST /v1/jobs` accept `strategy:` ids; a
  strategy job wraps the whole walk as one synthetic `JobRecord` — the walk runs to completion
  inside the POST itself (threadpool-offloaded `api.run_request`, exactly as `/v1/parse`) and the
  record is created already `succeeded` with the response and its `orchestration` attached; there
  is no worker task and no observable `processing` state. `"auto"` / `strategy:none` on `/v1/jobs`
  stay rejected.
- When a strategy engages, compile strips `routing.fallback` before calling `Router.route` so
  `auto` leaves see pure stage-3 score order (warning `strategy_overrides_fallback`).
  `optimize_for` keeps exactly its role: stage-3 weights at `auto` leaves and the comparator's
  tiebreak input; it never overrides a named leaf. `limits:` binds only here (strategy runs),
  never a direct-named request.

Value domain: Outcomes
----------------------
Every node evaluation produces exactly one `Outcome`:
`Ok(response, quality)` — succeeded and passed the applicable gate; `Deficient(response,
quality)` — succeeded but a gate fired, the response is real and retained; `Err(class)` — no
usable response. `class` is the closed taxonomy `classify_error` maps onto (timeout,
rate_limited, auth, provider_error, unsupported_feature, invalid_input) plus the composite
classes `exhausted` and `budget_exhausted`. `quality` for a cascade rung is the fraction of
applicable `escalate_if` predicates the response passed (inapplicable predicates leave the
denominator; none applicable → 1.0); for a `pick: best` candidate it is the same fraction over
the default-bundle Tier-1 predicates (`_branch_quality`), so comparison is never 0/0.

- Law 1 (retention): a `Deficient` response is never discarded — it is best-so-far for the
  enclosing walk and may ultimately be returned.
- Law 2 (status honesty): a returned `Deficient` keeps the `status.state` the backend reported.
  Degradation is `orchestration.outcome: "degraded"` plus a `quality_below_threshold` warning —
  the engine never fabricates a status the backend did not produce.

Cascade evaluation (`steps:`)
-----------------------------
Per step, in order: (1) deadline check — `now > deadline` stops the walk, resolved below;
(2) credential check — unresolvable → `skipped(missing_credentials)`, continue (the skip is a
real, journaled step outcome, and on a replayed walk it still falls through to the next rung);
(3) run the child — a leaf dispatches through the executor, a composite evaluates by its own
rules; (4) on `Err(class)` consult the effective `on_error` map — the step's own, else the
cascade's, else the built-in defaults (`_DEFAULT_ACTION`: `invalid_input → fail`, everything else
`next`; a map may key an exact class, `transient` = timeout/rate_limited/provider_error, or
`any`) — `next` records and continues, `fail` resolves the cascade `Err`; (5) on success evaluate
the step's gates on the normalized response: `escalate_if` fires → `Deficient`, retained,
attempt recategorized `quality_escalated` (each predicate's observed-vs-threshold bound to the
attempt), continue; else `review_if` fires → a `gate_band` decision point with candidates
`accept` / `escalate` (engine default `review_default`, itself defaulting to `escalate`) —
`accept` returns `Ok`, `escalate` retains as `review_escalated`; neither fires → `Ok`
immediately, later steps never run. Only a `parallel` step carries a step-position gate,
evaluated on its winner's response; nested cascade / `use:` / route / decide steps own their
escalation (D-v3-7 — distributing a parent gate onto a nested cascade made `normalize` not a
fixed point).

- Law 3 (recovery): if any step is `Ok`, the cascade is `Ok`; earlier failures and escalations
  become `warnings[]` (`fallback_used`, `quality_escalated`), never errors.
- Attempted set (T12): one request-wide set of backend ids shared across rungs, parallel
  branches (shadows and losers included) and nested strategies. `backend: auto` picks the first
  not-yet-attempted backend in stage-3 order; none left → `Err(exhausted)` with detail
  `no_untried_backend`. Scoping the set per node would silently re-run backends at `auto` leaves.
- Law 4 (keep-best): when the steps run out or the deadline ends the walk, a retained
  `Deficient` is returned — highest quality wins; ties keep the earliest-retained rung (the
  comparison is strict-greater). The spec asks for the opposite tiebreak — highest rung index
  (a later, more capable rung), then first-completed; the engine does not implement it. That is
  `on_quality_exhausted: best_effort` (the default); `fail` resolves `Err(exhausted)` instead.
  Nothing retained and the STEP LIST ran out → `Err(exhausted)`; nothing retained and the
  DEADLINE ended the walk → `Err(budget_exhausted)`.
  The distinction is routable through `on_error`. At the root EVERY `Err` outcome — `exhausted`,
  `budget_exhausted`, `invalid_input` alike — raises `PlanExhaustedError` (itself a
  `TerminalError`) carrying the node-path-annotated trail, the class named in the message; there
  is no distinct exception type per class.
- Honest money: every billed rung (escalated and winner) is summed into the returned response's
  `usage.cost_usd`; `usage.cost_basis` folds by the priority reduction `billed > estimated >
  infra_only > unknown`, monotonically non-decreasing (never last-write-wins, so a weaker later
  rung cannot downgrade a `billed` rung); a real cost with no basis coalesces to `unknown`, and a
  nonzero total can never leave `cost_basis` at `infra_only` or unset (BL-126 / BL-134).

Parallel evaluation (`parallel:`)
---------------------------------
1. At node entry the node deadline is its own `budget.max_duration` clamped to the enclosing
   deadline — a child can tighten, never extend. A branch whose `start_after` would land past
   the deadline is not launched: `deadline_pruned`.
2. Branches launch as main-loop coroutines, each dispatching its leaf on
   `ctx.registry.get(backend)` — the ONE per-request adapter instance `build_registry()` made,
   not a fresh instance per branch. No credential-bound client is shared across siblings only
   because validation forbids two sibling branches naming the same backend (T4 holds by that
   rule, not by instantiation). A hedged branch sleeps the FULL `start_after` on the shared clock
   and is added to the attempted set only when it actually launches, so a hedge cancelled
   mid-stagger is neither attempted nor billed; the node resolving first cancels a still-parked
   hedge, but a sibling's failure does NOT shortcut the remaining delay (the spec asks for that
   wake; the code has none — a hedge waits out its stagger even after every earlier sibling has
   failed). Composite branches recurse through `_eval_node`.
3. `pick: fastest` — the first non-shadow success wins. Per-branch quality gates are not
   evaluated. Losers are `raced_lost`.
4. `pick: best` / `merge` — wait until `require:` non-shadow branches SUCCEED (`all` = every
   non-shadow branch; a failed branch lowers the attainable count, and once no more can succeed
   the comparison runs over what completed). The spec also ends the wait when the node deadline
   fires (still-running branches cancelled or drained, then the comparator runs over the
   completed set); `_drive_best` has no deadline check — it returns only on `require` successes
   or every non-shadow branch resolving, so the node deadline bounds hedge launch, cancel
   dispatch and the FakeClock drain, never the wait itself. Comparator: composite quality, then
   cheaper backend (descriptor cost midpoint), then lower branch index — or the LLM judge when
   `judge:` is configured, enabled and eligible. Losers are `judged_lost`; under `merge` they are
   `merge_source` and the base is `merge_base`.
5. Shadows (`shadow: true`) run and are recorded (`shadow`) but never win, never count toward
   `require`, never enter the composite-failure class set, and are always drained.
6. Law 5 (composite failure): all non-shadow branches failed → `Err(exhausted)` with every
   branch's own class preserved in the trail, EXCEPT unanimity — every branch `invalid_input` →
   `invalid_input` (the document itself is bad, and the default `fail` correctly stops the walk).
   One branch's `invalid_input` is branch-terminal only; mixed failures are never laundered into
   a more benign class. A composite branch that resolved `Deficient` counts as a success here.
7. Law 6 (cancellation, drain, money):
   - `on_win: cancel` (default; suppressed under `merge`, where every loser is a vote) cancels
     each non-shadow loser's task and asks its backend to stop via `adapter.cancel()` — a POLL
     loser stops being polled; a WEBHOOK loser is marked in `orchestration.webhook_dropped[]`
     so a late delivery is acknowledged and dropped against the closed decision (T8), never
     applied and never an error. `on_win: drain` lets losers finish and bills them.
   - Honest cancel semantics: `adapter.cancel()` reaches the vendor only where the descriptor
     says `cancel_supported: true` — `openreading backends` / `GET /v1/backends` give the
     current per-backend answer (chunkr, nuextract, pulse, reducto at the time of writing);
     elsewhere it stops LOCAL polling only, the vendor job keeps running and billing, and
     `cancel` vs `drain` differ only in whether the run waits and records the loser's result.
     Even where a real stop call exists, do not assume it avoids the charge absent a vendor
     statement: none of chunkr / nuextract / reducto document billing-on-cancel either way, and
     Pulse documents the opposite (pages are charged regardless). chunkr's stop is further
     scoped to a task still QUEUED, not one already processing, so most real races — where the
     loser has usually started by the time a winner is picked — see no cost benefit from it.
   - The spec's promise is that the response never blocks past the node deadline waiting for a
     drain or a cancel. The CANCEL half holds on every clock (next bullet). The DRAIN half is
     enforced only under a coordinated `FakeClock`: `_drain` cuts a branch whose next virtual wake
     lands past the deadline, recording it (loser or shadow, detail `drain_over_deadline`) with
     NO cost — never awaited to completion, its cost is unknown, not fabricated. Under a
     `RealClock` `_drain` has no wake to inspect and waits until every remaining task completes,
     and a parallel leaf's own driver runs with `DEFAULT_DEADLINE_MS` rather than the node's
     remaining budget — so in production a drained loser CAN hold the response past the node
     deadline. A completed loser's real cost stays billed (`cost_basis: "billed"`). T9's
     variant — a drained loser outliving the response records `cost_basis: "estimated"` — is
     not implemented: such a loser or shadow carries neither cost nor basis, only the detail.
   - Cancel calls are dispatched concurrently on `_CANCEL_DISPATCH_EXECUTOR`, a dedicated pool,
     and awaited only up to the node's remaining deadline; one still running is abandoned, not
     retried. It is NOT the default `to_thread` executor on purpose: `asyncio.run()` (one per
     `run_strategy`) waits for every outstanding default-executor future at shutdown, so an
     abandoned cancel there would still add its full duration to the response.
   - `usage.cost_usd` on the final response totals ALL attempts — winners, losers, shadows,
     judges, deciders — with the per-attempt breakdown in `orchestration.attempts[]`. A
     composite branch's spend lives on its nested response and is folded in the same way.
8. `pick: best` also computes the worst pairwise text disagreement (1 − token Jaccard) across
   the compared branches; it is exposed to the enclosing step gate as `disagreement_over` and
   recorded on the winner attempt. `pick: merge` votes `typed_fields` per field — majority
   value, then highest reported confidence, then cheaper backend, then first-listed — always a
   real backend's `TypedField` (never a fabricated value or confidence; a field no branch produced
   stays absent); text / markdown / pages are the base's wholesale; per-field provenance goes to
   `orchestration.merge[]`, not the response. Every parallel winner stamps
   `pages[].source_backend`.

Route, decide, use, leaf, page granularity
------------------------------------------
- Route: facts are computed once per document before evaluation; rules evaluate top-to-bottom,
  the first match dispatches, and ALL rules are still evaluated for the trace (shadowed-rule
  debugging) — `_eval_route` appends one `point: "route"` record to `orchestration.decisions[]`
  carrying every rule's `matched` flag and per-fact records. An uncomputable fact makes its rule
  not match and its fact record reads `status: "unavailable"` (the spec's `fact_unavailable`;
  `openreading.strategies.facts`). Byte-dependent facts (`pages_over` / `pages_under`,
  `size_over_mb` / `size_under_mb`, `sample_percent`) read `document.bytes_base64` only: compile
  (`api._run_strategy_request`) materializes a URL document before fact computation whenever any
  ELIGIBLE backend's descriptor lacks `accepts_url` (mirroring the `auto` arm); the spec's second
  trigger — a byte-dependent fact or signal referenced by the tree — is not implemented, so a
  URL document every eligible backend accepts reaches fact computation un-materialized and those
  facts are `unavailable`. Route adds no class of its own.
- Decide: candidates are the `among:` labels (post-pruning) plus `otherwise`; engine mode
  dispatches `otherwise:`; an enabled LLM decider picks one candidate; any decider failure
  resolves to `otherwise:` with the reason on the decision record's `downgraded` field (the
  spec's `decider_downgraded`). A decide whose own `otherwise:` was pruned dispatches the
  substituted first-listed survivor, traced `otherwise_pruned`
  (D-v3-24 — a pruning event, not a decider failure, so it fires with no decider configured).
- `use:` — resolves a named sibling tree and preserves the name in the trace: the target
  evaluates under node path `{path}->{name}`; a name already on the CURRENT recursion path raises
  `StrategyReferenceCycle` (typed, catchable) instead of exhausting the interpreter stack; a
  referenced tree pruned to nothing is `Err(exhausted)`.
- A bare leaf strategy has no step gate: success is `Ok`, skip/exhausted is `Err(exhausted)`.
- `granularity: page` (cascades only, PDF): each rung's `escalate_if` is evaluated PER PAGE on
  a one-page snapshot; only failing pages escalate, sent as `pages.ranges` when the backend
  declares the `page_range_selection` capability (else that rung runs document granularity).
  Pages stitch per page, each carrying `source_backend`, mirrored in `orchestration.pages[]`;
  `document.text` stays rung 0's. No keep-best across rungs, and no forward escalation of a
  failed rung either: a rung that raises (or returns non-ok) `break`s the walk and the pages
  stitched so far from earlier rungs are returned as they stand — only gate-failing PAGES move
  forward to the next rung. Every rung's cost is summed (BL-120).

Decision layer, judge, masking, replay
--------------------------------------
- Enablement is two keys, resolved ONCE per walk so every decision point binds identically: a
  file `decider:` block AND `OPENREADING_LLM_DECIDER`. No request field can enable it. Downgrade
  reasons, in priority: no block → pure engine (no annotation); block but env off →
  `env_disabled`; env on but the decider backend compliance-dropped (checked via
  `Router.check_eligible` against request ∪ policy compliance) → `compliance`; enabled + eligible
  but no `DeciderPort` wired → `unavailable`; at call time `malformed` (port raised, or an
  out-of-set action) or `refusal` (`None`). `timeout` is a member of the decider's
  `DOWNGRADE_REASONS` but has no producer: the port call runs via `to_thread` with no per-call
  deadline (deferred work), so a hanging port is bounded only by the enclosing walk deadline.
  Every downgrade resolves to the deterministic engine default — an LLM outage can never fail a
  parse. The action space is a closed candidate
  list; the engine re-validates whatever the port returns and meters each call as a
  `decider_call` attempt at its backend-reported cost. The port sees signals, candidates, intent
  and remaining time budget only — never compliance (rail 4).
- Judge (`pick: best` + `judge:`): per-node enablement with the SAME env gate and compliance
  filter, keyed on the judge block's own backend; the port compares ONE positional pair with
  sources hidden (labels A/B, capped excerpt, masked `typed_fields`); the engine runs both
  orderings (position-bias guard), calls an inconsistent verdict a tie broken like the engine
  comparator, and runs single elimination in listed order (n−1 pairs), metering `judge_call`s.
  The judge selects a real candidate; it never synthesizes.
- `decider.llm.mask_fields` masks ONCE where the `DecisionPoint` is built, so the port input and
  the trace record share one masked view; the judge masks candidates identically. `typed_fields`
  are rendered to a plain `{value, confidence}` view first so a pydantic object cannot smuggle a
  value past a scan. A canary test proves a planted secret reaches no decider input and no
  `json.dumps(orchestration)`.
- Replay (`run_strategy(replay=<decisions>)`, `openreading replay --trace`) is a decision MODE,
  not a live port: every decision point takes its logged choice by `decision_id`
  (`decider: "trace"`), no LLM is called, and the env / compliance gates are bypassed. A point
  absent from the trace, or whose logged choice is no longer a candidate, takes the engine
  default traced `trace_missing`. The judge replays by logged winner backend. `replay=[]` is a
  valid empty trace (mode ON — everything `trace_missing`), distinguished from `None` by
  identity, so an empty trace never silently disables replay.

Budgets
-------
The outer time budget is `limits.max_duration_per_doc` if set, else the caller's `deadline_ms`,
else `DEFAULT_DEADLINE_MS` — resolved with `is not None` checks so a deliberate zero ("no time
budget") survives as an immediate deadline instead of being swallowed by `0 or default`. Each
node's `budget.max_duration` clamps to its parent's remaining time. Cost budgets were removed;
`limits:` is time-only. For cascade and paged-cascade leaves the remaining budget reaches the
adapter as `rc.deadline_ms` (clamped at 0) as well as bounding the driver's wait loop; a
parallel branch's leaf instead runs with the constant `DEFAULT_DEADLINE_MS` — the node deadline
bounds the orchestration loop (hedge pruning, cancel dispatch, FakeClock drain), not the
branch's own driver or `rc.deadline_ms`.

Determinism and replay (Law 8)
------------------------------
Given (document bytes, normalized tree + config hash, eligible set, per-attempt responses) the
decision sequence is a pure function: route facts are computed once; ties break by index
everywhere; `sample_percent` is content-hashed; no decision rule reads the wall clock.
`decision_id` is a digest of (config_hash, node_path, walk-global seq), never a time-based id.
The wall-clock-dependent constructs are exactly: the `pick: fastest` winner, hedged `start_after`
launch-or-not, the `require: N` completion set, and deadline-fired cancellation — each logs its
resolved outcome so a replay is closed over the trace even where a live re-run may differ.
Engine-mode runs replay exactly; live LLM-mode divergence is attributable via the decision
record's (`model_id`, `prompt_hash`) pair — that pair is the contract, though today only
`model_id` is emitted and `prompt_hash` is not yet populated (`openreading.strategies.decider`).

Under a coordinated `FakeClock` (tests) branches model latency with `clock.sleep`, and the
winner / `require` threshold is judged only at a fully-drained virtual instant (`inflight == 0`,
D-v3-16): a same-latency tie decided by real thread-completion order was a ~50/50 flake. The
advance rule (T3, `_advance_if_parked`): virtual time moves only to the EARLIEST pending wake,
and only when every still-pending branch is parked on `clock.sleep` and no `to_thread` offload
is in flight — a pending future blocks advancement; `_drain` applies the same rule and, instead
of taking a wake past the node deadline, cuts the branch (Law 6). The race and `require`
admission order by the ledger `journal_seq` (the journaled completion order,
which is the true order on a live run and the only stable order on a resumed one, where every
branch's `exec()` returns near-instantly), falling back to branch index; a composite branch has
no `journal_seq` and sorts last. `clock` is a required argument and this module never constructs
a real clock (pinned by test): a defaulted wall clock was itself a leakage source.

Concurrency contract
--------------------
- T2 — the engine NEVER calls `run_to_completion` on the running loop (it wraps `asyncio.run`,
  which crashes inside a running loop). Each leaf's whole synchronous submit → drive → normalize
  → meter runs in a worker thread via the executor (`_execute_leaf_sync`), where the driver's own
  `asyncio.run` is safe and the tested driver is reused verbatim. `run_strategy` wraps the walk
  in exactly one `asyncio.run` at the sync boundary (D-v3-10); the server keeps its threadpool
  offload of `run_request`, so loop nesting never recurs. `DeciderPort.decide` and
  `JudgePort.compare` also run via `to_thread`: a slow synchronous port on the loop thread starved
  every sibling coroutine, a timing-dependent nondeterminism source.
- T5 — idempotency cache: the engine consults NONE; a strategy run does the work every time
  (D-v3-3 — the dead seam was removed rather than left threaded-but-unread). The only cache is
  the server's: one `BoundedResultCache` per `create_app` at `app.state.result_cache`, passed by
  `/v1/parse` into `run_request(cache=…)` → `execute_plan` for the legacy `auto` chain only;
  `run()`, the CLI and `/v1/batch` pass none. Whoever builds it (Law 7) must:
  key per backend id (sibling branches cannot share a backend, so no collisions); count a hit as a
  `succeeded` attempt at `cost_usd: 0` with the `idempotent_replay` warning — an instant success,
  so a hit racing a live sibling resolves `pick: fastest` at once and cancels the rest; never cache
  cancelled
  or partial results; cache `Deficient` results (real, complete responses); read/write only on
  the orchestration loop thread after gather (the cache is an unlocked `OrderedDict`), never in
  an adapter thread; keep `sample_percent` buckets content-hash-deterministic.
- T7 — same-backend retry is the driver's alone; a `RetryableError` reaching the engine means
  "this backend is exhausted → advance". No retry knobs exist in the strategy layer (the vendored
  schema rejects them).
- T12 — the attempted set is walk-wide (see cascade rules above).
- Nested composite steps get `replace(ctx, deadline_ms=...)`: the walk-wide accumulators (trace,
  attempted set, candidates) are shared by reference, so never add a merge-back step (it would
  double-count). A hand-copied field list once silently dropped fields added later.
- Attempt records are bound BY REFERENCE (`_RunResult.attempt`, `_resolve_decision_point`'s
  returned record), never via `trace.attempts[-1]`: a `decider_call` appended while resolving a
  review band once made `[-1]` the wrong record.
- A crash out of ordinary adapter code (`normalize`) is classified `provider_error` and treated
  as a recoverable leaf failure — it used to blow up the whole parallel node / cascade (BL-99).
- Ledger: every leaf dispatch goes through `ctx.executor` (`openreading.ledger.ports.Executor`)
  as one `submit` step; `step_id = sha256(run_id ‖ step_path ‖ step_seq)` so a re-journaled step
  lands on the identical id (a random UUID cannot); `content_key` derives from the document's
  real bytes, never a machine-local path identity, so a run relocated to another worker computes
  the same key. A missing-credentials skip is journaled so a resume can replay it.

The orchestration block
-----------------------
`trace.orchestration()` yields: `strategy`, `config_hash` (compliance-aware — it folds in the
effective compliance block, the `RouterConfig`, and a per-backend eligible/dropped digest, so two
runs with identical pruned trees compiled under different postures hash differently),
`chosen_backend`, `fallback_depth` (rungs advanced past before the returned result —
implemented as the count of non-`succeeded` attempts, `openreading.strategies.trace`), `outcome`
(`ok` | `degraded`), `attempts[]` (the full trail), `decisions[]` (decision points — gate
bands, decide nodes, judges — plus one `point: "route"` rule-evaluation record per route node;
never plain attempts), and when non-empty `dropped[]`
(compliance/capability prunes — NOT attempts — carrying the
router's `DropReason {backend, stage, code, detail}` verbatim), `pages[]`, `merge[]`,
`webhook_dropped[]`; plus `candidates[]` when `keep_candidates=True` (every completed non-winner
parallel branch's full envelope, for `compare --from`; default off for payload size; cascade
rungs are not retained — D-v4-14). Each attempt carries backend, category, node path / label,
duration, cost + `cost_basis`, and for gate events each predicate's observed vs threshold, fired
or `signal_unavailable`. Closed category vocabulary: `succeeded` · `error(<class>)` ·
`skipped(missing_credentials)` · `skipped(circuit_open)` · `deadline_pruned` ·
`quality_escalated` · `review_escalated` · `raced_lost` · `judged_lost` · `shadow` ·
`merge_base` · `merge_source` · `decider_call` · `judge_call`. `skipped(circuit_open)` is
reserved vocabulary (`openreading.strategies.trace`) with NO emitter: no circuit breaker exists
anywhere in `src/` — `defaults.advanced.circuit_breaker` is schema-accepted and unread — so the
spec's T6 floor (never bench a request's last compliance-eligible backend) is designed, not
shipped. Warnings this module puts on the final response: the compile-time warnings
(`strategy_overrides_fallback` among them),
`fallback_used` and `quality_escalated` (one per rung walked past, naming from → to),
`quality_below_threshold` (a degraded result), plus the BAA-tier note for the chosen backend.
The spec's wider warning vocabulary is NOT emitted as warnings here: `raced_lost` / `judged_lost`
/ `merge_base` are attempt categories, `decider_downgraded` is the decision record's
`downgraded` field, and `hedged_start` / `budget_exhausted` / `idempotent_replay` have no
emitter in this module (the
`idempotent_replay` warning belongs to the legacy chain's server cache). The response schema
carries `orchestration` and `pages[].source_backend` as an additive
v0.2 bump (old responses still validate). `openreading explain` renders the block as narrative.

Environment
-----------
This module reads `os.environ` only as the default for `run_strategy(env=...)`, which it hands to
the decider layer; the single key consumed from it is:

- `OPENREADING_LLM_DECIDER` — the second enablement key for the LLM decider and judge. `1` /
  `true` / `yes` / `on` enables; a backend id both enables AND overrides `decider.llm.backend`
  (and the judge backend); unset or `0` / `false` / `no` / `off` disables. Unset: every configured
  decision point downgrades `env_disabled` to its deterministic default — a checked-in config must
  not be able to start spending LLM tokens without operator-level consent at the environment
  layer. (`OPENREADING_DECIDER_EXECUTOR` / `OPENREADING_DECIDER_MODEL` are specified in
  internal/design/decider-executor.md but have no reader.)

`OPENREADING_LEDGER` is read by `openreading.api`, not here: when set, `run()` / `run_request()`
pass a `run_id` and an armed `InlineExecutor` (real journal / blob store); unset — and for every
other caller, tests included — `_WalkCtx.__post_init__` supplies a fresh `run_id` and an unarmed
`InlineExecutor(journal=NullJournal(), blobs=None)`, preserving pre-ledger behaviour.

Edge-case catalog
-----------------
1. Hosted loser in a race — stays billed; real cost summed; `raced_lost`; the response never
   waits for the drain; a drain past the deadline is recorded with no cost (Law 6).
2. `confidence_below` on a confidence-less backend — inapplicable: does not fire, recorded
   `signal_unavailable`. Gates that can never bind anywhere are load-time errors; the default
   bundle degrades by design. Bindability checks skip `auto` leaves (no fixed descriptor).
3. Outer 3 s remaining, inner `max_duration` 10 s — the inner clamps to 3 s; a hedge past it is
   `deadline_pruned`; a deadline ending a walk with nothing retained is `budget_exhausted`.
4. Escalation target compliance-dropped — the cascade simply has one fewer rung (the drop is in
   `orchestration.dropped[]`); every rung pruned → terminal `no_compliant_backend` before any
   attempt, never a silent downgrade.
5. WEBHOOK backend in a race — one Job state machine; a cancelled loser's late delivery is
   acknowledged and dropped; interior branches degrade to polling where no webhook bus exists.
6. Decider returns out-of-set / goes rogue — closed strict-tool action space, so out-of-set is
   `malformed`; any failure → engine default with `downgraded: <reason>` on the decision record
   (no per-call timeout exists — a hanging port is bounded by the walk deadline only); decider
   spend is a billed attempt like any other.
7. Both branches fail differently — `exhausted` with branch classes preserved; unanimous
   `invalid_input` propagates as `invalid_input` (Law 5).
8. Unknown backend id / reference cycle in the file — load-time errors with the path printed;
   a cycle that survives to run time raises `StrategyReferenceCycle`. Unknown `strategy:<name>`
   on the wire — `unknown_strategy`, HTTP 400 / exit 2.
9. Idempotency cache hit racing a live call (Law 7 — specified, not built; today no cache is
   consulted) — branches key separately; a hit is an instant `succeeded` attempt at zero cost,
   so `pick: fastest` resolves immediately and cancels the rest.
10. Same config run twice — engine mode: identical decision path (Law 8). LLM mode: replayable
    via the trace; live-run divergence is attributable. Rails (budgets, eligibility, thresholds)
    bind identically; choices inside gray bands may differ and are always traced.

Decisions (internal/decisions/DECISIONS.md)
-------------------------------------------
- D-v3-2: `strategy:` reserved prefix, no wire bump; `loader.strip_strategy_prefix` is the
  recognizer the server uses, while `openreading.api` guards both `make_adapter` sites with its
  own inlined `startswith` check (T10 — no strategy import on a named-backend run) against a
  `KeyError`-turned-404.
- D-v3-3: no engine cache; server-only cache on the legacy chain — a silently-memoizing library
  call, and a batch total summing replayed costs nobody was billed, were the failures avoided.
- D-v3-5: additive response v0.2 (`orchestration`, `pages[].source_backend`); legacy output stays
  byte-identical.
- D-v3-7: gate distribution stops at nested-cascade steps so `normalize` is a fixed point.
- D-v3-10: whole-leaf offload to a worker thread; one `asyncio.run` per walk (T2).
- D-v3-12: `limits:` is the outermost budget in `run_strategy`, so it binds every strategy run
  and never a direct-named request.
- D-v3-13/14: coordinated FakeClock + virtual latency; `start_after` sleeps before attempting;
  shadows excluded from pick and failure set; `drain` finish-and-bill; `require` counts
  successes; late-webhook acknowledge-and-drop.
- D-v3-15: two-key enablement once per walk; `unavailable` reason; deterministic `decision_id`
  (a wall-clock id would break replay); the port is re-validated, never trusted.
- D-v3-16: winner judged only at `inflight == 0` — fixes the thread-order tie flake.
- D-v3-17: engine owns metering / re-validation / pairwise orchestration; the port is thin.
- D-v3-18: replay is a decision mode, not a live port; masking feeds both sinks once.
- D-v3-19: merge composes `typed_fields` only; base wins document channels; provenance in trace.
- D-v3-20: page granularity is its own evaluator; range support is the `page_range_selection`
  capability.
- D-v3-23: audit close-out — cascade honest money, deadline-bounded drain, webhook drop marker,
  `/v1/jobs` synthetic job, no eager strategy import, compliance-never-widened property test.
- D-v3-24: `otherwise_pruned` downgrade reason.
- D-v4-14: `keep_candidates` retains parallel branches only; threaded as a Python param, never a
  request-schema field (the vendored schema is `additionalProperties: false`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import os
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from openreading.credentials import DEFAULT_DEADLINE_MS, EnvCredentialBroker, build_run_context
from openreading.ledger.header import slim_request
from openreading.ledger.inline import InlineExecutor, NullJournal
from openreading.ledger.ports import Executor
from openreading.ledger.step import ExecResult, StepRequest
from openreading.readiness import auth_hinted, missing_required
from openreading.router.cache import content_key, document_digest
from openreading.router.clock import Clock, FakeClock
from openreading.router.compliance import BAA_TIER_CONFIRMED_WARNING
from openreading.router.cost import apply_cost_report
from openreading.router.driver import run_to_completion
from openreading.router.registry import Registry
from openreading.strategies.decider import (
    DeciderPort,
    DeciderStatus,
    DecisionPoint,
    JudgeCandidate,
    JudgePort,
    decision_id,
    decision_record,
    mask_typed_fields,
    resolve_decider_status,
    resolve_judge_status,
    revalidate_action,
)
from openreading.strategies.facts import Facts, compute_facts, doc_bytes, evaluate_when
from openreading.strategies.normalize import DEFAULT_BUNDLE, candidate_label
from openreading.strategies.plain import ADVANCED_TO_PLAIN
from openreading.strategies.prune import CompiledPlan
from openreading.strategies.signals import evaluate_gate, probe
from openreading.strategies.trace import Attempt, GateRecord, Trace
from openreading.types.enums import WaitMode
from openreading.types.errors import (
    ComplianceRefused,
    PlanExhaustedError,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest
from openreading.types.response import NormalizedResponse

# Default on_error action per class (spec §5.1). `missing_credentials` is skip (handled before
# on_error); `compliance` is uncatchable (pruned).
_DEFAULT_ACTION = {
    "timeout": "next",
    "rate_limited": "next",
    "auth": "next",
    "provider_error": "next",
    "unsupported_feature": "next",
    "invalid_input": "fail",
    "exhausted": "next",
    "budget_exhausted": "next",
}
_TRANSIENT = {"timeout", "rate_limited", "provider_error"}
_AUTH_CODES = {"auth_rejected", "permission_denied", "invalid_key"}
_INVALID_INPUT_CODES = {"corrupt_document", "invalid_password", "unreadable_input"}
_UNSUPPORTED_LIMIT_CODES = {"doc_too_large", "page_limit_exceeded"}
_RATE_CODES = {"429", "rate_limited", "throttled", "resource_exhausted"}
_TIMEOUT_CODES = {"timeout", "deadline_exceeded", "deadline exceeded"}


class StrategyConstructUnavailable(TerminalError):
    """A grammatically-valid node whose engine support has not shipped in this milestone."""


class StrategyReferenceCycle(TerminalError):
    """A `use:` reference resolves back to a strategy already on the current recursion path (T2
    §4.3, internal/design/ledger.md §7's determinism work) — an infinite recursion, caught as a typed,
    catchable error instead of exhausting the interpreter's own stack."""


def classify_error(exc: Exception) -> str:
    """Map an adapter exception onto the closed error-class taxonomy (spec §5.1, verbatim)."""
    code = (getattr(exc, "backend_code", None) or "").lower()
    if isinstance(exc, UnsupportedFeatureError):
        return "unsupported_feature"
    if isinstance(exc, RetryableError):
        if code in _TIMEOUT_CODES:
            return "timeout"
        if code in _RATE_CODES:
            return "rate_limited"
        return "provider_error"
    if isinstance(exc, TerminalError):
        if code in _AUTH_CODES:
            return "auth"
        if code in _INVALID_INPUT_CODES:
            return "invalid_input"
        if code in _UNSUPPORTED_LIMIT_CODES:
            return "unsupported_feature"
        return "provider_error"
    return "provider_error"


def on_error_action(cls: str, *maps: dict[str, str] | None) -> str:
    """Resolve `next`/`fail` for a class, most-specific map first, then class defaults."""
    for m in maps:
        if not m:
            continue
        if cls in m:
            return m[cls]
        if cls in _TRANSIENT and "transient" in m:
            return m["transient"]
        if "any" in m:
            return m["any"]
    return _DEFAULT_ACTION.get(cls, "next")


# --------------------------------------------------------------------------- Outcome domain


@dataclass
class Outcome:
    kind: str  # "ok" | "deficient" | "err"
    response: NormalizedResponse | None = None
    quality: float = 1.0
    error_class: str | None = None
    # cross-branch disagreement of a pick:best parallel (§11); the enclosing step's gate reads it.
    disagreement: float | None = None

    @classmethod
    def ok(cls, resp: NormalizedResponse, quality: float = 1.0) -> Outcome:
        return cls("ok", resp, quality)

    @classmethod
    def deficient(cls, resp: NormalizedResponse, quality: float) -> Outcome:
        return cls("deficient", resp, quality)

    @classmethod
    def err(cls, error_class: str) -> Outcome:
        return cls("err", None, 0.0, error_class)


@dataclass
class _WalkCtx:
    req: OpenReadingRequest
    registry: Registry
    broker: EnvCredentialBroker
    # Shared across the main event-loop thread and every asyncio.to_thread worker a leaf dispatch
    # spawns (Ledger T2 §4.1 — all three _execute_leaf_sync call sites now pass this same instance,
    # not a fresh real-wall-clock one). Safe for a stateless real clock and a FakeClock in auto-advance mode;
    # NOT safe for a coordinated FakeClock backing a real (non-INLINE) branch that needs an actual
    # backoff sleep inside that boundary — see router/clock.py's own FakeClock docstring for the
    # cross-event-loop deadlock this can hit (Phase C round-1, alex-phasec F2).
    clock: Clock
    trace: Trace
    trees: dict[str, dict[str, Any] | None]
    eligible: list[str]
    facts: Facts = field(default_factory=Facts)
    attempted: set[str] = field(default_factory=set)
    deadline_ms: float | None = None
    # decision layer (M4): the once-per-walk decider verdict + the (optional) LLM executor seams.
    decider_status: DeciderStatus = field(default_factory=lambda: DeciderStatus("engine"))
    decider_llm: DeciderPort | None = None
    judge_llm: JudgePort | None = None
    config_hash: str = ""
    strategy_name: str = ""
    # this strategy was authored in the Plain dialect — tag gate records with the Plain source word
    # so `explain` groups them (§9). False for advanced strategies (records render flat).
    plain_sourced: bool = False
    # per-node judge enablement (a judge backend is on the node, so its gate is resolved lazily):
    env: Mapping[str, str] = field(default_factory=dict)
    effective_compliance: Mapping[str, Any] = field(default_factory=dict)
    router_config: Any = None
    mask_fields: list[str] = field(default_factory=list)  # decider.llm.mask_fields (§5, both sinks)
    # replay (14.3): decision_id -> logged decision record; when set, every decision point takes its
    # logged choice (decider: "trace") instead of consulting a live executor (decider.md §5).
    replay: dict[str, dict[str, Any]] | None = None
    # v0.4 Compare bridge (D-v4-6/D-v4-14): when set, every completed non-winner branch's full
    # normalized envelope is retained for `orchestration.candidates[]`. Default off — payload bloat.
    keep_candidates: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Ledger T1 (internal/design/ledger.md §5, plan §6): run_id identifies this walk to the journal/blob
    # store; executor is the Executor Protocol every leaf dispatches through. Both default to the
    # L1-preserving unarmed shape (a fresh run_id, InlineExecutor(journal=NullJournal(), blobs=None))
    # in __post_init__ so every pre-existing _WalkCtx(...) construction site — test and production
    # alike — keeps working unchanged; api.py's run()/run_request() pass an armed executor explicitly
    # when OPENREADING_LEDGER is set.
    run_id: str = ""
    executor: Executor | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = str(uuid.uuid4())
        if self.executor is None:
            self.executor = InlineExecutor(
                journal=NullJournal(), blobs=None, registry=self.registry, clock=self.clock
            )


def _get_executor(ctx: _WalkCtx) -> Executor:
    """`ctx.executor` is typed `Executor | None` only so `_WalkCtx(...)` construction (every
    pre-existing test call site included) doesn't have to pass one — `__post_init__` always leaves
    it set. This narrows that back to `Executor` at the three call sites that dispatch through it."""
    assert ctx.executor is not None
    return ctx.executor


def _response_payload(result: ExecResult) -> NormalizedResponse:
    """Ledger T3: `result.payload` is already a live `NormalizedResponse` on a fresh "ok"
    dispatch. A REPLAYED "ok" record's payload came back through the journal/blob store as plain
    JSON (`InlineExecutor` is deliberately response-type-agnostic — `ledger/inline.py` never
    imports a concrete response type) and is reconstructed here, where the real type is known, so
    every downstream consumer (`_branch_quality`, `_actual_cost`, gate evaluation, the final
    envelope) sees the identical object shape whether the step ran live or replayed."""
    if result.replayed:
        return NormalizedResponse.model_validate(result.payload)
    assert isinstance(result.payload, NormalizedResponse)
    return result.payload


def _keep_candidate(
    ctx: _WalkCtx, backend: str, node: str, category: str, resp: NormalizedResponse | None
) -> None:
    """Retain one completed attempt's envelope for compare (opt-in). No-op unless keep_candidates."""
    if ctx.keep_candidates and resp is not None:
        ctx.candidates.append(
            {
                "backend": backend,
                "node": node,
                "category": category,
                "response": resp.to_schema_dict(),
            }
        )


@dataclass
class StrategyResult:
    response: NormalizedResponse
    orchestration: dict[str, Any]


def _resolve_outer_budget_ms(max_duration_ms: int | None, deadline_ms: int | None) -> int:
    """The outer time budget (ms) for `run_strategy`'s whole walk: the file `limits:
    {max_duration_per_doc}` operator ceiling (`compiled.max_duration_ms`) wins if set, else the
    caller's own `deadline_ms` override, else `DEFAULT_DEADLINE_MS`. Nested `is not None` checks
    (mirroring credentials.py's resolution style and BL-138's own fix pattern), not an `or`-chain
    (BL-142): a deliberate zero-valued ceiling — "this strategy gets no time budget" — is a
    legitimate, schema-valid value (the duration schemas accept a zero magnitude) that must survive
    as an immediate deadline, not be silently replaced by the default the way `0 or X` always
    evaluates `X` regardless of whether the `0` was deliberate or merely absent."""
    if max_duration_ms is not None:
        return max_duration_ms
    if deadline_ms is not None:
        return deadline_ms
    return DEFAULT_DEADLINE_MS


def run_strategy(
    compiled: CompiledPlan,
    req: OpenReadingRequest,
    *,
    registry: Registry,
    clock: Clock,
    broker: EnvCredentialBroker | None = None,
    deadline_ms: int | None = None,
    decider_llm: DeciderPort | None = None,
    judge_llm: JudgePort | None = None,
    env: Mapping[str, str] | None = None,
    replay: list[dict[str, Any]] | None = None,
    keep_candidates: bool = False,
    run_id: str | None = None,
    executor: Executor | None = None,
) -> StrategyResult:
    """Execute a compiled strategy and return the response + orchestration block. Raises
    PlanExhaustedError when every eligible backend fails/skips with nothing retained (today's
    contract, with a richer trail).

    `decider_llm` is the optional LLM decision executor (14.2); absent, every declared decision
    point resolves to its engine default. `env` overrides the enablement environment (defaults to
    `os.environ`) — the second of the two enablement keys (decider.md §1).

    `run_id`/`executor` (Ledger T1, internal/design/ledger.md §5): `api.py`'s `run()`/`run_request()`
    mint a `run_id` and construct an armed `InlineExecutor` (real `JsonlJournal`/`LocalFsBlobStore`/
    `LocalFsKeyStore`) when `OPENREADING_LEDGER` is set, and pass both through. Every other caller
    — every existing test included — omits them and gets `_WalkCtx.__post_init__`'s L1-preserving
    default: a fresh `run_id` and an unarmed `InlineExecutor(journal=NullJournal(), blobs=None)`.

    `clock` is required, not defaulted (Ledger T2, internal/design/ledger.md §7.2): a caller-supplied
    real-wall-clock default here was itself a leakage source — every caller must now decide and
    state which clock a walk runs under, and `strategies/engine.py` itself never constructs one
    (pinned by `test_no_realclock_construction_or_import_in_engine_py`, T2)."""
    broker = broker or EnvCredentialBroker()
    trace = Trace(strategy=compiled.name, config_hash=compiled.config_hash)
    trace.dropped = list(compiled.dropped)

    # The decider verdict is computed ONCE per walk and binds every decision point identically
    # (decider.md §3.5). In engine mode (the default — no block, or no OPENREADING_LLM_DECIDER)
    # every decision point takes its deterministic engine default.
    decider_status = resolve_decider_status(
        decider=compiled.decider,
        req=req,
        registry=registry,
        effective_compliance=compiled.effective_compliance,
        router_config=compiled.router_config,
        env=os.environ if env is None else env,
        port=decider_llm,
    )

    ctx = _WalkCtx(
        req=req,
        registry=registry,
        broker=broker,
        clock=clock,
        trace=trace,
        trees=compiled.trees,
        eligible=list(compiled.eligible),
        facts=compute_facts(req, compiled.effective_compliance or None),
        deadline_ms=clock.now_ms()
        + _resolve_outer_budget_ms(compiled.max_duration_ms, deadline_ms),
        decider_status=decider_status,
        decider_llm=decider_llm,
        judge_llm=judge_llm,
        config_hash=compiled.config_hash,
        strategy_name=compiled.name,
        plain_sourced=compiled.plain_sourced,
        env=os.environ if env is None else env,
        effective_compliance=compiled.effective_compliance,
        router_config=compiled.router_config,
        mask_fields=list(compiled.decider.mask_fields or []) if compiled.decider else [],
        # `replay=[]` is a valid empty trace (replay mode ON → every point trace_missing); only
        # `replay=None` means "no replay". Distinguish by identity, not truthiness.
        replay=(
            {d["decision_id"]: d for d in replay if d.get("decision_id")}
            if replay is not None
            else None
        ),
        keep_candidates=keep_candidates,
        run_id=run_id or "",
        executor=executor,
    )

    outcome: Outcome = asyncio.run(_eval_node(compiled.root, "root", ctx))

    if outcome.kind in ("ok", "deficient"):
        resp = outcome.response
        assert resp is not None
        chosen = _chosen_backend(resp, trace)
        _summarize_trail(resp, trace, chosen)  # fallback_used / quality_escalated on the FINAL resp
        for code, msg in compiled.warnings:
            resp.add_warning(code, msg)
        baa_note = compiled.baa_tier_notes.get(chosen or "")
        if baa_note is not None:
            resp.add_warning(BAA_TIER_CONFIRMED_WARNING, baa_note, chosen)
        degraded = outcome.kind == "deficient"
        if degraded:
            resp.add_warning("quality_below_threshold", "all rungs gated; returning best result")
        orch = trace.orchestration(chosen_backend=chosen, outcome="degraded" if degraded else "ok")
        if ctx.keep_candidates and ctx.candidates:
            orch["candidates"] = ctx.candidates
        return StrategyResult(response=resp, orchestration=orch)

    raise PlanExhaustedError(
        f"strategy {compiled.name!r}: {outcome.error_class} — no usable result",
        trail=[a.as_dict() for a in trace.attempts],
    )


def _chosen_backend(resp: NormalizedResponse, trace: Trace) -> str | None:
    """The backend whose response this is — the last `succeeded`, else the best retained rung."""
    for a in reversed(trace.attempts):
        if a.category == "succeeded":
            return a.backend
    return resp.backend.id if resp.backend else None


def _summarize_trail(resp: NormalizedResponse, trace: Trace, chosen: str | None) -> None:
    """Add the human-readable trail warnings to the FINAL response (Law 3): each rung walked past
    becomes a `quality_escalated` or `fallback_used` warning naming from→to."""
    for a in trace.attempts:
        if a.backend == chosen and a.category == "succeeded":
            continue
        if a.category in ("quality_escalated", "review_escalated"):
            resp.add_warning(
                "quality_escalated", f"{a.backend} gated at {a.node} → {chosen}", a.backend
            )
        elif a.category.startswith("error(") or a.category.startswith("skipped("):
            resp.add_warning("fallback_used", f"{a.backend} {a.category} → {chosen}", a.backend)


# --------------------------------------------------------------------------- node dispatch


async def _eval_node(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    """`visited` is the set of `use:` names already resolved on THIS recursion path (not walk-wide
    — the same tree may legitimately be referenced from two different, non-overlapping paths in one
    walk, e.g. two top-level cascades sharing a sub-strategy). Only `_eval_reference` adds to it;
    every other composite type passes it through unchanged (T2 §4.3)."""
    if "steps" in node:
        return await _eval_cascade(node, path, ctx, visited)
    if "backend" in node:
        return await _eval_leaf(node, path, ctx)
    if "use" in node:
        return await _eval_reference(node, path, ctx, visited)
    if "route" in node:
        return await _eval_route(node, path, ctx, visited)
    if "decide" in node:
        return await _eval_decide(node, path, ctx, visited)
    if "parallel" in node:
        return await _eval_parallel(node, path, ctx, visited)
    raise StrategyConstructUnavailable(
        "unknown node type (arrives in a later milestone)",
        backend_code="strategy_construct_unavailable",
    )


async def _eval_route(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    """First-match dispatch over pre-parse facts. All rules are evaluated for the trace even
    after a match (shadowed-rule debugging, spec §2.5). Uncomputable fact → rule not match."""
    r = node["route"]
    chosen_idx: int | None = None
    rule_records: list[dict[str, Any]] = []
    for i, rule in enumerate(r["rules"]):
        res = evaluate_when(rule["when"], ctx.facts)
        rule_records.append(
            {
                "index": i,
                "matched": res.matched,
                "predicates": [p.as_dict() for p in res.predicates],
            }
        )
        if res.matched and chosen_idx is None:
            chosen_idx = i
    ctx.trace.decisions.append(
        {
            "point": "route",
            "node_path": path,
            "decider": "engine",
            "chosen": chosen_idx if chosen_idx is not None else "default",
            "rules": rule_records,
        }
    )
    target = r["rules"][chosen_idx]["use"] if chosen_idx is not None else r["default"]
    return await _eval_node(
        target, f"{path}.route[{chosen_idx if chosen_idx is not None else 'default'}]", ctx, visited
    )


async def _resolve_decision_point(
    ctx: _WalkCtx,
    *,
    point: str,
    node_path: str,
    label: str,
    candidates: list[str],
    engine_default: str,
    intent: str | None = None,
    signals: dict[str, Any] | None = None,
    typed_fields: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build one DecisionPoint, resolve it (replay → the logged choice; else engine default unless
    an enabled+eligible LLM executor overrides), meter any decider_call, append the one-shape
    decision record, and return `(chosen action, the decision record dict, by reference)`.
    Compliance never enters the DecisionPoint (decider.md §3.3 rail 4); masked typed_fields never
    enter it either (§5).

    Returns the record by reference (Ledger T2 §7.3/§4.2) rather than making a caller re-find it
    via `ctx.trace.decisions[-1]` — defensive hardening, not a fix for a proven live bug: asyncio's
    own cooperative scheduling means a coroutine's synchronous stretch between two `await` points
    can never be interleaved by a sibling task, so today's call sites (each reading the record
    immediately after this call returns, no intervening `await`) are already safe under `[-1]`
    indexing (Phase C round-1, alex-phasec F1, reproduced with a 12-branch/500-trial repro finding
    zero violations). By-reference return removes the DEPENDENCY on that invariant continuing to
    hold as the engine evolves, rather than closing an exploitable gap that exists today."""
    sig = {k: v for k, v in (signals or {}).items() if k not in ctx.mask_fields}
    if typed_fields:  # masked ONCE here → both the port input (dp) and the log see the masked set
        masked = mask_typed_fields(_fields_view(typed_fields), ctx.mask_fields)
        if masked:
            sig["typed_fields"] = masked
    dp = DecisionPoint(
        decision_id=decision_id(ctx.config_hash, node_path, ctx.trace.next_dp_seq()),
        point=point,
        node_path=node_path,
        label=label,
        candidates=candidates,
        engine_default=engine_default,
        intent=intent,
        signals=sig,
        budget_remaining=_budget_snapshot(ctx),
    )
    if ctx.replay is not None:
        chosen, decider_label, downgraded, extra = _replay_decision(dp, ctx)
    else:
        chosen, decider_label, downgraded, extra = await _decide(dp, ctx)
    record = (
        decision_record(
            dp,
            chosen,
            decider_label,
            downgraded,
            config_hash=ctx.config_hash,
            strategy=ctx.strategy_name,
        )
        | extra
    )
    ctx.trace.decisions.append(record)
    return chosen, record


async def _decide(dp: DecisionPoint, ctx: _WalkCtx) -> tuple[str, str, str | None, dict[str, Any]]:
    """Resolve `dp` to (chosen, decider_label, downgraded, llm-only-fields). Engine mode → the
    engine default. LLM mode → the strict-tool call via the port, recorded as a `decider_call`
    attempt (its backend-reported cost, if any), then engine re-validation (rail
    belt-and-suspenders).

    The port call itself runs via `asyncio.to_thread` (Ledger T2 §4.4, internal/design/ledger.md §7):
    `DeciderPort.decide` is a plain synchronous Protocol method, and calling it directly on the
    event-loop thread let a slow or hanging implementation stall every other coroutine cooperatively
    scheduled on the same loop (parallel branches, the coordinated-clock advance) for however long
    it took — a timing-dependent nondeterminism source. Off-thread dispatch removes the starvation;
    a deadline/timeout on the call itself is separate work, deferred to whichever tranche gives this
    port a `ctx.exec`-routed, journaled shape (T3+)."""
    status = ctx.decider_status
    if status.mode == "engine":
        return dp.engine_default, "engine", status.reason, {}
    assert status.backend is not None and ctx.decider_llm is not None

    try:
        verdict = await asyncio.to_thread(ctx.decider_llm.decide, dp)
    except Exception:
        return dp.engine_default, "engine", "malformed", {}

    # record the call as a decider_call attempt with its honest backend-reported cost, if any.
    cost = verdict.cost_usd or 0.0
    ctx.trace.record(Attempt(status.backend, "decider_call", dp.node_path, cost_usd=cost or None))

    action, reason = revalidate_action(verdict.action, dp.candidates)
    if reason is not None:
        return dp.engine_default, "engine", reason, {}
    assert action is not None
    extra: dict[str, Any] = {}
    if verdict.model_id:
        extra["model_id"] = verdict.model_id
    if verdict.rationale:
        extra["rationale"] = verdict.rationale
    return action, "llm", None, extra


def _replay_decision(
    dp: DecisionPoint, ctx: _WalkCtx
) -> tuple[str, str, str | None, dict[str, Any]]:
    """Replay mode (decider.md §5): take the logged choice for this decision_id. A decision point
    ABSENT from the trace (or whose logged choice is no longer a valid candidate) takes the engine
    default, traced `decider_downgraded: trace_missing` (§3.4). No live executor is consulted — the
    seam shared with a live run is the decision_id identity, which replays exactly (D-v3-18)."""
    assert ctx.replay is not None
    rec = ctx.replay.get(dp.decision_id)
    if rec is not None and rec.get("chosen") in dp.candidates:
        return rec["chosen"], "trace", None, {}
    return dp.engine_default, "engine", "trace_missing", {}


def _budget_snapshot(ctx: _WalkCtx) -> dict[str, Any]:
    """The remaining time budget the decider is allowed to see (decider.md §6) — no compliance
    surface, no cost pool (removed)."""
    snap: dict[str, Any] = {}
    if ctx.deadline_ms is not None:
        snap["duration_ms"] = max(0, round(ctx.deadline_ms - ctx.clock.now_ms()))
    return snap


async def _eval_decide(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    """Build the candidate list from `among:` (post-pruning); engine mode dispatches `otherwise:`,
    an enabled+eligible LLM decider picks one candidate (decider.md §2.2, execution.md §4). Any
    decider failure resolves to `otherwise:` with a `decider_downgraded` trace."""
    d = node["decide"]
    among = d.get("among") or []
    # candidate identity: each among entry's target key, so the closed action space is nameable and
    # the chosen action maps back to a child node. `otherwise` is always a valid fallthrough.
    # Uniqueness of these names is a normalize-time invariant (BL-35), not a hope held here.
    labelled = [(candidate_label(m), m) for m in among]
    candidates = [name for name, _ in labelled] + ["otherwise"]
    chosen, record = await _resolve_decision_point(
        ctx,
        point="decide",
        node_path=path,
        label=node.get("label", path),
        candidates=candidates,
        engine_default="otherwise",
        intent=node.get("intent"),
    )
    if chosen == "otherwise":
        # BL-54: prune.py marks a decide node whose OWN configured otherwise: was pruned and
        # substituted with the first-listed among: survivor (integration.md §2c). When dispatch
        # actually resolves to "otherwise", surface that on the decision record `_resolve_decision_
        # point` just returned by reference (never clobbering a real decider-level downgrade already
        # there) — so a caller inspecting the trace can see the configured default didn't fire,
        # instead of reading identically to the case where nothing unusual happened at all. Uses
        # `record` directly, not `ctx.trace.decisions[-1]` (Ledger T2 §7.3/§4.2, defensive — see
        # `_resolve_decision_point`'s own docstring for why `[-1]` is not currently exploitable
        # here, just a dependency this avoids introducing).
        if d.get("otherwise_pruned") and record["downgraded"] is None:
            record["downgraded"] = "otherwise_pruned"
        return await _eval_node(d["otherwise"], f"{path}.decide.otherwise", ctx, visited)
    target = next(m for name, m in labelled if name == chosen)
    return await _eval_node(target, f"{path}.decide[{chosen}]", ctx, visited)


# --------------------------------------------------------------------------- parallel (M3)


@dataclass
class _BranchOutcome:
    index: int
    status: str  # ok | error | skip | drained | cancelled | deadline_pruned | composite_ok/_err
    backend: str = ""
    response: NormalizedResponse | None = None
    error_class: str | None = None
    cost: float | None = None
    quality: float = 1.0
    # Ledger T3 (§4.0/§4.1): the leaf's own ExecResult.journal_seq/replayed, additive fields that
    # default to values reproducing today's behavior for any path that doesn't populate them. A
    # composite branch (never calls ctx.exec() at its own level — it recurses through _eval_node)
    # keeps journal_seq=None structurally, an explicit, disclosed exclusion from the journal_seq
    # tie-break below (round-3 alex F11): a composite-vs-leaf tie stays index-order, unchanged.
    journal_seq: int | None = None
    replayed: bool = False


async def _eval_parallel(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    """Fan-out: `pick: fastest` races to the first success (ties by branch index); `pick: best`
    waits for `require:` successes then keeps the highest composite score. `shadow` branches run
    and are recorded but never win and are always drained. `start_after` delays a branch (a hedge,
    cancelled if the node resolves first). `on_win: cancel` cancels non-shadow losers; `drain` lets
    them finish (billed). Sibling branches have distinct backends (validate) → no shared instance
    (T4)."""
    branches = node["parallel"]
    pick = node["pick"]
    on_win = node.get("on_win", "cancel")
    require = node.get("require", "all")
    deadline = _enter_budget(node, ctx)
    inflight = [0]  # in-flight to_thread count (drives the coordinated-clock advance)
    shadows = {i for i, b in enumerate(branches) if isinstance(b, dict) and b.get("shadow")}
    labels = {
        i: (b.get("backend", "?") if isinstance(b, dict) else "?") for i, b in enumerate(branches)
    }

    tasks: dict[int, asyncio.Task] = {}
    jobs: dict[int, Job] = {}  # branch index → the backend job it submitted (for on_win: cancel)
    fake = ctx.clock if isinstance(ctx.clock, FakeClock) else None
    if fake is not None:
        fake.enable_coordination()
    try:
        for i, b in enumerate(branches):
            tasks[i] = asyncio.create_task(
                _run_branch(i, b, path, ctx, deadline, inflight, jobs, visited)
            )

        if pick == "fastest":
            winner, results = await _drive_race(tasks, fake, inflight, shadows)
        else:  # best or merge — wait for `require` non-shadow branches, then select/compose
            results = await _drive_best(tasks, fake, inflight, require, shadows)
            # merge composes from ALL voters (the base is the best-scoring branch, §2.4); best
            # selects one whole response, judge-aware.
            winner = (
                _pick_best(results, ctx, shadows)
                if pick == "merge"
                else await _select_best(node, results, path, ctx, shadows)
            )
        # cancel non-shadow losers (on_win: cancel) — but NOT under merge, whose losers all vote;
        # shadows always drain; then wait out the rest.
        if on_win == "cancel" and pick != "merge":
            loser_jobs: list[Job] = []
            for i, t in tasks.items():
                if i in shadows or i == winner:
                    continue
                if not t.done():
                    _cancel_webhook_loser(ctx, branches[i], labels[i], path)
                    t.cancel()
                # A loser can hold a LIVE backend job whether or not its task is still running: one
                # interrupted mid-flight, and one whose poll faulted after the backend accepted the
                # job, both leave it running (and billing) at the vendor. Cancelling the task only
                # stops this process from waiting — it never reaches the backend.
                job = jobs.get(i)
                if job is not None:
                    loser_jobs.append(job)
            if loser_jobs:
                # BL-164: adapter.cancel() now issues a real, synchronous vendor HTTP call for
                # chunkr/nuextract/pulse/reducto (previously an instant in-memory no-op for every
                # adapter). A plain call here would block THIS event loop for the round trip,
                # serially per loser — so it's dispatched to a worker pool, concurrently across
                # every loser rather than one at a time (same shape as submit/poll's own
                # asyncio.to_thread elsewhere in this file — but a SEPARATE pool, not that one; see
                # `_CANCEL_DISPATCH_EXECUTOR`'s own comment for why that distinction is load-
                # bearing, not stylistic).
                #
                # BL-164 review round 2 (trent, High): concurrency alone doesn't cap the TOTAL
                # wait — a single slow vendor cancel could still extend the response past the
                # node's own deadline, breaking the exact Law 6 promise `_drain` already keeps for
                # a drained loser ("the response never blocks past the node deadline"). Bounded on
                # the same `deadline`: a cancel attempt that hasn't finished by then is abandoned,
                # not retried — left to finish (or not) on its own dedicated executor, unawaited;
                # its eventual result/exception is retrieved via a callback so an abandoned
                # failure doesn't log an "exception was never retrieved" warning once it completes.
                remaining_s = max(0.0, (deadline - ctx.clock.now_ms()) / 1000.0)
                loop = asyncio.get_running_loop()
                cancel_futures = [
                    loop.run_in_executor(_CANCEL_DISPATCH_EXECUTOR, _cancel_loser_job, ctx, job)
                    for job in loser_jobs
                ]
                _done, pending = await asyncio.wait(cancel_futures, timeout=remaining_s)
                for f in pending:
                    f.add_done_callback(_swallow_abandoned_cancel_result)
        await _drain(tasks, fake, inflight, results, deadline=deadline, ctx=ctx, branches=branches)
    finally:
        if fake is not None:
            fake.disable_coordination()

    return _resolve_parallel(winner, results, pick, path, ctx, labels, shadows)


async def _run_branch(
    i: int,
    branch: dict[str, Any],
    path: str,
    ctx: _WalkCtx,
    deadline: float,
    inflight: list[int],
    jobs: dict[int, Job],
    visited: frozenset[str] = frozenset(),
) -> _BranchOutcome:
    spath = f"{path}.parallel[{i}]"
    if "backend" not in branch:  # a composite branch (cascade/route/decide/use) — recurse
        outcome = await _eval_node(branch, spath, ctx, visited)
        status = "composite_ok" if outcome.kind in ("ok", "deficient") else "composite_err"
        return _BranchOutcome(
            i,
            status,
            response=outcome.response,
            error_class=outcome.error_class,
            quality=outcome.quality,
        )

    backend = _resolve_backend(branch["backend"], ctx)
    if backend is None:
        return _BranchOutcome(i, "error", error_class="exhausted")
    adapter = ctx.registry.get(backend)
    if adapter is None:
        ctx.attempted.add(backend)
        return _BranchOutcome(i, "error", backend, error_class="provider_error")
    desc = adapter.descriptor

    # start_after (hedge): don't launch if the delayed start lands past the node deadline. The
    # sleep is on the shared coordinated clock, so a winner that resolves first cancels a
    # not-yet-launched hedge (it is still parked here) — spec §2.3 / execution.md §3.1.
    start_after = _duration_ms(branch.get("start_after")) or 0
    if start_after:
        if ctx.clock.now_ms() + start_after > deadline:
            return _BranchOutcome(i, "deadline_pruned", backend)
        await ctx.clock.sleep(start_after / 1000.0)

    ctx.attempted.add(backend)  # walk-wide (T12) — only once the branch actually launches
    rc = build_run_context(ctx.req, desc, broker=ctx.broker)
    # Ledger T3 §4.3b: `missing_required` still runs here (this function alone holds ctx.broker),
    # but no longer returns early on a non-empty result — every branch now routes through
    # ctx.exec so a missing-credentials skip becomes a real, journaled outcome (F2), not one that
    # stays orchestration-only with nothing for a later resume to replay.
    missing = missing_required(desc, rc)

    if not missing:
        latency = getattr(adapter, "test_latency_ms", 0)
        if latency:
            await ctx.clock.sleep(latency / 1000.0)  # virtual latency (coordinated clock in tests)

    # The worker thread publishes the job here the moment the backend accepts it (one dict store,
    # so the loop thread can read it while the branch is still being driven) — `on_win: cancel`
    # cannot cancel a job it cannot see.
    def record(job: Job) -> None:
        jobs[i] = job

    inflight[0] += 1
    try:
        # the branch's internal driver uses ctx.clock, not a fresh RealClock (Ledger T2 §7.2 —
        # RealClock is banned from the walk; under a test's FakeClock this makes the leaf-dispatch
        # driver loop advance on the SAME virtual clock as the rest of the walk, rather than
        # silently sneaking in real wall-clock behavior during an otherwise-deterministic test).
        # INLINE dispatch is instant either way; the race itself is timed by the branch latency on
        # the shared coordinated clock, not by run_to_completion. Routed through ctx.executor
        # (Ledger T1 engine-wiring cutover, plan §6): InlineExecutor wraps this exact call as one
        # 'submit' step and journals attempted/terminal around it; `record` (on_win: cancel's own
        # job-visibility hook, BL-164) still fires the moment the adapter accepts the job, since it
        # is captured by the `run` closure, not routed through the journal-facing StepRequest.
        result = await _get_executor(ctx).exec(
            _step_request(ctx, spath, backend, desc, ctx.req, missing_credentials=missing),
            run=lambda: _execute_leaf_sync(
                adapter, ctx.req, ctx.broker, ctx.clock, DEFAULT_DEADLINE_MS, on_submit=record
            ),
        )
    except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused) as e:
        return _BranchOutcome(i, "error", backend, error_class=classify_error(e))
    except Exception as e:
        # BL-99: adapter.normalize() is ordinary adapter code, not one of the four taxonomy types
        # above — a plain crash out of it used to propagate straight out of this branch's
        # asyncio.Task, surfacing (via _collect's task.result()) as an uncaught exception that blew
        # up the WHOLE parallel node, not just this one losing branch. classify_error(e) already
        # falls through to "provider_error" for anything outside the taxonomy. auth_hinted (widened
        # in readiness.py) has already redacted e's message by the time it reaches here.
        return _BranchOutcome(i, "error", backend, error_class=classify_error(e))
    finally:
        inflight[0] -= 1

    # Ledger T3 §4.0: exec() now hands back an ExecResult, not the bare payload — "ok" carries the
    # real response (live or reconstructed from a replay's own blob-resolved JSON); anything else
    # ("skipped"/"cancelled" — "failed" always raises above instead of reaching here) is a real,
    # journaled non-dispatch outcome, not a crash on `_branch_quality(None, ...)`/`_actual_cost
    # (None)` the way a bare `None` return used to produce (round-2 F6/F7).
    if result.status != "ok":
        status = (
            "skip" if result.status == "skipped" else result.status
        )  # "cancelled" passes through
        return _BranchOutcome(
            i, status, backend, journal_seq=result.journal_seq, replayed=result.replayed
        )
    resp = _response_payload(result)
    quality = _branch_quality(resp, ctx)
    return _BranchOutcome(
        i,
        "ok",
        backend,
        response=resp,
        cost=_actual_cost(resp),
        quality=quality,
        journal_seq=result.journal_seq,
        replayed=result.replayed,
    )


def _collect(tasks: dict[int, asyncio.Task], results: dict[int, _BranchOutcome]) -> None:
    for i, t in tasks.items():
        if t.done() and i not in results and not t.cancelled():
            results[i] = t.result()


async def _drive_race(
    tasks: dict[int, asyncio.Task],
    fake: FakeClock | None,
    inflight: list[int],
    shadows: set[int],
) -> tuple[int | None, dict[int, _BranchOutcome]]:
    """Advance until the first NON-SHADOW branch returns a success (lowest index among
    simultaneous completions). Cancellation/draining is handled by the caller."""
    results: dict[int, _BranchOutcome] = {}
    while any(not t.done() for t in tasks.values()):
        await asyncio.sleep(0)
        _collect(tasks, results)
        # Judge only when NO branch is mid-execution (`inflight == 0`): all branches that woke at
        # the current virtual instant have finished their (virtually-instant) work, so the
        # lowest-index success is a determinate tie-break — never a real thread-completion race
        # (D-v3-16). A still-sleeping higher-latency branch has a strictly greater virtual finish
        # time and cannot beat a success already in hand.
        if inflight[0] == 0:
            # Ledger T3 (§4.1/§7.4): judge by journal_seq — the replay-stable, journaled
            # completion order — not by branch index. On a fresh run under RealClock this is the
            # TRUE completion order (a JsonlJournal append happens on the event loop, serialized
            # across concurrent branches, the instant each one's dispatch actually finishes); on a
            # resumed process every branch's exec() call returns near-instantly (a replay lookup,
            # never a real to_thread wait), so index/scheduling order alone would pick a different
            # "winner" than the ORIGINAL run's real timing did. A composite branch's journal_seq is
            # always None (§4.0 F11's disclosed exclusion — it never calls ctx.exec() at its own
            # level) and sorts after every real value, falling back to index order among ties —
            # never raising on a None/int comparison, and never changing today's tie-break for a
            # composite-vs-leaf or composite-vs-composite tie.
            succ = sorted(
                (
                    i
                    for i in results
                    if i not in shadows and results[i].status in ("ok", "composite_ok")
                ),
                key=lambda i: (results[i].journal_seq is None, results[i].journal_seq or 0, i),
            )
            if succ:
                return succ[0], results
            _advance_if_parked(tasks, fake, inflight)
    _collect(tasks, results)
    return None, results


async def _drive_best(
    tasks: dict[int, asyncio.Task],
    fake: FakeClock | None,
    inflight: list[int],
    require: Any,
    shadows: set[int],
) -> dict[int, _BranchOutcome]:
    """Advance until `require` non-shadow branches succeed (or all non-shadow branches resolve),
    then return what has completed. Shadows keep draining (the caller waits them out)."""
    non_shadow = [i for i in tasks if i not in shadows]
    target = len(non_shadow) if require == "all" else min(int(require), len(non_shadow))
    results: dict[int, _BranchOutcome] = {}
    while any(not t.done() for t in tasks.values()):
        await asyncio.sleep(0)
        _collect(tasks, results)
        # Evaluate the `require` threshold only at a fully-drained virtual instant (`inflight == 0`)
        # so same-latency completions are collected together and the stop point is determinate
        # (D-v3-16), not decided by thread-completion order.
        if inflight[0] == 0:
            # Ledger T3 (§4.1, F5): admit by journal_seq, the same replay-stable key _drive_race
            # uses — which non-shadow branches count toward a PARTIAL `require` must not depend on
            # real thread-completion order (live) or scheduling order (replay, where every branch
            # resolves near-instantly). A composite branch's journal_seq is always None (§4.0 F11)
            # and sorts last, falling back to index order among itself and any tie.
            succ = sorted(
                (
                    i
                    for i in results
                    if i not in shadows and results[i].status in ("ok", "composite_ok")
                ),
                key=lambda i: (results[i].journal_seq is None, results[i].journal_seq or 0, i),
            )
            resolved = [i for i in results if i not in shadows]
            if len(succ) >= target or len(resolved) == len(non_shadow):
                return results
            _advance_if_parked(tasks, fake, inflight)
    _collect(tasks, results)
    return results


async def _drain(
    tasks: dict[int, asyncio.Task],
    fake: FakeClock | None,
    inflight: list[int],
    results: dict[int, _BranchOutcome],
    *,
    deadline: float,
    ctx: _WalkCtx,
    branches: list[Any],
) -> None:
    """Wait out still-running branches (shadows always drain; non-shadow drain under on_win: drain),
    BUT never past the node deadline (execution.md §3.3 Law 6): a branch whose next wake lands after
    the deadline is NOT awaited — it is recorded as `drained` (with no measured cost) so the
    response returns on time, rather than blocking on a drain that would outlive it (D-v3-14)."""
    while any(not t.done() for t in tasks.values()):
        await asyncio.sleep(0)
        if fake is None or inflight[0] > 0:
            continue
        pending = [i for i, t in tasks.items() if not t.done()]
        if not (pending and fake.pending_sleepers() >= len(pending)):
            continue
        nxt = fake.next_wake_ms()
        if nxt is not None and nxt > deadline:  # Law 6: don't block past the node deadline
            for i in pending:
                b = branches[i]
                backend = str(b.get("backend", "?") if isinstance(b, dict) else b)
                results.setdefault(i, _BranchOutcome(i, "drained", backend))
                tasks[i].cancel()
            break
        fake.advance_to_next()
    for i, t in tasks.items():
        if i not in results:
            results[i] = (
                t.result() if (t.done() and not t.cancelled()) else _BranchOutcome(i, "cancelled")
            )


def _advance_if_parked(
    tasks: dict[int, asyncio.Task], fake: FakeClock | None, inflight: list[int]
) -> None:
    """When every pending branch is parked on `clock.sleep` (coordinated FakeClock) and no
    to_thread is in flight, advance virtual time to the next wake. (RealClock: nothing to
    advance — the tasks make progress on their own threads.)"""
    if fake is None or inflight[0] > 0:
        return
    pending = [t for t in tasks.values() if not t.done()]
    if pending and fake.pending_sleepers() >= len(pending):
        fake.advance_to_next()


def _cancel_webhook_loser(ctx: _WalkCtx, branch: Any, label: str, path: str) -> None:
    """T8 — a cancelled loser whose backend delivers via WEBHOOK can still fire its callback later;
    mark it so a late delivery is acknowledged and DROPPED (never applied to the resolved result).
    The marker is a trace note keyed on the branch backend; INLINE/POLL branches have no late
    callback, so nothing is marked for them. (`_cancel_loser_job` asks the backend itself to stop —
    the drop set is the belt to that suspenders, since a callback already dispatched arrives
    regardless of when the cancel lands.)"""
    backend = branch.get("backend") if isinstance(branch, dict) else branch
    if not isinstance(backend, str):
        return
    adapter = ctx.registry.get(backend)
    if adapter is None or WaitMode.WEBHOOK not in adapter.descriptor.wait_modes:
        return  # no webhook → no late delivery to drop
    ctx.trace.webhook_dropped.append({"backend": backend, "node": f"{path}.parallel"})


# BL-164 review round 2 (trent, High): dispatching a loser's cancel via `asyncio.to_thread` (the
# event loop's own DEFAULT executor) means an abandoned-past-deadline future is still tracked by
# `asyncio.run()`'s own shutdown sequence (`shutdown_default_executor()`), which WAITS for every
# outstanding default-executor future before returning — even one this function has already
# stopped awaiting. `run_strategy` wraps its whole walk in exactly one `asyncio.run()` per call
# (D-v3-10, this module's own docstring) — there is no persistent, cross-request event loop for a
# genuinely detached background future to hide in. Confirmed empirically: an abandoned
# `to_thread` future still added its full duration to `asyncio.run()`'s own total wall time,
# defeating the deadline bound entirely. A SEPARATE executor, never registered as the loop's
# default, is outside that shutdown sequence — its futures can be genuinely abandoned; confirmed
# empirically this lets `asyncio.run()` return promptly while the future keeps running unseen.
# Not shut down explicitly: `ThreadPoolExecutor` registers its own interpreter-exit hook, the same
# way the default executor's own threads are joined at real process exit either way.
_CANCEL_DISPATCH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ledger-cancel")


def _swallow_abandoned_cancel_result(future: asyncio.Future) -> None:
    """Intended to retrieve the result/exception of a loser's `_cancel_loser_job` dispatch that
    was abandoned past the node deadline (still running on `_CANCEL_DISPATCH_EXECUTOR` — Python
    cannot forcibly stop a running thread) once it eventually finishes on its own, unawaited — so
    Python doesn't log an "exception was never retrieved" warning when it does.

    BL-164 review round 3 (trent): confirmed by direct execution that this callback is NEVER
    actually invoked in `run_strategy`'s own architecture — CPython's `asyncio.futures`-internal
    future-chaining silently discards the completion once the per-call event loop this future was
    scheduled on has already closed (which it always has, by the time an abandoned future finishes
    — `run_strategy` wraps its whole walk in exactly one short-lived `asyncio.run()` per call,
    D-v3-10). Kept anyway as defense-in-depth documentation, not dead weight: `_cancel_loser_job`
    itself never raises (it already suppresses every taxonomy error), so there is nothing this
    callback would ever need to act on even if it did fire; and if this engine's own event-loop
    lifecycle ever changes (e.g. a persistent, cross-request loop, which is NOT how it works
    today), this becomes reachable and the `.cancelled()` guard below — skipping a future the
    loop's own shutdown cancelled, since `.exception()` would re-raise `CancelledError` for one —
    would matter again."""
    if not future.cancelled():
        future.exception()


def _cancel_loser_job(ctx: _WalkCtx, job: Job | None) -> None:
    """`on_win: cancel` — ask the backend to stop working on a loser's job (execution.md §3.3): a
    POLL loser stops being polled always; whether a job the vendor already accepted also stops
    billing depends on that backend's own `cancel_supported` descriptor field (BL-164) — `true`
    only for a backend with a real, documented vendor stop call, and no cited claim here or in
    execution.md §3.3 about whether stopping the job also stops billing for any specific backend,
    absent a source saying so. Nothing to do for a branch that never submitted, or whose job
    already reached a terminal state.

    Best-effort by contract (`BackendAdapter.cancel`): a backend that refuses the cancel must never
    fail a node that already has its winner, so an adapter error here is swallowed. Dispatched at
    its one call site (`_eval_parallel`) via `_CANCEL_DISPATCH_EXECUTOR` — a dedicated executor,
    deliberately NOT `asyncio.to_thread`'s own default one (see that constant's own comment for
    why the distinction is load-bearing) — since `adapter.cancel()` now issues a real, synchronous
    vendor HTTP call for four adapters (previously an instant in-memory no-op for all of them), so
    this function itself stays a plain sync function; the caller is responsible for not blocking
    the event loop with it."""
    if job is None or job.is_terminal():
        return
    adapter = ctx.registry.get(job.backend_id)
    if adapter is not None:
        with contextlib.suppress(TerminalError, RetryableError, UnsupportedFeatureError):
            # Ledger T4a: cancel() now takes ctx: RunContext (matching submit's own shape) so an
            # overriding adapter can build a client from it rather than relying on one bound at
            # submit() time surviving on this instance — build_run_context is the one factory
            # every other call site in this file already uses.
            rc = build_run_context(ctx.req, adapter.descriptor, broker=ctx.broker)
            adapter.cancel(job, rc)


def _pick_best(results: dict[int, _BranchOutcome], ctx: _WalkCtx, shadows: set[int]) -> int | None:
    """Highest composite quality among NON-SHADOW successes; ties → cheaper backend, then index."""
    succ = [
        r for r in results.values() if r.index not in shadows and r.status in ("ok", "composite_ok")
    ]
    if not succ:
        return None
    best = max(succ, key=lambda r: (r.quality, -_cost_midpoint(r.backend, ctx), -r.index))
    return best.index


def _branch_quality(resp: NormalizedResponse, ctx: _WalkCtx) -> float:
    """The default-bundle Tier-1 composite score (execution.md §1): fraction of applicable
    default-bundle predicates the response passed. Used to compare `pick: best` candidates."""
    snap = probe(resp, doc_bytes=doc_bytes(ctx.req), mime_type=ctx.req.document.mime_type)
    gr = evaluate_gate(dict(DEFAULT_BUNDLE), snap)
    applicable = [p for p in gr.predicates if not p.unavailable]
    if not applicable:
        return 1.0
    return sum(1 for p in applicable if not p.fired) / len(applicable)


def _cost_midpoint(backend: str, ctx: _WalkCtx) -> float:
    adapter = ctx.registry.get(backend)
    if adapter is None:
        return 0.0
    c = adapter.descriptor.cost
    lo, hi = c.usd_per_page_equiv_low, c.usd_per_page_equiv_high
    vals = [v for v in (lo, hi) if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


# --------------------------------------------------------------------------- judge (decider.md §4)


async def _select_best(
    node: dict[str, Any],
    results: dict[int, _BranchOutcome],
    path: str,
    ctx: _WalkCtx,
    shadows: set[int],
) -> int | None:
    """Pick the winner for a `pick: best` node. With a `judge:` block AND an enabled+eligible LLM
    judge, the winner is chosen by pairwise LLM-as-judge (decider.md §4); otherwise the engine's
    deterministic composite score, with the downgrade traced. Both paths record ONE `judge`
    decision. The judge selects a real candidate — it never synthesizes (never-fabricate)."""
    judge_cfg = node.get("judge")
    succ = [
        results[i]
        for i in sorted(results)
        if i not in shadows and results[i].status in ("ok", "composite_ok")
    ]
    if judge_cfg is None or len(succ) <= 1:
        return _pick_best(results, ctx, shadows)  # nothing to judge → engine comparator, no record

    dp_id = decision_id(ctx.config_hash, f"{path}.judge", ctx.trace.next_dp_seq())
    eligible = [r.backend or "?" for r in succ]

    if ctx.replay is not None:  # replay (decider.md §5): take the logged winner for this judge
        rec = ctx.replay.get(dp_id)
        if rec is not None and rec.get("chosen") in eligible:
            chosen = rec["chosen"]
            _record_judge(ctx, dp_id, path, judge_cfg, eligible, chosen, "trace", None)
            return next(r.index for r in succ if (r.backend or "?") == chosen)
        winner = _pick_best(results, ctx, shadows)
        _record_judge(
            ctx,
            dp_id,
            path,
            judge_cfg,
            eligible,
            _label_of(winner, results),
            "engine",
            "trace_missing",
        )
        return winner

    status = resolve_judge_status(
        judge_backend=judge_cfg["backend"],
        req=ctx.req,
        registry=ctx.registry,
        effective_compliance=ctx.effective_compliance,
        router_config=ctx.router_config,
        env=ctx.env,
        port=ctx.judge_llm,
    )
    downgraded = status.reason
    if status.mode == "engine":
        winner = _pick_best(results, ctx, shadows)
        _record_judge(
            ctx, dp_id, path, judge_cfg, eligible, _label_of(winner, results), "engine", downgraded
        )
        return winner

    assert ctx.judge_llm is not None and status.backend is not None
    winner_outcome = await _pairwise_judge(succ, judge_cfg, status.backend, path, ctx)
    _record_judge(ctx, dp_id, path, judge_cfg, eligible, winner_outcome.backend or "?", "llm", None)
    return winner_outcome.index


async def _pairwise_judge(
    succ: list[_BranchOutcome], judge_cfg: dict[str, Any], backend: str, path: str, ctx: _WalkCtx
) -> _BranchOutcome:
    """Single-elimination against the current best in listed order — n−1 pairs (decider.md §4).
    Each pair is judged in BOTH orderings; an inconsistent verdict is a tie broken deterministically
    (identically to the engine comparator: cheaper backend, then first-listed)."""
    excerpt_chars = int(judge_cfg.get("excerpt_chars", 4000))
    intent = judge_cfg.get("intent")
    best = succ[0]
    for challenger in succ[1:]:
        best = await _judge_pair(best, challenger, intent, excerpt_chars, backend, path, ctx)
    return best


async def _judge_pair(
    a: _BranchOutcome,
    b: _BranchOutcome,
    intent: str | None,
    excerpt_chars: int,
    backend: str,
    path: str,
    ctx: _WalkCtx,
) -> _BranchOutcome:
    """`JudgePort.compare` runs via `asyncio.to_thread` (Ledger T2 §4.4), for the identical reason
    `_decide` dispatches `DeciderPort.decide` off-thread: a plain synchronous port call on the
    event-loop thread can starve sibling coroutines for however long the implementation takes."""
    assert ctx.judge_llm is not None
    ca = _judge_candidate("A", a, excerpt_chars, ctx)
    cb = _judge_candidate("B", b, excerpt_chars, ctx)
    # both orderings — position-bias guard (decider.md §4); sources are hidden behind the labels.
    v1 = await asyncio.to_thread(ctx.judge_llm.compare, ca, cb, intent)
    _meter_judge_call(ctx, backend, path, v1.cost_usd)
    v2 = await asyncio.to_thread(
        ctx.judge_llm.compare,
        _judge_candidate("A", b, excerpt_chars, ctx),
        _judge_candidate("B", a, excerpt_chars, ctx),
        intent,
    )
    _meter_judge_call(ctx, backend, path, v2.cost_usd)
    pick1 = a if v1.winner == "A" else b  # ordering 1: a=A, b=B
    pick2 = b if v2.winner == "A" else a  # ordering 2: b=A, a=B
    if pick1.index == pick2.index:
        return pick1  # consistent across both orderings
    return _tie_break_pair(a, b, ctx)  # inconsistent → tie


def _judge_candidate(
    label: str, r: _BranchOutcome, excerpt_chars: int, ctx: _WalkCtx
) -> JudgeCandidate:
    """A candidate as the judge sees it: a positional label, a capped excerpt, MASKED typed_fields —
    never the backend id (sources hidden, decider.md §4) and never a masked field's value (§5)."""
    text = r.response.document.text if r.response and r.response.document else ""
    fields = (r.response.typed_fields if r.response else None) or {}
    return JudgeCandidate(
        label=label,
        excerpt=(text or "")[:excerpt_chars],
        typed_fields=mask_typed_fields(_fields_view(fields), ctx.mask_fields),
    )


def _fields_view(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-safe plain-dict view of typed_fields (`{name: {value, confidence}}`) — TypedField
    pydantic objects would not survive `json.dumps` in the trace, and a raw object could smuggle a
    value past a naive string scan."""
    out: dict[str, Any] = {}
    for k, v in (fields or {}).items():
        if hasattr(v, "value"):
            out[k] = {"value": v.value, "confidence": getattr(v, "confidence", None)}
        else:
            out[k] = v
    return out


def _tie_break_pair(a: _BranchOutcome, b: _BranchOutcome, ctx: _WalkCtx) -> _BranchOutcome:
    """Deterministic tie-break identical to the engine comparator: cheaper backend, then the
    first-listed (lower branch index)."""
    ka = (_cost_midpoint(a.backend or "", ctx), a.index)
    kb = (_cost_midpoint(b.backend or "", ctx), b.index)
    return a if ka <= kb else b


def _meter_judge_call(ctx: _WalkCtx, backend: str, path: str, cost: float) -> None:
    """Record one pairwise call as a `judge_call` attempt with its honest backend-reported cost, if
    any (decider.md §4, execution.md §7)."""
    ctx.trace.record(Attempt(backend, "judge_call", f"{path}.judge", cost_usd=cost or None))


def _label_of(index: int | None, results: dict[int, _BranchOutcome]) -> str:
    return results[index].backend or "?" if index is not None and index in results else "?"


def _record_judge(
    ctx: _WalkCtx,
    dp_id: str,
    path: str,
    judge_cfg: dict[str, Any],
    eligible: list[str],
    chosen: str,
    decider_label: str,
    downgraded: str | None,
) -> None:
    """Append the one-shape `judge` decision record (decider.md §5). `eligible`/`chosen` name the
    backends for the operator's trace; the judge PORT never saw those names."""
    ctx.trace.decisions.append(
        {
            "decision_id": dp_id,
            "node_path": f"{path}.judge",
            "label": judge_cfg.get("intent", f"{path}.judge"),
            "point": "judge",
            "eligible": eligible,
            "chosen": chosen,
            "decider": decider_label,
            "config_hash": ctx.config_hash,
            "strategy": ctx.strategy_name,
            "downgraded": downgraded,
        }
    )


def _resolve_parallel(
    winner: int | None,
    results: dict[int, _BranchOutcome],
    pick: str,
    path: str,
    ctx: _WalkCtx,
    labels: dict[int, str],
    shadows: set[int],
) -> Outcome:
    loser_cat = "raced_lost" if pick == "fastest" else "judged_lost"
    # cross-branch disagreement (§11): computed once over the compared branches; exposed to the
    # enclosing step's gate as `disagreement_over` and recorded on the winner attempt (telemetry).
    disagreement = _branch_disagreement(results, shadows) if pick == "best" else None
    if pick == "merge":
        _merge_fields(winner, results, path, ctx, shadows)  # compose typed_fields into the base
    total_cost = 0.0
    total_basis: str | None = None  # BL-126: priority-ordering reduction alongside total_cost
    for i in sorted(results):
        r = results[i]
        spath = f"{path}.parallel[{i}]"
        bk = r.backend or labels.get(i, "?")
        if r.status.startswith("composite"):
            if i != winner and r.status == "composite_ok":
                # BL-134/Alex: a composite branch's real spend lives on its nested response's own
                # usage, never on _BranchOutcome.cost (_run_branch's own composite construction
                # never passes cost=, so it is always None there) — a composite branch that LOSES
                # pick: best/merge, or that SHADOWS and completes within the node deadline, must
                # still have that real cost/basis folded in exactly like a leaf loser/shadow below.
                # Only the composite WINNER's own fold is handled separately, once, after this loop
                # (BL-126).
                total_cost += _branch_cost(r) or 0.0
                total_basis = _fold_basis(total_basis, _branch_basis(r))
            continue  # a composite branch recorded its own attempts; no additional trace entry
        if i in shadows:  # a shadow runs and is recorded but never wins (execution.md §3.1(5))
            if r.status in ("ok", "cancelled", "drained"):
                # a shadow that outlived the node deadline is still recorded (§4 M3: shadows are
                # always recorded), with no measured cost (Law 6) — never silently dropped.
                drained = r.status == "drained"
                ctx.trace.record(
                    Attempt(
                        bk,
                        "shadow",
                        spath,
                        cost_usd=r.cost,
                        cost_basis="billed" if r.cost else None,
                        detail="drain_over_deadline" if drained else "",
                    )
                )
                _keep_candidate(ctx, bk, spath, "shadow", r.response)
                total_cost += r.cost or 0.0
                total_basis = _fold_basis(total_basis, _branch_basis(r))
            continue
        if i == winner:
            # under merge the winner is the base whose document channels are kept wholesale (§2.4)
            cat = "merge_base" if pick == "merge" else "succeeded"
            ctx.trace.record(
                Attempt(
                    bk,
                    cat,
                    spath,
                    cost_usd=r.cost,
                    cost_basis="billed" if r.cost else None,
                    disagreement=disagreement,
                )
            )
            total_cost += r.cost or 0.0
            total_basis = _fold_basis(total_basis, _branch_basis(r))
        elif r.status == "ok":
            cat = "merge_source" if pick == "merge" else loser_cat
            ctx.trace.record(
                Attempt(bk, cat, spath, cost_usd=r.cost, cost_basis="billed" if r.cost else None)
            )
            _keep_candidate(ctx, bk, spath, cat, r.response)  # a discarded loser worth comparing
            total_cost += r.cost or 0.0  # a completed loser is still billed (T9)
            total_basis = _fold_basis(total_basis, _branch_basis(r))
        elif r.status == "drained":
            # Law 6: a drain that would outlive the node deadline — recorded, but with no measured
            # cost (it was never awaited to completion).
            ctx.trace.record(Attempt(bk, loser_cat, spath, detail="drain_over_deadline"))
            _keep_candidate(ctx, bk, spath, loser_cat, r.response)
        elif r.status == "error":
            ctx.trace.record(Attempt(bk, f"error({r.error_class})", spath))
        elif r.status == "skip":
            ctx.trace.record(Attempt(bk, "skipped(missing_credentials)", spath))
        elif r.status == "deadline_pruned":
            ctx.trace.record(Attempt(bk, "deadline_pruned", spath))
        elif r.status == "cancelled":
            ctx.trace.record(Attempt(bk, loser_cat, spath, detail="cancelled"))

    if winner is not None:
        wr = results[winner]
        resp = wr.response
        assert resp is not None
        # per-page provenance: the winner/base supplied document.pages wholesale (§2.4/§2.7).
        _tag_source_backend(resp, wr.backend or labels.get(winner, "?"))
        # a composite winner recorded its own attempts (skipped from total_cost above); its own
        # internal spend already lives on resp.usage — fold it in so overwriting doesn't drop it.
        own = (
            (resp.usage.cost_usd or 0.0)
            if wr.status.startswith("composite") and resp.usage
            else 0.0
        )
        if wr.status.startswith("composite") and resp.usage and resp.usage.cost_usd is not None:
            # the composite winner's own basis is already correctly folded (it went through this
            # same reconciliation internally); fold it into the sibling total via the shared,
            # coalescing _branch_basis helper (BL-134) — not a raw passthrough, so a composite
            # winner whose own cost lacks a resolved basis coalesces to "unknown" here too, rather
            # than silently folding as if it contributed nothing (Trent).
            total_basis = _fold_basis(total_basis, _branch_basis(wr))
        if total_cost + own > 0:
            _set_total_cost(
                resp, total_cost + own, total_basis
            )  # honest money: all billed attempts summed, basis reconciled (T9, BL-126)
        out = Outcome.ok(resp, wr.quality)
        out.disagreement = disagreement  # the enclosing step gate reads it as `disagreement_over`
        return out

    # composite failure (Law 5): all non-shadow branches failed
    err_classes = [
        r.error_class
        for i, r in results.items()
        if i not in shadows and r.status in ("error", "composite_err")
    ]
    if err_classes and all(c == "invalid_input" for c in err_classes):
        return Outcome.err("invalid_input")  # unanimity
    return Outcome.err("exhausted")


_COST_BASIS_PRIORITY: dict[str, int] = {"billed": 3, "estimated": 2, "infra_only": 1, "unknown": 0}


def _fold_basis(current: str | None, contributed: str | None) -> str | None:
    """Priority-ordering reduction for `usage.cost_basis` as each rung/branch's cost folds into a
    running total (BL-126): `billed > estimated > infra_only > unknown`, monotonically
    non-decreasing as more contributions fold in — never last-write-wins, so a cheap `billed` rung
    can never be silently downgraded by a later, weaker-basis rung's own value."""
    if contributed is None:
        return current
    if current is None:
        return contributed
    if _COST_BASIS_PRIORITY.get(contributed, -1) > _COST_BASIS_PRIORITY.get(current, -1):
        return contributed
    return current


def _branch_cost(r: _BranchOutcome) -> float | None:
    """The real cost contributed by a parallel branch, leaf or composite (BL-134). `_BranchOutcome.
    cost` is populated for a leaf branch, but `_run_branch`'s own composite-branch construction
    never passes `cost=`, so it is always `None` there — a composite branch's real spend instead
    lives on its nested response's own `usage.cost_usd`, already correctly totalled by that
    response's own recursive fold."""
    if r.cost is not None:
        return r.cost
    if r.response is not None and r.response.usage is not None:
        return r.response.usage.cost_usd
    return None


def _branch_basis(r: _BranchOutcome) -> str | None:
    """The basis to fold for a parallel branch, leaf or composite (BL-126, extended BL-134) — None
    unless the branch contributed a genuinely non-null cost (`_branch_cost`, which — unlike a bare
    `r.cost` check — also recognizes a composite branch's nested cost), so a branch that reported no
    cost at all can never dilute the reduction. Coalesces a confirmed-but-unset `cost_basis` to
    `"unknown"` rather than a bare `None` (BL-134/Trent) — a real cost with no known basis must
    never be folded as if it contributed nothing at all."""
    if _branch_cost(r) is None or r.response is None or r.response.usage is None:
        return None
    return r.response.usage.cost_basis or "unknown"


def _rung_basis(resp: NormalizedResponse | None, cost: float | None) -> str | None:
    """The basis to fold for a cascade/paged-cascade rung (BL-134) — the non-parallel sibling of
    `_branch_basis`, for the two call shapes that fold a rung's `NormalizedResponse` + already-
    extracted cost directly rather than a `_BranchOutcome`. None unless the rung contributed a
    genuinely non-null cost, so a rung that reported no cost at all can never dilute the reduction.
    Coalesces a confirmed-but-unset `cost_basis` to `"unknown"` rather than a bare `None`
    (BL-134/Trent) — a real cost with no known basis must never be folded as if it contributed
    nothing at all."""
    if cost is None or resp is None or resp.usage is None:
        return None
    return resp.usage.cost_basis or "unknown"


def _set_total_cost(resp: NormalizedResponse, total: float, basis: str | None = None) -> None:
    """Write the folded `usage.cost_usd` and, when given, `usage.cost_basis` (BL-126). `basis` is
    the CALLER's own priority-ordering reduction (`_fold_basis`) over every rung/branch that
    contributed a non-null cost as it was folded into `total` — this function applies it, it does
    not compute it.

    BL-134/Trent hardening: a nonzero `total` must never leave `cost_basis` at the "zero-cost"
    label `infra_only`, or unset (`None`) — reachable even after every fold site coalesces a bare
    `None` contribution to `"unknown"` (the fold-site fix alone), because `_COST_BASIS_PRIORITY`
    ranks `unknown` BELOW `infra_only`: a genuinely-free branch/rung's own correct `infra_only` tag
    can still outrank a billed-but-basis-unset branch/rung's coalesced `"unknown"` tag in the SAME
    reduction, landing here as an `infra_only`-tagged response despite a real, nonzero, partly-
    unexplained total. When that happens, downgrade honestly to `"unknown"` rather than assert a
    zero-cost label on a response that just got a nonzero total."""
    from openreading.types.response import Usage

    if resp.usage is None:
        resp.usage = Usage(cost_usd=total, cost_basis=basis)
    else:
        resp.usage.cost_usd = total
        if basis is not None:
            resp.usage.cost_basis = basis
    if total > 0 and resp.usage.cost_basis in (None, "infra_only"):
        resp.usage.cost_basis = "unknown"


def _tag_source_backend(resp: NormalizedResponse, backend: str) -> None:
    """Stamp per-page provenance (response `pages[].source_backend`, v0.2) where it is not already
    set — the base/winner backend supplied these pages (§2.4/§2.7)."""
    for pg in (resp.document.pages if resp.document else None) or []:
        if pg.source_backend is None:
            pg.source_backend = backend


def _merge_fields(
    winner: int | None,
    results: dict[int, _BranchOutcome],
    path: str,
    ctx: _WalkCtx,
    shadows: set[int],
) -> None:
    """`pick: merge` (§2.4): vote-merge `typed_fields` across the completed non-shadow branches into
    the base (best-scoring) response. Per field: majority value; tie → highest reported confidence;
    still tied → cheaper backend, then first-listed. The chosen field is a REAL backend's TypedField
    — its value and confidence are never fabricated; a field no branch produced stays absent. Text /
    markdown / pages are the base's, wholesale. Per-field provenance is recorded in the trace."""
    if winner is None:
        return
    voters = [
        results[i]
        for i in sorted(results)
        if i not in shadows and results[i].status == "ok" and results[i].response is not None
    ]
    base = results[winner].response
    if base is None or len(voters) < 1:
        return
    names: list[str] = []
    for r in voters:
        for name in (r.response.typed_fields or {}) if r.response else {}:
            if name not in names:
                names.append(name)

    merged: dict[str, Any] = {}
    provenance: list[dict[str, Any]] = []
    for name in names:
        entries = [  # (index, backend, TypedField) for every branch that produced this field
            (r.index, r.backend or "?", r.response.typed_fields[name])
            for r in voters
            if r.response and r.response.typed_fields and name in r.response.typed_fields
        ]
        idx, backend, tf = _vote_field(entries, ctx)
        merged[name] = tf  # a real TypedField — value + confidence verbatim, never fabricated
        provenance.append({"field": name, "backend": backend, "node": f"{path}.merge"})

    base.typed_fields = merged or base.typed_fields
    if provenance:
        ctx.trace.merge.extend(provenance)


def _vote_field(entries: list[tuple[int, str, Any]], ctx: _WalkCtx) -> tuple[int, str, Any]:
    """Choose one (index, backend, TypedField) for a field: majority value, then highest reported
    confidence, then cheaper backend, then first-listed (§2.4). `None` confidence sorts lowest —
    never invented."""
    votes: dict[str, int] = {}
    for _, _, tf in entries:
        votes[_value_key(tf)] = votes.get(_value_key(tf), 0) + 1

    def rank(entry: tuple[int, str, Any]) -> tuple:
        idx, backend, tf = entry
        conf = tf.confidence if tf.confidence is not None else -1.0
        # highest votes for this value, then highest confidence, then cheaper backend, then index
        return (votes[_value_key(tf)], conf, -_cost_midpoint(backend, ctx), -idx)

    return max(entries, key=rank)


def _value_key(tf: Any) -> str:
    """A stable equality key for a typed_field value (majority voting groups by it)."""
    return str(getattr(tf, "value", tf))


async def _eval_leaf(node: dict[str, Any], path: str, ctx: _WalkCtx) -> Outcome:
    """A standalone leaf node (a strategy that is a single backend). No step-position gate (the
    schema forbids escalate_if on a bare leaf), so success is Ok and failure maps to on_error."""
    deadline = _enter_budget(node, ctx)
    rr = await _run_leaf(node, path, ctx, deadline)
    if rr.status == "ok":
        assert rr.response is not None
        return Outcome.ok(rr.response)
    if rr.status in ("skip", "exhausted"):
        return Outcome.err("exhausted")
    return Outcome.err(rr.error_class or "provider_error")


async def _eval_reference(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    name = node["use"]
    if name in visited:
        raise StrategyReferenceCycle(
            f"strategy reference cycle: {name!r} already on this recursion path "
            f"({sorted(visited)})",
            backend_code="strategy_reference_cycle",
        )
    tree = ctx.trees.get(name)
    if tree is None:  # referenced strategy pruned to nothing under this request's compliance
        return Outcome.err("exhausted")
    return await _eval_node(tree, f"{path}->{name}", ctx, visited | {name})


# --------------------------------------------------------------------------- page granularity (§2.7)


async def _eval_paged_cascade(node: dict[str, Any], path: str, ctx: _WalkCtx) -> Outcome:
    """`granularity: page` cascade (spec §2.7, cascades only): each rung's `escalate_if` is evaluated
    PER PAGE; only the failing pages escalate to the next rung, sent as `pages.ranges` when that
    backend supports page-range selection (else the rung runs document-granularity — validate warns).
    Results are stitched per page, each output page carrying `pages[].source_backend`; per-page
    provenance also lands in `orchestration.pages[]`. PDF-only; a rung that fails escalates the whole
    page set forward (no keep-best rungs here — the page is simply re-parsed by a stronger backend).
    Honest money (BL-120): `base_resp` is frozen to whichever rung ran first, so every later rung's
    real cost is folded into the final response's `usage` here, mirroring `_eval_cascade`'s own
    `billed_total` accumulator — a rung's spend is never dropped just because it wasn't first.
    `cost_basis` folds via the same priority-ordering reduction as every other cascade/parallel
    return path (BL-126), coalescing a confirmed-but-unset basis rather than a bare `None`
    (BL-134/Trent)."""
    from openreading.types.request import PageRange, Pages

    steps = node["steps"]
    deadline_ms = _enter_budget(node, ctx)
    stitched: dict[int, tuple[Any, str]] = {}  # page_number -> (Page, source_backend)
    base_resp: NormalizedResponse | None = None
    escalate_from: list[int] | None = None  # None = parse the whole doc; else these page numbers
    billed_total = 0.0  # honest money: sum every billed rung, not just whichever ran first
    billed_basis: str | None = None  # BL-126: priority-ordering reduction, not last-write-wins

    for si, step in enumerate(steps):
        spath = f"{path}.steps[{si}]"
        backend = _resolve_backend(step["backend"], ctx)
        if backend is None:
            break
        ctx.attempted.add(backend)
        adapter = ctx.registry.get(backend)
        if adapter is None:
            ctx.trace.record(
                Attempt(backend, "error(provider_error)", spath, detail="not in registry")
            )
            break

        req_i = ctx.req
        forced_doc = False
        if escalate_from is not None:
            if _supports_page_ranges(adapter.descriptor):
                req_i = ctx.req.model_copy(
                    update={
                        "pages": Pages(ranges=[PageRange(start=p, end=p) for p in escalate_from])
                    }
                )
            else:
                forced_doc = True  # backend lacks range selection → document granularity this rung

        try:
            budget_ms = max(1, int(deadline_ms - ctx.clock.now_ms()))
            result = await _get_executor(ctx).exec(
                _step_request(ctx, spath, backend, adapter.descriptor, req_i),
                run=lambda adapter=adapter, req_i=req_i, budget_ms=budget_ms: _execute_leaf_sync(
                    adapter, req_i, ctx.broker, ctx.clock, budget_ms
                ),
            )
        except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused) as e:
            ctx.trace.record(Attempt(backend, f"error({classify_error(e)})", spath))
            break
        except Exception as e:
            # BL-99: adapter.normalize() is ordinary adapter code, not one of the four taxonomy
            # types above — a plain crash out of it used to propagate straight out of the page
            # cascade uncaught, past even the cheap rung's already-good pages recovered below.
            # classify_error(e) already falls through to "provider_error" for anything outside the
            # taxonomy. auth_hinted (widened in readiness.py) has already redacted e's message by
            # the time it reaches here.
            ctx.trace.record(Attempt(backend, f"error({classify_error(e)})", spath))
            break

        # Ledger T3 §4.0: this call site never populates missing_credentials (it computes no
        # readiness check of its own, unlike _run_branch/_run_leaf), so exec() can only ever
        # return "ok" or raise here — a non-"ok" status is defensive, not a live path today.
        if result.status != "ok":
            ctx.trace.record(Attempt(backend, "error(provider_error)", spath, detail=result.status))
            break
        resp_i = _response_payload(result)

        cost = _actual_cost(resp_i)
        ctx.trace.record(
            Attempt(
                backend, "succeeded", spath, cost_usd=cost, cost_basis="billed" if cost else None
            )
        )
        if cost is not None:
            billed_total += cost  # honest money: sum every billed rung (escalated + first)
            billed_basis = _fold_basis(billed_basis, _rung_basis(resp_i, cost))
        if base_resp is None:
            base_resp = resp_i
        for pg in (resp_i.document.pages if resp_i.document else None) or []:
            stitched[pg.page_number] = (pg, backend)  # rung output overwrites the escalated pages

        gate = step.get("escalate_if")
        if si == len(steps) - 1 or not isinstance(gate, dict) or forced_doc:
            break  # last rung, no gate, or a doc-granularity rung → nothing more to escalate
        failing = sorted(pn for pn, (pg, _bk) in stitched.items() if _page_gate_fires(pg, gate))
        if not failing:
            break
        escalate_from = failing

    if base_resp is None:
        return Outcome.err("exhausted")
    ordered = sorted(stitched.items())
    for _pn, (pg, bk) in ordered:
        pg.source_backend = bk  # per-page provenance (§2.7)
    if base_resp.document is not None and ordered:
        base_resp.document.pages = [pg for _pn, (pg, _bk) in ordered]
    ctx.trace.assign_pages([{"page": pn, "backend": bk} for pn, (_pg, bk) in ordered])
    if billed_total > 0:
        # honest money: every rung, not just rung 1 (BL-120); basis is the priority-ordering
        # reduction over every contributing rung, not base_resp's own single-rung value, and never
        # last-write-wins (BL-126).
        _set_total_cost(base_resp, billed_total, billed_basis)
    return Outcome.ok(base_resp)


def _supports_page_ranges(desc) -> bool:
    """A backend advertises native page-range selection via the (extra-allowed) capability
    `page_range_selection` (§2.7). Absent/false → the rung runs document granularity."""
    return bool(getattr(desc.capabilities, "page_range_selection", False))


def _page_gate_fires(pg: Any, gate: dict[str, Any]) -> bool:
    """Evaluate a step's `escalate_if` against a SINGLE page (§2.7). Builds a per-page snapshot
    (doc_confidence = the page's confidence, chars_per_page = its text length) and reuses the same
    gate evaluator as document granularity."""
    from openreading.strategies.signals import SignalSnapshot

    text = pg.text or ""
    snap = SignalSnapshot(
        chars_per_page=float(len(text)),
        doc_text=text,
        doc_confidence=pg.confidence,
        page_confidences=[pg.confidence] if pg.confidence is not None else [],
    )
    return evaluate_gate(gate, snap).fired


async def _eval_cascade(
    node: dict[str, Any], path: str, ctx: _WalkCtx, visited: frozenset[str] = frozenset()
) -> Outcome:
    if node.get("granularity") == "page":  # §2.7 — per-page gating + range escalation + stitching
        return await _eval_paged_cascade(node, path, ctx)
    steps = node["steps"]
    on_quality_exhausted = node.get("on_quality_exhausted", "best_effort")
    cascade_on_error = node.get("on_error")

    deadline_ms = _enter_budget(node, ctx)
    best: tuple[float, int, NormalizedResponse] | None = None
    ran = 0
    deadline_hit = False
    billed_total = (
        0.0  # honest money: sum every billed rung (escalated + winner), not just the last
    )
    billed_basis: str | None = None  # BL-126: priority-ordering reduction, not last-write-wins

    for i, step in enumerate(steps):
        spath = f"{path}.steps[{i}]"
        if ctx.clock.now_ms() > deadline_ms:
            deadline_hit = True
            break

        is_last = i == len(steps) - 1
        # a nested cascade / composite step. A `parallel` step MAY carry a step-position gate
        # (§6.3) — evaluated on the winner's response with leaf-step semantics. Every other
        # composite (nested cascade / use / route / decide) and a gate-less parallel keep exactly
        # today's accept behavior (D-v3-7).
        if _is_composite(step):
            child_ctx = _child_ctx(ctx, deadline_ms)
            outcome = await _eval_node(step, spath, child_ctx, visited)
            if outcome.kind == "err":
                cls = outcome.error_class or "provider_error"
                if on_error_action(cls, step.get("on_error"), cascade_on_error) == "fail":
                    return outcome
                continue

            if _gated_parallel_step(step) and outcome.response is not None:
                # honest money: the composite's own spend joins the running total exactly as a
                # leaf's does, so a later accept / keep-best reports every billed rung.
                own = (outcome.response.usage.cost_usd or 0.0) if outcome.response.usage else 0.0
                billed_total += own
                if outcome.response.usage and outcome.response.usage.cost_usd is not None:
                    billed_basis = _fold_basis(billed_basis, outcome.response.usage.cost_basis)
                snap = probe(
                    outcome.response,
                    doc_bytes=doc_bytes(ctx.req),
                    mime_type=ctx.req.document.mime_type,
                )
                snap.disagreement_over = outcome.disagreement  # cross-branch signal (§11)
                g = _apply_gates(step, snap, is_last, ctx.plain_sourced)
                winner = _winner_attempt(ctx, spath)
                if winner is not None:
                    winner.bind_gates(g.records)
                if g.hard_category:  # escalate_if fired on the winner → retain best-so-far, advance
                    if winner is not None:
                        winner.recategorize(g.hard_category)
                    if best is None or g.quality > best[0]:
                        best = (g.quality, i, outcome.response)
                    continue
                if billed_total > 0:
                    _set_total_cost(outcome.response, billed_total, billed_basis)
                return outcome

            # a gate-less composite step → accept, folding prior escalated rungs' billed cost.
            if billed_total > 0 and outcome.response is not None:
                own_usage = outcome.response.usage
                own_cost = own_usage.cost_usd if own_usage is not None else None
                own_basis = (
                    own_usage.cost_basis if own_usage is not None and own_cost is not None else None
                )
                _set_total_cost(
                    outcome.response,
                    (own_cost or 0.0) + billed_total,
                    _fold_basis(billed_basis, own_basis),
                )
            return outcome

        # a leaf step
        rr = await _run_leaf(step, spath, ctx, deadline_ms)
        if rr.status == "skip":
            # Ledger T3 (§4.0/§4.3b), corrected Phase C round 1 (jay + sophia, independently): a
            # skip — live or replayed — always fails over to the next rung, exactly as a live skip
            # always has. An earlier version special-cased `rr.replayed` to `return
            # Outcome.err("missing_credentials")` instead, reasoning it was needed for AC-15's
            # "terminal on resume" guarantee — but that guarantee is already fully delivered one
            # layer down, by InlineExecutor.exec's own replay branch: a replayed missing-credentials
            # skip never re-attempts a live dispatch, regardless of what a live credentials re-check
            # would say today; it just returns the recorded skip outcome. The special case added no
            # protection AC-15 needed and instead abandoned any LATER rung's own independently-
            # terminal record without ever consulting it — turning an ordinary, already-successful
            # multi-rung cascade (an early rung skipped for missing credentials, a later rung
            # succeeded) into a hard `PlanExhaustedError` on the very next resume. `continue` is the
            # whole fix: it falls through to this same rung's own next iteration, which replays that
            # later rung's terminal record exactly as it would live.
            continue
        if rr.status == "exhausted":
            if on_error_action("exhausted", step.get("on_error"), cascade_on_error) == "fail":
                return Outcome.err("exhausted")
            continue
        if rr.status == "error":
            cls = rr.error_class or "provider_error"
            if on_error_action(cls, step.get("on_error"), cascade_on_error) == "fail":
                return Outcome.err(cls)
            continue

        # success — this leaf ran
        ran += 1
        if rr.cost is not None:
            billed_total += rr.cost  # honest money: sum every billed rung
            billed_basis = _fold_basis(billed_basis, _rung_basis(rr.response, rr.cost))
        resp = rr.response
        assert resp is not None
        snap = probe(resp, doc_bytes=doc_bytes(ctx.req), mime_type=ctx.req.document.mime_type)
        g = _apply_gates(step, snap, is_last, ctx.plain_sourced)
        # The leaf's own Attempt, by reference from _run_leaf (Ledger T2 §7.3/§4.2) — not
        # `ctx.trace.attempts[-1]`: resolving a review-band decision below may append a
        # `decider_call` attempt of its own, which a LATER `[-1]` read would incorrectly pick up
        # (this part is the pre-existing reason this binding always ran before the decision point,
        # not new to T2). By-reference is defensive hardening against a sibling parallel branch's
        # own concurrent attempt append, not a fix for a proven live bug there — asyncio's
        # cooperative scheduling means that specific interleaving cannot happen today (see
        # `_resolve_decision_point`'s own docstring; Phase C round-1, alex-phasec F1).
        assert rr.attempt is not None
        leaf_attempt = rr.attempt
        leaf_attempt.bind_gates(g.records)

        escalated: str | None = g.hard_category
        if g.review_fired:  # a gate-band decision point — the decider (or engine default) chooses
            action, _record = await _resolve_decision_point(
                ctx,
                point="gate_band",
                node_path=f"{spath}.review_if",
                label=step.get("label", spath),
                candidates=["accept", "escalate"],
                engine_default=step.get("review_default", "escalate"),
                intent=step.get("intent"),
                signals=_gate_signals(g.review_records),
                typed_fields=resp.typed_fields,
            )
            if action == "escalate":
                escalated = "review_escalated"

        if escalated:
            leaf_attempt.recategorize(escalated)  # "quality_escalated" | "review_escalated"
            if best is None or g.quality > best[0]:
                best = (g.quality, i, resp)
            continue
        if billed_total > 0:
            _set_total_cost(
                resp, billed_total, billed_basis
            )  # honest money: all billed rungs, not just the winner (basis reconciled, BL-126)
        return Outcome.ok(resp, g.quality)

    # exhausted (or the deadline ended the walk)
    if best is not None and on_quality_exhausted == "best_effort":
        if billed_total > 0:
            _set_total_cost(best[2], billed_total, billed_basis)
        return Outcome.deficient(best[2], best[0])
    if deadline_hit:
        return Outcome.err("budget_exhausted")
    return Outcome.err("exhausted")


@dataclass
class _RunResult:
    status: str  # "ok" | "error" | "skip" | "exhausted"
    backend: str = ""
    response: NormalizedResponse | None = None
    error_class: str | None = None
    cost: float | None = None
    # Ledger T2 §7.3/§4.2: the exact Attempt this call appended (status == "ok" only), returned by
    # reference so a caller binds gates/category to the RIGHT record directly rather than
    # re-deriving "the last one" via `ctx.trace.attempts[-1]`. Defensive hardening, not a fix for a
    # proven live bug: asyncio's cooperative scheduling means a sibling parallel branch's own
    # attempt append cannot actually interleave between this call returning and its caller reading
    # `[-1]` today (Phase C round-1, alex-phasec F1, reproduced with zero violations across 12
    # branches / 500 trials) — this removes the DEPENDENCY on that invariant, it doesn't close an
    # exploitable gap.
    attempt: Attempt | None = None
    # Ledger T3 (§4.0): the leaf's own ExecResult.journal_seq/replayed — additive, default to
    # values that reproduce today's behavior for any path that doesn't populate them.
    journal_seq: int | None = None
    replayed: bool = False


async def _run_leaf(
    step: dict[str, Any], path: str, ctx: _WalkCtx, deadline_ms: float
) -> _RunResult:
    backend = _resolve_backend(step["backend"], ctx)
    if backend is None:
        ctx.trace.record(
            Attempt(step["backend"], "error(exhausted)", path, detail="no_untried_backend")
        )
        return _RunResult("exhausted")
    ctx.attempted.add(backend)  # walk-wide (T12)
    adapter = ctx.registry.get(backend)
    if adapter is None:
        ctx.trace.record(Attempt(backend, "error(provider_error)", path, detail="not in registry"))
        return _RunResult("error", backend, error_class="provider_error")
    desc = adapter.descriptor

    rc = build_run_context(ctx.req, desc, broker=ctx.broker)
    # Ledger T3 §4.3b: `missing_required` still runs here (this function alone holds ctx.broker),
    # but no longer returns early — every leaf now routes through ctx.exec so a missing-credentials
    # skip becomes a real, journaled outcome instead of staying orchestration-only (F2).
    missing = missing_required(desc, rc)

    started = ctx.clock.now_ms()
    try:
        result = await _get_executor(ctx).exec(
            _step_request(ctx, path, backend, desc, ctx.req, missing_credentials=missing),
            run=lambda: _execute_leaf_sync(
                adapter, ctx.req, ctx.broker, ctx.clock, int(deadline_ms - started)
            ),
        )
    except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused) as e:
        cls = classify_error(e)
        ctx.trace.record(
            Attempt(
                backend,
                f"error({cls})",
                path,
                duration_ms=int(ctx.clock.now_ms() - started),
                detail=str(e),
            )
        )
        return _RunResult("error", backend, error_class=cls)
    except Exception as e:
        # BL-99: adapter.normalize() is ordinary adapter code, not one of the four taxonomy types
        # above — a plain KeyError/IndexError/ValueError/AttributeError out of it used to propagate
        # straight out of the cascade uncaught. classify_error(e) already falls through to
        # "provider_error" for anything outside the taxonomy, so this leaf is treated exactly like
        # an unrecognized TerminalError — recoverable, never a hard crash. auth_hinted (widened in
        # readiness.py) has already redacted e's message by the time it reaches here, since
        # _execute_leaf_sync routes every call through it.
        cls = classify_error(e)
        ctx.trace.record(
            Attempt(
                backend,
                f"error({cls})",
                path,
                duration_ms=int(ctx.clock.now_ms() - started),
                detail=str(e),
            )
        )
        return _RunResult("error", backend, error_class=cls)

    if result.status != "ok":
        # "skipped" (missing_credentials, live or replayed) — a cascade leaf's own step_path is
        # never subject to task cancellation (only a `parallel:` branch can be), so "cancelled"
        # is structurally unreachable here; treated the same as "skipped" defensively rather than
        # left to crash on an unhandled status.
        detail = ",".join(missing) if missing else "replayed"
        ctx.trace.record(Attempt(backend, "skipped(missing_credentials)", path, detail=detail))
        return _RunResult("skip", backend, journal_seq=result.journal_seq, replayed=result.replayed)

    resp = _response_payload(result)
    cost = _actual_cost(resp)
    attempt = Attempt(
        backend,
        "succeeded",
        path,
        duration_ms=int(ctx.clock.now_ms() - started),
        cost_usd=cost,
        cost_basis="billed" if cost else None,
    )
    ctx.trace.record(attempt)
    return _RunResult(
        "ok",
        backend,
        response=resp,
        cost=cost,
        attempt=attempt,
        journal_seq=result.journal_seq,
        replayed=result.replayed,
    )


def _step_id(run_id: str, step_path: str, step_seq: int) -> str:
    """`step_id = sha256(run_id ‖ step_path ‖ step_seq)` (internal/design/ledger.md §6, verbatim) — the
    only form of the key that leaves the process, so it must be deterministic: the same
    `(run_id, step_path, step_seq)` re-journaled (a re-executed walk, a future distributed
    executor's at-least-once redelivery) has to land on the identical `step_id`, which a random
    UUID structurally cannot do (Phase C round-1, jay F1)."""
    h = hashlib.sha256()
    h.update(run_id.encode())
    h.update(b"\x00")
    h.update(step_path.encode())
    h.update(b"\x00")
    h.update(str(step_seq).encode())
    return h.hexdigest()


def _step_request(
    ctx: _WalkCtx,
    path: str,
    backend: str,
    desc: Any,
    doc: OpenReadingRequest,
    missing_credentials: list[str] | None = None,
) -> StepRequest:
    """The audit-facing `StepRequest` for one leaf dispatch (internal/design/ledger.md §5.4, plan §6).
    T1 always dispatches `step_seq=0`, `attempt=1` — no per-path retry loop and no drive
    decomposition exist yet to make either vary (that is a later tranche's job).

    `content_key` is built from `document_digest` — the document's real, content-derived bytes
    (streamed for a local path) — never from `document_identity`'s machine-local
    `(realpath, size, mtime_ns)` blob for a path: §5.5 ("Three identities, deliberately distinct")
    names this as the one identity Ledger must not reuse, since a run relocated to a different
    worker would otherwise compute a different key for byte-identical content (Phase C round-1,
    jay F2). `idempotency_key` falls back to the same value unless the caller supplied its own.

    `missing_credentials` (Ledger T3 §4.3b): the caller's own `readiness.missing_required(desc,
    rc)` result, threaded through so `InlineExecutor.exec`'s missing-credentials gate can journal
    a real, replayable outcome instead of the decision staying orchestration-only. Defaults to
    empty — `_eval_paged_cascade`'s own call site never computes this check at all today."""
    digest = document_digest(doc.document)
    version = desc.runtime.version_pin if desc.runtime else None
    content_key_val = (
        content_key(digest, backend, version, doc.to_schema_dict()) if digest is not None else None
    )
    idempotency_key = doc.idempotency_key if doc.idempotency_key is not None else content_key_val
    return StepRequest(
        step_id=_step_id(ctx.run_id, path, 0),
        run_id=ctx.run_id,
        kind="submit",
        step_path=path,
        step_seq=0,
        attempt=1,
        idempotency_key=idempotency_key,
        content_key=content_key_val,
        backend_id=backend,
        missing_credentials=missing_credentials or [],
    )


def _execute_leaf_sync(
    adapter,
    req,
    broker,
    clock,
    budget_ms: int,
    on_submit: Callable[[Job], None] | None = None,
) -> NormalizedResponse:
    """The synchronous submit→drive→normalize→meter path — runs in a worker thread (T2), so
    run_to_completion's asyncio.run is safe. Mirrors executor.execute_plan's inner loop, metering
    included: every rung's own `usage.cost_usd` is what the honest-money aggregation sums.

    `on_submit` (parallel branches) hands the accepted Job back to the orchestration loop before
    the drive begins, so a loser's live backend job can be cancelled (execution.md §3.3)."""
    # BL-146: budget_ms (the real, already-resolved remaining time — the wait-loop bound just
    # below) must also reach rc.deadline_ms, the field an adapter's own code actually reads (e.g.
    # TesseractAdapter.submit()'s subprocess timeout) — clamp negative to 0 (explicit
    # zero/negative means fail fast, matching this value family's established semantics).
    rc = build_run_context(
        req, adapter.descriptor, broker=broker, deadline_ms=max(0, int(budget_ms))
    )
    with auth_hinted(adapter.descriptor, rc.credentials):
        job = adapter.submit(req, rc)
        if on_submit is not None:
            on_submit(job)
        job = run_to_completion(
            adapter, job, ctx=rc, deadline_ms=clock.now_ms() + max(1, budget_ms), clock=clock
        )
        return apply_cost_report(
            adapter, job, adapter.normalize(job, rc, slim_request(req)), rc.credentials
        )


# --------------------------------------------------------------------------- gates + budget


@dataclass
class _GateOutcome:
    hard_category: str | None  # "quality_escalated" if escalate_if fired, else None
    review_fired: bool  # review_if fired and escalate_if did not — a gate-band decision point
    quality: float
    records: list[GateRecord]  # all gate records, attached to the leaf's attempt
    review_records: list[GateRecord]  # review_if's own records → the decision-point signals


def _apply_gates(
    step: dict[str, Any], snap, is_last: bool, plain_sourced: bool = False
) -> _GateOutcome:
    """Evaluate a step's gates against its normalized response (execution.md §2.1). `escalate_if` is
    the hard gate — fired → `quality_escalated`, the decider is never consulted. Otherwise
    `review_if` firing marks a gate-band decision point; the cascade resolves it (accept/escalate)
    through the decider layer (decider.md §2.1). On a Plain strategy (`plain_sourced`), each record
    is tagged with the Plain word its predicate compiled from, for `explain` grouping (§9)."""
    records: list[GateRecord] = []
    review_records: list[GateRecord] = []
    quality = 1.0

    def collect(gate_result, into: list[GateRecord] | None = None) -> None:
        for p in gate_result.predicates:
            source = ADVANCED_TO_PLAIN.get(p.key) if plain_sourced else None
            rec = GateRecord(p.key, p.threshold, p.observed, p.fired, p.unavailable, source=source)
            records.append(rec)
            if into is not None:
                into.append(rec)

    escalate_if = step.get("escalate_if")
    if isinstance(escalate_if, dict):
        gr = evaluate_gate(escalate_if, snap)
        collect(gr)
        applicable = [p for p in gr.predicates if not p.unavailable]
        if applicable:
            quality = sum(1 for p in applicable if not p.fired) / len(applicable)
        if gr.fired:
            return _GateOutcome("quality_escalated", False, quality, records, review_records)

    review_if = step.get("review_if")
    if isinstance(review_if, dict):
        gr = evaluate_gate(review_if, snap)
        collect(gr, review_records)
        if gr.fired:
            return _GateOutcome(None, True, quality, records, review_records)
    return _GateOutcome(None, False, quality, records, review_records)


def _gate_signals(records: list[GateRecord]) -> dict[str, Any]:
    """The observed-vs-threshold signals a gate-band decider is shown (decider.md §3.1, §5) — only
    the predicates that actually reported (available), never a compliance surface."""
    return {
        r.predicate: {"observed": r.observed, "threshold": r.threshold}
        for r in records
        if not r.unavailable
    }


def _enter_budget(node: dict[str, Any], ctx: _WalkCtx) -> float:
    """The effective deadline (ms) for this node — its own `max_duration` clamped to the outer
    deadline. Cost budgets were removed; this returns only the time deadline."""
    budget = node.get("budget") or {}
    outer = (
        ctx.deadline_ms if ctx.deadline_ms is not None else ctx.clock.now_ms() + DEFAULT_DEADLINE_MS
    )
    dur_ms = _duration_ms(budget.get("max_duration"))
    return min(ctx.clock.now_ms() + dur_ms, outer) if dur_ms is not None else outer


def _child_ctx(ctx: _WalkCtx, deadline_ms: float) -> _WalkCtx:
    """The walk context for a nested composite step: identical to its parent except for the step's
    clamped deadline. `replace` rather than a hand-written field list — a hand-copy silently drops
    every field added later (it dropped `keep_candidates`/`candidates`/`plain_sourced` this way, so
    a cascade-wrapped parallel returned zero `orchestration.candidates[]`).

    The walk-wide accumulators (`trace`, `attempted`, `candidates`) are shared by reference, so
    everything the nested walk records lands in the parent's lists directly — do not add a
    merge-back step, it would double-count."""
    return replace(ctx, deadline_ms=deadline_ms)


def _resolve_backend(slug: str, ctx: _WalkCtx) -> str | None:
    if slug != "auto":
        return slug
    for candidate in ctx.eligible:  # stage-3 order, first not-yet-attempted
        if candidate not in ctx.attempted:
            return candidate
    return None


def _actual_cost(resp: NormalizedResponse) -> float | None:
    return resp.usage.cost_usd if resp.usage and resp.usage.cost_usd is not None else None


def _is_composite(node: dict[str, Any]) -> bool:
    return any(k in node for k in ("steps", "parallel", "route", "decide", "use"))


def _gated_parallel_step(node: dict[str, Any]) -> bool:
    """A cascade step whose node is a `parallel` carrying a step-position `escalate_if` (§6.3).
    Only this composite shape gets a step gate; nested cascade / use / route / decide keep their
    own internal rules (D-v3-7)."""
    return "parallel" in node and isinstance(node.get("escalate_if"), dict)


def _winner_attempt(ctx: _WalkCtx, spath: str) -> Attempt | None:
    """The parallel step's winning *direct* branch attempt (`succeeded`/`merge_base` at
    `{spath}.parallel[i]`), where a step-position gate records its predicates — the composite
    analogue of a leaf binding its own attempt. None when the winner was itself a composite (its
    own nested attempts, not a direct branch); the gate still fires, only the annotation is
    unattached."""
    prefix = f"{spath}.parallel["
    for att in reversed(ctx.trace.attempts):
        # a DIRECT branch attempt is `{spath}.parallel[<i>]` — the remainder has no further `.`
        if (
            att.node.startswith(prefix)
            and "." not in att.node[len(prefix) :]
            and att.category in ("succeeded", "merge_base")
        ):
            return att
    return None


def _branch_disagreement(results: dict[int, _BranchOutcome], shadows: set[int]) -> float | None:
    """Worst pairwise content disagreement — 1 − token-set Jaccard of the guaranteed text channel —
    among the finished non-shadow branch responses (§11). Inlined rather than reusing
    `comparison/content_overlap` because `comparison/` must stay a leaf (H6a): strategies never
    imports it. None with fewer than two branches to compare."""
    tokens: list[set[str]] = []
    for i, r in results.items():
        if i in shadows or r.status != "ok" or r.response is None:
            continue
        doc = r.response.document
        tokens.append(set(((doc.text if doc else None) or "").lower().split()))
    if len(tokens) < 2:
        return None
    return max(1.0 - _token_jaccard(a, b) for a, b in itertools.combinations(tokens, 2))


def _token_jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 1.0  # two empties = identical = no disagreement


def _duration_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    units = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}
    for suffix, mult in units.items():
        if value.endswith(suffix) and value[: -len(suffix)].isdigit():
            return int(value[: -len(suffix)]) * mult
    return None
