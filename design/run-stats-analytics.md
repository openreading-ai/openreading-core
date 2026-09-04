# Run Stats and Routing Analytics

**Status:** DESIGN — revision 2. Proposal for review; no implementation is in scope for this branch.
**Product intent:** `product/specs/run-stats-analytics.product-spec.md` (revision 2). The spec's
revision 2 carries the matching amendments to AC-3, AC-4, AC-6, AC-9, AC-10, AC-11 and AC-14,
listed in §15. Design and spec revisions move together; neither is authoritative alone.
**Reviewed against:** this repo at `070992f`, 2026-09-03. Every line reference in §3 was
re-verified against that tree.
**Decision requested:** approve the trimmed v0.1 contract, the deferral register in §10, and the
ProductSpec amendments in §15, before a build prompt is written.

## 0. What changed in revision 2

Revision 1 was accepted in direction and cut in surface. The audit (§3), the fact-sink architecture
(§6) and the honesty laws (RS-1, RS-2, RS-3, RS-6, RS-7, RS-9) survive intact — they are the reason
to build this at all. What shrank is the *published contract*, on one principle:

> **A v0.1 closed vocabulary is a promise you pay a schema version to withdraw. Publish the facts
> the engine measures; let the consumer and the renderer derive the rest.**

Cut from the published v0.1 block:

| Cut | Because |
|---|---|
| `shares[]` | Every row is two integers already present in `backends[]` / `totals`. Publishing them again creates state a validator must cross-check for agreement. RS-4's own reasoning — "an agent can divide integers itself" — argues for deleting the array, not for shipping it with explicit denominators. The measure registry (§7.3) moves to this document; the percentage moves to `--format table`. |
| `decisions[]` | For the case that motivated the feature — a serial switch and its reason — the attempt row already carries `role`, `reason_code` and (new in rev 2) `from_backend`. The array earned its place only for gate/judge/decider `evidence`, which is deferred with them. |
| `parallel_groups[]` | Replaced by one optional `group_ref` string on the attempt. Group membership is preserved; the group *object* (kind, selected list, per-loser disposition table) is not. `role` already names the kind and `disposition` already names each loser's fate, so the object was a second copy of both. |
| `POST /v1/stats` | Its stated justification — "an agent wants a small answer from a saved artifact without sending its document payload onward" — is self-defeating: the agent must POST the whole artifact, payload included, to get the sub-object back. It is `jq .stats` with a network hop and an auth boundary. |
| `--error-format json` | A change to the CLI *failure* contract riding along with an addition to the success contract. It is a real need for agents and it deserves its own decision; bundling it means one objection blocks both. |
| async poll detail (`wait_mode`, `completion_source`, `poll_calls`, `poll_faults`, `cancellation_disposition`) | Six closed sets and four counters for the surface with the fewest live consumers today. A terminal job artifact still gets a block in v0.1; it just does not account for polls. |
| `timing.known_live_attempt_duration_sum_ms` | Derived from the attempt rows. The *counts* that say how complete the measurement is are kept, because those are not derivable. |
| `backends[].eligible_documents`, `race_entries`, `race_wins` | Plan-state and race-grouping projections. They belong with `decisions[]` and the group object in v0.2. |

Reversed from the review that prompted this revision: **the nine `role` values stay.** They are not
speculative. `strategies/trace.py:15-31` already emits `raced_lost`, `judged_lost`, `shadow`,
`merge_base`, `merge_source`, `decider_call` and `judge_call` today, and `types/response.py:66`
carries `Page.source_backend` for page routing. A role vocabulary that mirrors shipped behaviour
costs nothing to keep and is expensive to add back.

Kept despite being derivable — and this asymmetry is deliberate: **`cost` stays, `timing`'s sum
goes.** A consumer that mis-sums nullable per-attempt costs makes a billing misstatement; a consumer
that mis-sums durations makes a cosmetic one. Cost keeps its known-dollars sum and its coverage
counts. Timing keeps only counts.

Net: the published contract falls from nine top-level objects and arrays to six, and from 72
distinct field names to 45 — a 38% cut — with no honesty law withdrawn. One count moves the other
way: `disposition` grows from five values to nine, because dropping the group object folds each
loser's fate onto the attempt that had it. That is the trade, and it is a good one: nine values in
one closed set cost less to carry than a second object whose only content was those values.

## 1. Recommendation in one page

Add a versioned `stats` object to each newly executed **outer** artifact: a single response, a batch
result, or a terminal async-job result/error. It is captured automatically and kept in memory until
that artifact is returned. It is not a new database, event file, process counter, or lookup handle.

The object is a projection of one internal set of immutable execution facts. Direct execution, plain
fallback, strategy execution, batch orchestration, async driving, cache reuse and Ledger replay all
emit facts at their authoritative boundaries. Strategy `orchestration` and optional Ledger records
project from the same facts; stats never reverse-engineers them from warning prose or from a
permissive serialized trace.

The normal call stays normal:

```console
openreading parse invoice.pdf --strategy fast > result.json
```

The result gains an additive `stats` block. A caller who wants only the compact block runs
`openreading stats result.json` or calls `openreading.stats(result)`. Both are pure projections and
make no backend call. A human table is a renderer over the same canonical JSON; it is never the
contract.

The block answers "what happened in this invocation?", not "what has this installation done over
time?". Consequently there is no `GET /v1/stats/{id}` and no new `run_id`. If Ledger is armed, its
existing run identity remains its resume identity. A hosted product may wrap or ingest the block and
add identity, retention, search and cross-run analytics outside open core.

## 2. The ask and the boundary

The desired outcome is a caller-visible account of backend allocation and strategy power:

- which backends were attempted and which were actually sent work;
- how many documents each backend reached, and for how many it produced the final result;
- every serial switch and its reason;
- race, best, merge, shadow, judge and page outcomes, including a loser whose fate is unknown;
- time and cost with honest unknowns; and
- a representation a CLI user, Python program, HTTP client or agent can consume without parsing
  prose or receiving document content.

This proposal deliberately does **not** turn that account into operational telemetry. A per-run fact
is not a fleet counter. A saved response is not a core-owned history store. A comparison report is
not an execution receipt.

## 3. Audit: what exists today

Re-verified at `42f4f99`. The ingredients are real; the unified schema, aggregator, CLI command and
Python function are not.

| Surface | Evidence already present | Why it is not run stats |
|---|---|---|
| Normalized response | `types/response.py:33-40` backend identity; `:81-90` usage with cost, basis and optional duration; `:121-145` optional `orchestration` | No run grouping, attempt trail, dispatch distinction, or stable stats schema. `response.v0.3.json` leaves `orchestration` permissive. |
| Static router | `router/router.py:73-104` chosen backend, fallbacks, drops, terminal reason; `:173-222` scores | It is a plan, not execution. Scores are discarded; fallback reordering has no decision event. |
| Plain auto/fallback | `router/executor.py:69-79` local `Attempt` — backend, category, code, detail, and **no duration or cost**; `:129-134` `_record_trail` turns that history into `fallback_used` warning **prose** | A successful call loses structured history entirely. The only machine-readable residue of a fallback is a warning string. |
| Strategy trace | `strategies/trace.py:15-31` closed attempt categories; `:60-111` duration/cost/gates; `:166-186` builds `orchestration` | Strategy paths only. `orchestration()` computes `fallback_depth = sum(1 for a in attempts if a.category != "succeeded")` (`:169`) — which counts `shadow`, `merge_source`, `judge_call` and `raced_lost` as fallback depth. It is a non-success attempt count, not a backend-transition count. |
| Batch | `batch/runner.py:103-130` `_summarize` | `summary.backends[bid] += 1` iterates **succeeded items only** (`:107`), so it tallies final result backends — never sends, losers, retries, or the native-vs-platform dispatch boundary. |
| Ledger | `ledger/header.py`, `ledger/step.py`, `ledger/inline.py` persist run/step facts when armed | Optional, durable, strategy-biased, built for replay/resume. The package docstring itself records that Trace and journal can drift (`ledger/__init__.py:91-97`). Not a lightweight default stats channel. |
| Async jobs | `types/job.py:14-30` and the server job record retain state, vendor id, timestamps, attempts, cost hints | No terminal stats carrier. Poll "attempts" means consecutive faults, not total calls. |
| Server | `server/app.py:805-865` centralizes error mapping; `server/README.md` documents no metrics surface | No request-scoped stats object. |
| Compare | Pure over supplied artifacts; CLI fan-out can execute direct parses | Acquisition and comparison are separate. A comparison report must not claim it dispatched a backend. |

Two facts from this audit set the schema cost. `response.v0.3.json` and `batch-result.v0.1.json`
both declare `additionalProperties: false` at the top level, so an additive field genuinely requires
a successor of each — the envelope's `extra="ignore"` (`types/response.py:130`) makes an old
*reader* tolerant, but does not make the new field schema-valid. That cost is unavoidable and is
priced into §13.

The conclusion is incremental: preserve these facts and their detailed consumers, add one common
fact boundary and a stable analytical projection, then expose that projection consistently.

## 4. Design laws

The review contract. Law numbers are stable across revisions; two are amended and none withdrawn.

**RS-1 — One outer artifact, one stats block.** A document response has one block; a platform or
native batch has one root block; a terminal job artifact has one block. Child facts carry an item
index inside the root and are not recursively counted from nested response blocks. This prevents a
platform batch from counting each child once and the root a second time (AC-1, AC-7).

**RS-2 — Facts are singular; projections are plural.** There is one request-scoped immutable fact
set. `stats`, strategy `orchestration` and an armed Ledger consume it; stats is not a third mutable
log. When Ledger is the durable source, a replay folds the recorded facts instead of pretending it
performed a fresh dispatch (AC-2, AC-8).

**RS-3 — A dispatch is a real submit boundary.** `sent: true` means an adapter `submit` or
`submit_many` was invoked for this invocation. Compliance drops, missing credentials, strategy
pruning, cache hits, Ledger replay and progress polls are not dispatches. A native `submit_many`
over nine documents is one live dispatch and nine dispatched documents; a platform batch doing nine
child submits is nine live dispatches (AC-7).

**RS-4 — A published number is a count; a ratio is a rendering.** *Amended in revision 2.* Canonical
JSON publishes integer counts only — never a percentage, never a pre-divided ratio, and never a
denominator-free numerator. Every measure a renderer may compute is defined in §7.3 with a fixed
numerator and denominator. A renderer may show `50.0%` only beside its exact fraction and the
measure's name, and must render an undefined `0/0` as unavailable, never as zero percent. The
failure this prevents is a rounded float in a contract: `66.7%` cannot be re-divided, cannot express
`0/0`, and cannot be checked against the counts it came from (AC-3, amended).

**RS-5 — Result and contribution differ.** `response.backend.id` is the result backend for a
document. A page, field or merged value may have additional contributors. `result_documents` has one
final backend per successful document and sums to the successful population;
`contributor_documents` can exceed it across backends. Both are published as counts because the
distinction is invisible from a single number.

**RS-6 — Reuse is visible and free of double billing.** Cache and Ledger replay are delivery modes,
not live dispatches. Invocation cost counts newly reported work only. Historical usage on a reused
payload remains in the response `usage` and is never silently relabeled as new spend (AC-8).

**RS-7 — Unknown is not zero.** Missing provider cost, unconfirmed cancellation, unmeasured duration
and unavailable signals are null or explicitly uncovered. Numeric zero is reserved for a measured
zero or a denominator of no applicable live dispatches (AC-9).

**RS-8 — Wall time and work time are different.** Wall duration covers the outer invocation and is
measured by the finalizer. Per-attempt durations are measured per attempt and may sum above wall
time under parallel execution. Neither is fabricated for a cache read or a missing timer, and the
block publishes how many live attempts were timed so a consumer knows how complete its own sum is
(AC-9, amended).

**RS-9 — Content-minimal and bounded.** Stats carries backend ids, controlled codes, booleans,
counts, scalar evidence and structural references. It never carries document text, credentials,
signed URLs, raw provider errors, provider job ids or model rationale. Its size follows documents
and attempts, not output bytes (AC-12, AC-14).

**RS-10 — Invocation shape is additive; presentation is optional.** Existing calls, default CLI
stderr, exit codes and HTTP statuses stay as they are. A new response and batch schema version
carries the optional block. *Amended in revision 2:* v0.1 changes no failure-path bytes on any
surface; a machine-readable CLI failure mode is a separate proposal (AC-1, AC-10 amended).

**RS-11 — Stats never decides.** Capture observes routing and strategy decisions. It cannot widen
the compliance set, reorder a plan, retry a backend, or make an adaptive choice from prior runs.

**RS-12 — Compare stays pure.** A compare operation over saved artifacts creates no stats and no
dispatch. CLI fan-out has independent child execution facts; if a future acquisition summary is
needed it belongs to the CLI surface, not `comparison` (AC-13).

**RS-13 — Unknown future roles remain representable.** v0.1 fixes the roles the engine emits today,
but consumers branch on stable structural fields (`sent`, `delivery`, `outcome`, `disposition`) and
tolerate a future intent, translation or decider role without guessing its meaning.

## 5. Vocabulary

| Term | Meaning | Not to confuse with |
|---|---|---|
| outer invocation | One call producing a response, batch result, terminal job artifact, or Python failure carrier | a backend submit, a poll, a nested batch item |
| attempt | A reached router/strategy branch or rung, including a structured skip | a live dispatch; an attempt can have `sent: false` |
| dispatch | One real adapter submit boundary; the billing/accounting boundary | a poll, a cache read, a compliance drop |
| dispatched document | One document included in one live dispatch; a document in a native bulk call counts once | number of submit calls |
| result backend | `response.backend.id` for a successful document | every backend that contributed a page or field |
| contributor | A backend supplying any accepted output for a document, page or field | the sole final result backend |
| transition | A serial move after a backend was attempted (A → B after a retryable failure) | initial selection or a race winner |
| delivery | `live`, `cache` or `ledger_replay` — the fact's source | backend wait mode |
| group | A set of attempts launched together, joined by `group_ref` | a serial fallback chain |
| stats | The public analytical projection | the internal fact set, Trace, or Ledger |

No new public `run_id` is coined. Ledger's existing id remains a durable resume identity; callers
needing correlation wrap their own artifact or use their own request id.

## 6. Architecture and ownership

```text
authoritative boundaries
  router plan / compliance     executor submit + completion     strategy branch/race/merge
  batch intake + item result   async submit/completion          cache or Ledger replay
                 \                     |                     /
                  -> immutable ExecutionFact stream (one outer scope)
                       |             |                 |
                 orchestration   stats v0.1       optional Ledger journal
                       |             |                 |
             response/batch/job/error carrier   pure stats projections
```

The implementation seam is an internal `ExecutionFact` sink, **not** a public package named
`openreading.stats` — that would collide with the top-level `openreading.stats(...)` function.
`openreading.statistics` or `openreading.observability` are implementation choices for the build
plan.

Facts are emitted at these boundaries:

1. **Plan:** selection mode and the backends dropped before any branch was reached.
2. **Attempt:** a branch or rung is reached, skipped, failed, or joins a group.
3. **Dispatch:** adapter submit or native bulk submit starts; delivery and document membership
   attach here.
4. **Completion:** normalized success, typed failure, unknown cancellation, cache reuse or Ledger
   replay closes the attempt.
5. **Outer finalizer:** item outcomes and wall time close the root scope and produce one stats
   block, even on a typed failure.

Revision 1 listed a sixth **Decision** boundary. The facts it carried are folded into the attempt
row in v0.1 (`role`, `from_backend`, `reason_code`, `group_ref`); the boundary itself returns in
v0.2 when gate, judge and decider evidence is published.

The first implementation must migrate strategy Trace emission to this sink rather than append
independent stats facts beside it. With Ledger armed, journal sequence and replay flag travel on the
fact; without Ledger, facts live only until the outer carrier is finalized. This resolves the
existing Trace/journal drift instead of hiding it (AC-2) — and it is the riskiest change in the
plan, which is why §13 gates it alone.

## 7. Public `stats` contract v0.1

The proposed shape. Every object is content-free and `additionalProperties: false` at the schema
boundary except where §7.4 marks a value forward-open.

```json
{
  "schema_version": "0.1",
  "scope": "batch",
  "selection_mode": "strategy",
  "execution_mode": "platform_batch",
  "state": "partial",
  "documents": {"total": 3, "succeeded": 2, "failed": 1, "skipped": 0},
  "totals": {
    "attempts": 4,
    "live_dispatches": 3,
    "dispatched_documents": 4,
    "cache_hits": 0,
    "ledger_replays": 0,
    "backend_transitions": 1,
    "parallel_groups": 1
  },
  "timing": {
    "wall_duration_ms": 842,
    "live_attempts": 3,
    "timed_live_attempts": 3
  },
  "cost": {
    "known_new_reported_usd": 0.031,
    "covered_live_dispatches": 2,
    "coverage": "partial"
  },
  "backends": [
    {
      "backend_id": "open-ocr",
      "attempts": 2,
      "live_dispatches": 2,
      "dispatched_documents": 2,
      "reached_documents": 2,
      "successful_completions": 1,
      "failed_completions": 1,
      "result_documents": 1,
      "contributor_documents": 1
    },
    {
      "backend_id": "reducto",
      "attempts": 2,
      "live_dispatches": 1,
      "dispatched_documents": 2,
      "reached_documents": 2,
      "successful_completions": 1,
      "failed_completions": 0,
      "result_documents": 1,
      "contributor_documents": 1
    }
  ],
  "attempts": [
    {
      "sequence": 1,
      "document_indexes": [0],
      "backend_id": "open-ocr",
      "node_ref": "n1",
      "role": "primary",
      "delivery": "live",
      "sent": true,
      "outcome": "failed",
      "disposition": "fallback_source",
      "duration_ms": 210,
      "new_reported_cost_usd": null,
      "reason_code": "retryable_error"
    },
    {
      "sequence": 2,
      "document_indexes": [0],
      "backend_id": "reducto",
      "node_ref": "n2",
      "role": "fallback",
      "from_backend": "open-ocr",
      "delivery": "live",
      "sent": true,
      "outcome": "succeeded",
      "disposition": "result",
      "duration_ms": 392,
      "new_reported_cost_usd": 0.031,
      "reason_code": "fallback_after_retryable_error"
    }
  ]
}
```

### 7.1 Top-level fields

- `schema_version` — the independent `run-stats` version, starting at `0.1`.
- `scope` — `document` or `batch`. Async is a carrier, not a scope.
- `selection_mode` — `named`, `auto` or `strategy`; forward-open for a future intent layer.
- `execution_mode` — `inline`, `job`, `platform_batch`, `native_batch` or `mixed`.
- `state` — mirrors the terminal artifact: `succeeded`, `partial` or `failed`.
- `documents` — the outer input population. An intake skip belongs here; a compliance drop does not
  remove the document from the population.
- `totals` — invocation counters. `backend_transitions` counts serial moves only (§7.4).
- `timing` — `wall_duration_ms` is measured by the finalizer. `live_attempts` and
  `timed_live_attempts` say how complete a consumer's own sum of per-attempt `duration_ms` can be.
  No duration sum is published (RS-8).
- `cost` — `known_new_reported_usd` sums known dollars from live dispatches in *this* invocation.
  `covered_live_dispatches` over `totals.live_dispatches` is the coverage fraction; `coverage` is
  `complete`, `partial`, `none` or `not_applicable`. `infra_only` work is uncovered, not zero
  dollars. This is the one derived sum v0.1 publishes, because a consumer's own summation error
  here is a billing misstatement (§0).

### 7.2 Backend rows

Counts only. A backend dropped in planning before any branch was reached does **not** get a row in
v0.1 — a row implies it was reached, and the drop reason has no carrier until `decisions[]` lands in
v0.2. Until then the existing compliance-drop warnings remain the record, and §10 tracks the gap.

For a named backend refused by the compliance gate before a branch: no row. For a reached strategy
branch missing credentials: a row with `attempts: 1`, `live_dispatches: 0`, and an attempt with
`sent: false`.

### 7.3 The measure registry

Not published as data. This table is the definition a renderer, an agent or a hosted consumer
computes against; the numbers on both sides of every fraction are already in the block.

| Measure | Numerator | Denominator | Can exceed 100% across backends? |
|---|---|---|---|
| `live_dispatches` | `backends[].live_dispatches` | `totals.live_dispatches` | no; sums to 1 |
| `dispatched_documents` | `backends[].dispatched_documents` | `totals.dispatched_documents` | no; sums to 1 |
| `document_reach` | `backends[].reached_documents` | `documents.total - documents.skipped` | yes, with race or fan-out |
| `result_documents` | `backends[].result_documents` | `documents.succeeded` | no; sums to 1 |
| `contributor_documents` | `backends[].contributor_documents` | `documents.succeeded` | yes, with merge or page composition |

`race_win_rate` is deferred with the group object (§10): its denominator is per-backend group
participation, which v0.1 does not count.

### 7.4 Attempts

Compact and content-free. Required: `sequence`, `backend_id`, `delivery`, `sent`, `outcome`,
`disposition`, `reason_code`. Present when they explain a batch or strategy path:
`document_indexes`, `node_ref`, `role`, `from_backend`, `group_ref`, `duration_ms`,
`new_reported_cost_usd`.

`sent` is the only safe dispatch test. A skipped attempt has `sent: false`; a live submit that later
fails normalization has `sent: true` and `outcome: failed`.

`role` — `primary`, `fallback`, `race`, `best`, `merge`, `page`, `shadow`, `judge`, `decider`.
Forward-open for `intent` and `translate`. Each mirrors behaviour the engine emits today
(`strategies/trace.py:15-31`, `types/response.py:66`).

`delivery` — `live`, `cache`, `ledger_replay`.

`outcome` — `succeeded`, `failed`, `skipped`, `unknown`.

`disposition` — `result`, `fallback_source`, `cache_result`, `replayed`, `skipped`, `discarded`,
`cancel_requested`, `drained`, `unknown`. For a race loser whose provider may still have completed
or billed, use `outcome: unknown` with `cancel_requested` (or `drained` when completion is
observed). Never `cancelled` unless the adapter proves it.

`from_backend` is set only on an attempt that is a serial move from a previously attempted backend,
and exactly those attempts increment `totals.backend_transitions`. An initial route to A sets
neither. A race selecting A from A/B sets neither — it is a parallel selection, not a fallback.

`group_ref` is an opaque string shared by attempts launched together; `totals.parallel_groups` counts
distinct values. The group's kind is readable from its members' `role`, each loser's fate from its
`disposition`. `group_ref` never implies that a cancellation stopped remote billing.

`new_reported_cost_usd` is populated only for cost reported by a live dispatch in this invocation.
Null for unknown, infra-only, replay, cache or unpriced work. It never replaces the response's
historical `usage.cost_usd`.

`reason_code` is a controlled registry that grows by stats schema version. Provider job ids and raw
provider errors never enter it.

## 8. Aggregation semantics and edge cases

### 8.1 Outer batch aggregation

The batch finalizer receives child facts before it strips nested stats from child response payloads.
The root block owns the aggregate, and every child attempt appears in the root `attempts[]` carrying
its `document_indexes`. A platform batch has one child dispatch per non-skipped document; a native
batch has one `submit_many` dispatch whose `document_indexes` cover every document the adapter
received. Child results keep their ordinary response shape inside `items[]`, but root stats is
authoritative.

For ten inputs with one intake skip, seven successes and two failures:

| execution path | documents | live dispatches | dispatched documents |
|---|---:|---:|---:|
| native bulk | 10 total / 7 ok / 2 failed / 1 skipped | 1 | 9 |
| platform fan-out | 10 total / 7 ok / 2 failed / 1 skipped | 9 | 9 |

Both report `result_documents` over seven successful documents — never seven over ten.

### 8.2 Cache and Ledger replay

A cache hit is a new outer invocation with `cache_hits: 1`, `live_dispatches: 0` and newly reported
cost zero. The existing cache already helps here: `router/executor.py:243` stores a deep copy of the
adapter's response *before* the request's own trail warnings are added, so the payload is already
separated from that invocation's narrative. The payload's historical `usage` may still describe the
original backend work; v0.1 must not relabel it as new spend.

A Ledger replay marks reused attempts `delivery: ledger_replay`, `sent: false`, and
`ledger_replays > 0`. A completed replay has zero new live dispatches and zero new reported cost;
route facts are folded from the journal and are not re-timed. A mixed resume carries both replayed
and live attempts. Logical path facts stay distinct from new invocation activity without inventing a
second run identity.

### 8.3 Refusal versus post-dispatch failure

Compliance or missing credentials can fail before a submit: `live_dispatches: 0` and a refusal or
skip code. If submit succeeded and normalization failed, the same backend has `sent: true`,
`live_dispatches: 1` and a failed completion. This is the minimum honest spend boundary and is
tested for the Python, HTTP, async and batch carriers (AC-15).

### 8.4 Timing and cost coverage

`timing.wall_duration_ms` is measured by the outer finalizer. `live_attempts` and
`timed_live_attempts` bound how complete a consumer's own duration sum is; under parallel execution
that sum may legitimately exceed wall time.

`cost.known_new_reported_usd` sums known dollars only. `covered_live_dispatches` against
`totals.live_dispatches` is mandatory. A race loser with unconfirmed cancellation contributes no
known cost and is not silently treated as free.

## 9. Surface contracts

### 9.1 Existing parse/run calls

Successful direct, auto and strategy responses gain `stats` beside `orchestration`. This requires a
response schema successor (`response.v0.4`) and a matching Pydantic field. `route` without `--run`
remains a plan and has no stats; `route --run` returns the normal executed response with stats.

### 9.2 Batch and async jobs

Batch result gets `batch-result.v0.2` with root `stats`. Nested child response stats are suppressed
from the serialized batch artifact after aggregation, enforcing RS-1.

The server's existing job record remains the temporary lifecycle holder. POST submission creates the
fact scope; the terminal GET returns one stats block beside `response` or `error`. DELETE removes the
job record. No second store is introduced, and an evicted job has no retrievable stats — the honest
consequence of the no-history boundary. v0.1 does not account for polls (§10).

### 9.3 CLI

One read-only command:

```console
openreading stats ARTIFACT.json
openreading stats - --format table
```

It accepts a response, batch result, terminal job artifact, or bare stats object. Default output is
canonical JSON; `--format table` is human-only and is the only place a percentage appears, always
beside its fraction and measure name (RS-4). It validates the embedded block and never runs a
backend. A legacy artifact without stats returns `stats_unavailable` rather than guessing from
warnings or orchestration. Exit ladder: 0 success, 2 usage, 3 unreadable/invalid/unsupported
artifact, 1 unexpected.

Executing commands keep their current failure behaviour unchanged in v0.1: diagnostic stderr, empty
stdout, established exit code.

### 9.4 Python

Re-export `stats(artifact)` at the top level, implemented in a non-conflicting internal module. It
accepts a response, batch result, terminal job artifact, or an already-extracted stats mapping, and
returns the typed model or a stable `StatsUnavailable`. Pure; it takes no backend, credential or
execution argument.

Existing exceptions gain an optional `stats` attribute without changing their classes or messages.
The finalizer attaches it before the exception crosses the API boundary. A pre-intake validation
error has no stats because no execution scope exists; an execution failure after a valid request
does.

### 9.5 HTTP

No new endpoint. Execution failures return existing status codes with `{"error": {...}, "stats":
{...}}` beside the error, and successful responses carry the block as in §9.1. A client wanting the
bare block reads `.stats` from an artifact it already holds.

### 9.6 Future agent surfaces

Future MCP tools return this exact block or invoke the pure projector; they must not invent a receipt
shape or session counters. A future intent, translation or decider event uses the forward-open
`selection_mode` and `role` values while keeping the v0.1 invariants.

## 10. Deferral register (v0.2 candidates)

Cutting a field is only honest if the gap it leaves is written down. Each line names what a consumer
cannot answer from v0.1 and what would restore it.

| Deferred | Unanswerable in v0.1 | Restored by |
|---|---|---|
| `decisions[]` with `kind`, `decided_by`, `candidates`, scalar `evidence` | why a gate fired, what a judge or decider chose, what the observed value and threshold were | a decision array; the fact boundary already exists in §6 |
| plan-stage drops (`eligible_documents`, dropped rows) | which backends were eligible but never reached, and why | a decision row of kind `route` plus a backend row flag |
| the parallel group object | `race_win_rate`, and per-group selected/loser tables without walking `group_ref` | a `parallel_groups[]` array keyed by the same `group_ref` |
| async poll detail | how many polls a job took, requested wait mode vs actual completion source, cancellation disposition | the AC-6 field set, moved to v0.2 |
| `--error-format json` | a machine-readable failed **CLI** call (Python and HTTP failures already carry stats in v0.1) | its own proposal against the CLI failure contract |
| `POST /v1/stats` | nothing a client holding the artifact cannot already read | not planned; reopen only with a consumer that does not hold the artifact |
| router numeric scores | why the router ranked A above B | a later, justified schema revision; scores are discarded internal state today |

## 11. Schema, compatibility and privacy

The build adds `run-stats.v0.1.json` to the schema manifest, then `response.v0.4` and
`batch-result.v0.2` referencing it. Independent versioning lets a stats-only consumer evolve without
pretending the whole response payload changed. Response and batch envelopes stay forward-tolerant
for the additive field; the nested stats model is strict enough to catch construction errors.

The block contains no document bytes, extracted text, field values, filenames, URLs, credentials,
provider job ids, raw errors or free-form rationale. Backend ids, stable codes, node references,
document indexes and opaque group refs are permitted. Error messages stay on the existing error
channels, redacted by existing machinery; they are never copied into stats.

"No storage" means no independent core-owned stats history. A caller who saves a response saves its
stats, and an armed Ledger may retain overlapping facts by design. The server's in-memory job record
and result cache are existing bounded lifecycle mechanisms, not a stats query service.

## 12. Rejected alternatives

| Alternative | Rejection |
|---|---|
| Add more `fallback_used` warnings | Prose is not a stable machine contract and cannot express races, native batches, cost coverage, or cache/replay. |
| Reuse `orchestration` as analytics | Strategy-only, permissive, detailed, incomplete on failures; changing it breaks its debug/replay audience and still leaves direct and auto paths uncovered. |
| Make Ledger mandatory | Turns an ordinary read into durable storage, excludes direct and native paths, violates the lightweight requirement. |
| Derive stats from Ledger only | Most runs have no Ledger; deriving from Trace alone misses direct, cache, cancellation and common provenance. |
| Add a GET endpoint by id | An id implies retention, lookup, eviction, authentication and multi-worker state this feature does not own. |
| Add Prometheus/OTel counters | Operational metrics, not a per-artifact account; they need process and exporter lifecycle decisions. |
| Make capture opt-in | Callers then need a new flag exactly when they most need an explanation, and paths drift when one caller forgets it. |
| Publish every router score and signal | Scores are internal and discarded; exposing them creates a fragile tuning contract and risks sensitive evidence. |
| Put full attempt traces in every batch child | Duplicates root facts and grows with nested artifacts; root stats with item indexes is the single aggregation boundary. |
| **Publish `shares[]` as data** *(new in rev 2)* | Every row restates two integers already in the block, so a validator must now check the block against itself, and a disagreement between a share and its own counts has no correct resolution. The measure registry (§7.3) fixes the denominators without shipping a second copy of the numbers. |
| **`POST /v1/stats`** *(new in rev 2)* | To receive the small answer the caller must transmit the whole artifact, including the payload it wanted to avoid sending. It adds an auth boundary and a body limit to a field read. |
| **Ship async poll accounting in v0.1** *(new in rev 2)* | Six closed sets and four counters on the surface with the fewest consumers, priced at a schema version to withdraw. A terminal job still gets a block; only poll accounting waits. |

## 13. Implementation seams (future build only)

No code is requested on this branch. Staged, with the risky step isolated:

1. **Contract.** `run-stats.v0.1.json`, response and batch successors, typed models, the reason-code
   registry, fixture examples (AC-1, AC-3 amended, AC-14). Per `AGENTS.md`, each successor also
   needs its `*_SCHEMA_FILE` constant, a `src/openreading/schemas/README.md` manifest row, an
   `openreading.types` default bump and a `CHANGELOG.md` line; `tests/test_schema_evolution.py`
   byte-pins the old versions.
2. **Common fact sink, direct and plain paths only.** Introduce the request-scoped sink; emit from
   the router plan and `router/executor.py`. Strategy paths still use Trace unchanged. This is
   independently shippable and independently verifiable (AC-2, AC-15).
3. **Trace migration — its own milestone, its own gate.** Move `strategies/trace.py` emission onto
   the sink. Trace feeds `explain`, the Ledger journal and replay determinism, so the existing
   replay and explain suites are the gate, and they must pass **before** any strategy field is
   published. This is the single highest-risk change in the plan; revision 1 carried it as one
   bullet inside a larger phase, and that understated it.
4. **Reuse and outer finalization.** Cache and Ledger replay deliveries; direct/auto/strategy and
   native/platform batch aggregation; root failure carriers; nested-stats suppression (AC-5, AC-7,
   AC-8, AC-10 amended).
5. **Pure projections.** Python `stats`, CLI `stats` with `--format table`. Both adapter-free
   (AC-11 amended).
6. **Ledger alignment.** Make overlapping fact/journal fields comparable; prove replay adds no
   dispatch and no cost (AC-2, AC-8).

The build prompt does not begin until §15 is resolved. Phase 1 writes the §14 fixtures failing
before capture code changes.

## 14. Offline proof matrix

Trimmed to what v0.1 claims. Every row is offline; none needs a key or the network (`AGENTS.md`
golden rule).

| Proof | Expected invariant |
|---|---|
| direct named success | one attempt, one live dispatch, one result document |
| auto A failure → B success | two live dispatches, one transition, B's attempt carries `from_backend: A` |
| compliance drop | dropped backend has no row, no attempt, no dispatch |
| missing credentials after branch reached | row present, attempt `sent: false`, zero dispatches |
| normalize failure after submit | one live dispatch and a failed completion |
| race winner plus unconfirmed loser cancel | two live dispatches sharing one `group_ref`; loser `unknown`/`cancel_requested`, cost uncovered, never free |
| native versus platform batch | 10/7/2/1 outcomes; native one dispatch, platform nine; both `result_documents` over 7 |
| cache second invocation | zero live dispatches, one cache hit, no copied first-run timing or cost |
| Ledger complete replay | zero submits, replay deliveries, zero new cost, no retiming |
| mixed Ledger resume | replayed and live attempts coexist without double counting |
| failed Python call | stats attached to the existing exception class, class and message unchanged |
| failed HTTP or terminal job | stats beside the existing error; status and state unchanged |
| failed or partial batch | root stats present; nested child facts counted once |
| legacy artifact | stable `stats_unavailable`; no inference from warnings or orchestration |
| compare over saved responses | zero dispatch, no stats block on the report |
| measure registry | for every fixture, each §7.3 fraction is computable from the block and a `0/0` case renders unavailable |
| no published ratio | byte scan finds no `%`, no float ratio field, in any canonical stats JSON |
| secret and content scan | no planted document, key, URL, job id, raw error or rationale survives |
| projector purity | CLI and Python projectors make zero adapter calls and write no file |
| determinism | counts and attempt sequence are deterministic where facts are logically equal; completion order is explicit where it is the decision |

## 15. Maintainer decisions

Revision 1 asked six questions. Five now have a proposed answer carried by this revision; each is
still a decision the maintainer owns.

1. **Automatic additive `stats` plus both schema successors** — proposed: accept. Opt-in is wrong for
   the reason revision 1 gave: a caller needs the explanation exactly when they forgot the flag.
2. **`--error-format json`** — proposed: exclude from v0.1, propose separately (§10). Python and HTTP
   failures still carry stats, so no surface is left without a machine-readable failure account
   except the CLI.
3. **One root block, nested suppressed, item indexes as correlation** — proposed: accept, no new run
   id.
4. **Omit router scores and gate/rationale text from v0.1** — proposed: accept, and extend it: omit
   `decisions[]` entirely (§10), since the serial-switch reason survives on the attempt row.
5. **Ledger as canonical durable source before build, or one staged milestone** — proposed: one
   staged sequence, but with the Trace migration promoted to its **own** gated milestone (§13
   phase 3) rather than folded into a larger phase. Two projection paths landing separately is how
   the current Trace/journal drift happened.
6. **ProductSpec SM-1..SM-4 measurement method** — no phone-home in open core. This is a company-repo
   question, not an engine design decision; the engine's only contribution is that the block is
   present by default and therefore observable wherever a hosted product already sees artifacts.

**New in revision 2 — ProductSpec amendments, applied in that spec's revision 2:**

- **AC-3** — reword from "every published backend share names its measure and carries its numerator
  and denominator" to: *canonical JSON publishes counts only and contains no bare or pre-rounded
  percentage; every measure a renderer computes is defined with a fixed numerator and denominator,
  and a zero denominator renders as unavailable rather than zero percent.* The failure it guards is
  unchanged; the mechanism moves from data to registry.
- **AC-6** — move to v0.2. v0.1 gives a terminal job artifact a stats block but does not count polls.
- **AC-9** — amend "overlapping attempt-duration sum" to per-attempt durations plus the timed/total
  attempt counts. Cost keeps its covered and total counts unchanged.
- **AC-10** — drop the JSON error mode clause; the Python, HTTP, terminal-job and batch clauses
  stand unchanged.
- **AC-11** — drop `POST /v1/stats` from the equivalence set. The embedded block, `openreading
  stats` and the Python function must still produce identical canonical JSON with zero adapter calls
  and no writes.
- **AC-4** — the source, destination and reason of a serial move now land on the destination
  attempt rather than in a decision row; "decision kind" becomes the attempt's `role`. The clause
  guarding against miscounting an initial selection or a race winner as a fallback is unchanged.
- **AC-14** — the projection is bounded by documents and attempts. Decisions and parallel groups
  leave the bound because they leave v0.1.
- **AC-5** — no amendment needed. `group_ref` plus per-attempt `role` and `disposition` identify
  participants and selected outcomes, and the "never free, never stopped" clause is fully kept.

The spec's related-artifact anchors `#7-aggregation-semantics` and `#11-proof-matrix` were stale
from revision 1 — no such headings exist in either revision. They now point at
`#8-aggregation-semantics-and-edge-cases` and `#14-offline-proof-matrix`.

## 16. Scope-cut ledger

This proposal is complete when the maintainer can review the ProductSpec amendments and answer §15.
It contains no build prompt, schema file, Python model, CLI parser change, server route, persistence
layer or test. Those are implementation artifacts for a later accepted revision and must cite
ProductSpec AC ids rather than silently turning this design into code.

Revision 2 changes no law, adds no capability, and removes no honesty guarantee. It removes
published surface — the part of a v0.1 that costs a schema version to take back.
