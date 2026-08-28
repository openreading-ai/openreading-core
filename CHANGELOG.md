# Changelog

All notable changes to OpenReading are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Tagged `v0.3.0`, not yet on PyPI.** These are development milestones, each complete and merged
> to `main` on the date shown, with `make verify` green on a clean clone. `v0.3.0` is tagged in git
> and `pyproject.toml` is at `0.3.0`, but nothing is published to PyPI yet. The first PyPI release
> is tracked in [`ROADMAP.md`](https://github.com/multiversal-ventures/openreading/blob/main/runs/ROADMAP.md) §1.2.

## [Unreleased]

### Repository split — this is `openreading-core`

The open-core engine (library, CLI, thin JSON server, tests) now lives in its own repository,
`multiversal-ventures/openreading-core`, imported from the private `openreading` company repo at
its `feedback` branch (tag `engine-last-in-tree` there). The web UI (`openreading serve-ui`)
moved to that repo as the `openreading_webui` package (`openreading-ui`); the `webui` extra is
gone. Research, design specs, the engineering-council record, product intent, run logs, the
decision log and labeled data stay private there — links below that point at it resolve for
company members only. History before this point is in the private repo.

Deferred-by-design items and the path to a first published release. See [`PENDING.md`](https://github.com/multiversal-ventures/openreading/blob/main/runs/PENDING.md)
for the engineering backlog and [`ROADMAP.md`](https://github.com/multiversal-ventures/openreading/blob/main/runs/ROADMAP.md) for the Community/Enterprise split.

### Renamed — the project is `openreading`, was `openmanifold`

The vendors this repo integrates all sell the category as *document intelligence*. The name now
claims the plain-English version of it: a parser is named for its input, reading is named for what
the reader came to find out — which is the intent thesis the repo is built around
([`docs/thesis.md`](https://github.com/multiversal-ventures/openreading/blob/main/product/thesis.md) §1).

Everything moved at once, while the cost was lowest: nothing is published to PyPI, so no consumer
had pinned an import path or a schema `$id`.

- Python package `openmanifold` → `openreading`; `import openmanifold` no longer resolves.
- CLI `openmanifold …` → `openreading …`.
- Environment variables `OPENMANIFOLD_*` → `OPENREADING_*` (including every
  `OPENMANIFOLD_<SLUG>_<KEY>` credential override).
- Strategy file `openmanifold.yaml` → `openreading.yaml`.
- Schema `$id` host `openmanifold.dev` → `openreading.ai`, across released and unreleased files.
- `billing_target` enum value `"openmanifold"` → `"openreading"` — a wire value, not just metadata.
- Intent-layer JSON Schema keywords `x-om-*` → `x-read-*` (`x-read-criticality`, `x-read-aliases`,
  `x-read-fidelity`, `x-read-on-fail`, `x-read-target`, `x-read-tolerance`) — `om` stood for
  openmanifold. Design-stage only, nothing in `src/` parses them yet, so the vocabulary moved
  before it shipped rather than fossilising the old name in every caller's schema.
- Response `source` media-type tree `vnd.openmanifold.*` → `vnd.openreading.*` — also a wire value.
- The released-schema byte-freeze digests were re-pinned once to absorb this; §8 immutability
  resumes from the new digests. See the note on `_FROZEN_RELEASED` in
  `tests/test_schema_evolution.py`.

### Descriptors — non-secret config moved out of `credentials_spec`

the `openreading.adapters` docstring (formerly ADDING_ADAPTERS.md) has always said endpoints, regions, and resource ids are ConfigFields, but
three adapters declared them as non-secret `CredentialField`s. They now sit in `config_spec`, so
they resolve into `ctx.runtime` (overridable per request) instead of `ctx.credentials`, and
`ctx.credentials` holds only true secrets. **Env var names, the readiness table's MISSING set, and
the live-test gates are unchanged** — only the internal bucket moved.

- `aws-textract`: `region` (`AWS_REGION` / `AWS_DEFAULT_REGION`) → `config_spec`.
- `azure-document-intelligence`: `endpoint` → `config_spec`.
- `google-document-ai`: `project_id`, `processor_id`, `location` → `config_spec`; the adapter
  authenticates purely by ADC and now declares an empty `credentials_spec`.

The conformance kit enforces the rule: a `credentials_spec` field graded `secret=False` is a
violation. Downstream adapters declaring non-secret credentials must move them to `config_spec`.

### Removed — `openreading compare --serial`

The flag was never read by any code path: compare's fan-out has always been serial, and the
concurrent mode the flag implied an opt-out of was never built. Fan-out remains serial
(deterministic subject order, one hosted call in flight); passing `--serial` is now an argparse
error rather than a silent no-op.

### Strategies — cost budgets removed

The strategy layer no longer offers a cost **budget** or a cost **estimator**. Prices change
often and can't be known reliably at plan time, so the engine never estimates or enforces a
dollar figure — it only ever **reports** the real cost each backend billed.

- **Removed (config options):** the Plain `max_cost` body key; `budget.max_cost_usd`;
  `limits.max_cost_usd_per_doc`; `defaults.advanced.assumed_pages` / `assumed_rate`. The `budget:`
  object is now `{max_duration, max_attempts}`; `limits:` is `{max_duration_per_doc}`.
- **Removed (engine):** the rate×pages cost estimator, the per-node cost pool, the parallel
  cost-floor reservation, the leaf/decider/judge budget pre-checks, and the `budget_pruned`
  attempt category. The hosted-fan-out "money-safety wall" (a `race`/`compare` over >1 hosted
  backend required a `max_cost`) is gone — no budget is required anywhere now.
- **Kept (honest reporting):** `usage.cost_usd` still totals every billed attempt (winners and
  losers), `cost_basis: "billed"` on trace attempts, the cheaper-backend tie-break on `pick: best`
  ties, and `openreading calibrate`'s advisory descriptor-based cost prediction. Time budgets
  (`max_duration` / `max_attempts` / `limits.max_duration_per_doc`) are unchanged;
  `budget_exhausted` now means only the time deadline was reached. A parallel drain that would
  outlive the node deadline is still cut off on time (Law 6) but recorded with no cost instead of
  a fabricated estimate.

### Plain (v0.7) — the eleven-word simple-strategies dialect (on `v0.7-plain`)

A closed dialect of `openreading.yaml` — `try` / `race` / `compare` / `escalate_when` / `then` /
`max_time`, the criteria `looks_bad` / `low_confidence` / `missing` / `disagree`, and
`auto` — that desugars to today's five-node grammar (one engine, one trace, one validator). Front
door: [`openreading.strategies.plain`](src/openreading/strategies/plain.py). Schema cut to
`strategy-config.v0.2.json` (v0.1 stays frozen; config `version` const unchanged).

- **Added:** `strategy validate` badges each strategy `dialect: plain | advanced`; `strategy show`
  prints a body as written, `--strategy show … --longhand` the canonical tree; `explain` groups a
  Plain strategy's gate rows under their source word.
- **Changed — behavior of existing runs:** `disagreement_over` now computes (phase 2). Any strategy
  that compares backends and routes on disagreement — a Plain `compare` + `then` (whose default
  gate includes `disagree`), or an advanced `pick: best` parallel step gating on
  `disagreement_over` — now **escalates when the branches materially disagree**, where before the
  predicate was inert (parsed, never fired). compare+then strengthens from "escalate when the
  winner looks bad" to "… or when the branches disagree." Disagreement = `1 − token-set overlap`
  of the branches' text; the winner attempt records it as calibration telemetry.
- **Engine:** a `pick: best` / `pick: fastest` parallel used as a cascade *step* now honors a
  step-position `escalate_if` on its winner (previously a silent no-op — D-v3-7); this is both the
  Plain `compare`+`then` runtime and the cookbook's "wrap the race in a gated step" idiom.

### Manifest (v0.6) — batch intake + corpus compare (on `v0.6-manifest`)

One command over a **directory** (or glob / many files / http(s) URLs) of *any supported format* →
**one JSON** covering every document, and two such runs compared per-document. Composes the
single-document pipeline without touching `request.v0.1` / `response.v0.3`. Design:
[`internal/design/batch-intake.md`](https://github.com/multiversal-ventures/openreading/blob/main/design/batch-intake.md).

**Added**

| Artifact / feature | Notes |
|---|---|
| `batch-result.v0.1.json` (new family) | one envelope over many documents: per-item succeeded/failed/skipped with honest reasons, each succeeded item a full `response.v0.3`, plus a `summary` (counts, cost with honest bases, per-backend tally) |
| `corpus-report.v0.1.json` (new family) | batch-vs-batch: documents paired by `relpath`→`filename`→`sha256`, per-document `comparison-report.v0.2` + verdict rollup |
| adapter-descriptor `v0.3`→`v0.4` | optional `batch` block (native-batch declaration); additive |
| `openreading parse <dir\|glob\|files…>` | batch intake (M1–M10): deterministic recursive expansion, honest format filter, `--jobs`/`--max-items`/`--save-dir`, exit `4` on partial |
| `openreading.run_batch(...)` | Python API returning a `batch-result` dict |
| `openreading compare runA.json runB.json` | **corpus mode** when subjects are batch envelopes — per-document verdicts; `--format diffs` is value-first (the actual lines each backend captured that the other missed, per divergent doc) |
| native-batch protocol | opt-in `submit_many`/`normalize_many` (feature-detected); **anthropic Message Batches** reference impl (live-lane-gated); platform fan-out otherwise, observationally equivalent (M10) |
| `POST /v1/batch` | server route composing per-item requests; `make serve-smoke` exercises it |

### Canon (v0.5) — schema evolution + mapping quality (on `v0.5-canon`)

The response contract gains *named, fixture-tested channel semantics*. Response schema **v0.3**,
adapter-descriptor **v0.3**. Design + rationale:
[`internal/design/canonical-normalization.md`](https://github.com/multiversal-ventures/openreading/blob/main/design/canonical-normalization.md).

**Added** (additive, backward-compatible — proven transitively in `tests/test_schema_evolution.py`)

| Field / artifact | Notes |
|---|---|
| `document.confidence` | document-level confidence `[0,1]` — the honest home for whole-document signals |
| `channel_provenance` | per-response `{channel: native\|derived}` (x-stability: **experimental**) |
| `schema_url` | the `$id` of the declared schema version (OTel `schema_url` pattern) |
| descriptor `output.block_granularity` | native block-granularity hint (`word`/`line`/`paragraph`/`section`/`element`) |
| `page_attribution_unavailable` warning code | the §4.3 container-rule signal for page-unattributable derived blocks |

**Changed** (named `0.x` MINOR-slot tightenings — *not* additive; enumerated with invariant IDs)

| Change | Invariant |
|---|---|
| Confidence bounds tightened to `[0,1]` on `TableCell`/`Page`/`doc_type`/`Citation` | **C7** confidence.unit |
| `text` strengthened to plain, markup-free, and complete (incl. table content) | **C1** text.plain / **C2** text.complete |
| Version-identity fix: response schema const `"0.1"`→`"0.3"` (v0.2's known-bad const frozen + regression-pinned; C12 meta-test added) | — |
| Honesty re-grades: anthropic `text N→D`; qwen `markdown N→D` (mode-dependent) | §6.5 |
| **comparison-report `v0.1`→`v0.2`** (finding-semantics change): new `structure` finding code (packaging/granularity differences demote out of content-miss severity), a content-first `headline` over the guaranteed channels, and the non-determinism rule (generative subjects compared by similarity; their content findings cap at informational) | §9 |

All 13 adapters remediated to the channel contract (Phase B), and the conformance kit's default
flipped to all-strict (C1/C6/C7 hard; C11 advisory) now that every backend passes them (Phase C).

**Also in v0.5:**
- **Superpowered `compare --format diffs`** — a content-equivalence verdict (packaging-immune
  token coverage) + structure deltas (TABLES / TYPES / GRANULARITY), so a wall of findings becomes
  one screen of "what actually differs, and does it matter". See [`openreading.comparison`](src/openreading/comparison/__init__.py).
- **Live verification (partial)** — reducto, pulse, and nuextract exercised against their real APIs;
  fixes landed for live-only defects (pulse multipart `/extract` + id-prefix leaks; nuextract
  multipart job upload + HTML-island table grids; the `.env` inline-comment loader bug); pulse/
  nuextract structured extraction verified (pulse now captures per-field confidence + citations).
  See [`internal/design/live-verification-plan.md`](https://github.com/multiversal-ventures/openreading/blob/main/design/live-verification-plan.md).

### Pending
- **Live verification (remaining)** — the hosted lanes without keys here (anthropic, textract,
  azure, google, chunkr, open-ocr) are still fixture-tested only; textract's async per-op routing is
  the highest-risk unverified path. `make verify-live` with real keys is the credibility floor.
- **Production LLM wire adapter** — the decider/judge/replay contract is built and tested offline
  via in-process fakes; the live Messages-API call behind the port is not wired, so an env-enabled
  decider on a compliant backend resolves `decider_downgraded: unavailable`.
- **Distribution** — tag a release, add trusted-publishing to PyPI, ship a baseline Dockerfile /
  compose, publish the generated OpenAPI spec, and stand up a docs site.
- Smaller deferred items (per-token cost metering, `start_after: auto` hedging, the joint
  threshold-vector `calibrate` sweep, `text_source: prior_ocr`, `file:line:column` validate
  locations) are catalogued in [`PENDING.md`](https://github.com/multiversal-ventures/openreading/blob/main/runs/PENDING.md).

## [0.4.0] — 2026-07-27 — "Compare"

The read-only cross-backend delta layer: run a document through many backends and see *how they
differ* — which fields each got, missed, or disagreed on; where they disagree on a value; which
blocks only one saw; what each cost. Because every backend returns the one envelope, `compare` is a
**pure function over N schema-valid responses** — a leaf feature that changes no adapter, router,
engine, or the response schema. Settled design in [`openreading.comparison`](src/openreading/comparison/__init__.py); judgment
calls (D-v4-13/14) in [`DECISIONS.md`](https://github.com/multiversal-ventures/openreading/blob/main/decisions/DECISIONS.md).

### Added
- **The report** — vendored `comparison-report.v0.1.json` (validated in `make verify`), with a
  closed finding vocabulary (12 codes) and field verdicts (`agree`/`disagree`/`partial`/`unique`/
  `not_capable`).
- **Four dimensions** — facts scoreboard (state/latency/cost/counts), field delta over
  `typed_fields` with a deterministic value-equivalence ladder (exact → normalized → money → number
  → date), text delta (canonical-text similarity matrix + unified diff), and structural delta
  (text-first, IoU-validated block alignment with granularity merge; type/position conflicts,
  confidence gaps, block missed/unique, table shape).
- **Honesty** — a backend that cannot produce a dimension (no confidence, no blocks) is
  `not_capable`, never "missed"; symmetric mode reports difference, never crowns a winner.
  `unaligned_ratio` is surfaced as a first-class trust signal.
- **Stances** — symmetric (with a `consensus` section at N≥3), `--baseline` (sign deltas against one
  subject), and `--truth` (score each subject with the shared `evals.scorers` — one metric stack).
- **Surfaces** — `openreading compare` (files / `--backends` fan-out / `--from`; `--format
  json|table|diff|md`, delta-first); `openreading.compare()` in the public API; pure
  `POST /v1/compare`; `explain` renders a report; `parse --keep-candidates` retains a strategy run's
  discarded branches under `orchestration.candidates[]` for `compare --from` (opt-in, additive, no
  schema change; a direct run stays byte-identical).
- **Harness** — the H1–H8 kit: envelope builder, alignment torture table, equivalence-ladder table,
  property suite (schema-validity / determinism / reflexivity / order-invariance), a live
  heterogeneous corpus (pymupdf × tesseract), leaf-isolation + byte-identity guards, surface tests,
  and `make compare-smoke` joined to `verify`. 788 offline tests, coverage 92%.

## [0.3.0] — 2026-07-25 — "Strategies"

The optional multi-backend orchestration layer: an `openreading.yaml` that composes backends into
quality-threshold cascades, parallel fan-outs, routes, and LLM decision points — executed by a
deterministic engine under hard rails (compliance never widened, budgets enforced, every decision
traced). Settled design in [`openreading.strategies`](src/openreading/strategies/__init__.py); build history in
[`PROGRESS.md`](https://github.com/multiversal-ventures/openreading/blob/main/runs/PROGRESS.md); judgment calls in [`DECISIONS.md`](https://github.com/multiversal-ventures/openreading/blob/main/decisions/DECISIONS.md).

### Added
- **Config spine** — vendored `strategy-config.v0.1.json` grammar (five node types plus shorthands,
  gates, budgets, `on_error`, `route`, `decide`, `policy`, `limits`, `decider`, `defaults`); a
  discovery-ordered loader; `normalize` (shorthand→longhand, `extends`, the four presets); and a
  complete `validate` world-consistency catalog wired into `openreading.schemas validate`.
- **Serial cascade engine** — quality-gated fallback with a reference-free signal probe
  (`signals.py`: text metrics, garble composite, table sanity, PDF-layer scanned/text-source
  detection), keep-best law, `on_error` routing, and budget pruning.
- **`route` nodes + facts** — first-match dispatch on doc type / mime / page count / size /
  compliance, with all rules evaluated for the trace (shadowed-rule debugging).
- **`policy:` / `limits:` blocks** — a file-level compliance policy that only ever *narrows* the
  effective request, plus outermost per-document cost and duration budgets.
- **Parallel execution** — `pick: fastest` / `best` / `merge`, hedging (`start_after`), shadow
  branches, budget reservation, `require: <n>`, and a coordinated `FakeClock` for offline
  determinism.
- **Decision points (M4)** — the env-gated LLM decider/judge contract: two-key gate, compliance
  gate, strict-tool schema, engine re-validation, pairwise judge, downgrade taxonomy, per-call
  metering, and `replay --trace` with `mask_fields` and canary checks. Exercised end-to-end by
  in-process fakes; the live wire adapter is deferred (see Unreleased).
- **Page granularity** — `granularity: page` with per-page gates, range escalation, stitching, and
  `pick: merge` (typed-field vote ensemble); `pages[].source_backend` in the response.
- **`calibrate`** — derives gate thresholds from a labeled sample by sweeping each gated predicate
  over its domain.
- **Surfaces** — `parse --strategy/--no-strategy/--config`, `strategy plan|show|list|normalize|
  validate|calibrate`, `explain`, `replay`; `api.run(strategy=, config=)`; server config via
  `OPENREADING_CONFIG`. A `strategy-smoke` target added to `make verify`.

### Changed
- Response schema **v0.2** (additive, backward-compatible): an `orchestration` object carrying the
  strategy, chosen backend, fallback depth, attempts, decisions, drops, and per-page trace; plus
  `pages[].source_backend`. A no-config run remains byte-identical to the legacy path.

## [0.2.0] — 2026-07-24 — "Hosted backends, for real (BYO-key)"

Hosted backends run against real provider APIs from your own keys, exposed through three surfaces.

### Added
- **11 adapters, all conformance-verified** against fixtures: pymupdf, tesseract, docling, qwen-vl,
  reducto, chunkr, pulse, anthropic-claude, aws-textract, azure-document-intelligence,
  google-document-ai.
- **BYO-key credential broker** — `.env` / environment resolution driven by each descriptor's
  `credentials_spec`; preflight errors name the exact missing variables and the provider's signup
  URL; clear invalid-key handling; ambient SDK chains (boto3, GCP ADC, Anthropic) preserved. Keys
  are held only in memory and never stored, logged, or resold.
- **Three surfaces** — the CLI (`parse`, `route --run`, `backends`, `serve`), the Python API
  (`openreading.run()` / `route()`), and an HTTP server (`/v1/parse`, `/v1/route`, `/v1/jobs`,
  `/v1/webhooks` with Svix signature verification, `/v1/backends`, `/healthz`), with one Job state
  machine covering INLINE / POLL / WEBHOOK.
- **Live-test lane** — keyed `@pytest.mark.live` tests for every hosted backend with clean skips,
  a record mode with secret scrubbing, `make verify-live`, and a manually dispatched CI job.
- **Documentation** — README quickstart, `.env.example` covering every backend with signup URLs,
  `docs/credentials.md`, `docs/cli.md`, `docs/server.md`; an offline CI matrix (Python 3.11 / 3.12).

## [0.1.0] — 2026-07-22 — "The contract and engine core"

The foundation: one request shape and one response schema over many backends, with a compliance-first
router and a fully-local tier. Design research in [`research/openreading/`](https://github.com/multiversal-ventures/openreading/tree/main/research/openreading)
(127 backends profiled from primary sources).

### Added
- **The contract** — vendored request / response / descriptor JSON Schemas with pydantic mirrors;
  canonical bounding boxes with lossless `bbox_native`; a four-category error taxonomy; an anyOf
  response envelope.
- **Adapter interface** — the eight-method adapter interface with a static `AdapterDescriptor`
  (N/D/X channels); the router never branches on backend type.
- **Compliance-first router** — three stages, compliance never relaxed by fallback, unverified
  claims fail closed; an executable fallback chain (executor with skip-on-missing-credentials, an
  attempt trail, `PlanExhaustedError`, and a bounded idempotency cache).
- **Local tier** — the `pymupdf` and `tesseract` adapters, HIPAA-by-architecture (documents never
  leave the environment); the pattern the descriptor-driven hosted adapters follow.
- **Tooling** — a conformance kit and secret scrubber shipped in the package for downstream adapter
  authors, and an evaluation harness (scorers, datasets, runner).

[Unreleased]: https://github.com/shad0wfax/openreading/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/shad0wfax/openreading/releases/tag/v0.3.0

<!-- 0.2.0 and 0.1.0 predate git tags; they are development milestones, not tagged releases. -->
