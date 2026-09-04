# Run Stats and Routing Analytics

**Status:** DESIGN — proposal for review; no implementation is in scope for this branch.
**Product intent:** `product/specs/run-stats-analytics.product-spec.md` (revision 1).
**Reviewed against:** adjacent `openreading-core` working tree at `1a1ed64`, on branch
`design/run-stats-analytics-proposal`, 2026-09-03. The company repo's `core/` submodule is not
moved by this proposal.
**Decision requested:** approve or change the artifact boundary, the no-store surface, the schema
shape, and the failure delivery described below before a build prompt is written.

## 1. Recommendation in one page

Add a versioned `stats` object to each newly executed **outer** artifact: a single response, a
batch result, or a terminal async-job result/error. It is captured automatically and kept in
memory until that artifact is returned. It is not a new database, event file, process counter, or
lookup handle.

The object is a projection of one internal set of immutable execution facts. Direct execution,
plain fallback, strategy execution, batch orchestration, async driving, cache reuse, and Ledger
replay all emit facts at their authoritative boundaries. Strategy `orchestration` and optional
Ledger records project from the same facts; stats never tries to reverse-engineer them from warning
prose or from a permissive serialized trace.

The normal call stays normal:

```console
openreading parse invoice.pdf --strategy fast > result.json
```

The result gains an additive `stats` block. A caller that wants only the compact block can run
`openreading stats result.json`, call `openreading.stats(result)`, or post the supplied artifact to
`POST /v1/stats`. All three are pure projections and make no backend call. A human table is a
renderer over the same canonical JSON; it is never the contract.

The block answers “what happened in this invocation?” rather than “what has this installation done
over time?” Consequently there is no `GET /v1/stats/{id}` and no new `run_id`. If Ledger is armed,
its existing run identity remains its resume identity. A hosted product may wrap or ingest the
block and add identity, retention, search, and cross-run analytics outside open core.

## 2. The ask and the boundary

The desired outcome is a caller-visible account of backend allocation and strategy power:

- which backends were eligible, attempted, and actually sent work;
- the share of live calls and documents reached by each backend;
- every serial switch and its reason;
- race, best, merge, shadow, judge, page, and async outcomes;
- time and cost with honest unknowns; and
- a representation that a CLI user, Python program, HTTP client, or agent can consume without
  parsing prose or receiving document content.

This proposal deliberately does **not** turn that account into operational telemetry. A per-run
fact is not a fleet counter. A saved response is not a core-owned history store. A comparison report
is not an execution receipt. These distinctions are the guardrails for ProductSpec AC-1, AC-8,
AC-11, and AC-13.

## 3. Audit: what exists today

The audit found useful ingredients, but no unified stats schema, aggregator, CLI command, Python
function, or HTTP endpoint.

| Surface | Evidence already present | Why it is not run stats |
|---|---|---|
| Normalized response | `types/response.py:33-40` has backend identity; `:81-90` has usage, cost, basis, and optional duration; `:121-145` has optional orchestration | No run grouping, attempt trail, dispatch distinction, or stable stats schema. `response.v0.3.json:627-630` leaves orchestration permissive. |
| Static router | `router.py:73-104` records chosen backend, fallbacks, drops, and terminal reason; `:173-222` computes scores | It is a plan, not execution. Scores are discarded and fallback reordering has no decision event. |
| Plain auto/fallback | `router/executor.py:69-79` has a local attempt; `:129-140` turns history into `fallback_used` warning prose; `:143-250` walks the chain | Success loses structured history, timing, cost, and switch decisions. Cache replay is mixed into the returned response. |
| Strategy trace | `strategies/trace.py:14-31` has attempt categories; `:60-111` has duration/cost/gates; `:130-187` builds orchestration | It exists only on strategy paths, failed walks use a different carrier, some decisions and durations are absent, and `fallback_depth` counts non-success attempts rather than backend transitions. |
| Batch | `batch/types.py:65-76` and `batch/runner.py:103-130` summarize outcomes and final backend tally | `summary.backends` counts successful final responses, not sends, losers, retries, or native-vs-platform dispatch boundaries. |
| Ledger | `ledger/header.py`, `ledger/step.py`, and `ledger/inline.py` can persist run/step facts when armed | It is optional, durable, strategy-biased, and designed for replay/resume. The package itself records that Trace and journal can drift (`ledger/__init__.py:91-97`). It is not a lightweight default stats channel. |
| Async jobs | `types/job.py:14-30` and server job records retain state, vendor id, timestamps, attempts, and cost hints | Poll count, completion source, cancellation certainty, and a terminal stats carrier are absent. Poll “attempts” means consecutive faults, not total calls. |
| Server | `server/app.py:805-865` centralizes error mapping; `server/README.md` documents no metrics/tracing surface | There is no request-scoped stats object and no `/stats` operation. |
| Compare | Compare is pure over supplied artifacts; CLI fan-out can execute direct parses | Acquisition and comparison are separate. A comparison report must not claim it dispatched a backend. |

The conclusion is incremental: preserve these facts and their detailed consumers, add one common
fact boundary and a stable analytical projection, then expose that projection consistently.

## 4. Design laws

These laws are the review contract. Each names the failure it prevents.

**RS-1 — One outer artifact, one stats block.** A document response has one block; a platform or
native batch has one root block; a terminal job artifact has one block. Child facts carry an item
index inside the root and are not recursively counted from nested response blocks. This prevents a
platform batch from counting each child once and the root a second time (AC-1, AC-7).

**RS-2 — Facts are singular; projections are plural.** There is one request-scoped immutable fact
set. `stats`, strategy `orchestration`, and an armed Ledger consume it; stats is not a third
mutable log. When Ledger is the durable source, a replay folds the recorded facts instead of
pretending it performed a fresh dispatch (AC-2, AC-8).

**RS-3 — A dispatch is a real submit boundary.** `sent: true` means an adapter `submit` or
`submit_many` was invoked for this invocation. Compliance drops, missing credentials, strategy
pruning, cache hits, Ledger replay, and progress polls are not dispatches. A native `submit_many`
over nine documents is one live dispatch and nine dispatched documents; a platform batch doing
nine child submits is nine live dispatches (AC-7).

**RS-4 — Shares name their denominator.** Canonical JSON publishes counts, not a naked percentage.
Every share has a fixed measure, numerator, and denominator. A renderer may show `50.0%` only next
to `1/2` and its measure. A zero denominator is undefined, never zero percent (AC-3).

**RS-5 — Result and contribution differ.** `response.backend.id` is the result backend for a
document. A page, field, or merged value may have additional contributors. Result shares have one
final backend per successful document; contribution coverage can exceed 100% across backends.

**RS-6 — Reuse is visible and free of double billing.** Cache and Ledger replay are delivery modes,
not live dispatches. Invocation cost counts newly reported work only. Historical usage on a reused
payload remains in the response usage and is never silently relabeled as new spend (AC-8).

**RS-7 — Unknown is not zero.** Missing provider cost, unconfirmed cancellation, unmeasured
duration, and unavailable signals are null or explicitly uncovered. Numeric zero is reserved for a
measured zero or a denominator of no applicable live dispatches (AC-9).

**RS-8 — Wall time and work time are different.** Wall duration covers the outer invocation.
Measured live-attempt duration is a separate sum; parallel work may make it larger than wall time.
Neither is fabricated for a cache read or a missing timer (AC-9).

**RS-9 — Content-minimal and bounded.** Stats carries backend ids, controlled codes, booleans,
counts, scalar evidence, and structural references. It never carries document text, credentials,
signed URLs, raw provider errors, provider job ids, or model rationale. Its size follows documents,
attempts, decisions, and parallel groups, not output bytes (AC-12, AC-14).

**RS-10 — Invocation shape is additive; presentation is optional.** Existing calls, default CLI
stderr, exit codes, and HTTP statuses stay as they are. A new response/batch schema version carries
the optional block. Only an agent that needs a failed CLI artifact opts into JSON errors (AC-1,
AC-10).

**RS-11 — Stats never decides.** Capture observes routing and strategy decisions. It cannot widen
the compliance set, reorder a plan, retry a backend, or make an adaptive choice from prior runs.

**RS-12 — Compare stays pure.** A compare operation over saved artifacts creates no stats and no
dispatch. CLI fan-out has independent child execution facts; if a future acquisition summary is
needed, it belongs to the CLI surface, not `comparison` (AC-13).

**RS-13 — Unknown future roles remain representable.** Core v0.1 defines known roles and reason
codes, but consumers branch on stable structural fields (`sent`, `delivery`, `outcome`, and
measure) and tolerate a future intent, translation, or decider role without guessing its meaning.

## 5. Vocabulary

| Term | Meaning | Not to confuse with |
|---|---|---|
| outer invocation | One call that produces a response, batch result, terminal job result/error, or Python failure carrier | a backend submit, a poll, or a nested batch item |
| attempt | A reached router/strategy branch or rung, including a structured skip | a live dispatch; one attempt can have `sent: false` |
| dispatch | One real adapter submit boundary; the billing/accounting boundary | a poll, a cache read, or a compliance drop |
| dispatched document | One document included in one live dispatch; one document in a native bulk call counts once here | number of submit calls |
| result backend | `response.backend.id` for a successful document | every backend that contributed a page or field |
| contributor | A backend that supplied any accepted output for a document, page, or field | the sole final result backend |
| transition | A serial move after a backend was attempted, such as A → B after a retryable failure | initial route selection or a race winner |
| delivery | `live`, `cache`, or `ledger_replay` for the fact's source | backend wait mode |
| wait mode | `inline`, `poll`, or `webhook` used to complete an async backend job | whether the submit was live |
| completion source | `inline`, `poll`, `webhook`, `cache`, `ledger_replay`, or `none` | the requested wait mode |
| stats | Public analytical projection | the internal fact set, Trace, or Ledger |

No new public `run_id` is coined here. Ledger's existing id remains a durable resume identity;
callers needing correlation can wrap their own artifact or use their own request id.

## 6. Architecture and ownership

```text
authoritative boundaries
  router plan / compliance     executor submit + completion     strategy branch/race/merge
  batch intake + item result   async submit/poll/webhook         cache or Ledger replay
                 \                     |                     /
                  -> immutable ExecutionFact stream (one outer scope)
                       |             |                 |
                 orchestration   stats v0.1       optional Ledger journal
                       |             |                 |
             response/batch/job/error carrier   pure stats projections
```

The implementation seam is an internal `ExecutionFact` sink, not a new public package named
`openreading.stats` (that would collide with the top-level `openreading.stats(...)` function).
Names such as `openreading.statistics` or `openreading.observability` remain implementation
choices for the build plan.

Facts are emitted at these boundaries:

1. **Plan:** eligible and dropped backend ids, selection mode, and static route decisions.
2. **Attempt:** a branch/rung is reached, skipped, failed, or enters a parallel group.
3. **Dispatch:** adapter submit or native bulk submit starts; delivery, document membership, and
   wait mode are attached.
4. **Completion:** normalized success, typed failure, unknown cancellation, cache reuse, or Ledger
   replay closes the attempt.
5. **Decision:** fallback, gate, parallel selection, merge, page assignment, judge, or decider
   outcome is recorded with a controlled reason code and scalar evidence only.
6. **Outer finalizer:** item outcomes and wall time close the root scope and produce one stats
   block, even on a typed failure.

The first implementation must migrate strategy Trace emissions to this sink rather than append
independent stats facts beside them. With Ledger armed, its journal sequence and replay flag travel
on the fact; without Ledger, facts live only until the outer carrier is finalized. This resolves
the existing Trace/journal drift instead of hiding it (AC-2).

## 7. Public `stats` contract v0.1

The following is the proposed shape, not a schema file in this design-only change. Every object is
content-free and `additionalProperties: false` at the schema boundary unless explicitly marked as
a forward-open string.

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
    "parallel_groups": 1,
    "poll_calls": 0
  },
  "timing": {
    "wall_duration_ms": 842,
    "known_live_attempt_duration_sum_ms": 1160,
    "timed_live_attempts": 3,
    "live_attempts": 3
  },
  "cost": {
    "known_new_reported_usd": 0.031,
    "covered_live_dispatches": 2,
    "live_dispatches": 3,
    "coverage": "partial",
    "bases": ["billed", "infra_only"]
  },
  "backends": [
    {
      "backend_id": "open-ocr",
      "eligible_documents": 3,
      "attempts": 2,
      "live_dispatches": 2,
      "dispatched_documents": 2,
      "successful_completions": 1,
      "failed_completions": 1,
      "result_documents": 1,
      "contributor_documents": 1,
      "race_entries": 1,
      "race_wins": 0
    },
    {
      "backend_id": "reducto",
      "eligible_documents": 3,
      "attempts": 2,
      "live_dispatches": 1,
      "dispatched_documents": 2,
      "successful_completions": 1,
      "failed_completions": 0,
      "result_documents": 1,
      "contributor_documents": 1,
      "race_entries": 1,
      "race_wins": 1
    }
  ],
  "shares": [
    {"measure": "live_dispatches", "backend_id": "open-ocr", "numerator": 2, "denominator": 3},
    {"measure": "dispatched_documents", "backend_id": "reducto", "numerator": 2, "denominator": 4},
    {"measure": "document_reach", "backend_id": "open-ocr", "numerator": 2, "denominator": 3},
    {"measure": "result_documents", "backend_id": "reducto", "numerator": 1, "denominator": 2},
    {"measure": "contributor_documents", "backend_id": "open-ocr", "numerator": 1, "denominator": 2},
    {"measure": "race_win_rate", "backend_id": "reducto", "numerator": 1, "denominator": 1}
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
      "wait_mode": "inline",
      "completion_source": "inline",
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
      "delivery": "live",
      "sent": true,
      "wait_mode": "poll",
      "completion_source": "poll",
      "poll_calls": 3,
      "poll_faults": 0,
      "outcome": "succeeded",
      "disposition": "result",
      "duration_ms": 392,
      "new_reported_cost_usd": 0.031,
      "reason_code": "fallback_after_retryable_error"
    }
  ],
  "decisions": [
    {
      "sequence": 1,
      "node_ref": "n2",
      "kind": "fallback",
      "decided_by": "completion",
      "reason_code": "retryable_error",
      "from_backend": "open-ocr",
      "to_backends": ["reducto"],
      "changed_backend": true,
      "evidence": {"available": true}
    }
  ],
  "parallel_groups": []
}
```

### 7.1 Top-level fields

- `schema_version` is the independent `run-stats` version, starting at `0.1`.
- `scope` is `document` or `batch`. Async is a carrier, not a scope.
- `selection_mode` is known as `named`, `auto`, or `strategy`; it is a forward-open string for a
  future intent layer.
- `execution_mode` distinguishes `inline`, `job`, `platform_batch`, `native_batch`, or `mixed`.
- `state` mirrors the terminal artifact: `succeeded`, `partial`, or `failed`.
- `documents` counts the outer input population. An intake skip belongs here; a compliance drop
  does not remove the document from the population.

### 7.2 Backend rows and shares

Backend rows are counts, not percentages. A row can include a backend dropped during planning with
`eligible_documents: 0` and `dropped_documents` plus a reason in a decision; it must not imply it
was sent work. For a named backend rejected before dispatch, `attempts` is zero if the compliance
gate refused before a branch was reached. For a reached missing-credential strategy branch,
`attempts` is one, `sent` is false, and `live_dispatches` is zero.

The v0.1 measure meanings are fixed:

| Measure | Numerator | Denominator | Can exceed 100% across backends? |
|---|---|---|---|
| `live_dispatches` | live submit/submit-many calls for backend | all live dispatch calls | no; sums to 1 |
| `dispatched_documents` | backend-document placements in live submits | all backend-document placements | no; sums to 1 |
| `document_reach` | distinct non-skipped documents sent to backend | non-skipped documents | yes, with race/fan-out |
| `result_documents` | successful docs whose final response backend is backend | successful docs with a result backend | no; sums to 1 |
| `contributor_documents` | successful docs receiving accepted output from backend | successful docs | yes, with merge/page composition |
| `race_win_rate` | race groups won by backend | race groups in which backend participated | no per backend; rows have different denominators |

The JSON deliberately does not carry a rounded `ratio`. `openreading stats --format table` computes
and displays the percentage beside the exact fraction. Agents can divide integers themselves and
can detect an undefined `0/0` without trusting a rounded float.

### 7.3 Attempts

An attempt is compact and content-free. Required fields are sequence, backend, delivery, sent,
outcome, disposition, and a reason code; document indexes and node references are present when
needed to explain a batch or strategy path. `role` is a known-code string (`primary`, `fallback`,
`race`, `best`, `merge`, `page`, `shadow`, `judge`, `decider`) but forward-open for `intent` and
`translate`.

`sent` is the only safe dispatch test. A skipped attempt can have `sent: false`; a live submit that
later fails normalization has `sent: true` and `outcome: failed`. For a race loser whose provider
may still have completed or billed, use `outcome: unknown` and `disposition: cancel_requested` (or
`drained` when completion is observed); never use `cancelled` unless the adapter proves it.

`new_reported_cost_usd` is populated only for cost reported by a live dispatch in this invocation.
It is null for unknown, infra-only, replay, cache, or unpriced work. It is not a replacement for
the response's historical `usage.cost_usd`.

Async fields are optional: `wait_mode`, `completion_source`, `poll_calls`, `poll_faults`, and
`cancellation_disposition`. A poll is progress activity, not a dispatch. Provider job ids never
leave the job manager or stats block.

### 7.4 Decisions

Decision rows explain why the path changed without duplicating detailed strategy evidence. `kind`
is one of `route`, `fallback`, `gate`, `parallel_selection`, `merge`, `page_route`, `judge`, or
`decider`; `reason_code` is a controlled code registry that can grow by stats schema version.
`decided_by` is `router`, `engine`, `completion`, `llm`, `replay`, or `finalizer`.

`from_backend`, `to_backends`, `changed_backend`, and `candidates` make a switch or winner
machine-readable. `evidence` may contain only allowlisted scalar metrics such as an observed
confidence and threshold. It never includes a signal's document field name, free-form predicate,
LLM rationale, or provider response.

An initial route from no backend to A has `changed_backend: false`. A race selecting A from A/B
also has `changed_backend: false`; it is a parallel selection, not a serial fallback. A later A →
B fallback has `changed_backend: true` and increments `backend_transitions` once.

### 7.5 Parallel groups

Each group records `kind` (`race`, `best`, `merge`, or `shadow`), participant attempt sequences,
selected attempts, and each loser disposition. Possible dispositions include `completed`,
`cancelled_before_dispatch`, `cancel_requested`, `drained`, and `unknown`. The group never claims
that a cancellation stopped remote billing. A race winner's completion order is recorded in
`decided_by` and does not change the dispatch denominator.

## 8. Aggregation semantics and edge cases

### 8.1 Outer batch aggregation

The batch finalizer receives child facts before it strips nested stats from child response payloads.
The root block owns the aggregate. A platform batch has one child dispatch per non-skipped document;
a native batch has one `submit_many` dispatch whose `document_indexes` cover every document the
adapter received. Child results retain their ordinary response shape inside `items[]`, but root
stats is authoritative for the batch.

For ten inputs with one intake skip, seven successes, and two failures:

| execution path | documents | live dispatches | dispatched documents |
|---|---:|---:|---:|
| native bulk | 10 total / 7 ok / 2 failed / 1 skipped | 1 | 9 |
| platform fan-out | 10 total / 7 ok / 2 failed / 1 skipped | 9 | 9 |

Both report `result_documents` over seven successful documents, never seven over ten unless a
separate measure explicitly names all inputs as its denominator.

### 8.2 Cache and Ledger replay

A cache hit is a new outer invocation with `cache_hits: 1`, `live_dispatches: 0`, and newly
reported cost zero. The cache must store payload separately from invocation stats; it must not
return the first invocation's timing, fallback trail, or cost as if it happened now. The payload's
historical `usage` may still describe the original backend work.

A Ledger replay marks reused attempts `delivery: ledger_replay`, `sent: false`, and
`ledger_replays > 0`. A completed replay has zero new live dispatches and zero new reported cost;
the route/decision facts are folded from the journal and are not re-timed. A mixed resume can have
both replayed and live attempts. The result therefore distinguishes logical path facts from new
invocation activity without inventing a second run identity.

### 8.3 Refusal versus post-dispatch failure

Compliance or missing credentials can fail before a submit. The stats carrier says `live_dispatches:
0` and names the refusal/skip code. If submit succeeded and normalization failed, the same backend
has `sent:true`, `live_dispatches:1`, and a failed completion. This distinction is the minimum
honest spend boundary and is tested for Python, HTTP, async, and batch carriers.

### 8.4 Timing and cost coverage

`timing.known_live_attempt_duration_sum_ms` is the sum of measured live attempt durations, not a
promise of wall time. Its denominator is `live_attempts`; `timed_live_attempts` says how complete
the measurement is. `wall_duration_ms` is measured by the outer finalizer.

`cost.known_new_reported_usd` sums known dollars only. `covered_live_dispatches/live_dispatches`
is mandatory. `coverage` is `complete`, `partial`, `none`, or `not_applicable`; `infra_only` is a
basis, not a zero-dollar claim. A race loser with unconfirmed cancellation contributes no known
cost and is not silently treated as free.

## 9. Surface contracts

### 9.1 Existing parse/run calls

Successful direct, auto, and strategy responses gain `stats` beside `orchestration` and other
top-level fields. This requires a response schema successor (likely `response.v0.4`) and a matching
Pydantic field. Older readers remain forward-tolerant; strict consumers must adopt the new schema
version. `route` without `--run` remains a plan and has no stats. `route --run` returns the normal
executed response with stats.

### 9.2 Batch and async jobs

Batch result gets a successor (likely `batch-result.v0.2`) with root `stats`. Nested child response
stats are suppressed from the serialized batch artifact after aggregation to enforce RS-1.

The server's existing job record remains the temporary lifecycle holder. POST submission creates the
fact scope; polls append facts; terminal GET returns one stats block beside `response` or `error`.
DELETE removes the existing job record. No second stats store is introduced, and an evicted job has
no retrievable stats (which is the honest consequence of the no-history boundary).

### 9.3 CLI

Add a read-only command:

```console
openreading stats ARTIFACT.json
openreading stats - --format table
```

It accepts a response, batch result, terminal job artifact, or bare stats object. Default output is
canonical JSON; `--format table` is human-only. It validates the embedded block and never runs a
backend. A legacy artifact without stats returns `stats_unavailable` rather than guessing from
warnings or orchestration. Proposed exit ladder: 0 success, 2 usage, 3 unreadable/invalid/
unsupported artifact, 1 unexpected.

Executing commands keep current failure behavior by default: diagnostic stderr, empty stdout, and
the established exit code. An explicit `--error-format json` produces one JSON error carrier on
stdout with `error` and `stats`, suppressing prose for that invocation. It is needed for an agent
that must inspect failed CLI calls; it is not required on successful calls and does not alter the
default script contract.

### 9.4 Python

Re-export `stats(artifact)` at the top level, implemented in a non-conflicting internal module.
It accepts a response, batch result, terminal job artifact, or already extracted stats mapping and
returns the typed stats model or a stable `StatsUnavailable`/validation error. It is pure and does
not accept backend, credential, or execution arguments.

Existing exceptions gain an optional `stats` attribute without changing their classes or messages.
The finalizer attaches it before the exception crosses the API boundary. A pre-intake validation
error has no stats because no execution scope exists; an execution failure after a valid request
does.

### 9.5 HTTP

Add a stateless endpoint:

```http
POST /v1/stats
Content-Type: application/json

<response artifact, batch result, terminal job artifact, or bare stats block>
```

It returns the canonical bare stats block with `200`. Invalid or legacy unsupported artifacts
return `400` with `{"error":{"category":"stats_unavailable",...}}`. It uses the existing body
limit and authentication boundary, makes no adapter call, writes no file, and never accepts an id
to look up. HTTP execution failures return existing status codes with `{"error": {...},
"stats": {...}}` beside the error. This endpoint is useful when an agent wants a small answer from
a saved artifact without sending its document payload onward.

### 9.6 Future agent and intent surfaces

Future MCP tools should return this exact block or invoke the pure projector; they must not invent a
receipt shape or session counters. A future intent, translation, or decider event can use the
forward-open `selection_mode`, `role`, and `kind` values while keeping the v0.1 invariants. The
agent branches on counts and stable delivery/outcome fields; detailed orchestration remains an
optional follow-up read.

## 10. Worked examples

### 10.1 Fallback plus race in a three-document batch

Suppose document 0 goes A then B after a retryable A failure. Document 1 is raced against A and B,
and B wins while A completes too late. Document 2 succeeds directly on A. The correct accounting
is:

```text
live dispatches:       A=3, B=2, total=5       => A 3/5, B 2/5
dispatched documents:  A=3, B=2, total=5       => same here; a bulk call may differ
document reach:        A=3/3, B=2/3           => 100%, 66.7%; sum may exceed 100%
result documents:      A=1/3, B=2/3           => 33.3%, 66.7%; sums to 100%
serial transitions:    1 (A → B on document 0)
race wins:             B=1/1, A=0/1
```

If A's race cancellation is unconfirmed, A's attempt is `outcome: unknown`,
`disposition: cancel_requested`, and its cost is uncovered. The stats do not call it free.

### 10.2 Cache hit

The first auto invocation sends one document to A and reports `$0.02`. An identical second
invocation returns the cached payload:

```json
{
  "totals": {"attempts": 1, "live_dispatches": 0, "dispatched_documents": 0,
             "cache_hits": 1, "ledger_replays": 0},
  "cost": {"known_new_reported_usd": 0.0, "covered_live_dispatches": 0,
           "live_dispatches": 0, "coverage": "not_applicable"},
  "attempts": [{"backend_id": "A", "delivery": "cache", "sent": false,
                "outcome": "succeeded", "disposition": "cache_result"}]
}
```

The response's `usage.cost_usd` can still be `0.02` as historical payload usage. An agent must not
sum that field as the second invocation's new spend.

### 10.3 Race followed by resume

The original run launches A and B; A wins in 480 ms. B's provider cancellation is unconfirmed. The
original stats have two live dispatches, A's result, B's unknown loser, and attempt-duration sum
greater than wall time. A later resume reads both completed facts from Ledger, performs zero
submits, and reports `ledger_replays: 2`, `live_dispatches: 0`, and zero new reported cost. It
retains the original decision shape with `decided_by: replay`; it does not retime the vendor work.

### 10.4 Agent recipe

```text
1. Call parse/run normally.
2. Read `.stats.state` and `.stats.totals`.
3. If `.stats.totals.backend_transitions > 0`, inspect `.stats.decisions` for the reason code.
4. Use `.stats.shares[]` by `measure`; never infer a denominator from a rounded table.
5. Discard `document`, `typed_fields`, and `backend_raw` when only routing analytics are needed.
6. On a failed CLI call, retry the same command with `--error-format json` and branch on the
   structured `error` plus `stats`.
```

## 11. Schema, compatibility, and privacy

The build phase should add `run-stats.v0.1.json` to the schema manifest, then successor response
and batch schemas that reference it. The stats schema is independently versioned so a stats-only
consumer can evolve without pretending the entire response payload changed. The response and
batch Pydantic envelopes remain forward-tolerant for the additive field, while the nested stats
model is strict enough to catch construction errors.

The block contains no document bytes, extracted text, field values, filenames, URLs, passwords,
credentials, provider job ids, raw errors, or free-form rationale. Backend ids, stable reason codes,
node references, document indexes, and allowlisted scalar evidence are permitted. Error messages
remain on the existing error channels and are redacted by existing machinery; they are not copied
into stats.

“No storage” means no independent core-owned stats history. A caller that saves a response also
saves its stats, and an armed Ledger may retain overlapping facts by design. The server's in-memory
JobRecord and result cache are existing bounded lifecycle mechanisms, not a stats query service.

## 12. Rejected alternatives

| Alternative | Rejection |
|---|---|
| Add more `fallback_used` warnings | Prose is not a stable machine contract and cannot express races, native batches, cost coverage, or cache/replay. |
| Reuse `orchestration` as analytics | It is strategy-only, permissive, detailed, and incomplete on failures; changing it would break its debug/replay audience and still leave direct/auto paths uncovered. |
| Make Ledger mandatory | It turns an ordinary read into durable storage, excludes direct and native paths, and violates the requested lightweight behavior. |
| Derive stats after the fact from Ledger only | Most ordinary runs have no Ledger; deriving from current Trace alone misses direct, cache, cancellation, and common fact provenance. |
| Add a GET endpoint by id | An id implies retention, lookup, eviction, authentication, and multi-worker state that this feature explicitly does not own. |
| Add Prometheus/OTel counters | Those are operational metrics, not a per-artifact account; they also require process/exporter lifecycle decisions. |
| Make capture opt-in | Agents and callers then need a new flag exactly when they most need an explanation, and paths drift when one caller forgets it. |
| Publish every router score and signal | Scores are currently internal and discarded; exposing them creates a fragile tuning contract and risks sensitive evidence. Add only in a later, justified schema revision. |
| Put full attempt traces in every batch child | It duplicates root facts and can grow with nested artifacts; root stats with item indexes is the single aggregation boundary. |

## 13. Implementation seams (future build only)

No code is requested on this branch. If the proposal is accepted, implementation should be staged:

1. **Contract:** write stats schema, response/batch successors, typed models, known measure/code
   registry, and fixture examples (AC-1, AC-3, AC-14).
2. **Common facts:** introduce the request-scoped fact sink; migrate plain executor and strategy
   Trace emission; add cache/replay delivery and async completion facts (AC-2, AC-5, AC-6, AC-8).
3. **Outer finalization:** add direct/auto/strategy and native/platform batch aggregation, root
   failure carriers, and nested-stats suppression (AC-1, AC-7, AC-10, AC-15).
4. **Pure projections:** add Python `stats`, CLI `stats`, CLI JSON error mode, and HTTP POST
   endpoint. Keep all projectors adapter-free (AC-10, AC-11).
5. **Ledger alignment:** make overlapping fact/journal fields compareable and ensure replay does
   not add new dispatch or cost (AC-2, AC-8).
6. **Agent readiness:** publish canonical examples and future MCP consumption guidance; do not
   build MCP as part of this proposal (AC-12, AC-14).

The build prompt should not begin until the maintainer resolves the decisions in §15. The first
phase must write failing fixtures for the adversarial cases in §14 before changing capture code.

## 14. Offline proof matrix

| Proof | Expected invariant |
|---|---|
| direct named success | one attempt, one live dispatch, one result document |
| auto A failure → B success | two live dispatches, one transition, reason names A → B |
| compliance drop | dropped backend has no dispatch and no fabricated attempt |
| missing credentials after branch reached | skipped attempt has `sent:false`, zero dispatches |
| normalize failure after submit | one live dispatch and failed completion |
| race winner plus unconfirmed loser cancel | two live dispatches; loser unknown/uncovered, never free |
| native versus platform batch | ten/7/2/1 outcomes; native one dispatch, platform nine |
| cache second invocation | zero live dispatches, cache hit one, no copied first timing/stats |
| Ledger complete replay | zero submits, replay deliveries, zero new cost, no retiming |
| mixed Ledger resume | replayed and live facts coexist without double counting |
| async poll | polls counted separately; requested wait mode and completion source present |
| failed Python call | same stats object attached to existing exception class |
| failed HTTP/job call | stats beside existing error and status/state unchanged |
| failed/partial batch | root stats present; nested child facts counted once |
| legacy artifact | stable `stats_unavailable`, no warning/orchestration inference |
| compare saved responses | zero dispatch and no compare stats block |
| secret/content scan | no planted document, key, URL, job id, raw error, or rationale survives |
| projector purity | CLI/Python/HTTP projectors make zero adapter calls and no writes |
| concurrency/hash seed | counts and decision sequence are deterministic where facts are logically equal; completion order is explicit where it is the decision |

## 15. Maintainer decisions required

1. Accept the automatic additive `stats` field and the response/batch schema advances, or choose a
   stats-only opt-in surface with the additional call complexity that entails.
2. Accept `--error-format json` for machine-readable CLI failures, or explicitly exclude CLI
   failures from the first release.
3. Confirm that one outer root block suppresses nested batch response blocks and that item indexes
   are sufficient correlation without a new run id.
4. Confirm that v0.1 omits router numeric scores and detailed gate/rationale text.
5. Decide whether a future Ledger revision must make the common fact sink its canonical durable
   source before this feature is built, or whether the two projection paths may land in one staged
   milestone with overlap tests.
6. Choose the voluntary method and target for ProductSpec SM-1 through SM-4; open core must not
   add phone-home solely to measure adoption.

## 16. Scope-cut ledger

This proposal is complete when the maintainer can review the ProductSpec and answer the six
decisions above. It intentionally does not include a build prompt, Agent Run receipt, schema file,
Python model, CLI parser change, server route, persistence layer, or tests. Those are implementation
artifacts for a later accepted revision and must cite the ProductSpec AC ids rather than silently
turning this design into code.
