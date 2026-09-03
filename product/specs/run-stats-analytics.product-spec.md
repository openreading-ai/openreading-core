---
spec_format_version: "0.1"
title: "Run Stats and Routing Analytics"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-09-03T16:13:35Z"
updated_at: "2026-09-03T16:13:35Z"
applies_to:
  - path: "src/openreading/schemas/"
  - path: "src/openreading/router/"
  - path: "src/openreading/strategies/"
  - path: "src/openreading/batch/"
  - path: "src/openreading/server/"
  - component: "run-statistics"
---

## Problem

OpenReading can choose a backend, fall through to another one, start several in parallel, select a
winner, and compose work across pages. Afterward, the caller usually sees only the result. A batch
summary counts the backend on each successful final response, so it cannot answer how many calls
were actually made. A plain fallback survives as warning prose. A successful strategy run carries
a richer orchestration trace, but the shape is deliberately permissive, some decisions never reach
it, and failures use a different carrier. Cache hits, Ledger replay, async polling, race losers, and
native batch calls each account for work differently.

That leaves the user unable to answer the basic questions that justify routing and strategy in the
first place: which backends received documents, what share of the work each received, why the
selected backend changed, whether parallel work was useful, who won a race, and how much measured
time and cost the path consumed. The answers can sometimes be reconstructed by a maintainer who
knows several internal formats. A CLI script, application, or agent cannot obtain them from one
stable contract.

The obvious substitutes are both wrong for this need. Operational metrics describe a process or a
fleet, not one result. The Ledger is an optional durable execution record designed for replay and
resume, and enabling storage should not be the price of understanding an ordinary run. The hosted
product may retain and aggregate much deeper evidence; open core still needs a bounded account that
travels with the artifact and creates no history service of its own.

## Hypothesis

If every artifact-producing execution automatically carries one small, schema-defined `stats`
block that distinguishes planned, attempted, dispatched, selected, cached, and replayed work, then
people and agents will use routing and strategies with more confidence because they can verify the
actual path without changing how they invoke OpenReading or adopting a control plane.

If the block exposes named counts with their denominators, explicit backend-change reasons, and
honest coverage for cost and timing, users will tune strategies from measured behavior instead of
final-backend guesses. If a pure CLI, Python, and HTTP projection can return that same block without
the document payload, agents can inspect a run within a small context budget and without parsing
prose.

## Product Summary

Every newly executed single-document response, batch result, and terminal async-job artifact gains
one additive top-level `stats` block. It is produced automatically; a normal `parse`, `run`, or
batch invocation takes no new argument. The block says what kind of execution occurred, how many
documents succeeded or failed, which backend-document pairs were actually dispatched, which
backend supplied each result, which serial and parallel decisions changed the path, and how much
of the time and cost is known.

`stats` is a compact projection of execution facts that the router, strategy trace, and, when
armed, Ledger already own. It is not a separately mutable trace. Detailed gates, candidate output,
and model rationale stay in `orchestration`; durable replay facts stay in Ledger. Cache and replay
are labeled as reuse and never presented as fresh dispatch or newly incurred cost.

The same contract has four views. Callers may read `.stats` directly from a successful artifact,
run `openreading stats` to extract or render it, call `openreading.stats(...)` in Python, or post a
supplied artifact to a stateless `POST /v1/stats`. JSON is canonical for programs and agents; a
table is an optional human rendering. None of these views runs a backend or creates a store.

There is no run-history endpoint and no new run identifier. Saving an output naturally saves the
block inside it, just as saving any response does, but open core keeps no independent stats
database, cross-run index, or session counter. A hosted product can ingest this public contract and
add retention, search, aggregation, and deeper introspection outside core.

## Scope

```productspec-scope
in:
  - "Attach one versioned, bounded stats block automatically to every newly executed outer response, batch result, and terminal async-job artifact."
  - "Cover directly named, automatic, and strategy-selected backends through the same statistics contract."
  - "Distinguish eligibility, attempts, live dispatches, dispatched documents, cache hits, Ledger replays, result backends, and output contributors."
  - "Record serial backend transitions and parallel selection outcomes with stable reason codes, including fallback, race, best, merge, shadow, judge, and page-routing roles when they occur."
  - "Expose async wait mode, completion source, poll activity, and cancellation disposition without claiming that a cancellation stopped vendor billing."
  - "Publish every backend allocation as a named numerator and denominator, so percentages have explicit meaning."
  - "Report wall time, overlapping attempt time, and newly reported cost with measurement coverage and unknown values kept distinct from zero."
  - "Give Python and HTTP failures the same statistics shape, give failed and partial batches root statistics, and offer an explicit machine-readable CLI error mode without changing default stderr behavior."
  - "Provide JSON-first CLI, Python, and stateless HTTP projections that accept a supplied artifact and never execute a backend."
  - "Keep the projection free of document content, credentials, signed links, provider job identifiers, raw provider errors, and free-form model rationale."
out:
  - "Do not retain, index, query, search, or aggregate statistics across runs in open core."
  - "Do not build a dashboard, alerting system, Prometheus or OpenTelemetry surface, or process-wide counter registry in this version."
  - "Do not make routing, compliance, fallback, or strategy decisions from accumulated statistics."
  - "Do not claim billing-grade reconciliation when a provider omits cost or continues work after cancellation."
  - "Do not add statistics to pure comparison; comparison remains a calculation over artifacts and performs no execution."
  - "Do not duplicate detailed strategy evidence, candidate responses, gate narratives, or LLM reasoning from orchestration."
  - "Do not define the hosted product's retention, tenant analytics, dashboards, or deeper introspection model here."
cut:
  - "Cut a GET-by-run-id endpoint and any new run-id lookup contract."
  - "Cut opt-in capture; capture is automatic, while alternate presentation is optional."
  - "Cut inference of complete statistics from legacy warning prose or permissive orchestration objects."
  - "Cut a public raw-event stream; the first contract is the bounded artifact projection."
  - "Cut rounded percentages from canonical JSON; canonical shares carry exact numerators and denominators, and renderers compute display percentages."
```

## User Experience

The common CLI call does not change:

```console
$ openreading parse invoice.pdf --strategy fast > result.json
$ jq '.stats.totals.live_dispatches' result.json
2
```

The result already contains the machine contract. `openreading stats` is a convenience for a
smaller payload or a human view, not a second capture path:

```console
$ openreading stats result.json
{
  "schema_version": "0.1",
  "scope": "document",
  "state": "succeeded",
  "documents": {"total": 1, "succeeded": 1, "failed": 0, "skipped": 0},
  "totals": {"attempts": 2, "live_dispatches": 2, "cache_hits": 0,
             "ledger_replays": 0, "backend_transitions": 1}
}

$ openreading stats result.json --format table
STATE       FILES       ATTEMPTS  LIVE CALLS  SWITCHES  WALL
succeeded   1/1 ok      2         2           1         842 ms

BACKEND     SENT FILES  DISPATCH SHARE  RESULT SHARE  ROLE
open-ocr    1/1         1/2 (50.0%)     0/1 (0.0%)    fallback source
reducto     1/1         1/2 (50.0%)     1/1 (100.0%)  result
```

The table labels each denominator; the JSON does not reduce `1/2` to an unexplained `50%`.
Document reach can sum above 100% when the same file is sent to several backends, while result
share has one final response backend per successful document. Merge and page composition identify
additional contributors separately instead of pretending there was one winner.

Python and HTTP expose the same object:

```python
result = openreading.run(request, strategy="fast")
assert result.stats.totals.live_dispatches == 2

compact = openreading.stats(result)
```

```http
POST /v1/stats
Content-Type: application/json

<a response, batch result, terminal job artifact, or bare stats block>
```

The default CLI failure remains a diagnostic on stderr with the established exit code. An agent
that needs a failure artifact opts into structure explicitly:

```console
$ openreading parse invoice.pdf --strategy fast --error-format json
{"error":{"code":"plan_exhausted","message":"..."},"stats":{...}}
```

That JSON is written to stdout and prose is suppressed for that invocation. Successful calls need
no flag. Python attaches the block to the existing exception as `exc.stats`; HTTP and terminal job
errors place `stats` beside `error`. The exception class, HTTP status, CLI exit code, and human
default all remain unchanged.

An agent can therefore answer “where did this run go?” from stable fields, discard the potentially
large document result, and branch on counts and codes. It never has to interpret warning messages,
table spacing, or strategy prose.

## Acceptance Criteria

```productspec-acceptance-criteria
- id: AC-1
  criterion: A successful single-document or batch call through CLI, Python, or HTTP requires no new execution argument and returns exactly one schema-valid top-level stats block on the outer artifact; a terminal async job does the same, and nested responses do not create a second countable block.
- id: AC-2
  criterion: Direct, automatic, and strategy execution report the exact backend attempts, live dispatches, dispatched documents, result backends, and output contributors observed at their real execution boundaries, without deriving facts from warning prose.
- id: AC-3
  criterion: Every published backend share names its measure and carries its numerator and denominator; the JSON contains no bare or pre-rounded percentage, and a zero denominator is represented as unavailable rather than zero percent.
- id: AC-4
  criterion: A serial move from one backend to another records the source, destination, decision kind, and stable reason code, while an initial selection or parallel winner is not miscounted as a fallback transition.
- id: AC-5
  criterion: Race, best, merge, shadow, judge, and page-routing execution identify participants and selected or contributing outcomes, and a losing branch whose vendor cancellation is unconfirmed is never reported as stopped, free, or zero-cost.
- id: AC-6
  criterion: An async backend attempt records requested wait mode, actual completion source, poll-call and poll-fault counts, and final cancellation disposition without counting polls as backend dispatches.
- id: AC-7
  criterion: For the same ten-input fixture with one intake skip, seven successes, and two execution failures, native batch reports one live submit-many dispatch while platform batch reports nine per-document live dispatches; both report the same document outcomes and neither double-counts nested responses.
- id: AC-8
  criterion: A cache hit and a completed Ledger replay create a new outer artifact with zero new live dispatches and zero newly reported vendor cost; historical usage on the reused payload remains distinguishable from invocation statistics, and replay never retimes or rebills original execution facts.
- id: AC-9
  criterion: Wall duration and overlapping attempt-duration sum are separately labeled, and cost and timing totals carry covered and total counts so a partial or unknown measurement cannot be read as complete or as zero.
- id: AC-10
  criterion: Existing Python exception classes carry stats on a stats attribute, HTTP and terminal-job errors carry stats beside the error, and failed or partial batches carry root stats; default CLI failure bytes and exit codes remain unchanged, while an explicit JSON error mode returns the same shape on stdout.
- id: AC-11
  criterion: Reading an embedded block, invoking openreading stats, calling the Python stats function, and posting the same artifact to POST /v1/stats produce the same canonical JSON, make zero adapter calls, and create no file, index, history row, or process-wide counter.
- id: AC-12
  criterion: A byte scan over every stats-bearing success and failure fixture finds no planted document content, credential, password, signed URL, provider job identifier, raw provider error, or free-form model rationale.
- id: AC-13
  criterion: Pure comparison performs no dispatch and creates no stats block; CLI comparison fan-out leaves statistics only on the independently executed source responses and never inserts acquisition facts into a comparison report.
- id: AC-14
  criterion: A legacy artifact with no stats block is rejected with a stable stats_unavailable code rather than reconstructed from incomplete fields, and a supported stats projection remains bounded by documents, attempts, decisions, and parallel groups rather than extracted payload size.
- id: AC-15
  criterion: A pre-dispatch compliance refusal or missing credential reports zero live dispatches, while a failure after submit but before normalization reports one; Python, HTTP, async-job, and batch carriers normalize those facts identically.
```

## Success Metrics

```productspec-success-metrics
- id: SM-1
  metric: Known external strategy integrations that use the stats contract to validate or change backend allocation without parsing orchestration or warning prose
  target: tbd
  target_status: provisional
  target_owner: "Akshay"
  window: 90 days after the stats contract ships
- id: SM-2
  metric: Median time in observed user or support investigations to answer which backends received a document and why the result backend changed
  target: tbd
  target_status: provisional
  target_owner: "Akshay"
  window: 90 days after the stats contract ships
- id: SM-3
  metric: Known autonomous agent workflows that consume stats without receiving the document payload or parsing a human rendering
  target: tbd
  target_status: provisional
  target_owner: "Akshay"
  window: 90 days after the agent surface can consume the contract
- id: SM-4
  metric: Share of sampled routing and strategy investigations that still require raw process logs after the stats contract is available
  target: tbd
  target_status: provisional
  target_owner: "Akshay"
  window: 90 days after the stats contract ships
```

## Risks

**A precise-looking percentage tells the wrong story.** “Backend A handled 60%” could mean live
calls, distinct documents reached, final results, pages, or cost. This is worse than no statistic
because it gives a wrong answer authority. The product carries named measures and exact
denominators, and the table repeats the denominator label rather than shortening it away.

**Stats becomes a third execution log.** Strategy Trace and Ledger can already drift today. A new
recorder independently updated at call sites would multiply that problem and violate Ledger's
single-source design. The stats block must be a projection from common execution facts, with
overlap tests against orchestration and Ledger when they are present.

**Reuse looks like fresh spend.** The current cached response retains its original usage. Copying
its stats would make a cache hit look billed again; replay can create the same error. Cache and
replay must rebuild the invocation view and keep historical payload usage distinct from newly
reported cost.

**An additive field still breaks a brittle consumer.** The invocation remains unchanged, but a
consumer pinned to an exact response version or byte snapshot must adopt the new schema version.
The change therefore follows normal schema evolution, keeps the field optional for reading older
artifacts, and proves forward-tolerant readers. It is not described as byte-identical.

**The compact block leaks the document through evidence.** Filenames, field names, URLs, error
text, strategy prose, and LLM rationale are tempting debugging aids and can contain customer data.
They are excluded. Stats carries controlled codes and numeric or boolean evidence only; richer
debugging remains an explicitly separate artifact.

**The feature is built without evidence that users read it.** Open core cannot silently phone
home, so none of the success metrics is measurable by default. The owner must choose a voluntary
measurement method or accept that this ships on product conviction; the parser must not be
satisfied with an invented target.

## Open Questions

1. **Is an automatic additive `stats` block the accepted compatibility tradeoff?** Recommended:
   yes. It is the only option that adds no invocation complexity and gives an agent the facts at
   the moment it receives a result. The cost is a deliberate response and batch schema version
   advance, not byte identity.
2. **Should machine-readable CLI failures use `--error-format json`?** Recommended: yes, as a
   shared flag on executing verbs. Successful artifacts need no flag; default human stderr and
   exit codes stay untouched. A sidecar path is harder to compose and quietly introduces file
   lifecycle concerns.
3. **How much detail belongs inside the block?** Recommended: compact attempt, decision, and
   parallel-group rows stay because they answer “why did it change?”; gate trees, candidate
   responses, signal prose, and model rationale remain only in orchestration.
4. **Should router score components be published in v0.1?** Recommended: no. Current routing
   computes and discards them, their meaning is not a public contract, and selected/dropped codes
   answer the stable question. Add scores only through a later schema version if a real tuning
   workflow requires them.
5. **Which release carries the response and batch schema advances?** The design is version-ready,
   but this proposal does not choose a release number or implementation order against the Ledger,
   Intent, Decider, and Agent milestones.
6. **How will the provisional success metrics be measured without open-core telemetry?** Options
   include explicit design-partner studies, voluntary issue templates, or the hosted product's own
   consented analytics. The owner must choose the method and target; core must not add phone-home to
   make the metric easy.

## Related Artifacts

```productspec-related-artifacts
- type: engineering_spec
  url: "design/run-stats-analytics.md"
  title: "Run Stats and Routing Analytics — engineering proposal"
- type: engineering_spec
  url: "design/run-stats-analytics.md#4-design-laws"
  title: "Stats design laws and source-of-truth boundary"
  section_id: acceptance_criteria
  item_id: AC-2
- type: engineering_spec
  url: "design/run-stats-analytics.md#7-aggregation-semantics"
  title: "Named measures, denominators, native batch, cache, and replay"
  section_id: acceptance_criteria
  item_id: AC-3
- type: engineering_spec
  url: "design/run-stats-analytics.md#9-surface-contracts"
  title: "CLI, Python, HTTP, failure, and agent surfaces"
  section_id: acceptance_criteria
  item_id: AC-10
- type: engineering_spec
  url: "design/run-stats-analytics.md#11-proof-matrix"
  title: "Adversarial offline proof matrix"
  section_id: acceptance_criteria
  item_id: AC-15
- type: product_spec
  product_spec_path: "./ledger.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "Ledger Execution Plane — durable facts that stats must not duplicate"
- type: product_spec
  product_spec_path: "./agentic.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "Agent Surface — future machine consumer of the same stats contract"
- type: product_spec
  product_spec_path: "./intent.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "Intent — a future decision source the stats vocabulary must admit"
- type: product_spec
  product_spec_path: "./decider.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "In-Run Decider — model decisions summarized by code, not rationale"
```
