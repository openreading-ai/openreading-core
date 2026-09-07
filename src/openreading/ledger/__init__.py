"""The Ledger execution plane: ports, journal contract, replay/resume, and the
decisions behind them. Open core, zero new required third-party dependencies.

A run leaves a record, and the record is what makes the run repeatable. That record is the
journal, an append-only log holding one line per step, saying what was attempted and how it ended.
The plane sits between the strategy walk (`openreading.strategies.engine`) and the adapters: every
side effect the walk performs leaves through one await, `ctx.exec(StepRequest, run=...)`, on an
`Executor` port, and the executor journals what happened. Same code runs the CLI,
`openreading.run`, and the server; a durable/distributed substrate is one more implementation of
the same ports, admitted by a conformance kit and never named by core. Full design:
internal/design/ledger.md.

Vocabulary budget: the plane coins exactly two user-facing words, `run_id` and `resume`.
`Step`, `Journal`, `Executor`, `BlobRef`, ... are Python type names behind the advanced boundary
and must never appear in a `--help` line or a happy-path doc sentence. `plan`, `step`, `executor`,
`worker`, `blob`, `spool`, `latch` were all rejected as user-facing nouns because each already
means something else one directory away (a second meaning is a permanent tax on every reader).
`substrate` is claimed as an INTERNAL noun only (design prose and type names for what an executor
runs on; never a `--help` line or a happy-path doc word).

Arming (env only, no CLI flag)
------------------------------
- `OPENREADING_LEDGER` -- a directory path. Set (non-empty) arms the ledger; unset or empty
  disarms it (`api` reads it as `if not root`), and `resume` with it unset/empty exits 3. There is
  deliberately no flag, so `parse --help` gains nothing (same posture as D-v3-15's env-only
  enablement: no request field can arm it). Armed, the root fills with `<run_id>.jsonl` (the
  journal, one line per `StepResult`, append-only), `<run_id>.header.json` (the run header),
  `blobs/<run_id>/<digest>.bin` (payloads, encrypted under a per-run key), `keys/<run_id>.key`
**What arming copies.** `OPENREADING_LEDGER=<dir>` means "copy every document I process, and
every full response, into this directory, in the clear". A parse of one 117 KB PDF writes about
356 KB: the journal, the header, the input document and the response body. The response blob is
written whatever `include_backend_raw` / `typed_fields` / `image` settings the request itself
asked for, so the ledger can hold data a caller deliberately excluded from their own response.
Nothing encrypts it and nothing expires it. Point the variable somewhere you are willing to keep
documents, and delete that directory on whatever schedule your own retention policy requires.

What arming covers: `api._arm_ledger` is called from the strategy path and from `resume` only.
Four entry points journal a run: `parse --strategy X`, `parse` with `defaults.strategy`, `POST
/v1/parse` resolving to a strategy, and `POST /v1/jobs` with a `strategy:<name>` body. The whole
walk runs through `api.run_request` -> `_run_strategy_request` -> `_arm_ledger`, so each of the
four writes `<run_id>.jsonl`, a header and blobs exactly as `/v1/parse` does.
Only the `JobRecord` itself is unjournaled, because it is in-memory and per-process with no
server-side resume. Five journal nothing: `--backend <id>`, `parse` with no default strategy,
`strategy:none`, a named-backend `POST /v1/jobs`, and native `submit_many` batches. A batch
inherits whichever row its items resolve to (one run per item).

Exit codes (CLI): `6` = interrupted, resumable -- Ctrl-C or SIGTERM while armed. The two are one
path, not two: `openreading.cli.app._terminate_as_interrupt` turns a supervisor's stop signal
into the same `KeyboardInterrupt` a keystroke raises, so both leave the same records and the same
exit code. The run did not fail; it parked. A single document prints `[parse] interrupted; run
<id> is resumable` and the `openreading resume <id>` line; a batch prints only that per-item runs
may be resumable (batch-level resume is not supported, no id is named). Unarmed there is nothing
to resume and the two part company on the exit code alone: Ctrl-C stays an ordinary interrupt
(130), SIGTERM prints one line and exits 143 (128 + SIGTERM, the code a supervisor tests for),
neither ever 6 -- on the single-document path, which keys on a run having actually armed. The
batch path is looser: `parse <dir>` returns 6 on either signal whenever `OPENREADING_LEDGER`
is merely set, whether or not any item armed (a `--backend <id>` batch journals nothing and still
exits 6). A refused resume, an unknown `RUN_ID`, `OPENREADING_LEDGER` unset on
`resume` (a resume whose blobs were deleted from the ledger root raises `FileNotFoundError`, the
state) are all the existing `3` -- "replay refused" and "blobs expired" share it; `6` is the only
new code. `[resume] refused: ...` lines follow `[tag] line` + hint.

The nine Ledger laws
--------------------
L1  Zero delta. No ledger configured => every surface's bytes (stdout, stderr, exit code) are
    identical to the pre-Ledger build; no file is touched. Mechanically the walk still goes
    through `ctx.exec`, but the executor is `InlineExecutor(journal=NullJournal(), blobs=None)`:
    `append` is a no-op, `get` always misses, so replay is structurally impossible.
    L1 covers the UNARMED case only. Once `OPENREADING_LEDGER` is set the journal is a HARD
    dependency, not a side-car that degrades: an unwritable or non-directory root fails the whole
    run (`api._arm_ledger` -> `LedgerArmingError`, a `TerminalError`: CLI exit 3, one tagged line
    naming the variable and the path, stdout empty). Deliberate -- a run that cannot be journalled
    must not proceed as though it were resumable, since the operator's next move on a failure is
    `openreading resume` and there would be nothing to resume from. Operationally: the ledger
    volume is on the critical path of every armed parse, so size it, monitor it for space and
    permissions, and unset the variable rather than let a bad volume take parsing down with it.
L2  The compliance gate exists exactly once, orchestration-side; its OUTPUT (the eligible set
    plus descriptor digests) is pinned. An executor does a digest-equality check at `exec`,
    before any I/O: a backend outside the pinned set is `ScopeRefused(not_in_pinned_set)`,
    a drifted descriptor is `ScopeRefused(descriptor_digest_mismatch)` -- never `skipped`.
    Staleness detection, not a second eligibility decision. The full pin has TWO terms,
    `sha256(desc.to_schema_dict()) ‖ sha256(operator_runtime_config[backend_id])`; the second
    covers `runtime.endpoint`, the one `backend.runtime` field that is env-only operator config
    (refused from a request body; `mode`/`image`/`device`/`system_deps_ok` stay request-settable
    because they control local execution, not a network destination). A worker whose resolved
    endpoint for `backend_id` differs from the pinned one refuses with
    `ScopeRefused(endpoint_mismatch)`. Shipped: `InlineExecutor._gate` checks membership
    and the descriptor term only; the endpoint term is designed, not built. Why `exec` is the
    ONLY site: `strategies.engine._resolve_backend` stays as-is and is not a gate -- a literal
    `backend:` slug pruned by the compliance gate never reaches the walk because `prune.py`
    drops the node, and one that survives pruning is in the pinned set by construction.
L3  The journal is the trace's durable form -- one log, not two, so drift is impossible. That is
    the target; today they are two logs: `Trace.attempts` is an in-memory record the engine
    writes (`ctx.trace.record(Attempt(...))`) and `Trace.orchestration()` folds over it, never
    over the journal. `ExecResult.journal_seq`/`replayed` reach the engine only on its private
    `_RunResult`/`_BranchOutcome` (race ordering); `Attempt` carries neither field, so the sole
    trace-visible residue is the literal `detail="replayed"` on a replayed missing-credentials
    skip. Checking `Trace.orchestration()` against the journal by seq is designed, not built.
L4  Records are append-only. Nothing written is rewritten; a change is a follow-on record. This
    holds strictly for the journal; the in-memory trace ships a weaker form (see Determinism).
L5  Replay refuses rather than diverges. A pinned digest that no longer matches refuses with a
    named reason; silent fallback to a default is the failure that makes replay worse than none.
L6  Secrets never enter a payload: credential REFERENCES (key names), never values -- and also
    `document.url` (routinely presigned), `async_.webhook_url`, `document.password`.
L7  The journal holds references; payloads hold content. Anything over the executor's declared
    `limits.max_inline_payload_bytes` travels as a content-addressed `BlobRef` (`None` = no
    size-based spill); secret-class fields spill by classification regardless of size. The inline
    executor declares `None` and, armed, puts every step payload in the blob store anyway.
L8  Time is absolute across a boundary: every deadline and poll schedule is
    epoch millis. `Job.next_poll_at` is monotonic and never crosses a step. Enforced by the two
    clocks on `openreading.router.clock`: `now_wall_ms()` for anything that leaves the process
    (`started/ended_epoch_ms`, `expires_epoch_ms`), `now_ms()` for anything measured inside one
    (deadlines, backoff, TTLs). The ledger shipped with `now_ms()` in all four positions, which is
    what this law exists to forbid.
L9  A step is idempotent or it is not a step. At-least-once is the contract; a step that bills
    twice is a defect, not a tradeoff.

The port surface (`openreading.ledger.*`)
-----------------------------------------
- `ports`      -- `Executor`, `Journal`, `BlobStore` (Protocols).
- `step`       -- `StepRequest`, `StepResult`, `StepRef`, `BlobRef`, `ExecResult`, `StepError`,
                  mirrors `schemas/step.v0.1.json` and `journal.v0.1.json`.
- `descriptor` -- `ExecutorDescriptor{id, limits, capabilities}`.
- `inline`     -- `InlineExecutor` (the one core executor), `NullJournal`, `descriptor_digest`.
- `jsonl`      -- `JsonlJournal`: one JSONL file per run, fsync per append.
- `localfs`    -- `LocalFsBlobStore` + `LocalFsKeyStore`: per-run key, crypto-shred erasure.
- `header`     -- `RunHeader`, `write_header`/`read_header`/`compare_header`, `HeaderMismatch`,
                  `registry_fingerprint`, `plan_hash`, `slim_request`.
- `sanitizer`  -- `Sanitizer`, the single redaction chokepoint for journal/blob writes.

Core ships one real implementation of every port so `make verify` stays offline and the CLI
gains resumability with no new dependency. Structural pins: nothing under `router`, `strategies`,
`batch`, `comparison` or `evals` imports a package from outside open core (a pinned import scan,
`tests/test_ledger_journal.py`), and `RealClock` is banned from the engine
(`tests/test_strategy_determinism.py`). The plane's own law -- never branch on substrate type --
is a rule, not yet a scan: `_WalkCtx.executor` is typed as the
`Executor` Protocol, nothing `isinstance`-checks an executor, and every behavioural difference
is meant to be read from `executor.descriptor`, exactly as the router reads `AdapterDescriptor`
and never the adapter's type. The one concrete name core mentions is `InlineExecutor` itself:
`strategies.engine` imports it (with `NullJournal`) to build the L1 unarmed default when no
executor is passed. A scan for concrete executor names / `isinstance` over `Executor` under
`router`/`strategies`/`batch` is designed, not pinned.

The executor self-describes; core behaves from the description, never from a constant.
`limits` (`max_inline_payload_bytes`, `max_payload_bytes`, `max_steps_per_run`,
`max_step_duration_ms`; each `int | None`, `None` = unbounded, the inline executor declares all
`None`) and `capabilities` (`native_timers`, `durable_log`, `at_least_once_dispatch`,
`crash_reentry`, `child_runs`; each bool). Substituting a default number for `None` is forbidden
-- it is a substrate constant re-entering core by the back door. What core does with each
limit (contract; a `None` skips the comparison entirely): a `StepRequest`/`StepResult` whose
serialized size exceeds `max_payload_bytes` refuses at `ctx.exec`, before any I/O, as a typed
`ExecutorLimitExceeded` (`TerminalError` taxonomy -- retrying an oversized payload cannot shrink
it); `max_steps_per_run` is validated twice, at plan construction against the statically knowable
step count (fail fast, before any billing moment) and at every `ctx.exec` against the running
per-run count (drive steps are dynamic, so dispatch is the enforcement point), breached as a
typed `ExecutorLimitExceeded`, never a silent truncation; `max_step_duration_ms` is honoured, not
enforced: core caps the deadline it requests to `min(caller deadline, now + max_step_duration_ms)`
and the substrate enforces the actual timeout. Capabilities gate features, never code paths: the
design has `resume` require `durable_log` + `crash_reentry`, the drive wait follow
`native_timers`, and batch fan-out follow `child_runs`. A `False` never relaxes a requirement; it
says the executor emulates it and the kit tests the emulation as hard as the native form. Today
no core module reads any limit or capability: `ExecutorLimitExceeded` does not exist,
`InlineExecutor.descriptor` declares `native_timers=True` only, and `resume` runs on that very
executor -- its durability comes from the `JsonlJournal` it is armed with, not from its
declaration. Descriptor-driven gating is the contract a second executor will be held to.

`Executor.exec(req, *, run)` -- `run` is the caller's already-built closure over the real
adapter/request/broker. `InlineExecutor` runs in-process and reconstructs nothing, so the
`StepRequest` it receives is the audit-facing shape, not the dispatch mechanism. A distributed
executor rebuilds the call from the request's slim projection and ignores `run`. Named as an
interim shape, not the final one.

The step contract
-----------------
Orchestration side (pure, deterministic, re-executed on replay, never journaled as work): plan
resolution, the compliance gate, routing, quality gates, merge/judge/vote, the cost fold. Step
side (journaled, relocatable, at-least-once): `intake` (bytes -> BlobRef, streaming sha256),
`submit` (the vendor call, the billing moment), `drive` (one poll; returns a cursor plus an
absolute next-due time), `emit` (validate + envelope), `translate` (reserved so a translation
stage can land additively; the design has every core executor refuse it with
`UnsupportedFeatureError`, but `InlineExecutor.exec` never inspects `req.kind` today -- the walk
only ever issues `submit`). `kind` is a closed enum in the vendored schema; adding a value is a
schema change (D-v3-6: the vendored JSON Schema is the strict grammar authority), never an
adapter decision. There is no `compose` kind and `normalize` is not its own step: it consumes
`raw.payload`, the largest object in the system, and hoisting it across a boundary twice costs
more than the work. A stage becomes a step only when it is externally effectful or genuinely
expensive -- every pure-CPU stage measured is 10-100x cheaper than its own boundary (a handful of
journal records plus wall clock), so slicing finely is the intuitive move and the wrong one.
The paged cascade needs no step change: the engine loops over rungs, not pages, and escalated
pages ride as `pages.ranges`, so a paged cascade is one step per rung -- at most `len(steps)`.

Validate ONCE, at the terminal envelope, inside the step that produces it. Per-step schema
validation is deliberately cut: `validate_response` carries a fixed per-call cost and is
GIL-bound (threads give ~1.1x at 16 cores), so an 8-step plan validating at each boundary pays
~8x schema recompilation per document. For the same physics `Job.poll_handle` must carry only
the cursor (`next_token`, `items_key`, `meta`), with accumulated pages spilled to the blob store:
a poll handle that accumulates raw blocks grows quadratically in ticks (gigabytes through the
journal for one 200-page document), which is why the `BlobStore` port is a hard prerequisite of
the adapter change, not a later convenience.

The drive sequence (designed; the contract for the `drive` kind): one `submit` at `step_seq=0`
returns `{backend_job_id, cursor}`; `drive` steps follow at `step_seq=1,2,...` with
`parent_step_id` = the submit's `step_id` and `options.cursor` = the previous drive's cursor,
until a drive returns `cursor=None` carrying the normalized payload as a `BlobRef`; then one
`emit`. Every re-entry is a new `StepRequest` with its own key, so a crash between drives resumes
at the first drive with no recorded result. INLINE adapters return `cursor=None` from submit and
skip drive entirely; drives count one-for-one against `max_steps_per_run`. One drive is one
`adapter.poll` and the worker is released -- orchestration waits until `next_due_epoch_ms` on a
timer, so pool capacity scales with poll rate, not job duration. Progress guard (a contract rule,
not driver code): a non-terminal drive whose `next_due_epoch_ms` is absent or not in the future
is floored to one backoff interval so a misbehaving adapter cannot hot-spin any executor. An
executor MAY satisfy a single drive with a held worker that polls repeatedly before
`absolute_deadline_epoch_ms`; core cannot tell and journals exactly the drives it issued.

Shipped today: each leaf is one `kind="submit"` step wrapping the synchronous
submit -> drive-to-completion -> normalize -> meter call in a worker thread (the progress guard
and backoff live in `router.driver`). Designed but not built (internal/design/ledger.md 5.3, 6,
9.2, 9.4, 10, 16): the released-worker `submit`/`drive`/`emit` decomposition; L2's endpoint term;
limit/capability enforcement (`ExecutorLimitExceeded`); residency queues and the
`residency_changed` refusal; `replay_degraded` records; the executor id/descriptor digest and
`key_id` in the header; batch child runs and the coordinator; `DELETE /v1/jobs/{job_id}`; the
executor conformance kit (16.3); the `run(..., ledger=)` kwarg (10); the full determinism-
boundary scan (5.3.1); any distributed executor.

`StepRequest`: `step_id, run_id, kind, step_path, step_seq, attempt` required; optional
`idempotency_key`, `content_key`, `backend_id`, `credentials_ref[]` (names only), `document:
BlobRef`, `options` (a slim projection that NEVER copies `document.password`, `document.url`,
`async_.webhook_url`, or a `credentials_spec` secret -- exclusion at construction, not redaction
after), `absolute_deadline_epoch_ms`, `parent_step_id`, `missing_credentials[]`. The design's worker
contract has `document.password` arrive only by reference (`secret_ref`, resolved from the
worker's broker like `credentials_ref`); **designed, not built** -- there is no worker and no
`secret_ref` field, and today the password reaches the adapter by value inside `ctx.req`. It
never participates in `content_key` either way (`router.cache` excludes `document`).
`StepResult`: `step_id, status, attempt` required; `status` in `attempted | ok | skipped |
retryable | failed | cancelled | replayed`; `payload: BlobRef | JsonValue`, NEVER `Any` (a
non-serializable payload must raise before construction: under a JSONL journal it would
otherwise raise after the side effect, leaving an unrecorded real dispatch that replay
re-executes for real; sets/tuples are rejected rather than coerced); `error{code, taxonomy,
detail}`, `cost{usd, basis}`, `started/ended_epoch_ms` (absolute UTC epoch millis per L8 -- the
journal's only answer to "when did this run", so a consumer can load them as timestamps),
`resolved_version`, `journal_seq`.
`BlobRef`: `run_id, digest ("sha256:<64hex>" of the PLAINTEXT), size_bytes, media_type[, store]`.
No field in replay identity or lineage may be born `x-stability: experimental`: an experimental
replay key is a contract with an expiry date.

Three identities, deliberately distinct: the blob digest (sha256 over raw bytes, streamed);
`content_key(digest, backend, resolved_version, options)` (derived from the digest, never equal
to it; `_step_request` defaults the journaled `StepRequest.idempotency_key` to it when the
request omits one); and the server-local
result cache key `(realpath, size, mtime_ns)` (`router.cache.document_identity`), which must stay
fenced to that cache -- `realpath` is machine-local, and the size+mtime blind spot becomes a
wrong result served across workers the moment a shared store keys on it. Today the fence is a
rule, not a test: `credentials._default_idempotency_key` derives the default VENDOR-facing
`idempotency_key` (`RunContext.idempotency_key`, via `build_run_context`) from that same
`document_identity` (a `bytes_base64` document hashes its content; a local path uses the
metadata identity; a URL/file_id gets no default key). So for a request that omits a key, the
value journaled on the `attempted` record (digest-derived `content_key`) and the value the vendor
received (identity-derived) are different strings; no test pins either fence.
The journal is not a cache in D-v3-3's sense (the server owns the only result cache; `run()`, the
CLI and the strategy engine pass none): it serves recorded outcomes for one `run_id`, never
memoizes across runs.

The journal contract
--------------------
Write order: an `attempted` record (intent) strictly BEFORE dispatch, carrying `run_id, step_id,
backend_id, idempotency_key, content_key`; the terminal record (`ok | skipped | failed |
cancelled`) strictly after. The window between costs one orphaned vendor job per crash, and
recovery reconciles by idempotency key, not job id -- which is why the key must exist first.
`attempted` is intent, not outcome: a step whose only record is `attempted` is
indistinguishable from one that never dispatched and correctly re-runs. A refusal from the L2 or
missing-credentials gate writes a single terminal record and no `attempted` at all. `Journal.get`
returns EVERY record at a key in append order (`[attempted, <terminal>]`). `journal_seq` is a
per-run monotonic append count stamped by `append` onto the record itself (not re-derived from
read order, which only `JsonlJournal` happens to preserve); `NullJournal` leaves it `None`.
`JsonlJournal` fsyncs every line and, on read, skips a truncated trailing line (the signature of
a crash mid-append) rather than failing every earlier valid record. Its per-run counter is
seeded from what is already on disk so a resumed process continues numbering.

Replay key: `(run_id, step_path, step_seq)` -- structural only. `step_seq` is designed as the
count of prior `ctx.exec` calls at that exact path (0 for the first submit, 1 for its first
drive step, ...), per `step_path`, not walk-wide (parallel branches carry their index in the
path, so the key a branch gets never depends on which thread reached `exec` first). **As
shipped** `_step_request` hardcodes `step_seq=0` and `attempt=1` -- no per-path counter exists --
so the effective key today is `(run_id, step_path)`. `step_id = sha256(run_id + step_path + step_seq)`
is the only form that leaves the process. The key cannot be `config_hash`-derived: the old
`config_hash` hashed the pruned tree alone, so three mutually exclusive compliance postures and a
run over protected health information hashed identically. That fourth run rested on a confirmed
Business Associate Agreement, the HIPAA contract a vendor signs before it may see such data, and
fail-closed became fail-equal-hash. `config_hash` now folds in the effective compliance, the
router config and every participating descriptor's digest; that landed before the journal existed.

Batch identity is designed with the child-run tranche and not built. internal/design/ledger.md
holds the child `run_id` scheme, the coordinator's per-item records and the `child_runs`
capability. One run-log per batch is arithmetically impossible, because step count grows with
items while any bounded `max_steps_per_run` is a constant.

Determinism: the walk must re-execute to the same `ctx.exec` calls. Hash-seed ordering of the
dropped set is sorted; `clock` is required and `RealClock` is banned from the engine by a pinned
scan; race winners (`parallel:`, hedges) resolve by journaled completion order (`journal_seq`), the
one place orchestration reads a recorded fact instead of recomputing it, so the same branch wins on
replay under any substrate that provides an ordered per-run log. The design's L4 on the trace
(internal/design/ledger.md 7.3) names three follow-on record kinds replacing in-place mutation of
an appended attempt: `attempt.gates_bound`, `attempt.recategorized`, `pages.assigned`. Shipped
shape is mutate-PLUS-record, not follow-on-only: `Attempt.bind_gates` still assigns `gates` and
`recategorize` still assigns `category` in place (every consumer reading the current value keeps
working) and each additionally appends an `Attempt.revisions` entry (`kind` = `gates_bound` /
`recategorized`, the latter with `from`/`to`), so the sequence of updates survives beside the final
state; `Trace.assign_pages` extends `trace.pages` rather than reassigning it, so a second paged
cascade in one walk cannot clobber the first (there is no `pages.assigned` record).

The determinism boundary (internal/design/ledger.md 5.3.1, the contract): one document run is
one orchestration unit, and everything the walk reads must enter through its declared input -- the
header `{run_id, config_hash, plan_hash, registry_fingerprint, pinned_eligible, document:
BlobRef, slim_request}` -- or come back through `ctx.exec`; nothing else enters. Inside the unit
the design has `ctx.registry` as a descriptor-only view built from `pinned_eligible`, `ctx.broker`
absent (credentials resolve on the worker), `ctx.clock` the executor's clock, the three
`asyncio.to_thread` sites gone, and `asyncio.create_task` in `_eval_parallel` retained because
the race is resolved by `journal_seq`, not scheduler order; a pinned test would assert
`strategies/engine.py` contains no `to_thread`, `RealClock`, `threading` or `time.` reference.
Shipped: only the `RealClock` scan is pinned (`tests/test_strategy_determinism.py`); the engine
still awaits `asyncio.to_thread` for the decider/judge ports and imports `ThreadPoolExecutor`
only for `_CANCEL_DISPATCH_EXECUTOR` (a losing `parallel:` branch's `adapter.cancel`, run off
the loop); leaf dispatch goes through `ctx.executor.exec`, and its worker thread is
`InlineExecutor.exec`'s own `asyncio.to_thread(run)`, outside the engine. `_WalkCtx` still
carries the live registry and broker.

`ExecResult` (what `exec` returns): `payload` (exactly `run()`'s own object on a live "ok"; JSON
resolved through the blob store on a replayed one), `status` in `ok | skipped | cancelled` only,
`journal_seq`, `replayed`. A `failed` outcome -- live or replayed -- always RAISES, the recorded
`StepError` reconstructed into its taxonomy class (`ScopeRefused` with `constraint=`, else
`TerminalError`/`RetryableError`/`UnsupportedFeatureError` by name, unknown => `TerminalError`),
with the ORIGINAL exception's message as the reconstructed exception's own message
(`StepError.detail`, falling back to `code` -- the taxonomy class name -- when a record predates
the fix for security review finding M9 or a gate refusal never had a longer message to carry), so
every call site's existing `except (...)` handling works unmodified on both paths. Known gap:
replayed errors carry taxonomy + message, not `backend_code`/`retry_after` -- `StepError.code`
holds the exception's CLASS NAME, not its real `backend_code` value, and `StepError` has no
`retry_after` field at all; full fidelity needs a `step` schema field addition (the
schema-evolution procedure), not done. Replay is checked before either gate: an outcome once
decided replays uniformly even if a live re-check would now differ (credentials that appeared since
the original run still replay the original skip). `asyncio.CancelledError` out of `run()` (a losing
`parallel:` branch) gets its own `cancelled` terminal record and is re-raised: without it the step
is indistinguishable on resume from a crash-before-dispatch and would re-dispatch for real. A
replayed `BlobRef` whose key was shredded raises `TerminalError(backend_code="payload_expired")` --
not a bare `OSError`, which the engine's broad `except Exception` would reclassify as an
anonymous `provider_error`. A ZDR backend's recorded `ok` has `payload=None` by design and replays
as `TerminalError(zdr_payload_not_retained)` rather than crashing on `validate(None)`.

Missing credentials are a typed, journaled `StepResult(status="skipped",
code="missing_credentials")`, and only orchestration decides fallback. The design's "terminal on
resume" row means the RECORDED outcome is final, not that the run stops: on resume
`InlineExecutor.exec` returns the recorded skip and never re-attempts a live dispatch (a key that
appeared since the original run changes nothing), and `_eval_cascade` then `continue`s to the
next rung exactly as a live skip always has (`_run_leaf` returns `_RunResult("skip", ...)` for
live and replayed skips alike). An earlier build made a replayed skip a hard
`Outcome.err("missing_credentials")`; that abandoned a later rung's own recorded success and was
removed. A worker pool is not a compliance fence: the plan path used to turn a
missing key into a silent hop to the next rung, possibly another vendor in another region, and
report success -- a fence whose failure mode is "quietly pick someone else" is not a fence; the
journaled, typed skip is what makes the hop visible.

Retry policy by taxonomy (walked by `isinstance` so private subtypes report as their base):
`RetryableError` from `poll` is retried in place by `router.driver` (backoff 500 ms doubling to
a 30 s ceiling, a vendor `retry_after` wins when larger, at most `MAX_CONSECUTIVE_FAULTS` = 120
consecutive faults -- a healthy job that merely keeps running is bounded only by the absolute
deadline), then the leaf fails over; `RetryableError` from `submit` is NOT retried in place --
`router.executor` falls over to the next backend at once, a deliberate workaround for
non-idempotent submit (a hosted adapter whose `AdapterDescriptor` sets
`idempotency_supported: false` double-bills a retried submit). The design's own
table row for `RetryableError` is "retried in place, 5 attempts, 500 ms -> 30 s, honour
`retry_after`, fail over after exhaustion" -- the in-place budget for `submit` lands only once
idempotency exists. `TerminalError` no retry, fail over; `UnsupportedFeatureError` neither;
`ScopeRefused` neither, never -- retrying it is the one construct that widens the
compliance set; `MissingCredentialsError` is a routing signal on dispatch and, on resume, its
recorded skip is replayed as final rather than re-checked (the cascade still falls over; see
above).

Resume
------
`openreading resume <RUN_ID>` / `openreading.resume(run_id)` takes NO other input -- every
option comes from the ledger (`parse --resume` was rejected because `parse`'s options would
silently conflict with the recorded ones; `openreading runs` was rejected because a listing
needs sort/filter surface -- `ls` on the root is the v1 answer). The header is written once at
first arm (write-once; a second arm in the same run cannot clobber it) and carries `config_hash`,
`plan_hash`, `registry_fingerprint`, `journal_version`, the pinned eligible set as
`{backend_id: descriptor_digest}`, `strategy_name`, `document` (the original bytes or URL, stored
through the same encrypted blob store, never a plaintext second copy; `document_is_url` is a
field this module alone sets, never inferred from the caller-controlled `media_type`), and
`slim_request` (the request with `bytes_base64`, `url`, `password`, `webhook_url` stripped).
Resume recompiles from the LIVE `openreading.yaml` + registry and hard-refuses on a mismatch in
exactly `header._HARD_FIELDS` = `config_hash`, `plan_hash`, `journal_version` (`HeaderMismatch`,
one `[resume] refused:` line each); `pinned_eligible` is instead handed to the resumed
executor's L2 gate, so a pinned backend's drift is refused per step, live, rather than in a
blanket pre-walk check that would make the gate unreachable. `registry_fingerprint` is stored
for audit and is neither hard-refused nor read by the gate. An unknown journal family version
refuses; it never best-efforts (`JOURNAL_VERSION = 1` is the only version today). A resumed run
whose remaining steps need a password-protected document or an async webhook cannot re-dispatch
them from the header alone -- a disclosed limitation, not a crash.

Designed header/resume rules, not built (internal/design/ledger.md, and the gaps list above): the
executor id and its descriptor digest in the header so L5 covers a substrate change, `key_id`
recorded rather than key bytes, `replay_degraded` records when a v2 reader meets a v1 journal, and
residency queues with the `residency_changed` refusal.

Retention and erasure
---------------------
The ledger keeps no policy about the directory it writes to. It used to: a ceiling computed from
`min(max_retention_hours)` over the hosted backends on the run's path, an expiry stamp, a reaper
that crypto-shredded the run's key at every fresh arm and on a server timer, and a ZDR branch that
skipped the blob write entirely for a backend whose descriptor carried `zdr_flag`.

All of it is gone, for one reason: the directory is the operator's. Two of those inputs were
vendor claims this package cannot verify, so an unverifiable number about somebody else's servers
decided when files on the caller's own disk were destroyed. And the default was never chosen; its
own source called it a placeholder.

Erasure is `rm`. The ledger writes where `OPENREADING_LEDGER` points and then leaves what it wrote
alone, so a run stays resumable until its owner decides otherwise, on whatever schedule their own
retention policy sets. Nothing here expires, sweeps or encrypts.

`Sanitizer` is the backstop, not the primary defense: one instance per run, armed with the
resolved secret VALUES of every eligible descriptor (a value-less `Sanitizer()` has nothing to
scrub against), applied to error details, blob bodies and the header. Classification, not size,
keeps secrets out: L7's inline ceiling never triggers on a 300-byte presigned URL.

The substrate contract
----------------------
A substrate is anything hosting an executor with four properties (internal/design/ledger.md 16.2),
each stated as a test: (a) a durable ordered per-run log (order IS `journal_seq`); (b)
at-least-once dispatch honouring an idempotency key (a repeated dispatch of a journaled key does no
I/O); (c) re-entry after a crash between any two records, replaying recorded steps and re-executing
exactly the first without a result; (d) timers, native or emulated. Conformance to the kit, not
membership in a list, admits a substrate; core's own facts (KB/page, validator cost) stay in core,
a substrate's limits live only in its descriptor. Batch at scale is N child runs plus the bounded
coordinator described under Batch identity.

The executor conformance kit (internal/design/ledger.md 16.3) is designed, not built.
`src/openreading/testing/` holds only the adapter kit (`conformance.py`) plus `adapters.py`,
`sample_pdf.py`, `scrub.py` and `tesseract_probe.py`. Nothing outside `ledger/` references
`ExecutorDescriptor`, and the only executor-level tests are `InlineExecutor`'s own unit and replay
tests.

Surfaces: HTTP job shape `{job_id, state, backend, created_ms, response, error}` holds unchanged
-- no `/v2`, no new job field -- as a stated goal. The `/v1/jobs` store stays in-memory,
per-process, because durably persisting it has two preconditions neither of which is built:
`DELETE /v1/jobs/{job_id}` -> `adapter.cancel(job)` (today process death IS the cancel; persist
the store and an abandoned hosted job polls and bills forever with no user-reachable stop), and
something other than a client `GET` advancing progress (otherwise a record reads
`state: running` forever). Python: the design promised one kwarg, `openreading.run(...,
ledger="./dir")`, plus `openreading.resume(run_id)`. Only `resume` shipped: `run()` has no
`ledger` parameter (an unknown kwarg falls through into `**request_overrides` and is refused as a
request field), and the Python surface arms exactly as the CLI does -- `OPENREADING_LEDGER` in
the environment, read by `api._arm_ledger`.

Operational contract (internal/design/ledger.md 11). One piece of it ships, the CLI's SIGTERM
handling described just below. The rest is designed, not built: there is no kill-switch gate in
`InlineExecutor.exec`, no dead-letter queue, and `StepResult` has no `backend_job_id` field. As
designed, SIGTERM stops accepting steps, lets an in-flight `submit` finish (the billing moment),
journals `backend_job_id`, then exits, never killing between vendor-accept and journal-write. A
client disconnect cancels the WAIT and never the vendor job, so the run continues and stays
retrievable by `run_id`. A kill switch is a journal-side gate before each `submit` rather than a
config reload, because a fleet already holding work ignores config. Dead letters go to a queue
owned by the run's initiator, not an on-call rotation, because these are per-document data
failures rather than infrastructure failures. Shipped: the CLI raises
`KeyboardInterrupt` on SIGTERM as well as on Ctrl-C (`openreading.cli.app._terminate_as_interrupt`),
so a supervisor's stop signal ends the same way an interactive one does -- exit 6, the resumable
line and its run id, and the in-flight step recorded `cancelled` rather than left an
`attempted`-only orphan that re-dispatches and is billed again on resume. Only the first stop
signal does that, of either kind: a repeat from a forwarding parent (`uv run`, a container init
shim, a `killpg` that reaches a wrapper too) and a Ctrl-C landing on top of a supervisor's SIGTERM
are both dropped, because raising a second interrupt into the teardown the first one started is
what strands the event loop. The escalation is unchanged and
still works -- SIGKILL journals nothing, and a kill landing between vendor-accept and the terminal
record leaves the orphan the journal contract above reconciles by idempotency key. Paging
guidance, likewise design: cost-per-run p99 against `budget:`, steps in
`submitted` past 2x declared latency (the orphan detector, the only signal that catches a missed
journal write), and dead-letter arrival rate -- not queue depth, not individual adapter
failures, and not `decider_downgraded: unavailable` (no production `DeciderPort` exists, so it
would fire on 100% of runs). Ledger never caps a run. Cost per run is only reported against
`budget:`. Native `submit_many` stays off the adapter Protocol (D-v7-1) and unjournaled until
the child-run tranche.
"""
