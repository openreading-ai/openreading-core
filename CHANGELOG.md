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

- **HTTP file uploads.** You can send client files to parse, route, and jobs using multipart requests.
  Uploads reuse existing response schemas and need no server filesystem access configuration.
  A standard-library folder client sends files serially and saves responses under their relative paths.
  File, metadata, and streamed body limits return 413, while temporary uploads close before execution.

- **Website and tutorial ownership.** The OSS pages and guided walkthrough live in
  `openreading-web`, served at `/oss` and `/oss-tutorial` through Firebase.
  Core retains its README diagrams and points readers to the hosted tutorial. Tutorial checks
  run in the web repository against core, so core verification needs no website checkout.

- **`strategy-config` v0.3.** The `policy:` block is a closed, typed object: the five compliance
  keys carrying `request.compliance`'s own descriptions verbatim, `optimize_for` as its enum, and
  the three attestation keys as `boolean` and `array` of `string`. A quoted `"false"` and a bare
  `reducto` where a list belongs are refused by the schema now rather than by a hand-written
  validator standing in for it, and `validate_policy`, `POLICY_KEYS` and `PolicyError` are gone
  with it. `doc_type_hint` leaves the policy grammar and stays a request field, because no routing
  stage reads it and a key that does nothing in a file that gates compliance is one a reader will
  try to rely on. The config `version` const stays `1`: no file that was valid and meaningful
  becomes invalid.
- **`tutorial/README.md`, the guided walkthrough.** Seventeen steps from `uv sync` to a
  policy, a self-escalating strategy, a folder run and `openreading serve`, over the documents in
  `examples/`. It needs no key and no network, because it runs on `pymupdf` and `tesseract` only.
  Three IRS forms join the two bank statements there: `schedule_a_2024.pdf` and `1040_2024.pdf`
  are born-digital and come back as table blocks, and `1040-1988.pdf` is a five-page scan with no
  text layer, which is what makes an escalating strategy demonstrable on a shipped document.
  The tutorial and its configuration checks now live in `openreading-web`, alongside the website.
- **`openreading help [TOPIC]`.** The CLI now carries its own manual. `openreading help` prints a
  topic index grouped by what you are trying to do, and `openreading help batch` prints one
  chapter. The chapters are sections of the `openreading.cli` package docstring, located by
  heading and printed verbatim, so there is one source and `help`, `pydoc` and the reference
  cannot disagree. Aliases reach the same chapter, so `help folder` and `help glob` both open the
  batch chapter.
- **Every command carries a worked epilog.** `openreading <cmd> --help` now shows runnable
  examples, the verb that consumes this one's output, the exit codes that command can actually
  return, and a pointer to its chapter. Eleven commands previously had no description at all.
- **`openreading --help` has a front door**: a quickstart that runs from a fresh clone with no
  key, a task map from what you want to what you type, the folder and glob rule stated where
  everyone sees it, and the chain picture.
- **`parse`, `compare` and `leaderboard` group their flags.** The `parse` group title states the
  rule that decides which envelope you get back: "many documents (a directory, a glob, or two or
  more FILE arguments)".

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

**File and request constraints intersect, and neither can weaken the other.** The union took the
request's value for `data_region` and `max_retention` whenever it had one, which read as "the
caller is more specific" and behaved as "the caller may relax the deployment": a file requiring
`max_retention: zero` and a request asking for `48h` produced `48h`, so a backend retaining data
for 24 hours survived a policy that forbade retention outright. Retention now keeps the lower
ceiling. Two different regions refuse with `region_conflict`, because regions have no ordering and
no value means both. `optimize_for` still takes the request's value, because it orders the
survivors and never changes the set.

**A policy is enforced at every public entry point, not only through `openreading.run`.**
`compile_strategy` assumed some earlier surface had folded the block in, so a caller who built a
`StrategyConfig` and compiled it got no policy at all; it now applies the block itself, which is
idempotent. `config.apply`, `config.router_config` and `StrategyConfig` validate a mapping into a
typed `Policy` before reading a field, so the two shapes that used to buy permission instead of
raising (`allow_unverified_compliance: "false"`, which is truthy, and a bare
`train_optout_confirmed: "aws-textract"`, which became a set of characters) are refused from
Python exactly as the schema refuses them from a file.

**`leaderboard` applies the file's compliance to every case.** It passed only the three
attestations through, so the keys that widen the eligible set applied while the five requirements
they qualify did not. A hosted backend could be ranked under `require_local: true`.

**One batch reads the file once.** A two-item batch parsed it three times, so items from one
returned envelope could run under different policies.

**Resume compares the policy as written.** The ledger stores the request after the file was folded
into it, so removing a constraint left the stored request carrying it and the run identity
unchanged: the resume ran under a policy the file no longer asked for and said nothing. The block
as written now takes part in `config_hash`, so adding and removing both refuse. Runs armed before
this change cannot be resumed and report a header mismatch.

**Publisher pipeline identity follows the file's content.** It hashed the path, so editing the
policy left the artifact directory and resume identity unchanged and a rerun reused results
measured under the previous policy. Reformatting or reordering keys still resumes.

**A policy is written once, in `openreading.yaml`.** The `policy:` block of that file is now the
only place a compliance policy is spelled, and every command, every Python call and the server
find that file the same way and read the same block. `route` and `leaderboard` gain `--config`,
the two of the seven `--policy` verbs that lacked it. `config=` accepts a path or a mapping of the
file's own shape, so a caller with no file on disk writes `config={"version": 1, "policy": {...}}`
and gets the identical validation a file gets. Discovery order is unchanged, and a directory with
no file routes exactly as it did before. Reading the file no longer imports the strategy engine,
so a run that names a backend pays nothing for a package it does not use.

**A directory's `openreading.yaml` now gates a run that names a backend.** `parse --backend
reducto` in a folder whose file says `require_local: true` is refused, where before it ran as
though no file existed. That is the point of the file, and the refusal names the key. Anyone
keeping a strategy file next to documents they parse by name should read its `policy:` block
before upgrading.

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

**`auto` leaves every strategy dialect.** It survived the removal set in longhand, where a leaf
`{backend: auto}` still loaded and `engine._resolve_backend` resolved it at dispatch to the first
candidate the walk had not tried. That is the same inference the removal set deleted everywhere
else: `auto` meant "the best remaining backend", and the ranking that made "best" mean anything
was the compliance filter, the capability gate and the cost scorer, all of which are gone. It also
meant a strategy file could not be read: a rung naming no backend does not say what it will run.
`loader._refuse_auto` now refuses it in every dialect at load, naming the replacement, and Plain's
own earlier refusal stays because a Plain author is reading a different page. What replaces it:
name the backend the rung runs, or write the deployment's preferred order once in
`policy.backends` and leave the request unnamed. Two consequences worth knowing. `CompiledPlan.
dispatchable` is now exactly the ids the tree names, where it used to widen to the whole candidate
chain whenever any leaf said `auto` — so the `Sanitizer` and `pinned_eligible` are armed for what
can actually run and nothing more. And the `exhausted` error class no longer has a leaf-level
cause: only a composite that ran out of children raises it. `POST /v1/jobs` and
`openreading strategy --help` stop naming `auto` as a value a caller can send.

**`--policy PATH`, the `policy=` keyword, and three server environment variables.** The flag is
gone from `route`, `strategy validate`, `strategy plan`, `replay`, `calibrate`, `benchmark run`
and `leaderboard`; passing it is an argparse error and exit 2. `policy=` is gone from
`build_request`, `route`, `run` and `run_batch`; passing it raises `TypeError` naming the
replacement. `OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE`, `OPENREADING_TRAIN_OPTOUT_CONFIRMED` and
`OPENREADING_BAA_TIER_CONFIRMED` are no longer read: the server takes all three attestations from
the file's `policy:` block instead, and a deployment that still sets one gets the file's posture
rather than a widened one. Write the same keys in `openreading.yaml` and pass `--config` or
`config=` where a path is needed. The package is pre-release with no tag, so the flag is removed
outright rather than tombstoned.

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

- HTTP validation errors omit document password values, including incorrectly supplied numeric values.

- Recursive globs select each file once, including patterns such as `corpus/**` that also match
  directories. This prevents repeated backend calls and premature failures from `--max-items`.
- CLI help examples now use completed comparison alternatives, backend labels and resumable
  single-document runs. New `help` and `datasets` chapters cover manual usage and scoring inputs.
  Calibration help includes a runnable cascade and explains where its proposed thresholds belong.
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
- **`explain` reads a folder run.** A `parse <folder> --strategy X` run is a batch-result holding
  a response per document, so the orchestration sits one level down. `explain` read only the top
  level and told the reader "was it a strategy run?" when it was. It now walks the items, names
  each document, and names the ones that carry no orchestration rather than dropping them.
- **`compare --from` says why a run kept no candidates.** It used to tell the reader to re-run
  with `--keep-candidates`, which is usually the flag they already passed. The real cause is a
  strategy that never branched, and the message now says which case it hit.
- **`calibrate` refuses a parallel first rung instead of raising KeyError.** A `compare:` or
  `race:` step compiles to a node that names no single backend, and calibration sweeps one.
- **`compare <folder> --backends a,b` is usage, not an errno.** It reached the adapter and came
  back as a raw IsADirectoryError at exit 1. It now exits 2 and prints the way through.
- **`benchmark --target backend:NAME` checks the name.** It validated the syntax and never the
  identity, so a typo priced a run that could not exist and exited 0. It now refuses an unknown
  id by name and lists the known ones, the way every other place a backend id is typed does.
- **`compare <missing.pdf> --backends a,b` is usage, not an errno.** `parse` refused a mistyped
  filename at exit 2 with a sentence; fan-out returned a raw `SourceNotFoundError: [Errno 2]` at
  exit 1.
- **The caller names the backends, and nothing else decides.** The compliance filter, the
  stage-3 scorer, the capability gate and `auto` are all removed, and `policy.backends` replaces
  them in the same release so no caller is left without a way to bound the backend set. Selection
  is a lookup: the backend you named, else `policy.backends` in written order, else `pymupdf`,
  which needs no key and no config. An empty list permits nothing and refuses with `scope_denied`.
  Every source of an allow-list intersects and none widens.
- **`compliance` leaves the request and the descriptor.** 180 vendor claims across 15 adapters:
  whether each signs a BAA, trains on customer data, which regions it offers, how long it retains
  a document. Nothing in this package could observe any of it, so a stale entry did not fail
  loudly, it routed a document to a backend the operator believed was excluded and the run
  succeeded. Gone with it: the nine stage-1 drop codes, `ComplianceRefused`, the 403
  `compliance_refused`, `BAA_TIER_CONFIRMED_WARNING`, and the three attestation keys.
- **`optimize_for` is removed.** Four documented values feeding one formula. Its "quality" term
  ranked by `integration_priority`, this project's own P0/P1/P2 build label, and `latency` read no
  latency figure because no descriptor carried one: measured, `latency` and `accuracy` returned
  byte-identical chains. `integration_priority` and `priority_reason` leave the descriptor too.
- **The capability gate is removed.** `_truthy_cap` passed `claimed` and `verified` identically,
  so a vendor's documentation gated dispatch exactly as a test we ran did. The `False` side was
  worse: asking for signature detection dropped thirteen backends, Azure among them, which ships
  it. A backend that cannot do a thing refuses first-hand now.
- **`auto` is deleted.** It asked the engine to infer from data it could not verify. Requests that
  named it now name no backend, which resolves through the three rules above. **A request with no
  list gets a chain of ONE.** Previously `auto` fanned out to all fifteen backends; to get a
  fallback chain, list the backends you want.
- **Three schemas bump**: `request` v0.3 (no `compliance`, no `optimize_for`, `backend.id`
  nullable), `adapter-descriptor` v0.8 (no `compliance`, no priority hints), `strategy-config`
  v0.4 (`policy` is one key; `when` drops the `compliance` fact). A v0.8 descriptor is **not** a
  valid v0.1-v0.7 descriptor, because those required a `compliance` block.
- **The ledger keeps no policy about your disk.** Retention, the reaper, the expiry stamp,
  `OPENREADING_LEDGER_RETENTION_HOURS`, `OPENREADING_RETENTION_SWEEP_S` and the server's sweep
  loop are all removed, and so is encryption at rest. Retention was a destructor whose only job
  was deleting the caller's data on a timer, defaulting to a number its own source marked
  `# placeholder`, with a ceiling derived from `min(max_retention_hours)` across vendor
  descriptors: an unverifiable claim about somebody else's servers decided when files on your
  machine were destroyed. Encryption kept `keys/<run_id>.key` in the same directory tree as the
  ciphertext it protected, so it bought one narrow scenario at the cost of a native dependency on
  every install. **`cryptography` is no longer a dependency.** Erasure is `rm`, on whatever
  schedule your own policy sets.
- **`OPENREADING_LEDGER` says what it copies.** Arming it means "copy every document I process,
  and every full response, into this directory, in the clear". The response blob is written
  whatever `include_backend_raw` / `typed_fields` / `image` the request asked for, so the ledger
  can hold data a caller excluded from their own response. That disclosure is now in the
  `openreading.ledger` docstring, where the variable is documented.
- **A run journaled by an older build cannot be read by this one.** Its blobs are encrypted and
  nothing here decrypts them. The content is reproducible by re-running, and the ledger is
  arming-gated and pre-release, so no decryptor ships.
- **The router no longer decides what a backend can read.** The stage-2 format gate dropped a
  backend when the request's MIME type fell outside its descriptor's `input_formats`. Measured
  before removal: `.docx` dropped nine backends and `.svg` dropped none, because an unknown
  extension became `application/pdf` before the router saw it, so the gate was a projection of
  core's own table rather than knowledge of any vendor. Being wrong in the `False` direction
  silently excluded a backend that could have done the job. A backend that cannot read a document
  refuses first-hand now, and the fallback chain already handles that. `input_formats` stays on
  the descriptor as documentation; nothing branches on it.
- **A batch dispatches every source the caller named.** Intake used to sort files against a
  26-extension table and skip the ones it judged unsupported. `skip_reason`, the `skipped` item
  state and `summary.skipped` are gone with it (**`batch-result` v0.2**), and a file the backend
  cannot read is a `failed` item carrying that backend's own reason. Hidden files are still
  excluded, which is a rule about visibility rather than format. Two consequences worth knowing: a
  directory holding one unreadable file now exits **4** (batch partial) where it used to exit 0,
  and `openreading parse doc.txt --backend pymupdf` gets pymupdf's own refusal rather than the
  CLI's pre-check, with the same information in it.
- **One MIME resolver, and it never guesses PDF.** Core carried six extension-to-MIME tables and
  five defaulted an unrecognised input to `application/pdf`, so seventeen of the twenty-six
  extensions the batch layer already knew about, `.svg`, `.html`, `.epub`, `.txt` and more,
  reached a backend labelled as PDFs. That is worse than misjudging a capability: the vendor
  accepts the bytes and returns confident output, so nothing raises and no fallback fires.
  `openreading.derive.mime.resolve_mime_type` is the one decision point now, taking the caller's
  explicit type, else the bytes by signature, else the filename, else **`None`**. Content beats
  filename because a name is a claim and bytes are a fact: a PDF saved as `scan.txt` now resolves
  to `application/pdf`. Adds `puremagic>=1.30,<2` (MIT, pure Python, no system package).
- **A document core cannot identify is refused, not relabelled.** `anthropic-claude`,
  `google-gemini`, `google-document-ai` and `mistral-ocr` each guessed their own media type and
  fell back to PDF. They now read the resolved type and raise `unsupported_input` when it is
  absent, naming `document.mime_type` as the fix. This supersedes D-v2-9's rule that bytes with no
  `mime_type` are a PDF: unnamed bytes are the case core knows least about, which made it the
  least defensible place to invent a type.
- **`granularity: page` re-parses only the failing pages, on the backends that can.**
  `_supports_page_ranges` read `page_range_selection` through `getattr` on `Capabilities`, which
  is `extra="allow"`, and no shipped descriptor declared it. The lookup could not raise, so it
  answered `False` for all fifteen backends and every page-granularity rung silently ran document
  granularity, re-parsing whole documents. The field is declared now, and `pymupdf`, `tesseract`,
  `qwen-vl` and `mistral-ocr` set it, each having read `pages.ranges` all along. `open-ocr` does
  not: `max_pages` is a ceiling, not a selection. Editing those four descriptors changes
  `config_hash`, which folds a digest per descriptor by design (BL-163), so a run journaled before
  this release and resumed after it is refused with a hash mismatch. Finish in-flight runs before
  upgrading, or re-run them.
- **Core quotes no price for anything.** Cost was three things wearing one word. An
  **observation**: `pages_processed`, `credits`, `input_tokens`, `output_tokens` are counters the
  vendor returned for this call, and `duration_ms` comes off a clock on this machine. Those stay.
  An **assertion**: `descriptor.cost.usd_per_page_equiv_low`/`_high` on fifteen adapters, plus
  private price tables inside four of them, one dated `accessed 2026-06-24` in its own comment.
  Someone read a pricing page and typed numbers into Python. And a **derivation** laundering the
  second into the first: `router/cost.py` multiplied the tables out into `response.usage.cost_usd`
  and set it beside `input_tokens`, where no caller could tell which number was counted and which
  was guessed. The assertion and the derivation are gone. **Removed:** `descriptor.cost` and
  `provisioning.billing_target` (**`adapter-descriptor` v0.8**), `usage.cost_usd` and
  `usage.cost_basis` (**`response` v0.3**), `summary.cost_usd` and `summary.cost_bases`
  (**`batch-result` v0.2**), the `cost_outlier` compare finding and the per-subject cost columns
  (**`comparison-report` v0.2**), the leaderboard's `cost_per_doc` column
  (**`leaderboard-report` v0.1**), the `CostBasis` enum, `StepCost` on the ledger record
  (**`step` v0.1**, **`journal` v0.1**), the strategy engine's whole money fold (`_set_total_cost`,
  `_fold_basis`, `_branch_cost`, `_rung_basis`, `Attempt.cost_usd`/`cost_basis`,
  `Trace.total_cost`, `DecisionVerdict.cost_usd`, `JudgeVerdict.cost_usd`), `calibrate`'s
  `cost_per_doc` and its `--max-cost-per-doc` flag, and the benchmark spending preflight
  (`estimate_cost`, `CostEstimate`, `CONFIRM_ABOVE_USD`). `CostReport` keeps `native_unit`,
  `native_quantity`, `breakdown` and `duration_ms`. Every schema touched is an unreleased cut, so
  no released file moves.
- **Two tie-breaks and one gate change behaviour.** `pick: best` and `pick: merge` broke a tie on
  the cheaper backend, read from `descriptor.cost`; they break on the **first-listed** branch now,
  which is the author's own statement of preference and a fact core can actually check. The
  benchmark confirmation prompt fired above one dollar; it fires above **25 pages** on a hosted
  target, or whenever a target's call count cannot be stated at all, which is what a `strategy:`
  target is.
- **`openreading help cost` is now `openreading help usage`,** and `cost` is an alias so the old
  spelling still opens it. The chapter reports what a run consumes rather than what it charges.
  `benchmark estimate` and the batch `[preflight]` advisory both count calls and pages instead of
  multiplying a rate: `[preflight] 16 items on hosted backend reducto: 16 call(s) on your own key`.
- **`config_hash` moves again.** Deleting `cost` from every descriptor changes the per-descriptor
  digest it folds (BL-163), so a run journaled before this release cannot be resumed after it.
  Same remedy as the `page_range_selection` change above: finish in-flight runs first, or re-run.
- **`policy.backends` is a default chain, and the API-key scope is the boundary.** The removal set
  left the documentation claiming that nothing widens the list, "not a fallback, not a named
  `--backend`, not a strategy rung". Two of those three were never true of the code. A request that
  names NO backend resolves through the list and `routing.fallback` reorders within it; naming a
  backend, on the command line or as a rung inside your own strategy file, runs that backend, list
  or no list. On one machine the operator and the caller are the same person, and refusing what
  they just typed helps nobody. Where they are two different people, the server's API-key scope
  (`OPENREADING_API_KEY_SCOPES`) is the boundary: it refuses an out-of-scope backend with
  `scope_denied` before any credential is resolved, whatever the request named, and prunes a
  strategy's rungs to what the token may reach. Behaviour is unchanged; every page that said
  otherwise now says this.
- **`openreading strategy plan` crashed under any written policy.** `_canonical_router_config`
  dispatches on field type so a future `RouterConfig` field cannot fall through to a
  nondeterministic `default=str` (BL-163). `backends` is a TUPLE, which the dispatch did not
  recognise, so every strategy compile under a `policy.backends` list raised `TypeError:
  RouterConfig.backends is a tuple` at the hash rather than routing. Ordered types are now hashed
  in written order, never sorted: the order IS the chain, and two lists naming the same ids in
  different orders are two different runs.
- **The `compliance` decider downgrade reason is removed** from the closed `DOWNGRADE_REASONS` set.
  Its only producer was a `ScopeRefused` from the compliance filter, and the filter is gone, so it
  could never be emitted. `scope_denied` remains and is the reason a decider or judge backend
  outside the caller's allow-list is refused. Also gone with it: `evals.dataset`'s forwarding of a
  per-case `compliance` block into `request_body`, which the request schema now rejects outright;
  the `compliance` route fact, which matched on a posture core computed from that same table; and
  the `strategy validate` unreachable-step warning, whose evaluator row 4 deleted underneath it.
- **A URL-sourced document is no longer written to the ledger, and such a run cannot be resumed.**
  `document.url` is secret-class: a presigned URL is a live credential, and routinely the only
  thing standing between a reader of the file and the object. It used to be routed through the
  blob store, which was defensible while that store was encrypted. Removing the cipher removed the
  defence, so the URL is not persisted at all: the header records `document_is_url` with no
  document, and `openreading resume` on that run exits 3 with `payload_missing` instead of
  replaying against an input it does not hold. Materialize the document before arming the ledger
  if a URL-sourced run has to be resumable. Runs from `bytes_base64` are unaffected.
- **A strategy pins every backend it can dispatch, not only the ones its policy chain names.**
  `policy.backends` is a default chain, so a strategy may name a backend outside it and that
  backend runs. Everything derived from the chain missed it: the ledger pinned an incomplete
  `pinned_eligible`, the URL-materialization check consulted the wrong descriptors, `config_hash`
  folded no digest for it, and — the one that matters — the `Sanitizer` was armed without its
  credentials, so a failure message carrying that backend's key would have been journaled to a
  plaintext file unredacted. `CompiledPlan` now carries `dispatchable` beside `eligible`: the
  concrete ids the tree names. (It also carried the whole candidate chain when a leaf was `auto`,
  until `auto` was removed above.)
- **A descriptor's vendor claims are documentation, and core never branches on one.** The removal
  set deleted three features that read a per-vendor table and decided with it: the compliance
  filter, the capability gate and the cost scorer. That left the fields themselves, read at zero
  sites, and a proposal to delete them too. They stay instead. A maintainer's dated reading of a
  vendor's own documentation is useful to a person choosing a backend; what core has no business
  doing is BEHAVING on it, because a claim about a company this project does not control goes
  stale without notice and nothing here can detect that. `openreading.types.descriptor` now states
  which fields are load-bearing (`id`, `type`, `wait_modes`, `protocol_version`,
  `credentials_spec`, and the rest of what core verifies every run) and which are claims, and
  `tests/test_descriptor_is_documentation.py` asserts all 28 claim fields are read at zero sites.
  A change that starts branching on `capabilities.ocr` or `max_pages_per_request` now fails
  `make verify` and has to say what happens when the vendor revises it. Keeping the claims current
  is a documentation job with its own procedure in the `openreading.adapters` runbook: re-read the
  pages a backend's `sources` names, update the cells that moved, and set `accessed` in the same
  commit.
- **Every page that described the removed machinery is rewritten**, not annotated: the `router`,
  `config`, `api`, `schemas`, `batch`, `server`, `strategies` and `openreading` package docstrings,
  the adapters catalog (its compliance table is replaced by where each backend runs, its license
  and its signup page), the strategies guide's policy walkthrough, the CLI manual, the tutorial and
  the root README. `openreading help cost` is `openreading help usage`.
- **`--pages` explains the argparse trap it falls into.** `parse --pages 1 doc.pdf` feeds the
  file to `--pages`, and the error named a private function at the reader.
- **`anthropic-claude` sends an image as an image.** A PNG or JPEG was labeled `application/pdf`
  in a document block, on the single request and the native batch alike. It now travels as an
  image block carrying its own media type, and citations stay on the PDF path, which is the only
  block type that accepts them.
- **`POST /v1/route` answers 502, not 500, for a body it must refuse.** A body setting
  `runtime.endpoint` or naming an unapproved `credentials_ref` alias reaches the refusal during
  routing now that stage 1 resolves endpoints. The handler returns the documented 502 with its
  `backend_code`, the same answer `POST /v1/parse` gives.
- **Scoped parse requests preserve routing refusals.** A scoped API key triggers routing during
  its access check, before execution begins. Endpoint overrides and unapproved aliases return
  the documented 502 with their error code, including requests that select `strategy:none`.
- **The source distribution carries the source.** Local agent scratch and its dependency caches
  were packaged into `sdist`, which built at 259 MB. It is 8.4 MB.

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

**`require_local` reads the endpoint an approved alias selects.** A `credentials_ref` alias sets
configuration as well as credentials, so `MYALIAS_ENDPOINT` overrides the operator's local default
for the run. Compliance read only the default, so a request naming an approved alias passed
`require_local` and `require_baa` and then dispatched off the machine. Stage 1 now resolves the
endpoint through the same broker the run executes with.

**A glob no longer reaches outside the tree it names.** `parse 'corpus/**/*.pdf'` followed a
symlinked file or directory under `corpus/` and read documents from wherever it pointed. A match
with a linked ancestor is skipped. A symlink you name yourself is still read, because naming it is
a deliberate choice.

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
