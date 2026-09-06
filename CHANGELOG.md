# Changelog

All notable changes to OpenReading are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Pre-release. The package is `0.3.0`, with no git tags and nothing on PyPI.** Install from a
> clone by following the *Install* section of [`README.md`](README.md). The `0.3.0` you install
> today contains every entry in this file, including everything under `[Unreleased]` and
> `[0.4.0]`. The `[0.3.0]` heading below is the July milestone that first carried that number,
> not a description of the package you have. Every heading here names a development milestone
> rather than a published release. Each milestone merged with `make verify` green on the date
> shown, in the private repository this engine was imported from. This repository's own history
> starts later, on 2026-08-28, at commit `bd5ee43`. The package number and this file are
> reconciled when the first release is tagged. The Keep a Changelog link definitions are added at
> the same time. The root README's "Status and versioning" table lists every number this project
> carries and says which one to pin.

## [Unreleased]

**This repository is `openreading-core`.** The open-core engine (library, CLI, thin JSON server,
tests) lives here from commit `bd5ee43`, dated 2026-08-28. Its history before that commit is
private and is not published. The web UI and the `webui` extra are not part of this repository.
Research, design records and labeled data stay private as well, so nothing in this file links to
them.

### Added

- `openreading benchmark` discovers public corpora and runs backends or strategies through the
  official ParseBench and ExtractBench scorers. Static catalog entries keep source and dataset
  terms visible for additional research corpora without downloading or claiming support.
- A case's `expected` may declare `text_absent`, a plain list of strings that must not appear.
  It is the assertion that catches invented content and needs no publisher JSON.
- A case's `expected` may declare `rules`, which are ParseBench's own rule objects scored by
  ParseBench's own engine over your document. They are the only dimension that fails when a
  backend INVENTS content rather than merely missing it, which the other four cannot see. Rules
  are a dimension inside the existing scorer, so `leaderboard` and `calibrate` reach them through
  the one `run_case` they already use. Needs `openreading[parsebench]`.
- `benchmark run` runs two documents by default, `--limit N` or `--doc NAME` chooses others, and
  `--limit 0` runs the whole prepared corpus. It prints the chosen documents, their page count and
  a per-target dollar range before anything bills, and asks first when a run is unpriced or over a
  dollar. `--yes` answers in advance and is required with no terminal attached.

### Changed

**The project is `openreading`, was `openmanifold`.** The vendors this repository integrates all
sell the category as *document intelligence*. The name now claims the plain-English version of it.
A parser is named for its input, and reading is named for what the reader came to find out.
Everything moved at once, while the cost was lowest. Nothing is published to PyPI, so no consumer
had pinned an import path or a schema `$id`.

- Python package `openmanifold` → `openreading`. `import openmanifold` no longer resolves.
- CLI `openmanifold …` → `openreading …`.
- Environment variables `OPENMANIFOLD_*` → `OPENREADING_*`, including every
  `OPENMANIFOLD_<SLUG>_<KEY>` credential override.
- Strategy file `openmanifold.yaml` → `openreading.yaml`.
- Schema `$id` host `openmanifold.dev` → `openreading.ai`, across released and unreleased files.
- `billing_target` enum value `"openmanifold"` → `"openreading"`, a wire value, not metadata alone.
- Intent-layer JSON Schema keywords `x-om-*` → `x-read-*` (`x-read-criticality`, `x-read-aliases`,
  `x-read-fidelity`, `x-read-on-fail`, `x-read-target`, `x-read-tolerance`), because `om` stood for
  openmanifold. These keywords are design-stage only, and nothing in `src/` parses them yet. The
  vocabulary moved before it shipped rather than fossilising the old name in every caller's
  schema.
- Response `source` media-type tree `vnd.openmanifold.*` → `vnd.openreading.*`, also a wire value.
- The released-schema byte-freeze digests were re-pinned once to absorb this, and the byte-freeze
  resumes from the new digests. See the note on `_FROZEN_RELEASED` in
  `tests/test_schema_evolution.py`.

**The request schema forbids unknown fields at every nesting level.** A misspelled nested request
field, such as `document.mim_type` for `mime_type`, now fails schema validation. Before,
`request.v0.1.json` set `additionalProperties: false` at the top level only. The pydantic request
models (`openreading.types.request`) already forbade extra fields at every level. The misspelling
therefore passed the schema and was rejected later, at the pydantic layer. That gap is the drift
that "schemas are the source of truth" (`AGENTS.md`) exists to rule out.

`request.v0.2.json` adds `additionalProperties: false` to the 12 nested object nodes that lacked
it. They are `document`, `backend`, `backend.runtime`, `outputs`, `outputs.chunking`,
`extraction_schema`, `features`, `pages`, `pages.ranges[]`, `routing`, `compliance` and `async`.
`extraction_schema.json_schema` stays open, because it holds an arbitrary caller-supplied JSON
Schema rather than a field this contract shapes. `request.v0.1.json` is unchanged and stays
byte-frozen. `REQUEST_SCHEMA_FILE` and the `OpenReadingRequest.schema_version` default now point at
v0.2.

**A compliance policy is validated on every surface.** An invalid policy now stops the run and says
why:

```console
$ cat bad.json
{"require_baaa": true}
$ openreading route --policy bad.json examples/john_smith_1000_2026_01.pdf
[route] invalid policy bad.json: unknown policy key: 'require_baaa' (did you mean 'require_baa'?); valid keys: allow_unverified_compliance, baa_tier_confirmed, data_region, doc_type_hint, max_retention, no_train_on_data, optimize_for, require_baa, require_local, train_optout_confirmed
```

Before this change, a `--policy` file or a `policy=` argument was an unvalidated dict.
`_apply_policy` split it into its known compliance and routing keys *before* anything checked it,
so an unrecognised key was discarded in silence. `{"hipaa": true, "gdpr": "strict", "soc2":
["type2"]}`, or a misspelled `require_baaa`, left every backend eligible with an empty `dropped` map
and exit 0. A top-level `[]` or `null` ran with no compliance filter at all. A top-level JSON string
crashed with an uncaught `TypeError` or `AttributeError`. The same keys sent over HTTP as
`request.compliance` were already refused with a 400, so the answer depended on which surface the
policy entered through.

`openreading.api.validate_policy` now runs before any part of a policy is read. It is called from
`route`, `run`, `run_batch`, `build_request` and `router_config`, from every CLI `--policy` flag,
and from the `policy:` block of an `openreading.yaml`. The policy must be a JSON object. Every key
must be one of the ten documented names, and a near miss gets a `did you mean`. Every value is
type-checked strictly against the model that key feeds. A bad policy raises `PolicyError` in Python
and exits 3 at the CLI. Nothing about a valid policy changed and no key was added, so the compliance
semantics are untouched. The ten names are derived from `Compliance`, `Routing` and `RouterConfig`
rather than re-typed beside them. The policy grammar and the router therefore cannot drift apart
again.

**Response schema v0.3 and the channel contract.** The response contract gains named,
fixture-tested channel semantics. The response schema moves to v0.3 and the adapter descriptor to
v0.3. These are named `0.x` MINOR-slot tightenings rather than additive changes, so each is
enumerated with its invariant id.

| Change | Invariant |
|---|---|
| Confidence bounds tightened to `[0,1]` on `TableCell`/`Page`/`doc_type`/`Citation` | **C7** confidence.unit |
| `text` strengthened to plain, markup-free, and complete (incl. table content) | **C1** text.plain / **C2** text.complete |
| The response schema's `schema_version` const says `"0.3"`. Earlier files declared `"0.1"` whatever their version, and the v0.2 file keeps its wrong const under a pinned test | |
| Re-grades: anthropic `text N→D`, qwen `markdown N→D` (mode-dependent) | |
| **comparison-report `v0.1`→`v0.2`** (finding-semantics change): a new `structure` finding code, so packaging and granularity differences demote out of content-miss severity, a content-first `headline` over the guaranteed channels, and the non-determinism rule, under which generative subjects are compared by similarity and their content findings cap at informational | |

All 13 adapters were remediated to the channel contract. The conformance kit's default flipped to
all-strict (C1, C6 and C7 hard, C11 advisory) now that every backend passes them. Design and
rationale: the `openreading.derive` docstring, which states the channel contract C1 to C11.

**Non-secret adapter config lives in `config_spec`.** The `openreading.adapters` docstring says
that endpoints, regions and resource ids are `ConfigField`s. Three adapters declared them as
non-secret `CredentialField`s instead. They now sit in `config_spec`, so they resolve into
`ctx.runtime` (overridable per request) instead of `ctx.credentials`, and `ctx.credentials` holds
only true secrets. Env var names, the readiness table's `MISSING` set, and the live-test gates are
unchanged. Only the internal bucket moved.

- `aws-textract`: `region` (`AWS_REGION` / `AWS_DEFAULT_REGION`) → `config_spec`.
- `azure-document-intelligence`: `endpoint` → `config_spec`.
- `google-document-ai`: `project_id`, `processor_id`, `location` → `config_spec`. The adapter
  authenticates purely by ADC and now declares an empty `credentials_spec`.

The conformance kit enforces the rule. A `credentials_spec` field graded `secret=False` is a
violation. A downstream adapter declaring non-secret credentials must move them to `config_spec`.

**`disagreement_over` now fires.** This alters existing runs. Before, the predicate parsed but never
fired. A strategy that compares backends and routes on disagreement now escalates when the branches
materially disagree. That covers a Plain `compare` + `then`, whose default gate includes `disagree`,
and an advanced `pick: best` parallel step gating on `disagreement_over`. A `compare` + `then`
therefore escalates when the winner looks bad or when the branches disagree, where before only the
first condition fired. Disagreement is `1 − token-set overlap` of the branches' text, and the winner
attempt records it as calibration telemetry.

**Three strategy keys are refused instead of ignored.** `budget.max_attempts`,
`defaults.advanced.circuit_breaker` and `defaults.advanced.attempt_timeout` were accepted and never
enforced. `strategy validate` and the run path now refuse a file that declares any of them, and name
the enforced alternative (`budget.max_duration`, `limits.max_duration_per_doc`).

**SIGTERM stops a run the way Ctrl-C does.** The exit code is 143 with no ledger armed, and 6 with
one. A second stop signal of either kind is dropped. `serve` leaves SIGTERM to uvicorn.

**`strategy validate` errors on a gate that can never fire.** A dead predicate beside a live one now
reports at its own path instead of passing silently.

**A page range whose `end` is before its `start` is rejected** at request validation.

**The batch preflight states cost as a per-page floor**, and reports a `--jobs` value that a
backend's concurrency ceiling capped.

**The leaderboard prints a dash rather than `0.000` for a backend that scored nothing**, and it
tells a win from a tie.

**Ledger timestamps use a wall clock**, so a retention expiry survives a reboot.

### Removed

**`openreading compare --serial`.** The flag was never read by any code path. Compare's fan-out has
always been serial, and the concurrent mode the flag implied an opt-out of was never built. Fan-out
remains serial, with a deterministic subject order and one hosted call in flight. Passing `--serial`
is now an argparse error rather than a silent no-op.

**Strategy cost budgets.** The strategy layer no longer offers a cost budget or a cost estimator.
Prices change often and cannot be known reliably at plan time. The engine never estimates or
enforces a dollar figure, and only ever reports the real cost each backend billed.

- **Removed config options:** the Plain `max_cost` body key, `budget.max_cost_usd`,
  `limits.max_cost_usd_per_doc`, and `defaults.advanced.assumed_pages` / `assumed_rate`. The
  `budget:` object is now `{max_duration}`, and `limits:` is `{max_duration_per_doc}`. No surface
  accepts `budget.max_attempts`, and `strategy validate` refuses a file that declares it (see
  *Changed*).
- **Removed engine parts:** the rate×pages cost estimator, the per-node cost pool, the parallel
  cost-floor reservation, the leaf, decider and judge budget pre-checks, and the `budget_pruned`
  attempt category. The hosted-fan-out "money-safety wall", under which a `race` or `compare` over
  more than one hosted backend required a `max_cost`, is gone. No budget is required anywhere now.
- **Kept:** `usage.cost_usd` still totals every billed attempt, winners and losers. So do
  `cost_basis: "billed"` on trace attempts, the cheaper-backend tie-break on `pick: best` ties, and
  `openreading calibrate`'s advisory descriptor-based cost prediction. The time budgets
  `max_duration` and `limits.max_duration_per_doc` are unchanged. `budget_exhausted` now means only
  that the time deadline was reached. A parallel drain that would outlive the node deadline is still
  cut off on time (Law 6 in the `openreading.strategies.engine` docstring). It is recorded with no
  cost instead of a fabricated estimate.

**The `webui` extra and `openreading serve-ui`.** Neither ships in this package.

**The markdown reference pages `docs/credentials.md`, `docs/cli.md` and `docs/server.md`.** Their
content lives in the `openreading.credentials`, `openreading.cli` and `openreading.server`
docstrings (`uv run python -m pydoc openreading.cli`) and in the guides under `src/openreading/`.

### Added

**Google Gemini and Mistral OCR backends.** Two more hosted backends. Set `GEMINI_API_KEY` and run
`openreading parse --backend google-gemini`, or set `MISTRAL_API_KEY` and run `openreading parse
--backend mistral-ocr`. Both descriptors grade their capabilities `claimed`, which means vendor
documentation and fixtures back them and no live run has yet. Both fail closed when a compliance
claim is unverified. Neither declares native cancellation, idempotency, liveness or a batch path.

`google-gemini` sends the document inline to the Gemini Developer API's Interactions surface, and
returns native Markdown and schema-constrained typed fields. Text, blocks and table cells are
derived deterministically from that Markdown. Geometry and confidence are graded unavailable,
because the API returns neither. Token counts are reported, and `cost_usd` stays `unknown` because
pricing varies by model and service tier.

`mistral-ocr` runs Mistral Document AI OCR and annotations synchronously, over inline bytes or a
public URL. It returns native Markdown, blocks with normalized bounds and block confidence, and
typed fields. Text and table grids are derived deterministically. Cost is `estimated` from the
provider's processed-page count and the public OCR and Document AI page rates.

**NuExtract and open-ocr backends.** `nuextract` adds NuMind's job-based platform. Its `extract`
mode runs schema-first structured extraction from a template passed verbatim in
`extraction_schema.json_schema`. Its `parse` mode returns NuMarkdown, from which text, blocks and
table cells are derived. Token counts are reported and dollar cost stays `unknown`, because the
platform's token pricing is not public. `open-ocr` adds the open-ocr.com aggregator. It fans one
request out to the engine `OPENOCR_ENGINE` names, and returns plain text, a page count and one
document-level confidence. It is the one backend that reports the actual debit, so its cost basis is
`billed`. Neither adapter produces block geometry, and both omit that channel with a warning rather
than fabricate it.

**Example documents in the clone.** `examples/` ships two synthetic one-page bank statements, so a
first parse needs no key and no document of the reader's own. Both files are ReportLab output with
invented names, accounts and balances. [`examples/README.md`](examples/README.md) records what they
contain and where they came from. The root README's walkthrough now runs on them end to end, through
parse, compare, route, strategy and batch. The compare example shows a real OCR disagreement, where
`Account Holder:` is read as `; Account Hotder-` at `confidence: 0.0`. `samples/` stays gitignored
for the reader's own corpus. The generated `sample.pdf` the subsystem guides use is unchanged.

**Plain, the eleven-word strategy dialect.** Plain is a closed dialect of `openreading.yaml`. It
offers `try`, `race`, `compare`, `escalate_when`, `then` and `max_time`, the criteria `looks_bad`,
`low_confidence`, `missing` and `disagree`, and `auto`. It desugars to today's five-node grammar, so
there is still one engine, one trace and one validator. Front door:
[`openreading.strategies.plain`](src/openreading/strategies/plain.py). The schema is cut to
`strategy-config.v0.2.json`. v0.1 stays frozen and the config `version` const is unchanged.

- `strategy validate` badges each strategy `dialect: plain | advanced`.
- `strategy show` prints a body as written, and `strategy show … --longhand` the canonical tree.
- `explain` groups a Plain strategy's gate rows under their source word.

**Batch intake and corpus compare.** One `openreading parse` over a directory, a glob, many files
or http(s) URLs returns one JSON covering every document. The document can be in any format its
backend reads. Each
backend lists its own formats in
[`src/openreading/adapters/README.md`](src/openreading/adapters/README.md). Two such runs can be
compared per document. Batch composes the single-document pipeline and changes neither
`request.v0.1` nor `response.v0.3`. Design: the `openreading.batch` docstring
(`uv run python -m pydoc openreading.batch`).

| Artifact / feature | Notes |
|---|---|
| `batch-result.v0.1.json` (new family) | one response over many documents: per-item succeeded/failed/skipped with reasons, each succeeded item a full `response.v0.3`, plus a `summary` (counts, cost with its bases, per-backend tally) |
| `corpus-report.v0.1.json` (new family) | batch against batch: documents paired by `relpath`→`filename`→`sha256`, per-document `comparison-report.v0.2` plus a verdict rollup |
| adapter-descriptor `v0.3`→`v0.4` | optional `batch` block (native-batch declaration), additive |
| `openreading parse <dir\|glob\|files…>` | batch intake: deterministic recursive expansion, a format filter, `--jobs`/`--max-items`/`--save-dir`, exit `4` on partial |
| `openreading.run_batch(...)` | Python API returning a `batch-result` dict |
| `openreading compare runA.json runB.json` | corpus mode when the subjects are batch responses, giving per-document verdicts. `--format diffs` is value-first: the lines each backend captured that the other missed, per divergent document |
| native-batch protocol | opt-in `submit_many`/`normalize_many`, feature-detected. anthropic Message Batches is the reference implementation, gated to the live lane. Platform fan-out otherwise, observationally equivalent |
| `POST /v1/batch` | server route composing per-item requests. `make serve-smoke` exercises it |

**Document confidence, channel provenance and `schema_url` in the response.** These additions are
additive and backward-compatible, proven transitively in `tests/test_schema_evolution.py`.

| Field / artifact | Notes |
|---|---|
| `document.confidence` | document-level confidence `[0,1]`, the home for whole-document signals |
| `channel_provenance` | per-response `{channel: native\|derived}` (x-stability: **experimental**) |
| `schema_url` | the `$id` of the declared schema version (OTel `schema_url` pattern) |
| descriptor `output.block_granularity` | native block-granularity hint (`word`/`line`/`paragraph`/`section`/`element`) |
| `page_attribution_unavailable` warning code | the container-rule signal for page-unattributable derived blocks |

**`compare --format diffs`.** A content-equivalence verdict (packaging-immune token coverage) plus
structure deltas (`TABLES`, `TYPES`, `GRANULARITY`). A wall of findings becomes one screen of what
differs and whether it matters. See
[`openreading.comparison`](src/openreading/comparison/__init__.py).

**An execution journal, and `openreading resume`.** A run can write every step to a journal on
disk. An interrupted or failed run then restarts from where it stopped instead of from the first
page. `openreading resume <run-id>` replays the recorded decisions and re-runs only what did not
finish. Journal records are the vendored `journal.v0.1.json` and `step.v0.1.json` schemas. Payloads
are held in an encrypted blob store with a retention deadline, after which a resume fails with
`PayloadExpired` rather than returning stale content. The contract is the `openreading.ledger`
docstring (`uv run python -m pydoc openreading.ledger`).

**`openreading leaderboard`.** Rank several registered backends on one dataset by measured score
rather than by vendor claim. `openreading leaderboard <dataset_dir> --backends a,b` prints a ranked
table, and `--format json` emits the vendored `leaderboard-report.v0.1.json`. The repository ships
one synthetic sample dataset. A labeled corpus of your own is passed as a path and never lands here.

**Backend liveness reports.** A backend can be probed for whether it answers right now.
`openreading backends --check <slug>` and `POST /v1/backends/{backend_id}/liveness` both return the
vendored `liveness-report.v0.1.json`.

**Adapter descriptor `v0.5`, `v0.6` and `v0.7`.** Each version is additive over the one before it,
and `tests/test_schema_evolution.py` pins every released file byte for byte.

**`openreading --version`.** It prints the installed package number and exits 0.

### Fixed

- `pulse` uploads to `/extract` as multipart, and no longer leaks its id prefix into the response.
  Its structured extraction returns per-field confidence and citations.
- `nuextract` uploads a job as multipart, and reads table grids embedded as HTML.
- A `.env` value followed by an inline comment loads without the comment.
- A `pick: best` or `pick: fastest` parallel used as a cascade step now honors a step-position
  `escalate_if` on its winner. It was a silent no-op before (D-v3-7). This is both the Plain
  `compare` + `then` runtime and the cookbook's "wrap the race in a gated step" idiom.
- A URL document keeps the `mime_type` the caller passed instead of defaulting to PDF.
- A cached response is copied before it is annotated, so a cache hit carries one compliance
  confirmation and one fallback trail.
- The polling driver caps each sleep at the caller's deadline, and the fault streak resets on a
  healthy poll.
- A resumed run replays the step error's message, not only its class name.
- **`**` in a `parse` glob now matches every depth.** Python reads `**` as a plain `*` unless the
  caller asks for recursion, so `parse 'scans/**/*.png'`, the pattern the CLI reference itself
  printed, matched one directory level and reported success over a fraction of the corpus. It now
  walks the whole tree.
- **A glob keeps the directories it walked in each document's `relpath`.** A match used to be
  recorded under its bare filename, so two files named `invoice.pdf` under different parents
  collapsed into one `--save-dir` file and paired wrongly in a corpus compare. `relpath` is now
  measured from the pattern's fixed root, which is the identity the batch-result schema promises.
- **stdout carries only the envelope again on the paths that touch PyMuPDF.** Four modules
  imported the `fitz` alias, which prints a deprecation warning to stdout the first time any
  process imports it. `openreading compare DOC --all-ready --format json` therefore wrote a line
  of English in front of the JSON and stopped parsing at all. Every site now imports `pymupdf`,
  the same package under the name that stays quiet, and a test refuses the alias tree-wide. The
  `pymupdf` floor moves to 1.24.3, the release that introduced that name.

### Security

**`document.path` over HTTP is refused unless you opt in.** `openreading serve` refuses any request
whose document names a local path unless `OPENREADING_SERVER_PATH_ROOT` names a directory, and then
accepts only a regular file beneath it. The CLI and the Python API still accept a path, because
there the caller and the machine are one trust domain. Before this change any HTTP caller could make
the server read a file on its own disk.

**A document URL must resolve to a public address.** A `document.url` whose host resolves to a
loopback, private, link-local or reserved address is refused before any connection is opened, on
every surface. Set `OPENREADING_ALLOW_PRIVATE_URLS=1` for an intranet document store. The fetch
connects to the one address it vetted, streams under the 100 MB cap, and refuses redirects rather
than following them.

**Webhooks from `chunkr` and `open-ocr` carry a per-job token.** The server appends a token to the
callback URL it registers, and answers 401 to a callback that does not present it. Set
`OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1` for a vendor that strips query parameters. `reducto` keeps
its Svix signature check.

**A scoped API key is bound to every backend a request can reach.** `OPENREADING_API_KEY_SCOPES` now
prunes the fallback chain and every strategy tree, not only the backend a request names. A request
that can reach no in-scope backend fails with 403 `scope_denied`.

**The server bounds what it holds in memory.** A request body over `OPENREADING_MAX_BODY_BYTES`
(150 MiB) is refused with 413. `POST /v1/compare` accepts at most 50 responses and
`OPENREADING_MAX_COMPARE_BYTES` (8 MB) of body. A finished job is swept after
`OPENREADING_JOB_TTL_S` (3600 s). The job store holds at most `OPENREADING_MAX_ASYNC_JOBS` (1000)
records, and at most `OPENREADING_MAX_JOBS_PER_PRINCIPAL` (100) per API key.
`DELETE /v1/jobs/{job_id}` frees a record early. A job record no longer keeps the document bytes or
the document password.

**Ledger blobs are encrypted with AES-256-GCM.** A blob written by an earlier build still reads. A
tampered or truncated blob now fails with `PayloadExpired` instead of decoding to garbage. The
server sweeps expired ledger content every `OPENREADING_RETENTION_SWEEP_S` seconds (3600), and once
at startup. `cryptography>=50.0` is a base dependency.

**`require_local` reads where a service backend points.** A `docling` or `qwen-vl` endpoint
configured at a non-loopback address no longer passes `require_local`.

**`pypdf>=6.15.0` is required.** It closes PYSEC-2026-3655 and PYSEC-2026-3656. `make audit` runs
pip-audit against the resolved lock, and CI runs it on every push.

## [0.4.0] - 2026-07-27

**Compare.** The read-only cross-backend delta layer. Run a document through many backends and see
how they differ. The report says which fields each backend got, missed, or disagreed on. It says
where two disagree on a value, which blocks only one saw, and what each cost. Every backend returns
the one response shape, so
`compare` is a pure function over N schema-valid responses. It is a leaf feature that changes no
adapter, no router, no engine, and not the response schema. Settled design in
[`openreading.comparison`](src/openreading/comparison/__init__.py).

### Added
- **The report.** Vendored `comparison-report.v0.1.json` (validated in `make verify`), with a closed
  finding vocabulary (12 codes) and field verdicts (`agree`/`disagree`/`partial`/`unique`/
  `not_capable`).
- **Four dimensions.** A facts scoreboard (state, latency, cost, counts). A field delta over
  `typed_fields` with a deterministic value-equivalence ladder (exact → normalized → money → number
  → date). A text delta, which is a canonical-text similarity matrix plus a unified diff. A
  structural delta, text-first and IoU-validated block alignment with granularity merge, covering
  type and position conflicts, confidence gaps, block missed or unique, and table shape.
- **No fabricated misses.** A backend that cannot produce a dimension, having no confidence or no
  blocks, is `not_capable` rather than "missed". Symmetric mode reports differences and names no
  winner. `unaligned_ratio` is a first-class trust signal.
- **Stances.** Symmetric (with a `consensus` section at N≥3), `--baseline` (sign deltas against one
  subject), and `--truth` (score each subject with the shared `evals.scorers`, one metric stack).
- **Surfaces.** `openreading compare` over files, a `--backends` fan-out or `--from`, with
  `--format json|table|diff|md`, delta-first. `openreading.compare()` in the public API. The pure
  `POST /v1/compare`. `explain` renders a report. `parse --keep-candidates` retains a strategy run's
  discarded branches under `orchestration.candidates[]` for `compare --from`. It is opt-in and
  additive with no schema change, so a direct run stays byte-identical.
- **Harness.** A test kit: a response builder, an alignment torture table, and an
  equivalence-ladder table. A property suite covers schema-validity, determinism, reflexivity and
  order-invariance. A live heterogeneous corpus (pymupdf × tesseract) comes with leaf-isolation and
  byte-identity guards, and with surface tests. `make compare-smoke` is joined to `verify`.

## [0.3.0] - 2026-07-25

**Strategies.** The optional multi-backend orchestration layer. An `openreading.yaml` composes
backends into quality-threshold cascades, parallel fan-outs, routes, and LLM decision points. A
deterministic engine executes it under hard rails: compliance is never widened, budgets are
enforced, and every decision is traced. Settled design in
[`openreading.strategies`](src/openreading/strategies/__init__.py).

### Added
- **Config spine.** The vendored `strategy-config.v0.1.json` grammar: five node types plus
  shorthands, gates, budgets, `on_error`, `route`, `decide`, `policy`, `limits`, `decider` and
  `defaults`. A discovery-ordered loader. `normalize`, which resolves shorthand to longhand,
  `extends` and the four presets. A complete `validate` world-consistency catalog wired into
  `openreading.schemas validate`.
- **Serial cascade engine.** Quality-gated fallback with a reference-free signal probe. The probe
  (`signals.py`) covers text metrics, a garble composite, table sanity, and PDF-layer scanned and
  text-source detection. The keep-best law, `on_error` routing and budget pruning come with it.
- **`route` nodes and facts.** First-match dispatch on doc type, mime, page count, size and
  compliance, with all rules evaluated for the trace, which makes a shadowed rule debuggable.
- **`policy:` and `limits:` blocks.** A file-level compliance policy that only ever narrows the
  effective request, plus outermost per-document cost and duration budgets.
- **Parallel execution.** `pick: fastest`, `best` and `merge`, hedging (`start_after`), shadow
  branches, budget reservation, `require: <n>`, and a coordinated `FakeClock` for offline
  determinism.
- **Decision points.** The env-gated LLM decider and judge contract: a two-key gate, a compliance
  gate, a strict-tool schema, and engine re-validation. It adds a pairwise judge, a downgrade
  taxonomy, per-call metering, and `replay --trace` with `mask_fields` and canary checks.
  Exercised end-to-end by
  in-process fakes. The live wire adapter is not built, and the gap is recorded in
  `src/openreading/strategies/README.md`, *Not built yet*.
- **Page granularity.** `granularity: page` with per-page gates, range escalation and stitching,
  plus `pick: merge` (a typed-field vote ensemble) and `pages[].source_backend` in the response.
- **`calibrate`.** Derives gate thresholds from a labeled sample by sweeping each gated predicate
  over its domain.
- **Surfaces.** `parse --strategy/--no-strategy/--config`, `strategy
  plan|show|list|normalize|validate|calibrate`, `explain`, and `replay`. `api.run(strategy=,
  config=)`. Server config via `OPENREADING_CONFIG`. A `strategy-smoke` target added to
  `make verify`.

### Changed
- Response schema **v0.2**, additive and backward-compatible. An `orchestration` object carries the
  strategy, chosen backend, fallback depth, attempts, decisions, drops, and per-page trace.
  `pages[].source_backend` is added. A no-config run remains byte-identical to the legacy path.

## [0.2.0] - 2026-07-24

**Hosted backends with your own keys.** Hosted backends run against real provider APIs from your own
keys, exposed through three surfaces.

### Added
- **11 adapters, all conformance-verified** against fixtures: pymupdf, tesseract, docling, qwen-vl,
  reducto, chunkr, pulse, anthropic-claude, aws-textract, azure-document-intelligence,
  google-document-ai.
- **BYO-key credential broker.** `.env` and environment resolution driven by each descriptor's
  `credentials_spec`. Preflight errors name the exact missing variables and the provider's signup
  URL. Invalid keys are handled clearly, and ambient SDK chains (boto3, GCP ADC, Anthropic) are
  preserved. A key is held in memory for the duration of a call, and is never written to disk or to
  a log.
- **Three surfaces.** The CLI (`parse`, `route --run`, `backends`, `serve`). The Python API
  (`openreading.run()` and `route()`). An HTTP server carrying `/v1/parse`, `/v1/route`,
  `/v1/jobs`, `/v1/webhooks` with Svix signature verification, `/v1/backends` and `/healthz`. One
  job state machine covers `INLINE`, `POLL` and `WEBHOOK`.
- **Live-test lane.** Keyed `@pytest.mark.live` tests for every hosted backend with clean skips, a
  record mode with secret scrubbing, `make verify-live`, and a manually dispatched CI job.
- **Documentation.** A README quickstart, `.env.example` covering every backend with signup URLs,
  `docs/credentials.md`, `docs/cli.md` and `docs/server.md`, and an offline CI matrix
  (Python 3.11 and 3.12).

## [0.1.0] - 2026-07-22

**The contract and engine core.** The foundation is one request shape and one response schema over
many backends, with a compliance-first router and a fully-local tier. The contract rests on a survey
of 127 backends profiled from primary sources. That survey is not part of this repository.

### Added
- **The contract.** Vendored request, response and descriptor JSON Schemas with pydantic mirrors,
  canonical bounding boxes with lossless `bbox_native`, a four-category error taxonomy, and an anyOf
  response envelope.
- **Adapter interface.** The eight-method adapter interface with a static `AdapterDescriptor`
  (N/D/X channels). The router never branches on backend type.
- **Compliance-first router.** Three stages, with compliance never relaxed by fallback and
  unverified claims failing closed. An executable fallback chain carries an executor that skips on
  missing credentials, an attempt trail, `PlanExhaustedError`, and a bounded idempotency cache.
- **Local tier.** The `pymupdf` and `tesseract` adapters make no network calls, so documents never
  leave the environment. They are the pattern the descriptor-driven hosted adapters follow.
- **Tooling.** A conformance kit and a secret scrubber ship in the package for downstream adapter
  authors. An evaluation harness ships too, with scorers, a runner, and one synthetic sample case.
