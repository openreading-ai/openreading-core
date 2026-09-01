"""The batch layer ("Manifest", v0.6): one invocation over many documents of any supported format
produces ONE `batch-result.v0.1` JSON, and that JSON is a first-class `compare` subject.

Two modules: intake resolution (`openreading.batch.sources`, invariants M1-M5) and the platform
runner (`openreading.batch.runner`, M6-M9). `openreading.api.run_batch` composes them and owns
native-batch dispatch (M10). Full design record: internal/design/batch-intake.md.

Why a layer, not a loop
-----------------------
Running a corpus used to mean shell loops around `parse`, so the user wrote orchestration the
platform should own: bounded concurrency, per-item failure isolation, honest skip accounting,
cost roll-up, and a comparable artifact at the end. The batch layer composes the existing
single-document pipeline without touching it. Three load-bearing choices:

1. The single-document contract is untouched. `request.v0.2` / `response.v0.3` do not change; a
   batch is a separate envelope whose items CONTAIN ordinary `response.v0.3` envelopes. Failure
   avoided: churning the one contract every consumer, test and adapter depends on. (A
   `documents[]` field on the request was rejected for exactly that reason -- it also forces
   every adapter to reason about multiplicity, and the oneOf backward-compat story is ugly.)
2. Platform batching is the default for every backend; native batch is an opt-in optimization
   with identical observable semantics. The router still never branches on backend type -- it
   branches on a descriptor field (`descriptor.batch`), like `wait_modes` / `accepts_url`.
   Failure avoided: requiring native batch would exclude the locals; ignoring it leaves hosted
   throughput and cost on the table.
3. Corpus compare is the payoff: two batch envelopes pair documents by source identity and yield
   one corpus report answering "which backend is better on MY corpus".

Principles carried over from Canon: deliver-or-warn per item (the batch never silently shrinks);
compliance is never relaxed by batching (per-item routing runs the same compliance-first
elimination as a single run -- no side door); determinism (same inputs => same envelope modulo
backend nondeterminism: item order is input order, directory expansion is sorted, identity
hashes are content-based); honesty over convenience (native-batch claims are graded, never
assumed).

Intake resolution (`sources.resolve_intake`, pure: no network beyond stat/read)
--------------------------------------------------------------------------------
Each source becomes a `ResolvedSource` = `types.batch.SourceRef` {path|url, relpath, filename,
format, mime_type, size_bytes, sha256} + `skip_reason`. `relpath` (relative to the expanded
directory root) is the stable cross-run pairing key for corpus compare; `sha256` of the file
bytes is the identity fallback and the per-item idempotency ingredient. URLs get no sha at
intake (only computed if the item is materialized).

- M1 deterministic expansion: sources resolve in argument order; a directory expands
  RECURSIVELY, files sorted bytewise by relative path; hidden (dot-prefixed) files and
  directories skipped; symlinks not followed (loop safety). An explicitly named file -- even a
  hidden one -- is always included: naming it is a deliberate choice.
- M2 envelope decided by input FORM, not count (`looks_batch`): a directory, a glob pattern, or
  >=2 source arguments => batch envelope even if expansion yields one file; a single explicit
  file/URL => the single `response.v0.3` behaviour, byte-for-byte backward compatible. Failure
  avoided: an envelope type that flips depending on how many files happen to be in a folder.
- M3 honest format filter: `format` is the lowercased extension, matched against the effective
  format set -- a named backend's `input_formats`, or the union across READY backends for
  `auto` / a strategy. A descriptor entry like `"pdf (rasterized)"` matches on its first token
  (DECISIONS D12). Non-matching known formats => `state: "skipped"`,
  `skip_reason: "unsupported_format"`; unknown extensions => `"unknown_format"`. A `.docx`
  handed to pymupdf is a skip with a reason, never a crash and never a silent omission.
- M4 size guard: expansion beyond `max_items` (default `DEFAULT_MAX_ITEMS` = 200) raises
  `SourceLimitError` early -- BEFORE any bytes are read -- naming the count and the escape hatch
  (`--max-items` / `max_items=`). Protects against pointing the tool at a home directory and
  against accidental hosted-API spend. `server.app.MAX_BATCH_DOCUMENTS` imports the same
  constant so the CLI expansion default and the server body ceiling cannot drift apart again.
  A missing path or a zero-match glob raises `SourceNotFoundError` (an OSError; CLI exit 2).
- M4a jobs ceiling: `jobs` is bounded by `MAX_BATCH_JOBS` (32, the stdlib ThreadPoolExecutor
  heuristic's own cap) by the one shared `runner.bound_jobs`, called by every surface BEFORE it
  builds its `BatchRequestEcho` so the echo reports the corrected value (a check inside
  `run_batch` would run too late to fix what the echo already said). The two ends differ on
  purpose: `jobs <= 0` clamps silently to 1 (no legitimate intent exists for a non-positive
  worker count); above the ceiling raises `JobsLimitError` naming count and limit rather than
  silently capping, which would mask a probing caller or quietly under-serve one who meant a
  bigger pool. Escape hatch: `--max-jobs` / `max_jobs=` (mirrors M4's "sane default +
  override"); the HTTP surface deliberately has none -- its body is the untrusted boundary.
- M5 mixed sources: files, directories, globs and http(s) URLs may be mixed in one call; URLs
  pass through to the per-item request (`document.url`) exactly as a single run would.

Execution (`runner.run_batch`, platform fan-out -- the default for every backend)
----------------------------------------------------------------------------------
Each non-skipped item runs the EXISTING single-document path through a `run_one(source,
idempotency_key) -> response.v0.3 dict` seam (production wires it to `api.run`: route ->
materialize -> submit -> run_to_completion -> normalize -> validate). No new adapter surface,
so every adapter batches correctly on day one.

- M6 per-item isolation: a terminal error, timeout or compliance refusal records
  `state: "failed"` + `error {code, message}` on that item and the batch continues;
  `_run_item` never raises. The stderr progress line is the only place a failed item's message
  is read.
- M7 per-item compliance and routing: each item is routed exactly as a single run would be,
  including per-item backend choice under `auto` (a PNG may legitimately route to a different
  backend than a PDF in the same batch; each inner response carries `backend.id`). No batch-level
  cache of routing decisions that could widen the compliance-eligible set.
- M8 honest aggregation: `summary.cost_usd` sums only items that reported a cost (absent if
  none did); `summary.cost_bases` lists the distinct bases observed, so `estimated` and
  `metered` never blend into fake precision; `summary` also carries `total / succeeded / failed
  / skipped`, `duration_ms`, `pages_processed`, and `backends` (a per-item backend tally).
- M9 items are full envelopes: a succeeded item's `response` is a complete, schema-valid
  `response.v0.3` document -- anything compare/evals can already consume.
- Concurrency: default `jobs=1` (serial: deterministic, rate-limit-safe, consistent with the
  v0.4 "concurrent fan-out deferred" posture; a default of 4 was rejected -- hosted rate limits
  argue for opt-in concurrency until evidence says otherwise). `jobs>1` uses a bounded thread
  pool; items are I/O-bound and every item gets a fresh adapter instance (`make_adapter()`
  constructs per call), so there is no shared mutable adapter state. A named backend's
  `descriptor.batch.max_concurrency` caps the pool (`min(requested, cap)`; tesseract declares 4
  because it is CPU-bound). Only the capped value reaches `request.jobs`, so the CLI prints a
  `[preflight]` notice naming both numbers when the request exceeds the cap -- otherwise a caller
  who asks for 16 workers sees no speedup and no explanation for the 4 in their envelope.
  Results are index-placed, so input order survives `as_completed`.
- Retries/backoff: none added by the batch layer -- per item, inside the existing driver
  (`RetryableError`, `next_poll_at`).
- Idempotency: with a caller key `K`, item keys derive as `f"{K}:{sha256[:16]}"`; without `K`
  (or without a sha, e.g. a URL item) none is fabricated. `/v1/batch` never uses the server's
  idempotency cache: a replayed response still carries the original `usage.cost_usd`, which the
  summary would sum into a total nobody was billed for (DECISIONS D-v3-3).
- Progress: one stderr line per completed item -- skipped items included, so `[i/N]` always
  reaches N -- keeping stdout pure JSON.
- Cost preflight (advisory, stderr, CLI): when more than 10 live items target a directly named
  `hosted_api` backend, print the count and the descriptor's `usd_per_page_equiv` range before
  starting. The rate is per PAGE and an item is a document, so the line says so in words and
  multiplies out the one total that exists before any file is opened -- items x one page x rate --
  labelled as the single-page floor it is. Intake reads no bytes and never fetches a URL (M5), so
  real page counts are not knowable here and no truer total can be printed; a line naming the item
  count beside a per-page rate reads as a per-item price and under-states a real corpus by its
  average page count. Never an interactive prompt: batches must stay scriptable; the M4 guard is
  the real spend protection.
- Interplay: `--strategy X` batches fine (each item runs the strategy; native batch never
  applies to strategies). `--extract`, `--pages`, `features` are request-level and apply to
  every item. Materialization stays per item inside the existing pipeline.

The envelope: `batch-result.v0.1.json` (`openreading.types.batch`, `schemas.validate_batch_result`)
---------------------------------------------------------------------------------------------------
Required in-band `schema_version` const `"0.1"`; filename == `$id` == const == pydantic default;
`extra="ignore"` forward tolerance; golden fixtures and the non-additive-diff gate from birth.
Closed enums (named CLOSED in `$defs`): `status.state` succeeded|partial|failed; item `state`
succeeded|failed|skipped; item `transport` platform|native. Both new families are registered in
`openreading.schemas` (`BATCH_RESULT_SCHEMA_FILE` / `validate_batch_result`,
`CORPUS_REPORT_SCHEMA_FILE` / `validate_corpus_report`) and enrolled in the
`python -m openreading.schemas validate` sweep that `make verify` runs.

    status.state; request {backend, strategy, jobs, source_args} (echo for provenance/replay);
    items[] {source, state, response|null, error {code, message}|null, skip_reason|null,
             transport};
    summary {total, succeeded, failed, skipped, duration_ms, cost_usd?, cost_bases,
             pages_processed?, backends}; warnings[] {code, message}

Status rule (`runner.batch_state`): `succeeded` = >=1 succeeded and 0 failed; `partial` = some
of each; `failed` = 0 succeeded (all failed, all skipped, or empty -- nothing was produced).
Skips alone never fail a batch that produced something, but an all-skipped batch is `failed`
with the reason visible per item. Warnings: a non-empty batch with skips carries one
`items_skipped` entry tallying reasons; an EMPTY item list (empty/hidden-only directory,
zero-match expansion, `documents: []` body) carries a single `empty_batch` entry and
`summary.total == 0` -- so silence is never mistaken for a hang or a drop.

Native batch: the opt-in adapter protocol (`adapters.base.NativeBatchAdapter`)
-------------------------------------------------------------------------------
Descriptor block `batch: BatchIntake {native: "verified"|"claimed"|False, max_items,
max_concurrency, notes}` (adapter-descriptor v0.4, additive; the descriptor still carries no
in-band schema version -- filename + `$id` only -- and `DESCRIPTOR_SCHEMA_FILE` has since moved
on to v0.7). Optional methods `submit_many(reqs, ctx) -> Job` and `normalize_many(job, reqs,
credentials) -> list[NormalizedResponse | BatchItemError]` live behind a separate
runtime-checkable Protocol, NOT in the required eight:
most backends have no multi-document call, and a required stub that says "unsupported" teaches
nothing (the precedent `LivenessProbeAdapter` later copied, DECISIONS D-v7-1).

Dispatch rule (`api._native_adapter`): native iff the backend is directly named (not `auto`, not
a strategy), `descriptor.batch.native` is truthy, the adapter implements the Protocol, >=1 item
is non-skipped, and the live count is within `batch.max_items`; otherwise platform fan-out.
- M10 observational equivalence: both paths build the envelope through the same
  `runner.assemble_result`; the result differs only in timing/cost and `transport` provenance.
  Per-item failures inside a native batch -- vendor-reported or raised by the adapter's own
  per-item mapping -- become per-item `error` entries; an all-or-nothing vendor API gets wrapped
  and the wrapping documented in `batch.notes`. A batch-LEVEL failure out of `submit_many` /
  `run_to_completion` / `normalize_many` propagates like any single `run()` error (a named
  backend has no next rung to fall back to).
- Deadline: one `submit_many` starts a vendor-side job. The Message Batches API promises
  completion only within a 24h window (routinely "most <1h"), so the native batch `Job` is a
  POLL-mode job even though the adapter's single-document dispatch is INLINE, and the wait runs
  far past the generic 120s `DEFAULT_DEADLINE_MS`; the native path therefore defaults to
  `credentials.DEFAULT_NATIVE_BATCH_DEADLINE_MS` (1h); override via `run_batch(deadline_ms=)`
  or `--deadline SECONDS`. The HTTP surface has no override because `POST /v1/batch` never
  reaches native dispatch -- it always drives the platform pipeline.
- Compliance on the native path: `policy` is applied per item via `build_request` (the same
  spelling the platform path's `run()` uses), and the resulting `req.compliance` is enforced
  with the same `Router.check_eligible` call the named-backend single run uses, before
  `submit_many` sees a document. Because every item in a batch shares identical policy and
  overrides, `req.compliance` is identical across `reqs`, so the eligibility check runs ONCE on
  the first request as a stand-in for the whole batch (`api._run_native`).
- Reference implementation: anthropic-claude via the Message Batches API (submit, poll
  `processing_status`, fetch `results_url` JSONL; per-item succeeded/errored maps 1:1 onto M6),
  built against respx fixtures and verified only in the keyed live lane. Grade ladder:
  `verified` (proven live) / `claimed` (documented, unaudited -- an audit task, never a
  promotion without evidence) / none. Only anthropic-claude (`claimed`) and tesseract
  (`native=False`, `max_concurrency=4`) declare a `batch` block today; every other descriptor
  leaves `batch` None, which means platform batching. Audit outcomes: google-document-ai
  (`batchProcessDocuments`) and azure-document-intelligence (batch analysis) have real batch
  APIs but stage via GCS URIs / blob containers the adapter does not own -- native batch for
  them is deferred until that staging infrastructure exists; aws-textract (async per document
  over S3 objects), chunkr and open-ocr (task/endpoint per document) and the locals have no
  multi-document call; qwen-vl and nuextract are graded none because they are self-hosted
  endpoints where request-level batching is the serving layer's concern, not the adapter's;
  reducto and pulse are unproven (keys exist, so cheap to audit live).

Corpus compare (`openreading.comparison.corpus`)
-------------------------------------------------
`compare` recognises a batch envelope by shape (`corpus.is_batch_envelope`): a dict whose
`items` is a list and whose `summary` is a dict; `schema_version` is not inspected. When EVERY
subject is a batch: documents pair across runs by identity precedence `relpath` -> `filename`
-> `sha256`; a document present in some runs but not all is verdict `unpaired` (a finding, not
a crash); each pair runs the unchanged `build_report` (comparison-report v0.2); verdicts
`equivalent | divergent | mixed | unpaired` roll up into `corpus-report.v0.1.json` (a separate
family because comparison-report's `mode` enum is CLOSED and adding `corpus` would be a MAJOR
bump). The family (`types.batch.CorpusReport`, `schemas.validate_corpus_report`) follows the same
rules as batch-result: required in-band `schema_version` const `"0.1"`, `extra="ignore"` on the
envelope model, `verdict` named CLOSED in `$defs`, goldens (`tests/golden/corpus-report`) and
the non-additive-diff gate from birth. Shape:

    subjects[] {label, source (the batch-result path), backend_tally (from summary.backends)};
    documents[] {source {relpath, filename, sha256}, verdict,
                 report = the full comparison-report v0.2 (validated independently) | null
                 when unpaired};
    rollup {documents, equivalent, divergent, mixed, unpaired,
            by_finding_code (finding counts by code across all paired documents)}

Rendering: `--format table` = one verdict line per document + the rollup;
`--format diffs` prints the rollup line FIRST, then a one-row-per-document summary table
covering every verdict (equivalent documents collapse to their one verdict line), and only
then the content drill-down -- what each side captured that the other missed -- for divergent
documents only. Mixed single+batch subjects are a usage error.

Surfaces
--------
- CLI `parse` (positional `nargs="+"`, M2 picks the envelope): `--jobs N` (default 1),
  `--max-jobs N` (32), `--max-items N` (200), `--deadline SECONDS` (not batch-specific: on a
  single-document run it overrides the generic 120s default for a directly-named backend,
  BL-169; in batch mode it reaches only native dispatch -- `api.run_batch` forwards
  `deadline_ms` to `_run_native` alone, so platform fan-out items keep the single-run default;
  `auto` / `--strategy` ignore it; a value `<= 0` means fail fast, BL-138),
  `--save-dir DIR` writes each succeeded item's inner response to `DIR/<relpath>.json` (falling
  back to `<filename>.json`; mirrors compare's fan-out flag) so per-backend envelopes are
  reusable offline. Stdout is the one envelope; progress/advisories on stderr. Exits: 0 all
  succeeded; 4 partial (some items failed); 1 nothing succeeded; 2 unresolvable source, over
  `--max-items` or `--max-jobs`; 3 cannot run at all (credentials, policy, `ComplianceRefused`,
  or a native `submit_many` `RetryableError`/deadline); 6 interrupted with `OPENREADING_LEDGER` set (batch keys on the var, not on an armed run)
  (batch-level resume is not supported, so no run id is named).
- Python: `openreading.run_batch(sources, backend="auto", *, strategy, jobs=1, max_jobs=32,
  max_items=200, deadline_ms=None, env_file, idempotency_key, on_progress, on_preflight,
  **request_overrides) -> batch-result dict`; `run()`'s signature is untouched.
- Server: `POST /v1/batch` with `{"documents": [<request.document>...], <shared request
  fields>}` -> envelope, synchronous, `documents[]` capped at `MAX_BATCH_DOCUMENTS`. Directory
  expansion is a client-side concept and does not exist server-side.

NDJSON streaming output was rejected as the primary surface: the user wants ONE JSON that
compare can ingest, and an envelope carries summary/warnings honestly (a streaming writer can be
added later without schema changes).

Testing (offline, in `make verify`)
-----------------------------------
`sources` determinism (shuffled listing => same order), hidden/symlink skips, glob semantics,
format normalization, M3 skip honesty, M4 guard, mixed URL+file+dir; runner against
`tests.fakes.ScriptedBackend`: M6 isolation, M8 mixed cost bases, `jobs>1` completes all under
scripted latency, idempotency derivation, the status truth table, exit 4, `--save-dir` layout;
envelope round-trip + goldens + forward tolerance; a fake native adapter proving dispatch rule,
fallback and M10; anthropic `submit_many` against respx fixtures; corpus pairing precedence,
unpaired handling, rollup math, divergent-only rendering; CLI envelope selection (M2), stdout
purity and progress-on-stderr; `POST /v1/batch` offline through the ASGI test client (pymupdf,
no network, like the existing routes): schema-valid envelope with every item `transport:
platform`, 400 for a missing `documents`, over `MAX_BATCH_DOCUMENTS`, or a non-integer /
over-ceiling `jobs`, 404 unknown backend, and the echoed `jobs` actually used. Live lane
(`make verify-live`, skips without keys): one real directory batch per keyed backend, anthropic
native batch when its key exists; `make serve-smoke` hits `/v1/batch` over a real socket.

Deferred (deliberately out of scope): `--retry-failed <batch.json>` merge-rerun; webhook-mode
batches; server-side directory upload (multipart bundles); corpus-level evals (`--truth` per
document); native batch for GCS/blob-staged providers.
"""

from __future__ import annotations

from openreading.batch.sources import (
    ResolvedSource,
    SourceLimitError,
    SourceNotFoundError,
    looks_batch,
    resolve_intake,
)

__all__ = [
    "ResolvedSource",
    "SourceLimitError",
    "SourceNotFoundError",
    "looks_batch",
    "resolve_intake",
]
