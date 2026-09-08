"""OpenReading: one unified API for document processing.

One request shape, one response schema over 15 backends (hosted APIs, OSS libraries, an OCR
binary, self-hosted models) so a caller swaps `backend` from `pymupdf` to `reducto` to
`aws-textract` and nothing else changes. You bring your own key, so the charges land on your own
account. Two backends run with no key and no setup, `pymupdf` and `tesseract`. `docling` and
`qwen-vl` also stay on your own hardware, once you run the service yourself and point
`DOCLING_SERVE_URL` or `QWEN_VL_ENDPOINT` at it.

This docstring is the self-contained briefing for anyone, human or agent, USING the library. Every
backend id, flag and JSON shape below is taken from the vendored schemas
(`openreading/schemas/*.json`, the source of truth). Do not invent others. An agent EXTENDING
the library (a new backend) reads the `openreading.adapters` docstring (`src/openreading/adapters/__init__.py`) instead.

Paths beginning `internal/` name a PRIVATE company repository that a reader of this package does
not have, and so do decision ids of the form `BL-*`, `AC-*` and `D-v*`. They are maintainer
provenance. Never cite one to a user as something they can open; cite the module or the schema
instead.

The 3x3
=======
Three jobs x three surfaces; every cell has identical semantics and returns the same schema. The
fourth row is not a job but the question the first three cannot answer, and it is a verb an agent
otherwise never discovers:

    job        CLI `openreading ...`            Python `openreading.*`    HTTP `openreading serve`
    parse      parse doc.pdf --backend X        run(), run_batch()        POST /v1/parse, /v1/batch
    compare    compare a.json b.json            compare()                 POST /v1/compare
    strategy/  parse --strategy X;              run(strategy=), route()   backend.id "strategy:X";
    route      route doc.pdf                                             POST /v1/route
    evals      leaderboard DIR --backends X,Y   evals.run_leaderboard()   (none)

`compare` says WHERE two backends disagree and never which one is right, because it has no
ground truth to judge against. `leaderboard` says which one is CORRECT, by scoring each backend
against documents you labeled yourself (`openreading.evals`). Reach for compare when you have two
readings and no labels, and for the leaderboard when you are choosing a backend for a corpus.

30-second model
===============
- Every backend returns the same response envelope. The honest common denominator: at least one
  of `document.markdown` / `document.text` / `document.pages[].blocks` / `typed_fields` is
  populated. A channel a backend cannot produce is OMITTED with a `warnings[]` entry. It is never
  fabricated.
- `parse` runs one document; `parse <dir|glob|>=2 files>` runs a BATCH -> one `batch-result` over
  many documents; `compare` diffs N responses (two batches -> a `corpus-report`); `route` picks a
  backend from the caller's configured chain.
- Geometry is canonical [0,1] top-left / y-down; confidence is [0,1]; a truncated or partial
  result is `status.state="partial"`, never a bare success.

Setup
=====
NOTHING IS ON PyPI. `pip install openreading` does not work; install from a clone:

    git clone https://github.com/multiversal-ventures/openreading-core
    cd openreading-core
    uv sync --all-extras --dev          # every backend extra + dev tools (same as `make sync`)
    uv run openreading backends         # one row per backend, under the header
                                        #   BACKEND  TYPE  CONFIGURED  MISSING

The readiness column is headed CONFIGURED, and it reports `yes` / `no`. It is not a complete
provisioning source: for `anthropic-claude` and `aws-textract` the MISSING column stays `-` even
when they are unconfigured, so those two name nothing to provision and the only way to learn the
requirement is to attempt a parse and read the error (it names the var).

Do NOT run `cp .env.example .env`. That file pre-fills two localhost endpoints, which flips
`docling` and `qwen-vl` to CONFIGURED `yes` on a machine where neither is running. The router
still falls back to a working local backend, so the parse succeeds, but only after your document
is sent to `http://localhost:5001` first, an address you never chose. Write only the keys you
actually hold:

    echo 'REDUCTO_API_KEY=sk_...' >> .env

Keys are read from the environment per call, held only in memory, never stored. Credentials
never travel on the command line: no verb takes a key flag, environment / `.env` only, so a key
can never land in shell history or a process listing. The CLI auto-loads `./.env` (`--env-file`
to point elsewhere); the Python API loads a file ONLY when you pass `env_file=` to `run()` /
`run_batch()`. With it unset nothing is read, exported variables alone apply. A loaded file never
overrides an already-exported variable (`openreading.credentials.load_dotenv`). A venv synced
without `--all-extras` silently lacks the server and hosted extras and dies later with import
errors, so `make sync` is step zero.

A zero-key first parse. Two synthetic one-page bank statements SHIP in `examples/`, so the first
run needs no document of your own:

    openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf

The subsystem guides use a different, generated document with tables, columns and an image. Build
it whenever a guide asks for `sample.pdf`:

    from openreading.testing.sample_pdf import build_sample_pdf
    open("sample.pdf", "wb").write(build_sample_pdf())

Recipes (copy-paste)
====================
Parse one document. `source` is a path, an http(s) URL, or raw bytes (NOT a request dict):

    import openreading
    resp = openreading.run("doc.pdf", backend="pymupdf")   # -> dict, the response envelope
    resp = openreading.run("doc.pdf")                      # the policy's chain, else pymupdf
    resp = openreading.run("scan.png", backend="tesseract",
                           pages={"ranges": [{"start": 1, "end": 2}]})
    # CLI: openreading parse doc.pdf --backend pymupdf     (URL sources work; --pages 1 2)

Extract typed fields (schema-driven). A directly-named backend that cannot do it (pymupdf,
tesseract, docling) RAISES `UnsupportedFeatureError` (CLI exit 3, HTTP 422) rather than silently
returning a geometry-only result. A null-backend chain may learn this only from the backend's
refusal, then continue to its next entry.
Only an optional-but-unavailable channel is a returned envelope + `warnings[]` entry:

    resp = openreading.run("invoice.pdf", backend="reducto",
                           extraction_schema={"json_schema": {"type": "object", "properties": {
                               "total": {"type": "string"}, "date": {"type": "string"}}}})
    fields = resp.get("typed_fields")   # {name: {value, type?, confidence?, citations?}}
    # CLI: openreading parse invoice.pdf --backend reducto --extract "total, invoice date, vendor"

Batch a folder -> one JSON. Batch is decided by input FORM: a directory / glob / >=2 args is a
batch, a single file is single-doc. A file the backend cannot take is a `failed` item carrying that
backend's own reason, never a crash; a per-item failure never aborts the batch:

    env = openreading.run_batch(["invoices/"], backend="pymupdf", jobs=4)   # -> batch-result dict
    # CLI: openreading parse invoices/ --backend pymupdf > run.json
    #      openreading parse 'scans/**/*.png' --backend tesseract --jobs 4
    #      openreading parse invoices/ extra/w2.png --no-strategy      # auto, routed per file

`jobs` = documents run AT ONCE (pure speed knob; output is identical and input-ordered).
`max_items` caps expansion (default 200). `--save-dir D` also writes `D/<relpath>.json` per item.
Write outputs OUTSIDE the batched directory (or under a hidden subdir like `dir/.runs/`), else
the next batch re-ingests them and they fail on the backend's own terms.

Compare backends. Two forms, and only the first is free. Given >=2 already-computed responses
(dicts or paths) `compare` is PURE: it reads saved envelopes, runs no backend, and costs nothing.
Given one DOCUMENT plus `--backends`, it fans out and RUNS every backend named, billing each
hosted one, before diffing the results:

    report = openreading.compare([resp_a, resp_b])            # -> comparison-report dict
    # CLI: openreading compare a.json b.json --format table    # scoreboard + delta-first findings
    #      openreading compare a.json b.json c.json --format diffs
    #      openreading compare doc.pdf --backends pymupdf,tesseract,reducto
    #        ^ NOT pure: runs all three and bills reducto. Never loop this form.

`--format`: `json` (default) `table` `diff` (2-way text) `diffs` `md`. `--format diffs` answers
"what actually differs, and does it matter?" in four sections: (1) CONTENT, a packaging-immune
token-coverage verdict, EQUIVALENT / DIVERGENT, so one backend flattening a table onto one line
vs another splitting it into rows is not a false miss; (2) TABLES, each table's grid shape per
backend and who flattened it; (3) TYPES, blocks labeled differently; (4) GRANULARITY, who
over-fragments.

Corpus compare, over two batch runs of the same folder. When every subject is a batch envelope,
`compare` pairs documents across runs by identity (`relpath` -> `filename` -> `sha256`) and emits
a `corpus-report` (per-doc verdict + rollup). `--format diffs` is value-first: under each divergent
document it prints the real lines each side captured that the other missed (token-coverage
matched, capped per side). Not counts, not structure:

    openreading parse invoices/ --backend pymupdf > runA.json
    openreading parse invoices/ --backend reducto > runB.json
    openreading compare runA.json runB.json --format diffs

Route through the configured default chain. This returns a plan without execution:

    plan = openreading.route("doc.pdf")           # the policy: block of your openreading.yaml
    plan.chosen, plan.fallbacks, plan.dropped     # dropped = {backend_id: DropReason, ...}
    # CLI: openreading route doc.pdf --run        # print the plan, then run it

`policy.backends` is the whole of it: the backends this deployment permits, in the order you want
them tried, and `routing.fallback` reorders within it and never adds to it. An EMPTY list permits
nothing, so a request that names no backend refuses. Naming a backend runs it, list or no list:
that is an explicit act, and on one machine the operator and the caller are the same person. The
enforcement boundary, where they are not, is the server's API-key scope.

Strategies (optional `openreading.yaml` orchestration):

    resp = openreading.run("loan.pdf", strategy="main")       # sugar for backend="strategy:main"
    # CLI: openreading parse loan.pdf --strategy main > out.json
    #      openreading strategy validate | show main --longhand | list | plan
    #      openreading explain out.json      # every gate, observed vs threshold, per-attempt cost

Resume a journalled run (needs `OPENREADING_LEDGER`, see `openreading.api`):

    resp = openreading.resume("7dbf6b71-adb5-4e90-9188-a184fdba9d05")   # a run id is a UUIDv4
    # CLI: openreading resume 7dbf6b71-adb5-4e90-9188-a184fdba9d05      (no other flags)

HTTP server (`[server]` extra; binds 127.0.0.1:8787; NO built-in caller auth unless
`OPENREADING_API_KEYS` is set. Anyone reaching the port spends your vendor credits, so keep it
local or behind a gateway):

    openreading serve
    # GET /healthz · GET /v1/backends · POST /v1/parse {document, backend, ...} -> response
    # POST /v1/batch {documents: [...], backend} -> batch-result · POST /v1/compare {responses}
    # POST /v1/route · POST /v1/jobs + GET /v1/jobs/{id} (async) · POST /v1/webhooks/{backend_id}
    # POST /v1/backends/{id}/liveness (the probe RESULT is always 200, because "backend down" is a
    #   successful diagnostic; only 404 unknown backend / 403 scope_denied / 400 non-numeric
    #   `timeout_s` are raised, all before any probe runs)

Responses on `/v1/parse`, `/v1/batch`, `/v1/compare` are schema-validated before they leave the
process; async `/v1/jobs` responses come from the same round-trip-tested models but are NOT
re-validated on the way out. The server loads strategy config ONLY from `OPENREADING_CONFIG`,
never its cwd. HTTP statuses: 400 invalid body · 401 unauthorized · 403 ScopeRefused /
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
      "usage": {"duration_ms": 120, "pages_processed": 2},
      "warnings": [{"code": "confidence_unavailable", "message": "..",
                    "field": "block_confidence"}]
    }

Block `type` is a closed enum (title, section_header, header, footer, page_number, text, list,
list_item, table, table_cell, figure, image, caption, formula, code, key_value, form_field,
signature, selection_mark, barcode, table_of_contents, other). To read "the text of a response":
prefer `document.text`, else `document.markdown`, else join `pages[].blocks[].text` in
`reading_order`. `warnings[].code` is an OPEN set. Switch on the codes you know, tolerate the
rest.

The request (`request.v0.2`)
============================
Required: `document` + `backend`. `document` is EXACTLY ONE of `bytes_base64` | `url` | `path` |
`file_id` (+ optional `mime_type`, `filename`). Other top-level fields: `outputs` (markdown /
text / blocks / typed_fields / tables / chunking / include_backend_raw), `extraction_schema`
(`json_schema`, `instructions`, `citations`), `features` (`ocr`, `ocr_languages`, `layout`,
`tables`, `forms_key_value`, `handwriting`, ..), `pages` (`ranges`, `max_pages`), `routing`
(`doc_type_hint`, `fallback`), `async`, `idempotency_key`. `backend` =
`{"id": "<slug>|null|strategy:<name>", "operation"?, "version"?}`. The CLI and Python build it
from a source + flags; you construct it by hand only for the server. Extra keyword arguments to
`run()` / `run_batch()` are these top-level request fields.

Backends
========
`openreading backends` is the live readiness check. `key` = the primary env var (some hosted
backends need more than one; `OPENREADING_<SLUG>_<KEY>` overrides the service-native name). Its
MISSING column is not a complete provisioning source: `anthropic-claude` and `aws-textract` print
`-` there even when unconfigured, so read the table below for those two, or attempt a parse and
read the error, which does name the variable.

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
    google-gemini                 hosted_api         GEMINI_API_KEY
                                                       pdf
    mistral-ocr                   hosted_api         MISTRAL_API_KEY
                                                       pdf docx pptx png jpg avif
    qwen-vl                       self_hosted_model  QWEN_VL_ENDPOINT (self-hosted)
                                                       png jpg pdf (rasterized)

Batch & corpus shapes
=====================
`batch-result.v0.1`: `{schema_version, status{state: succeeded|partial|failed}, items[],
summary, warnings?}`. `items[i]` = `{source{relpath, filename, format, sha256, ..}, state:
succeeded|failed|skipped, response? (a full response.v0.3), error?{code, message},
transport: platform|native|null. That is null
on a `skipped` item, which never ran}`. `summary` =
`{total, succeeded, failed, duration_ms, pages_processed, backends{id: count}}`. Batch status: `succeeded` (>=1 ok, 0 failed) / `partial` (some of each) /
`failed` (0 succeeded).

`corpus-report.v0.1`: `{schema_version, subjects[], documents[{source, verdict:
equivalent|divergent|mixed|unpaired, report?}], rollup}`.

Rules a caller must not get wrong
=================================
- Never fabricate a channel. No confidence / blocks / tables from a backend means absent plus a
  `warnings[]` code saying why. Do not tell the user to expect it.
- `policy.backends` sets the chain an unnamed request resolves to, and no fallback reorders its
  way out of it. A named backend is the caller's own explicit act and runs; the server's API-key
  scope is the boundary that refuses one. Never propose a construct that lets a request slip past
  that scope. Core also holds no fact it cannot verify, so never propose that it decide from one: a
  vendor's terms, its retention, or what it can read are all claims core cannot check, and a
  constraint core cannot check is one it must not appear to enforce. `route` prints the resolved
  chain and the reason for any backend the caller's own list excluded.
- Batch is decided by input FORM, not count. Do not add `--jobs` / `--max-items` to a
  single-file parse (batch-only flags).
- `--jobs` vs native batch: most hosted APIs are one-document-per-call, so a batch is N
  independent calls and `--jobs` is how many run concurrently. Only a backend whose descriptor
  declares `batch.native` (Anthropic Message Batches) sends the whole list in one request. The
  envelope is identical either way.
- Usage: a batch of N files on a hosted backend is N calls on your own key, with no discount
  unless a native batch path applies. `usage` reports what each backend consumed in the unit it
  meters in: `pages_processed`, `credits`, `input_tokens`, `output_tokens`, `duration_ms`. There
  are no dollars anywhere in this package. Converting a counter into a price needed a per-vendor
  rate core kept in its own source and could not verify, so `usage.cost_usd` and `cost_basis` are
  gone along with the rates. Multiply these counters by the prices on
  your own invoice, which is the only rate card that carries your tier. A `report_cost` that
  raises degrades to whatever `normalize()` reported, plus a `cost_unavailable` warning.
- CLI exit codes: 0 ok · 1 unexpected error / a batch where nothing succeeded · 2 usage error
  (unknown backend/strategy, unresolvable source, over `--max-items`/`--max-jobs`, compare misuse)
  · 3 cannot run (missing credentials naming the env var + signup URL; auth rejected, where the key
  was found but the provider said no, with a `check <VAR>` hint naming the env var and never the
  provider's response body; unsupported feature, ScopeRefused, plan exhausted, a named
  backend's RetryableError, which has no next rung) · 4 partial batch (some items failed); `route`
  with no compliant backend · 5 compare inputs not schema-valid · 6 interrupted while
  `OPENREADING_LEDGER` was armed (resumable; a single document names its run id, a batch names
  none) · 143 terminated by SIGTERM with no ledger armed, so nothing was resumable. An unarmed
  Ctrl-C is an ordinary KeyboardInterrupt and exits 130.
- `compare` is pure ONLY over saved envelopes: `compare(a, b)` on dicts or paths, and the CLI's
  `compare a.json b.json`, run no backend and cost nothing. The fan-out form,
  `compare doc.pdf --backends x,y,z`, runs every backend named and bills each hosted one. Do not
  call the fan-out form in a loop believing comparison is free.
- Strategy decisions stay within the candidates enumerated by the compiled plan.

Let your agents decide: the triage playbook
===========================================
Everything an agent needs to act (succeed / retry / escalate / reject) is a typed field, not
prose. Branch on:

- `status.state "succeeded"` with no `orchestration`, or `orchestration.outcome "ok"`: clean
  parse -> consume `document` / `typed_fields`.
- `orchestration.outcome "degraded"` + warning `budget_exhausted`: the time budget ended the
  walk and the best result so far is returned. Do NOT escalate: a stronger, slower backend is the
  worst answer to having run out of time. Raise `budget.max_duration` /
  `limits.max_duration_per_doc`, or accept the result.
- `orchestration.outcome "degraded"` + warning `quality_below_threshold`: every rung gated;
  this is the best result KEPT, not a clean pass -> escalate: a stronger backend, a `compare`
  strategy, or reject.
- `status.state "partial"`: some channels/pages made it, some did not -> consume what is
  present; read `warnings[]` for what is missing.
- warning `fallback_used` / `quality_escalated`: the named backend failed or gated and another
  answered -> fine to consume; log the trail. Unattended, treat `fallback_used` as alert-worthy,
  not routine: the run exits 0 either way, so a permanent host fault (an OCR binary that fell off
  PATH, and every document since silently answered by the text-layer rung instead) looks exactly
  like a one-off vendor blip until someone reads the trail.
- warning `confidence_unavailable`: this backend never emits confidence: absence, not zero ->
  do not gate on a number that is not there.
- A FAILURE, and how you read its class depends on the surface you called. `timeout`,
  `rate_limited`, `provider_error`, `invalid_input` and `auth` are NOT response values: they are
  `on_error:` map keys you write in `openreading.yaml`, a config-authoring vocabulary. Do not look
  for them in output. Per surface:
    * Python raises a typed exception, and the type IS the branch: `RetryableError` (retry with
      backoff), `TerminalError` (do not retry), `UnsupportedFeatureError`, `ScopeRefused`,
      `MissingCredentialsError`, `PlanExhaustedError`, `UnknownStrategyError`,
      `SourceNotFoundError`. This is the only surface that separates every condition, so prefer it
      when an agent must branch. ALL EIGHT import from the top level:

          from openreading import ScopeRefused, RetryableError, TerminalError

      Their home module is `openreading.types.errors`, and importing from there still works. An
      `openreading.yaml` that will not load, a `policy:` block that is not a policy included,
      raises `openreading.config.ConfigError` instead, because it is a file the caller wrote
      rather than a backend outcome. THREE of them SUBCLASS `TerminalError` --
      `MissingCredentialsError`,
      `PlanExhaustedError` and `UnknownStrategyError` -- so catch those first or a broad
      `except TerminalError` swallows all three. Handling them as `TerminalError` is not WRONG
      (none is retryable), it just loses which one happened.
    * HTTP returns `error.category`, machine-readable: `scope_denied` (403),
      `unsupported_feature` (422), `terminal` (424 and 502), `plan_exhausted` (502),
      `retryable_exhausted` (504), `scope_denied` (403), `unauthorized`, `unknown_backend`,
      `unknown_strategy`, `bad_request`, `bad_signature`. On 424 the category is `terminal` and the
      DISCRIMINATOR is `error.backend_code` (`missing_credentials` or `auth_rejected`), with
      `missing_env[]` naming the vars.
    * The CLI gives you the exit code and English on stderr, and exit 3 covers six conditions with
      opposite correct actions, such as a missing dependency and a `RetryableError`. It carries
      no machine-readable discriminator. An agent that must tell them apart
      calls Python or HTTP instead of parsing stderr.
    * A single-document response never populates `status.error` at all. A batch item does:
      `items[].error.code`, which is the adapter's `backend_code` when it set one and otherwise the
      Python exception CLASS NAME. Open set, so match the ones you know and tolerate the rest.
  Inside a strategy trace the class is `error(<class>)` on the attempt, and `attempts[].code`
  carries the adapter's own failure code (e.g. `TesseractNotFoundError`). Branch on that `code`,
  not the class: `error(provider_error)` is the catch-all, and a missing local binary or an
  unusable install lands there beside a genuine transient blip while failing identically forever.
- `ScopeRefused` (HTTP 403 `scope_denied`): the declared allow-list permits none of the
  registered backends;
  fails closed -> change the policy or the ask. NEVER retry: nothing about a retry changes the
  answer. Do not confuse this with `retryable_exhausted` (504), which is rate limiting or a
  deadline and IS worth retrying later. The CLI reports both as exit 3.
- `MissingCredentialsError` (HTTP 424, `backend_code: missing_credentials`): names the exact env
  vars in `missing_env[]` -> provision keys.
- batch `status "partial"` (CLI exit 4): per-item state + skip reasons enumerate exactly what
  failed -> retry the failed subset only.
- corpus verdict `divergent`: backends materially disagree on this document -> route it through
  a `compare:` + `then:` strategy.

Why the output can be trusted blind: never fabricate (absence is signal); core holds no fact it
cannot verify, so what reaches you was measured or came off the wire; the `orchestration` trace
records machine-readably why every backend ran or did not.

ONE trace vocabulary is closed, and it is not the whole trace. Every attempt's `category` comes
from `openreading.strategies.trace.CATEGORIES`, a real 13-member frozenset you can import and
switch on exhaustively: `succeeded`, `skipped(missing_credentials)`, `skipped(circuit_open)`,
`deadline_pruned`, `quality_escalated`, `review_escalated`, `raced_lost`, `judged_lost`, `shadow`,
`merge_base`, `merge_source`, `decider_call`, `judge_call`, plus `error(<class>)` for a failed
rung. (`skipped(circuit_open)` is reserved vocabulary the engine never emits:
`defaults.advanced.circuit_breaker` is not implemented, and `strategy validate` refuses a config
that declares it.) That closure is a CODE-level guarantee, not a schema-level one: in
`response.v0.3.json` the whole `orchestration` block is `additionalProperties: true` with zero
declared properties, so nothing validates its inner shape. Treat every other field in it as open
unless this briefing names the constant it lives in. `orchestration.outcome` is `ok | degraded`,
fixed in code and enumerated in no schema; `decisions[].downgraded` comes from
`openreading.strategies.decider.DOWNGRADE_REASONS` (closed, 9 members); `decisions[].point` is
`gate_band | decide | judge | route`, enumerated nowhere and open in practice.

GATE RECORDS: each carries `predicate`, `threshold`, `observed`, `fired`, and `skipped` when the
signal was unmeasurable. The wire key is `skipped`, whose value is a reason string such as
`signal_unavailable`. There is NO `unavailable` key on the wire. This matters because `fired:
false` alone does NOT mean the gate passed: a gate whose signal the backend never produced also
reports `fired: false`, with `observed: null`. Check `skipped` FIRST, and only read `fired` on a
record that has no `skipped` key. A real unmeasurable record:

    {"predicate": "confidence_below", "threshold": 0.6, "observed": null,
     "fired": false, "skipped": "signal_unavailable"}

A failed attempt may carry the backend's own `code`, and LLM-eligible choices land in
`decisions[]`.

Strategies in brief
===================
A strategy is a named recipe in `openreading.yaml`: which backends run, in what order or
together, and when to move on. Start with the Plain dialect. That is six structure keys (`try`
/ `race` / `compare` / `escalate_when` / `then` / `max_time`), four criteria (`looks_bad` /
`low_confidence` / `missing` / `disagree`), and `auto`. Built-in presets work with no file:
`cost_saver`, `max_accuracy`, `fast`, `offline_first`. The loop: write -> `strategy validate`
(badges plain/advanced, flags what cannot work) -> `parse --strategy` -> `explain out.json` ->
`strategy show <name> --longhand` (the full-grammar tree Plain compiled to). Grammar and run
semantics: `openreading.strategies`, with the Plain dialect in `openreading.strategies.plain`.

Decisions DURING a run, the LLM decider: three decision points (gate-band review, `decide:`
nodes, `pick: best` judging) where an LLM chooses inside hard rails. The engine enumerates the
candidates, and the tool schema's action enum is that candidate list. Every failure mode
downgrades to the deterministic engine default, so an LLM
outage can never fail a parse).

`decisions[]` records only LLM-ELIGIBLE decision points, so it is EMPTY for a plain `pick: best`
selection and for every preset that declares no `decide:` or `review_if:` (verified:
`offline_first` produces `decisions == []`). A plain `pick: best` shows up instead as the
`judged_lost` trace category on the losing attempts, with no numeric score. Do not read an empty
`decisions[]` as "nothing was chosen".

An LLM-eligible record carries `decision_id` (deterministic, and byte-identical between a run and
its replay), `node_path`, `label`, `point`, `eligible`, `chosen`, `decider`, `config_hash`,
`strategy` and `downgraded`. `eligible` is the AUDIT HOOK: it is the enumerated candidate list the
engine built, so a second agent can assert `chosen in eligible` and prove the choice was in
bounds. A real record:

    {"decision_id": "dp_ced9c0cb5873cc675dbd928696", "node_path": "root", "label": "root",
     "point": "decide", "eligible": ["text_layer", "ocr", "otherwise"], "chosen": "otherwise",
     "decider": "engine", "config_hash": "sha256:5df2dbe7...", "strategy": "choose",
     "downgraded": "env_disabled"}

A `route:` node also appends to `decisions[]`, and its record is a DIFFERENT SHAPE: `{point:
"route", node_path, decider, chosen (an integer rule index or "default"), rules[]}`, with no
`decision_id`, no `eligible` and no `config_hash`. Test for the key before you read it.

Replay needs the DOCUMENT and the config, not just the trace. `openreading replay --trace t.json`
alone exits 2. The working form names all three:

    openreading replay sample.pdf --config choose.yaml --trace choose.json

Two keys arm the decider: a `decider:` block in the file AND `OPENREADING_LLM_DECIDER` in the env,
so a request can never talk a service into consulting an LLM it did not opt into. Honest current
state: rails, trace and replay ship and are tested; the executor is a `DeciderPort` protocol you
implement (`openreading.strategies.decider`), and no wire adapter ships, so out of the box every
decision point takes the engine default.

Known gaps: no MCP surface (integrate via CLI/JSON, Python dicts, or HTTP; design records:
`design/agentic.md`, `product/specs/agentic.product-spec.md`); no shipped `DeciderPort` executor
(design records: `design/decider-executor.md`, `product/specs/decider.product-spec.md`); no
intent schema or its routing mechanics (design records: `design/intent.md`,
`product/specs/intent.product-spec.md`); no translation
stage or profile grammar, which has no design record anywhere; `warnings[]` has no closed
registry (the `openreading.schemas` docstring lists today's known codes, which is a list to read
rather than an enum to validate against); the
`orchestration` block's inner shape is not itself schema-validated, so every closed set inside it
is a code-level guarantee only; `status.error` is never populated on a single-document response;
`confidence` is populated only where a backend honestly has one; there is no run-stats
projection. What one invocation actually did (backends eligible, attempted, dispatched; every
switch and its reason; time and cost with honest unknowns) is spread across `warnings[]` prose,
strategy `orchestration`, the batch summary and an armed ledger, with no common carrier
(design record: `design/run-stats-analytics.md`).

Recently removed, and worth knowing if you read older material about this package: the compliance
filter and its per-vendor table, the capability gate, the stage-3 scorer, `optimize_for`, `auto`,
ledger retention and encryption at rest, and every dollar figure. Each was a fact core could not
verify deciding what core did. `CHANGELOG.md` under Unreleased carries the account, and the law
that replaced them is in this file: core holds no fact it cannot verify.

Extending it (agent-executable)
===============================
Adding a backend: read the `openreading.adapters` docstring, then pick the closest
template adapter (hosted job+poll, sync-inline, webhook, OpenAI-compatible, ..), copy it plus its
two test files; fill the descriptor HONESTLY (capabilities are `verified` only after a live run
from this repo; channels never grade up; never invent a cost rate); every wiring step is guarded
by a test that fails until done; finish with `make verify` green (the offline gate: lint +
typecheck + tests + schema validation + smokes, with NO keys and NO network, and the conformance kit
runs inside it) plus one live run with real keys (`make verify-live` skips cleanly without them).
Other extensions (a compare dimension, CLI verb, strategy construct) follow the same pattern: a
design doc first, then a re-entrant build prompt an agent executes phase by phase. Those design
docs live in the private company repository, so do not send a reader of this package looking for
one. `AGENTS.md` in this repo carries the rules an extension has to satisfy.

Where deeper docs live
======================
- `openreading.api`: the Python surface contract, every exception, the env vars it reads.
- `openreading.cli` / `openreading --help`: every verb, flag and exit code.
- `openreading.server`: endpoints, request/response shapes, the HTTP status ladder, auth.
  `openreading.server.app` lists every environment variable the server reads.
- `openreading.schemas` (JSON) mirrored by `openreading.types` (pydantic, round-trip tested).
- `openreading.derive`: the channel contract, and why a channel is absent rather than wrong.
- `openreading.comparison`: compare dimensions, stances, `--format diffs`, corpus mode.
- `openreading.evals`: the scorer, the runner, and `leaderboard`: the verb that answers which
  backend is CORRECT on documents you labeled, where `compare` only says where two disagree.
- `openreading.batch`: intake resolution + platform runner.
- `openreading.router`: the configured chain, request fallback order, and execution plan.
- `openreading.strategies` (`loader`, `decider`): grammar, execution, the decider.
- `openreading.credentials`: key resolution order, `.env` handling, security posture.
- `openreading.ledger`: the journal / resume plane.
- `openreading.testing`: fixtures, conformance kit, both test lanes (`make verify`,
  `make verify-live`).

Each subsystem also has a guide under `src/openreading/<pkg>/README.md`, indexed by the docs home
at `src/openreading/README.md`. Those are files in this repo that a reader can open.
"""

from __future__ import annotations

from openreading.api import resume_run as resume
from openreading.api import route, run, run_batch
from openreading.comparison import compare

# The triage above tells an agent to branch on the exception TYPE, because Python is the only
# surface that separates every failure condition. That advice is only executable if the type can
# be imported, and every one of these classes used to live two packages down, so the import an
# agent actually writes -- `from openreading import ScopeRefused` -- raised ImportError on the
# very surface the briefing recommends. They are re-exported here and their home is unchanged:
# `openreading.types.errors` is the home of every one of them.
from openreading.types.errors import (
    MissingCredentialsError,
    PlanExhaustedError,
    RetryableError,
    SourceNotFoundError,
    TerminalError,
    UnknownStrategyError,
    UnsupportedFeatureError,
)

__version__ = "0.3.0"
SCHEMA_VERSION = "0.1"

__all__ = [
    "compare",
    "resume",
    "route",
    "run",
    "run_batch",
    "__version__",
    "SCHEMA_VERSION",
    "MissingCredentialsError",
    "PlanExhaustedError",
    "RetryableError",
    "SourceNotFoundError",
    "TerminalError",
    "UnknownStrategyError",
    "UnsupportedFeatureError",
]
