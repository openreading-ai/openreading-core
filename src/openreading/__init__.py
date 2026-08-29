"""OpenReading — one unified API for document processing.

One request shape, one response schema over 13 backends (hosted APIs, OSS libraries, an OCR
binary, self-hosted models) so a caller swaps `backend` from `pymupdf` to `reducto` to
`aws-textract` and nothing else changes. Bring-your-own key (charges land on the caller's
account); a fully-local tier (`pymupdf`, `tesseract`, `docling`) needs no keys and no network.

This docstring is the self-contained briefing for an LLM or agent USING the library. Every
backend id, flag and JSON shape below is taken from the vendored schemas
(`openreading/schemas/*.json`, the source of truth) — do not invent others. An agent EXTENDING
the library (a new backend) reads the `openreading.adapters` docstring (`src/openreading/adapters/__init__.py`) instead.

The 3x3
=======
Three jobs x three surfaces; every cell has identical semantics and returns the same schema:

    job        CLI `openreading ...`            Python `openreading.*`    HTTP `openreading serve`
    parse      parse doc.pdf --backend X        run(), run_batch()        POST /v1/parse, /v1/batch
    compare    compare a.json b.json            compare()                 POST /v1/compare
    strategy/  parse --strategy X;              run(strategy=), route()   backend.id "strategy:X";
    route      route doc.pdf --policy p.json                              POST /v1/route

30-second model
===============
- Every backend returns the same response envelope. The honest common denominator: at least one
  of `document.markdown` / `document.text` / `document.pages[].blocks` / `typed_fields` is
  populated. A channel a backend cannot produce is OMITTED with a `warnings[]` entry — never
  fabricated.
- `parse` runs one document; `parse <dir|glob|>=2 files>` runs a BATCH -> one `batch-result` over
  many documents; `compare` diffs N responses (two batches -> a `corpus-report`); `route` picks a
  backend under compliance constraints.
- Geometry is canonical [0,1] top-left / y-down; confidence is [0,1]; a truncated or partial
  result is `status.state="partial"`, never a bare success.

Setup
=====
    pip install 'openreading[reducto]'   # core + the extras you use; [server] for HTTP
    cp .env.example .env                 # keys for the hosted backends you use; local need none
    openreading backends                 # all 13 + READY or not, naming the missing env var

Keys are read from the environment per call, held only in memory, never stored. Credentials
never travel on the command line: no verb takes a key flag — environment / `.env` only, so a key
can never land in shell history or a process listing. The CLI auto-loads `./.env` (`--env-file`
to point elsewhere); the Python API loads a file ONLY when you pass `env_file=` to `run()` /
`run_batch()` — with it unset nothing is read, exported variables alone apply. A loaded file never
overrides an already-exported variable (`openreading.credentials.load_dotenv`). From a repo
checkout, `make sync` (`uv sync
--all-extras --dev`) is step zero: a venv synced without `--all-extras` silently lacks the
server and hosted extras and dies later with import errors. A zero-key first parse (no sample
file ships, so build one):

    from openreading.testing.sample_pdf import build_sample_pdf
    open("sample.pdf", "wb").write(build_sample_pdf())
    # then: openreading parse sample.pdf --backend pymupdf

Recipes (copy-paste)
====================
Parse one document — `source` is a path, an http(s) URL, or raw bytes (NOT a request dict):

    import openreading
    resp = openreading.run("doc.pdf", backend="pymupdf")   # -> dict, the response envelope
    resp = openreading.run("doc.pdf", backend="auto")      # compliance-first router chooses
    resp = openreading.run("scan.png", backend="tesseract",
                           pages={"ranges": [{"start": 1, "end": 2}]})
    # CLI: openreading parse doc.pdf --backend pymupdf     (URL sources work; --pages 1 2)

Extract typed fields (schema-driven). A directly-named backend that cannot do it (pymupdf,
tesseract, docling) RAISES `UnsupportedFeatureError` (CLI exit 3, HTTP 422) rather than silently
returning a geometry-only result; `auto` never submits an incapable backend (capability filter).
Only an optional-but-unavailable channel is a returned envelope + `warnings[]` entry:

    resp = openreading.run("invoice.pdf", backend="reducto",
                           extraction_schema={"json_schema": {"type": "object", "properties": {
                               "total": {"type": "string"}, "date": {"type": "string"}}}})
    fields = resp.get("typed_fields")   # {name: {value, type?, confidence?, citations?}}
    # CLI: openreading parse invoice.pdf --backend reducto --extract "total, invoice date, vendor"

Batch a folder -> one JSON. Batch is decided by input FORM: a directory / glob / >=2 args is a
batch, a single file is single-doc. A file the backend cannot take is a `skipped` item with a
reason, never a crash; a per-item failure never aborts the batch:

    env = openreading.run_batch(["invoices/"], backend="pymupdf", jobs=4)   # -> batch-result dict
    # CLI: openreading parse invoices/ --backend pymupdf > run.json
    #      openreading parse 'scans/**/*.png' --backend tesseract --jobs 4
    #      openreading parse invoices/ extra/w2.png --no-strategy      # auto, routed per file

`jobs` = documents run AT ONCE (pure speed knob; output is identical and input-ordered).
`max_items` caps expansion (default 200). `--save-dir D` also writes `D/<relpath>.json` per item.
Write outputs OUTSIDE the batched directory (or under a hidden subdir like `dir/.runs/`), else
the next batch re-ingests them as `unknown_format` skips.

Compare backends — PURE: never runs a backend; needs >=2 already-computed responses (dicts or
paths), or one document + a backend list to fan out:

    report = openreading.compare([resp_a, resp_b])            # -> comparison-report dict
    # CLI: openreading compare a.json b.json --format table    # scoreboard + delta-first findings
    #      openreading compare a.json b.json c.json --format diffs
    #      openreading compare doc.pdf --backends pymupdf,tesseract,reducto   # fan out, then diff

`--format`: `json` (default) `table` `diff` (2-way text) `diffs` `md`. `--format diffs` answers
"what actually differs, and does it matter?" in four sections: (1) CONTENT — a packaging-immune
token-coverage verdict, EQUIVALENT / DIVERGENT, so one backend flattening a table onto one line
vs another splitting it into rows is not a false miss; (2) TABLES — each table's grid shape per
backend and who flattened it; (3) TYPES — blocks labeled differently; (4) GRANULARITY — who
over-fragments.

Corpus compare — two batch runs of the same folder. When every subject is a batch envelope,
`compare` pairs documents across runs by identity (`relpath` -> `filename` -> `sha256`) and emits
a `corpus-report` (per-doc verdict + rollup). `--format diffs` is value-first: under each divergent
document it prints the real lines each side captured that the other missed (token-coverage
matched, capped per side) — not counts, not structure:

    openreading parse invoices/ --backend pymupdf > runA.json
    openreading parse invoices/ --backend reducto > runB.json
    openreading compare runA.json runB.json --format diffs

Route with compliance (HIPAA / no-train / local-only) — a plan, no execution:

    plan = openreading.route("doc.pdf", policy={"require_baa": True, "no_train_on_data": True})
    plan.chosen, plan.fallbacks, plan.dropped     # dropped = {backend_id: DropReason, ...}
    # CLI: openreading route doc.pdf --policy phi.json --run   # plan + WHY each drop, then run

Compliance is a hard filter never relaxed by fallback; an unverified claim fails closed (the
backend is dropped). So does a CONDITIONAL one: a training opt-out you have not applied
(`trains_on_customer_data: opt_out`) or a BAA the vendor sells only on a higher plan
(`hipaa_baa: tier_gated`). Assert those per deployment with `train_optout_confirmed` /
`baa_tier_confirmed` (lists of backend ids) in the policy; the run then carries a warning naming
the confirmation it rests on.

Strategies (optional `openreading.yaml` orchestration):

    resp = openreading.run("loan.pdf", strategy="main")       # sugar for backend="strategy:main"
    # CLI: openreading parse loan.pdf --strategy main > out.json
    #      openreading strategy validate | show main --longhand | list | plan
    #      openreading explain out.json      # every gate, observed vs threshold, per-attempt cost

Resume a journalled run (needs `OPENREADING_LEDGER`, see `openreading.api`):

    resp = openreading.resume("r_01J8QK")   # CLI: openreading resume r_01J8QK — no other flags

HTTP server (`[server]` extra; binds 127.0.0.1:8787; NO built-in caller auth unless
`OPENREADING_API_KEYS` is set — anyone reaching the port spends your vendor credits, so keep it
local or behind a gateway):

    openreading serve
    # GET /healthz · GET /v1/backends · POST /v1/parse {document, backend, ...} -> response
    # POST /v1/batch {documents: [...], backend} -> batch-result · POST /v1/compare {responses}
    # POST /v1/route · POST /v1/jobs + GET /v1/jobs/{id} (async) · POST /v1/webhooks/{backend_id}
    # POST /v1/backends/{id}/liveness (the probe RESULT is always 200 — "backend down" is a
    #   successful diagnostic; only 404 unknown backend / 403 scope_denied / 400 non-numeric
    #   `timeout_s` are raised, all before any probe runs)

Responses on `/v1/parse`, `/v1/batch`, `/v1/compare` are schema-validated before they leave the
process; async `/v1/jobs` responses come from the same round-trip-tested models but are NOT
re-validated on the way out. The server loads strategy config ONLY from `OPENREADING_CONFIG`,
never its cwd. HTTP statuses: 400 invalid body · 401 unauthorized · 403 ComplianceRefused /
scope_denied · 404 unknown backend · 413 doc too large · 422 unsupported feature · 424 missing
credentials · 502 plan exhausted / terminal · 504 deadline. Interactive docs at `/docs`.

The response envelope (`response.v0.3`)
=======================================
Top level: `schema_version`, `status`, `backend`, `document` (required) + optional
`typed_fields`, `chunks`, `usage`, `warnings`, `channel_provenance`, `backend_raw`,
`orchestration`, `job`.

    {
      "schema_version": "0.3",
      "status": {"state": "succeeded"},        // succeeded | partial | failed | processing
      "backend": {"id": "pymupdf", "type": "oss_library", "operation": "..",
                  "output_paradigm": [".."]},
      "document": {
        "markdown": "..",                       // GFM (native where the backend emits it)
        "text": "..",                           // plain, markup-free, complete; tables tab/newline
        "page_count": 2, "language": "en", "doc_type": {..},
        "confidence": 0.98,                     // document-level, [0,1]
        "pages": [{"page_number": 1, "blocks": [
          {"type": "title", "text": "..", "reading_order": 0,
           "confidence": 0.94,                 // [0,1]; OMITTED for deterministic parsers
           "bbox": {"x": 0.1, "y": 0.07, "w": 0.43, "h": 0.03, "page": 1,
                    "bbox_native": {"coords": [72, 58, 337, 86], "origin": "top_left",
                                    "unit": "pdf_point"}},
           "table": {"rows": [["Date", "Amt"], ["05/01", "$100"]], "cells": [..]}  // type=table
          }]}]
      },
      "typed_fields": {"total": {"value": "$4,400.00", "type": "string", "confidence": 0.96,
                                 "citations": [..]}},
      "usage": {"duration_ms": 120, "cost_usd": 0.02, "cost_basis": "estimated",
                "pages_processed": 2},
      "warnings": [{"code": "confidence_unavailable", "message": "..",
                    "field": "block_confidence"}]
    }

Block `type` is a closed enum (title, section_header, header, footer, page_number, text, list,
list_item, table, table_cell, figure, image, caption, formula, code, key_value, form_field,
signature, selection_mark, barcode, table_of_contents, other). To read "the text of a response":
prefer `document.text`, else `document.markdown`, else join `pages[].blocks[].text` in
`reading_order`. `warnings[].code` is an OPEN set — switch on the codes you know, tolerate the
rest.

The request (`request.v0.1`)
============================
Required: `document` + `backend`. `document` is EXACTLY ONE of `bytes_base64` | `url` | `path` |
`file_id` (+ optional `mime_type`, `filename`). Other top-level fields: `outputs` (markdown /
text / blocks / typed_fields / tables / chunking), `extraction_schema` (`json_schema`,
`instructions`, `citations`), `features` (`ocr`, `ocr_languages`, `layout`, `tables`,
`forms_key_value`, `handwriting`, ..), `pages` (`ranges`, `max_pages`), `routing`
(`doc_type_hint`, `optimize_for`, `fallback`), `compliance` (`require_baa`, `no_train_on_data`,
`data_region`, `require_local`, `max_retention`), `async`, `idempotency_key`. `backend` =
`{"id": "<slug>|auto|strategy:<name>", "operation"?, "version"?}`. The CLI and Python build it
from a source + flags; you construct it by hand only for the server. Extra keyword arguments to
`run()` / `run_batch()` are these top-level request fields.

Backends
========
`openreading backends` is the live readiness check. `key` = the primary env var (some hosted
backends need more than one; `OPENREADING_<SLUG>_<KEY>` overrides the service-native name).

    id                            type               key (primary env var)
                                                       input formats
    pymupdf                       oss_library        - (local)
                                                       pdf xps epub mobi cbz svg
    tesseract                     oss_library        - (local; needs the `tesseract` binary)
                                                       png jpg tiff bmp pdf
    docling                       oss_library        DOCLING_SERVE_URL (self-hosted)
                                                       pdf docx pptx xlsx html png jpg
    reducto                       hosted_api         REDUCTO_API_KEY
                                                       pdf png jpg docx xlsx pptx
    pulse                         hosted_api         PULSE_API_KEY
                                                       pdf docx pptx xlsx png jpg
    nuextract                     hosted_api         NUEXTRACT_API_KEY
                                                       pdf png jpg pptx odt txt
    anthropic-claude              hosted_api         ANTHROPIC_API_KEY
                                                       pdf png jpg
    chunkr                        hosted_api         CHUNKR_API_KEY
                                                       pdf docx pptx xlsx png jpg tiff webp html
    open-ocr                      hosted_api         OPENOCR_API_KEY
                                                       pdf png jpg gif webp tiff bmp
    aws-textract                  hosted_api         AWS_ACCESS_KEY_ID (+ secret, region)
                                                       pdf png jpg tiff
    azure-document-intelligence   hosted_api         AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT (+ key)
                                                       pdf png jpg tiff bmp docx xlsx pptx html
    google-document-ai            hosted_api         GCP_PROJECT_ID (+ processor id, credentials)
                                                       pdf tiff gif png jpg bmp webp
    qwen-vl                       self_hosted_model  QWEN_VL_ENDPOINT (self-hosted)
                                                       png jpg pdf (rasterized)

Batch & corpus shapes
=====================
`batch-result.v0.1`: `{schema_version, status{state: succeeded|partial|failed}, items[],
summary, warnings?}`. `items[i]` = `{source{relpath, filename, format, sha256, ..}, state:
succeeded|failed|skipped, response? (a full response.v0.3), error?{code, message},
skip_reason? (unsupported_format|unknown_format), transport: platform|native|null — null on a
`skipped` item, which never ran}`. `summary` =
`{total, succeeded, failed, skipped, duration_ms, cost_usd, cost_bases[], pages_processed,
backends{id: count}}`. Batch status: `succeeded` (>=1 ok, 0 failed) / `partial` (some of each) /
`failed` (0 succeeded).

`corpus-report.v0.1`: `{schema_version, subjects[], documents[{source, verdict:
equivalent|divergent|mixed|unpaired, report?}], rollup}`.

Rules an LLM must not get wrong
===============================
- Never fabricate a channel. No confidence / blocks / tables from a backend means absent plus a
  `warnings[]` code saying why — do not tell the user to expect it.
- Compliance fails closed. Never suggest a construct that widens the eligible set; `route
  --policy` prints the reason each backend was dropped.
- Batch is decided by input FORM, not count. Do not add `--jobs` / `--max-items` to a
  single-file parse (batch-only flags).
- `--jobs` vs native batch: most hosted APIs are one-document-per-call, so a batch is N
  independent calls and `--jobs` is how many run concurrently. Only a backend whose descriptor
  declares `batch.native` (Anthropic Message Batches) sends the whole list in one request. The
  envelope is identical either way.
- Cost: a batch of N files on a hosted backend is N billed calls — no discount unless a native
  batch path applies. Local backends are free. `usage.cost_usd` totals every backend that ran;
  `cost_basis` (closed enum) says what the number IS: `billed` (the provider's charge),
  `estimated` (pricing model — most hosted backends), `infra_only` (local / self-hosted: pymupdf,
  tesseract, docling, qwen-vl — no `cost_usd`), `unknown` (nuextract). Normally filled, not
  never null: the router fills it from the adapter's cost report when the adapter left it unset,
  but a `report_cost` that raises degrades to whatever `normalize()` set (typed `str | null`)
  plus a `cost_unavailable` warning — treat a missing `cost_basis` as "unmetered". The engine
  never invents a number or enforces a budget from one.
- CLI exit codes: 0 ok · 1 unexpected error / a batch where nothing succeeded · 2 usage error
  (unknown backend/strategy, unresolvable source, over `--max-items`/`--max-jobs`, compare misuse)
  · 3 cannot run (missing credentials naming the env var + signup URL; auth rejected — the key
  was found but the provider said no, with a `check <VAR>` hint naming the env var and never the
  provider's response body; unsupported feature, ComplianceRefused, plan exhausted, a named
  backend's RetryableError — it has no next rung) · 4 partial batch (some items failed); `route`
  with no compliant backend · 5 compare inputs not schema-valid · 6 interrupted while
  `OPENREADING_LEDGER` was armed (resumable; a single document names its run id, a batch names
  none).
- `compare` is pure — it never runs a backend.
- Three strategy laws: no config file => byte-identical legacy behavior; compliance prunes
  BEFORE execution and nothing can re-admit a backend; deciders choose but never widen.

Let your agents decide — the triage playbook
============================================
Everything an agent needs to act (succeed / retry / escalate / reject) is a typed field, not
prose. Branch on:

- `status.state "succeeded"` with no `orchestration`, or `orchestration.outcome "ok"` — clean
  parse -> consume `document` / `typed_fields`.
- `orchestration.outcome "degraded"` + warning `budget_exhausted` — the time budget ended the
  walk and the best result so far is returned. Do NOT escalate: a stronger, slower backend is the
  worst answer to having run out of time. Raise `budget.max_duration` /
  `limits.max_duration_per_doc`, or accept the result.
- `orchestration.outcome "degraded"` + warning `quality_below_threshold` — every rung gated;
  this is the best result KEPT, not a clean pass -> escalate: a stronger backend, a `compare`
  strategy, or reject.
- `status.state "partial"` — some channels/pages made it, some did not -> consume what is
  present; read `warnings[]` for what is missing.
- warning `fallback_used` / `quality_escalated` — the named backend failed or gated and another
  answered -> fine to consume; log the trail. Unattended, treat `fallback_used` as alert-worthy,
  not routine: the run exits 0 either way, so a permanent host fault (an OCR binary that fell off
  PATH, and every document since silently answered by the text-layer rung instead) looks exactly
  like a one-off vendor blip until someone reads the trail.
- warning `confidence_unavailable` — this backend never emits confidence: absence, not zero ->
  do not gate on a number that is not there.
- retryable error (`timeout`, `rate_limited`, `provider_error`) — transient -> retry with
  backoff, or the next backend. `provider_error` is the taxonomy's catch-all and is NOT always
  transient: a missing local binary or an unusable install lands here too, and retrying it will
  fail identically forever. The attempt's `code` (the adapter's own failure code, e.g.
  `TesseractNotFoundError`) is what separates the two — group alerts by it, not by the class.
- terminal error (`invalid_input`, `auth`) — retrying will not help -> fix the input / the key.
- `ComplianceRefused` (HTTP 403) — policy forbids every eligible backend; fails closed -> change
  the policy or the ask; never retry harder.
- `MissingCredentialsError` (HTTP 424) — names the exact env vars -> provision keys.
- batch `status "partial"` (CLI exit 4) — per-item state + skip reasons enumerate exactly what
  failed -> retry the failed subset only.
- corpus verdict `divergent` — backends materially disagree on this document -> route it through
  a `compare:` + `then:` strategy.

Why the output can be trusted blind: never fabricate (absence is signal); compliance fails
closed before anything runs; honest accounting (`usage.cost_usd` per attempt and in total, each
labeled by `cost_basis`; the `orchestration` trace records machine-readably why every backend
ran or did not). The trace vocabulary is CLOSED, unlike `warnings[]`: every attempt carries one
category from `openreading.strategies.trace.CATEGORIES` — `succeeded`,
`skipped(missing_credentials)`, `deadline_pruned`, `quality_escalated`,
`review_escalated`, `raced_lost`, `judged_lost`, `shadow`, `merge_base`, `merge_source`,
`decider_call`, `judge_call` — plus `error(<class>)` for a failed rung. (`skipped(circuit_open)`
is reserved vocabulary the engine never emits: `defaults.advanced.circuit_breaker` is not
implemented, and `strategy validate` refuses a config that declares it.) Each gate record carries
`predicate`, `threshold`, `observed`, `fired`, `unavailable`; a failed attempt may carry the
backend's own `code`; and LLM choices land in `decisions[]`. Switch on these exhaustively.

Strategies in brief
===================
A strategy is a named recipe in `openreading.yaml`: which backends run, in what order or
together, and when to move on. Start with the Plain dialect — six structure keys (`try` / `race`
/ `compare` / `escalate_when` / `then` / `max_time`), four criteria (`looks_bad` /
`low_confidence` / `missing` / `disagree`), and `auto`. Built-in presets work with no file:
`cost_saver`, `max_accuracy`, `fast`, `offline_first`. The loop: write -> `strategy validate`
(badges plain/advanced, flags what cannot work) -> `parse --strategy` -> `explain out.json` ->
`strategy show <name> --longhand` (the full-grammar tree Plain compiled to). Grammar and run
semantics: `openreading.strategies` (`internal/design/simple-strategies.md`).

Decisions DURING a run — the LLM decider: three decision points (gate-band review, `decide:`
nodes, `pick: best` judging) where an LLM chooses inside hard rails — the engine enumerates the
candidates (the tool schema's action enum IS the candidate list), compliance is invisible and
un-overridable, and every failure mode downgrades to the deterministic engine default (an LLM
outage can never fail a parse). Every decision lands in `decisions[]` with a deterministic
`decision_id`; `openreading replay --trace` re-executes them for audit. Two keys arm it: a
`decider:` block in the file AND `OPENREADING_LLM_DECIDER` in the env — a request can never talk
a service into consulting an LLM it did not opt into. Honest current state: rails, trace and
replay ship and are tested; the executor is a `DeciderPort` protocol you implement
(`openreading.strategies.decider`) — no wire adapter ships, so out of the box every decision
point takes the engine default.

Known gaps: no MCP surface (integrate via CLI/JSON, Python dicts, or HTTP); no shipped
`DeciderPort` executor; `warnings[]` has no closed registry; the `orchestration` block's inner
shape is not itself schema-validated; `confidence` is populated only where a backend honestly
has one.

Extending it (agent-executable)
===============================
Adding a backend: the `openreading.adapters` docstring (`src/openreading/adapters/__init__.py`) — pick the closest
template adapter (hosted job+poll, sync-inline, webhook, OpenAI-compatible, ..), copy it plus its
two test files; fill the descriptor HONESTLY (capabilities are `verified` only after a live run
from this repo; channels never grade up; never invent a cost rate); every wiring step is guarded
by a test that fails until done; finish with `make verify` green (the offline gate: lint +
typecheck + tests + schema validation + smokes, with NO keys and NO network — the conformance kit
runs inside it) plus one live run with real keys (`make verify-live` skips cleanly without them).
Other extensions (a compare dimension, CLI verb, strategy construct) follow the same pattern: a
design doc under `internal/design/`, then a re-entrant build prompt an agent executes phase by
phase.

Where deeper docs live
======================
- `openreading.api` — the Python surface contract, every exception, the env vars it reads.
- `openreading.cli.app` / `openreading --help` — every verb, flag and exit code.
- `openreading.server.app` — endpoints, request/response shapes, the HTTP status mapping, auth.
- `openreading.schemas` (JSON) mirrored by `openreading.types` (pydantic, round-trip tested).
- `openreading.comparison` — compare dimensions, stances, `--format diffs`, corpus mode.
- `openreading.batch` — intake resolution + platform runner (`internal/design/batch-intake.md`).
- `openreading.strategies` (`loader`, `decider`) — grammar, execution, the decider.
- `openreading.credentials` — key resolution order, `.env` handling, security posture.
- `openreading.ledger` — the journal / resume plane (`internal/design/ledger.md`).
- `openreading.testing` — fixtures, conformance kit, both test lanes (`make verify`,
  `make verify-live`). Design rationale: `internal/decisions/internal/decisions/DECISIONS.md`.
"""

from __future__ import annotations

from openreading.api import resume_run as resume
from openreading.api import route, run, run_batch
from openreading.comparison import compare

__version__ = "0.3.0"
SCHEMA_VERSION = "0.1"

__all__ = ["compare", "resume", "route", "run", "run_batch", "__version__", "SCHEMA_VERSION"]
