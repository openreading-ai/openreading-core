"""`openreading` command-line reference: every subcommand, the flags that matter, the exit codes.

The implementation is `openreading.cli.app` (`main` is re-exported here). The CLI is a thin shell
over the public API (`openreading.api`): a file or URL becomes one request, credentials resolve
from the environment (never from the command line -- see `openreading.credentials`), the request
goes to one backend, a strategy, or the router, and the one response schema is printed.

Invariants shared by every subcommand
-------------------------------------
- `--env-file PATH` is accepted everywhere (default `./.env` when present). It never overrides an
  already-set process variable, so an exported value always beats the file.
- stdout carries ONLY the JSON envelope (or the rendered report). Progress, cost preflight,
  backend chatter (stdout is redirected during the run) and every error line go to stderr, so
  `> out.json` is always safe. stderr lines carry a bracket tag. `[<command>]` (`[route]`,
  `[resume]`, `[compare]`, `[calibrate]`, ...) is the common shape, but a single-document
  `parse` tags its error lines with the RUN LABEL, not the command: the backend slug,
  `strategy:<name>` or `auto` (`[pymupdf] missing credentials ...`); `[parse]` appears only on
  selector misuse, a slug that fails catalog lookup, and the interrupt lines. A batch `parse`
  prints `[batch]` for usage / unexpected errors and the `empty_batch` warning, `[i/N]` for
  progress, `[preflight]` for the two pre-run advisories (cost, and a `--jobs` request the named
  backend's descriptor caps), and the run label (not `[batch]`) for its exit-3
  cannot-run line; a `compare` fan-out prints `[<backend>]` per fanned-out backend. The one
  untagged line is `strategy validate`'s grammar error (below).
- A printed response/envelope is schema-validated first (`schemas.validate_response` /
  `validate_batch_result`), so a non-conforming document never reaches stdout. Only the batch
  path turns a conformance failure into a coded exit (its validation sits inside the try, BL-84);
  in single-document `parse`, `resume` and `replay` the call sits after every except clause, so a
  non-conforming response surfaces as an uncaught traceback, not a coded exit.
- A `<file>` starting with `http(s)://` is a URL: backends that ingest URLs natively get it
  as-is, the rest download to bytes first.
- No flag widens the eligible set; the policy file sets it, and three of its keys widen it
  deliberately. A policy that leaves nothing compliant to run is a `ComplianceRefused` refusal,
  exit 3, from every command that EXECUTES under `--policy`; bare `route` prints the empty plan
  and exits 4 (internal/decisions/DECISIONS.md D7, D7a).
- `internal/<path>` pointers in this package name files of the private `openreading` company
  context repo (its `decisions/` and `design/` trees), not a directory of this package.
- The CLI passes no result cache (DECISIONS D-v3-3): silent memoization inside a library call is
  a footgun; a caller who wants it constructs one in Python.
- `OPENREADING_LEDGER` (a directory) arms the run journal that `resume` and exit 6 depend on.
  There is deliberately no CLI flag for it (arming is environment/config only, so `parse --help`
  gains nothing). Which runs actually journal: `openreading.cli.app` and internal/design/ledger.md.

parse <file|url|dir|glob ...> (--backend SLUG | --strategy NAME | --no-strategy)
--------------------------------------------------------------------------------
Run one backend, a named strategy or preset from `openreading.yaml`, or the router, and print the
normalized response. Exactly one of the three selectors is required (else exit 2).

    openreading parse loan.pdf --backend pymupdf --pages 1
    openreading parse https://example.com/loan.pdf --backend reducto
    openreading parse invoice.pdf --backend anthropic-claude --extract "totals and dates"
    openreading parse loan.pdf --strategy cheap_first

Single-document flags:
  --backend SLUG      one built-in backend (`openreading backends` lists them). The slug must
                      also resolve in the adapter catalog; a divergence is exit 2, not a traceback.
  --strategy NAME     a strategy or preset; the response then carries an `orchestration` block.
                      An unknown name is exit 2 (`unknown_strategy`, DECISIONS D-v3-2).
  --no-strategy       force the router's `auto` choice, ignoring `defaults.strategy`
                      (`strategy:none`, the reserved escape hatch back to the plain router).
  --config PATH       an `openreading.yaml`; else `OPENREADING_CONFIG`, else `./openreading.yaml`.
  --operation OP      backend sub-operation (e.g. `AnalyzeLending`, `prebuilt-invoice`).
  --pages N [N ...]   1-based page numbers.
  --extract [TEXT]    schema-driven field extraction (default instructions when TEXT is omitted).
                      A directly-named backend that cannot do it raises `unsupported_feature`
                      (exit 3) instead of silently returning geometry-only output, which would
                      deny the ask (DECISIONS D13); the router pre-filters such backends.
  --keep-candidates   retain every strategy branch's output under `orchestration.candidates[]`
                      (what `compare --from` reads). Off by default (payload bloat); no effect on
                      a direct backend run; in batch mode it applies per item (DECISIONS D-v4-14).
  --deadline SECONDS  absolute wall-clock budget for a directly-named (`--backend`) backend.
                      Default 120s (`openreading.credentials.DEFAULT_DEADLINE_MS`): enough for a
                      synchronous single-page backend, sometimes too short for a hosted async
                      backend's ordinary workload (e.g. Textract on a large document) -- raise it
                      rather than re-running into the same wall. No effect on `--strategy` /
                      `--no-strategy`, which manage their own per-node budget. `0` or negative
                      means fail fast: do not wait at all.

Single-document exits: 0 printed; 2 selector misuse, unknown backend/strategy, unresolvable
source; 3 cannot run (missing credentials -- the message names the exact vars and signup URL --
`auth_rejected` with its `check <VAR>` hint, `unsupported_feature`, `ComplianceRefused`, an
exhausted `auto` plan, or a `RetryableError` reaching a directly-named backend: rate-limit
exhaustion or a poll job past its deadline / `MAX_CONSECUTIVE_FAULTS`
(`openreading.router.driver`, 120), which has no next rung to fall back to the way `auto` does);
6 interrupted while the ledger was armed (below); 1 anything else.
Every other backend error message has any resolved secret value redacted to `***`.

Batch: a directory, a glob, or two or more sources
...................................................
The envelope is decided by input FORM (invariant M2, internal/design/batch-intake.md): a
directory, a glob, or >=2 arguments produce ONE `batch-result` JSON over every document; a single
explicit file/URL stays the single-document `response` above, byte-identical. Sources may mix
files, dirs, globs and URLs; a directory expands recursively (sorted; hidden files and symlinks
skipped). Each succeeded item carries a full `response` envelope, so a batch is a first-class
`compare` subject. Under `--no-strategy` routing is per file (`summary.backends` tallies which
backend took what).

    openreading parse invoices/ --backend pymupdf > run.json
    openreading parse invoices/ extra/w2.png --no-strategy
    openreading parse 'scans/**/*.png' --backend tesseract --jobs 4

Batch flags (in addition to the single-document ones):
  --jobs N            concurrent workers. Default 1 (serial: deterministic, rate-limit-safe);
                      `N <= 0` clamps to 1; above `--max-jobs` exits 2.
  --max-jobs N        ceiling on `--jobs` (default 32, `openreading.batch.runner.MAX_BATCH_JOBS`) --
                      the same floor-clamp / ceiling-reject the server enforces on `POST
                      /v1/batch`, with a caller escape hatch the server's untrusted boundary lacks.
  --max-items N       hard cap on expanded files (default 200); exceeding it exits 2.
  --deadline SECONDS  in batch mode, overrides the deadline of a directly-named NATIVE-batch
                      backend's one vendor `submit_many` (currently `anthropic-claude`); no effect
                      on the platform fan-out path. The default here is NOT the 120s above but an
                      adapter-appropriate budget (1h for `anthropic-claude`, per its descriptor's
                      "most <1h"). `0` or negative means fail fast.
  --save-dir DIR      also write each succeeded item's response to `DIR/<relpath>.json`.

A file whose format the backend cannot take is a SKIPPED item with a reason (`unsupported_format`
/ `unknown_format`) -- never a crash, never a silent omission. Per-item isolation (M6) means a
failure never raises out of the batch, so the stderr progress line (`[i/N] <path> <state> <code>:
<message>`) is the only place a failed item's message is read. Two `[preflight]` advisories print
before the run, for a directly named backend only (`auto` and `strategy:` resolve per item, so
neither number exists yet): the cost of >10 live items on a `hosted_api` backend, stated per PAGE
with the single-page total multiplied out; and, whatever the item count, a `--jobs N` above that
backend's `descriptor.batch.max_concurrency`, naming N and the cap, because only the capped value
survives into `request.jobs`. A source list that resolves to zero documents prints the envelope's
`empty_batch` warning to stderr so silence is never mistaken for a hang. Exits: 0 all succeeded;
4 partial (some items failed); 1 nothing succeeded (an unknown `--strategy` becomes a failed item
per file, so it lands here with the same hint on stderr AND each item's `error` -- `code:
unknown_strategy` -- in the envelope); 2 unresolvable source, `--max-items` or
`--max-jobs` exceeded; 3 cannot run at all (missing credentials, `ComplianceRefused`, or -- for a
native-batch backend -- a `RetryableError` / deadline from `submit_many`, which has no next rung
and does not retry; `parse` has no `--policy` flag, so a bad policy is not a `parse` exit);
6 interrupted while `OPENREADING_LEDGER` was set -- per-item runs may be individually resumable,
but batch-level resume is not supported, so no single run id is named.

resume RUN_ID
-------------
Re-drive a run recorded under `OPENREADING_LEDGER` from its own journal: every step already
terminal there replays byte-identical with zero network calls; anything genuinely unreached
executes for real. Takes ONLY `RUN_ID` -- every other option comes from the ledger, not the command
line. A run is resumable once the ledger was armed for it, whether the original `parse` went on to
succeed, was interrupted (exit 6) or crashed; the id is printed by `parse` on interrupt or read
from the run's own header under `$OPENREADING_LEDGER`.

    export OPENREADING_LEDGER=./.openreading
    openreading parse big-batch.pdf --strategy cheap_first    # Ctrl-C: exit 6, prints the run id
    openreading resume r_01J8QK

    [parse] interrupted; run r_01J8QK is resumable
    [parse] resume with: openreading resume r_01J8QK

A resumed run REFUSES BY NAME (exit 3) rather than falling back to a fresher config when
`openreading.yaml` (`config_hash`), the compiled plan, or the journal's record format no longer
match what the original run saw -- a resumed run replays recorded decisions; a changed config would
silently mean a different run:

    [resume] refused: openreading.yaml changed since r_01J8QK (sha256 3f9a... -> c21b...)
    [resume] a resumed run replays recorded decisions; start a new run instead

Also exit 3: an unknown `RUN_ID`, `OPENREADING_LEDGER` unset, or a run whose payloads the
retention reaper has already crypto-shredded (`PayloadExpired`). Python: `openreading.resume(id)`.

route <file|url> --policy policy.json [--run]
---------------------------------------------
Print the compliance-first plan; with `--run`, execute the whole chain (chosen, then fallbacks).

    echo '{"require_baa": true, "no_train_on_data": true, "optimize_for": "accuracy"}' > phi.json
    openreading route loan.pdf --policy phi.json [--run]

`policy.json` keys: `require_baa`, `no_train_on_data`, `data_region`, `require_local`,
`max_retention`, `optimize_for`, `doc_type_hint`, `allow_unverified_compliance`,
`train_optout_confirmed`, `baa_tier_confirmed`. Those ten are the whole grammar: the file must be
a JSON object, any other key is refused by name (with a `did you mean` for a near miss), and a
value of the wrong type is refused too -- exit 3, `[route] invalid policy <path>: ...`, from every
`--policy` flag in this CLI, because a compliance constraint that can be turned off by a typo is
not a constraint (`api.validate_policy`). Output is `{chosen, fallbacks, dropped: {id:
{stage, code, reason}}, terminal_reason}` plus, with `--run`, a `result`. `--run` never widens the
plan; a fallback actually used is recorded in the result's `warnings[]`. Exits: 0; 4 no compliant
backend (the empty plan is still printed as JSON); 3 an unreadable or invalid policy, an
unreadable document, or a plan-exhausted `--run` (the plan is still printed; the stderr trail
names each backend's failure and a `check <VAR>` hint for every rejected key).

The last three keys are router configuration, not request fields (`api.router_config` folds them
into `RouterConfig`, DECISIONS D7 / D7a), and they are two different kinds of knob.
`allow_unverified_compliance` is a TOLERANCE switch (default `false` = fail closed): it admits a
backend whose own disclosure is unverified on the axis you asked about
(`openreading.router.compliance`) -- training posture (`trains_unverified`), declared regions under
`data_region` (`region_unverified`) or retention under `max_retention` (`retention_unverified`).
It asserts nothing about your deployment and never excuses a `max_retention` string that does not
parse (`retention_unparseable` is a caller error and fails closed unconditionally). The last TWO
keys are DEPLOYMENT assertions about facts the vendor makes conditional, and both fail closed when
absent: `train_optout_confirmed` lists backends whose training opt-out you have applied
(`trains_on_customer_data: opt_out`); `baa_tier_confirmed` lists backends whose BAA you have
actually signed where the vendor gates it behind a higher plan (`hipaa_baa: tier_gated` -- Reducto
Growth+, Chunkr Enterprise, Pulse Pro). Without them `require_baa` / `no_train_on_data` drop those
backends at stage 1; with them the run carries a `baa_tier_confirmed` warning naming the
confirmation its compliance rests on. Without this, `require_baa` could route PHI to a vendor with
nothing signed and nothing said.

backends [--check SLUG[,SLUG...]|all] [--timeout SECONDS]
---------------------------------------------------------
Show every backend and whether it is CONFIGURED to run here (extra installed, credentials found),
with the exact env vars still missing. Bare `openreading backends` is offline and free: it never
touches the network.

    BACKEND                        TYPE               CONFIGURED  MISSING
    pymupdf                        oss_library        yes         -
    reducto                        hosted_api         no          REDUCTO_API_KEY

"Configured" is not "reachable" -- a `DOCLING_SERVE_URL` pointing at a dead port is configured.
`--check` answers the other question by actually probing, and it is NEVER implicit: on a CLI, a flag
the user typed is the consent a page load can never be. Reports print as they complete, so a slow
backend never hides the ones that already answered.

    BACKEND   PROBE     STATUS                 MEASURED  LATENCY  DETAIL
    docling   endpoint  unreachable            yes       20ms     no response (DOCLING_SERVE_URL)
    chunkr    none      configured_unverified  no        -        configured, but not verified
    pymupdf   local     live                   yes       0ms      responding

Status ladder (`openreading.types.liveness.LivenessStatus`, internal/design/liveness.md), seven
states ascending in what is known: `not_supported` (no probe AND nothing declared to infer from --
"every requirement resolves" is vacuous there, so it declines to guess) < `not_configured` (the
same `backend_readiness().ready` judgement as the table above, so "configured" cannot mean two
things; short-circuits the probe) < `configured_unverified` (inferred) < `live` (measured), with
`unreachable`, `unauthorized` (key rejected) and `error` (the probe itself raised -- reported as a
finding, never a crash) as the measured negatives.
`MEASURED` is the column to read: `yes` means the backend was called, `no` means the status was
inferred from the environment with no round trip (so no latency). Inference is a first-class state
distinct from measurement (DECISIONS D-v7-3) because rendering an inference as `live` is exactly
the "configured wearing the word ready" defect the column exists to fix. A probe is never a billed
request (D-v7-4): a vendor with no free liveness call declares no probe and reports
`configured_unverified` rather than spending your money. `--check all` probes only backends that
DECLARE a probe (probing the rest would re-print the free inference). `--timeout` omitted means
the adapter's own declared `liveness.timeout_s`, else 5s (`openreading.liveness.
DEFAULT_PROBE_TIMEOUT_S`; the argparse help's flat "default 5s" is a simplification); any value is
clamped to [0.1, 30] (clamped, not rejected, so nobody parks a worker indefinitely). Liveness is a
diagnostic, never routing input (D-v7-6). An unknown slug exits 3.

compare <a.json b.json ...> | <doc> --backends a,b[,...] | <doc> --all-ready | --from run.json
---------------------------------------------------------------------------------------------
Compare backends' outputs and print the delta (fields / text / blocks). Three ways to get the N
subjects: response JSON files (the primitive); fan-out over one document (`--backends a,b` or
`--all-ready`, optionally `--save-dir DIR` to keep each envelope as `DIR/<backend>.json`); or a
strategy run's retained losers (`--from run.json` after `parse --keep-candidates`, the winner
compared against `orchestration.candidates[]`). The comparison is pure; fan-out is CLI-only sugar
over independent direct parses, run SERIALLY (deterministic subject order, one hosted call in
flight, so a wide `--all-ready` cannot stampede provider rate limits).

    openreading compare a.json b.json --format table          # delta-first
    openreading compare a.json b.json --format table --show-agreements
    openreading compare doc.pdf --backends pymupdf,tesseract  # fan out, then compare
    openreading compare a.json b.json --format diff           # exactly 2 subjects
    openreading compare a.json b.json --baseline a            # sign deltas against `a`
    openreading compare a.json b.json --truth golden.json     # score against a golden
    openreading compare --from run.json
    openreading explain report.json                           # render a saved report

Flags: `--format json` (default, schema-valid, pipeable) | `table` | `md` | `diff` (2-way
git-style text diff plus field deltas; exactly two subjects) | `diffs` (N-way content verdict +
structure view, no packaging noise; see `openreading.comparison`). `--show-agreements` lists
agreeing fields in the human formats (hidden by default). `--baseline LABEL` signs deltas against
one subject (a label, or a response file added as a subject). `--truth golden.json` scores each
subject against a golden in the evals `expected` shape. `--deadline SECONDS` overrides the 120s
single-document deadline for every fanned-out backend (the same escape hatch as `parse --backend
--deadline`; `0` or negative means fail fast).

Corpus mode: when EVERY subject is a `batch-result` envelope (from `parse <dir>`), documents are
paired across runs by identity (`relpath`, then `filename`, then `sha256`) and a corpus report is
emitted -- a per-document verdict (`equivalent` / `divergent` / `mixed` / `unpaired`) with a
rollup. `--format json` is the schema-valid `corpus-report`; `table` prints one verdict line per
document; `diffs` is VALUE-FIRST: under each divergent document it prints the actual content each
backend captured that the other missed (real lines, token-coverage matched so packaging never fakes
a delta), capped at a few per side with a `... N more` tail, plus a footer one-liner to dump one
document's full text. It is not counts and not structure (table shapes / types / granularity live
in `table` and the single-pair drill), because a corpus-wide four-section diff is a wall. Mixing
batch and single-response subjects is a usage error (exit 2); `--format diff` is 2-way text only
and is refused in corpus mode.

Exits: 0; 2 misuse (<2 subjects, fan-out with more than one document, <2 or unknown fan-out
backends, `--format diff` with != 2 subjects, mixed subject kinds); 3 a fanned-out backend cannot
run (missing credentials, `ComplianceRefused`, `unsupported_feature`, `RetryableError`); 5 inputs
are not schema-valid responses (unreadable file, invalid envelope) or `--from` on a run that kept no
candidates (the message says to re-run with `--keep-candidates`, DECISIONS D-v4-14); 1 anything
else.

leaderboard <dataset_dir> (--backends a,b[,...] | --all-ready) [--policy p.json] [--format ...]
-----------------------------------------------------------------------------------------------
Rank N registered backends on ONE dataset -- measured, not vendor-claimed. Runs the same
`case.json` corpus (`openreading.evals.dataset`; the repo ships one under
`openreading/evals/sample`) through the unchanged `openreading.evals.runner.run_case` path for
every named backend -- the same per-case compliance gate, the same five-dimension scorer, no second
scoring or gating path -- and prints one ranked `BenchmarkReport`: measured mean score,
per-dimension breakdown, per-case result table, error tally, and each backend's cost basis
alongside its score (never a rank without the price that produced it).

    openreading leaderboard datasets/paystubs/ --backends aws-textract,google-document-ai
    openreading leaderboard src/openreading/evals/sample --backends pymupdf,tesseract --format json
    openreading leaderboard datasets/paystubs/ --all-ready

`--format table` (default) prints the dataset's own identity (path, case count, case names) above
the ranking so a screenshot is never read as a universal verdict rather than "on these N
documents"; `--format json` is the schema-valid `BenchmarkReport` (`leaderboard-report.v0.1.json`
in `openreading.schemas`) a script or CI job consumes.

The table carries `scored` (`n_scored/n_cases`) beside `mean`, and prints `mean` as an em dash when
`n_scored` is 0, because a mean over no scored case is not a measurement and a printed `0.000` is
indistinguishable from a backend that measured 0.00 on every case it ran. `errors` does not
separate them either: a case naming no recognized `expected` dimension is unscored without
erroring. A non-deterministic backend's row is marked in place -- its mean is one labeled sample.
The per-case block states `winner=`, `tie=`, `no winner (every scored backend got 0.00)` or
`no result (no backend produced a score)`, and totals the four, because the report's own `winner`
field breaks a tie alphabetically for byte-stability and printing that as a result turns ties and
mutual failures into a clean sweep for anyone tallying the block. The JSON `winner` is unchanged. A backend's per-case compliance refusal,
or any other per-case fault, is that backend's own scored, error-carrying case -- in its error
tally, excluded from its mean -- never a silently skipped case,
never a crash. Every backend makes a REAL call per case: `--all-ready` over a large dataset is N x M
billable calls, not N + M. The numbers are evidence a human reads and are never fed back into the
router's scoring or any adapter's `integration_priority`. Exits: 0; 2 <2 backends or an unknown
`--backends` id; 3 unreadable policy, an unresolvable/empty dataset directory, or a cannot-run
fault.

strategy <verb> / explain / replay / calibrate
----------------------------------------------
Inspect and drive `openreading.yaml` strategies (`openreading.strategies`; internal/design/ for the
grammar). Every verb takes `--config PATH` (else the discovery order above); those marked with
`--policy` take a compliance context. `validate`, `plan`, `normalize`, `replay` and `calibrate`
need a config and exit 3 without one ("no openreading.yaml found"); `list` and `show` run
config-free on the built-in presets (an unparseable config is exit 3 for every verb).

    openreading strategy validate [--policy p.json]
    openreading strategy plan doc.pdf --strategy NAME [--policy p.json]
    openreading strategy show NAME [--longhand]
    openreading strategy list
    openreading strategy normalize
    openreading explain response.json
    openreading replay doc.pdf --trace response.json [--strategy NAME] [--policy p.json]
    openreading calibrate samples/ --strategy NAME [--target-escalation 0.15]
                                                   [--max-cost-per-doc 0.05] [--policy p.json]

- `validate`: grammar (schema) + world-consistency check of the whole file, then per strategy a
  dialect badge (`dialect: plain` or `dialect: advanced (first advanced key: ...)`), the body as
  written, a plain-English summary, and a glossary of the criterion words used. Errors go to
  stderr and warnings to stdout; exit 3 on any error, 0 otherwise -- warnings never fail
  (DECISIONS D-v3-8). Issues are located by file + node path, not line number. With `--policy`,
  steps unreachable under that policy are flagged. A grammar error prints untagged (`ERROR
  <source>: <detail>`) so every line shares one `LEVEL source:path: message` shape.
- `plan`: the Terraform-style speculative plan -- the pruned tree for THIS document + policy
  (`{strategy, config_hash, eligible, dropped[], tree}`), no execution. Exit 3 on an unreadable
  document/policy, an unknown strategy, or `ComplianceRefused`.
- `show NAME`: a strategy or preset body AS WRITTEN (a Plain strategy prints Plain);
  `--longhand` prints the canonical desugared full-grammar tree instead. Unknown name: exit 3.
- `list`: built-in presets, then the configured strategies (with the config path).
- `normalize`: the whole config as canonical longhand YAML (the `docker compose config` analog).
- `explain FILE`: render a response's `orchestration` block as a story (strategy, chosen backend
  and outcome; each attempt's node, backend, category, duration and cost; gate rows -- grouped
  under their Plain source word when present -- with observed/threshold and FIRED/skipped/ok;
  decisions with `decider=` and, when a point resolved to something other than its configured
  choice, `downgraded=`; dropped backends by stage and code). A file that is a comparison report
  (`subjects` + `fields` + `findings`) renders as the compare table instead. No orchestration
  block: exit 3.
- `replay`: re-execute the strategy for the document but take, at each decision point, the
  choice logged in `--trace` (a saved response or bare orchestration JSON) -- the TraceDecider
  (`decider: "trace"` in the new records, DECISIONS D-v3-18). A decision absent from the trace
  takes the engine default (`trace_missing`). Deterministic and offline for local backends; it
  consults no LLM, so the LLM-enablement and decider-compliance gates are moot. The strategy name
  comes from `--strategy` or the trace (neither: exit 2). A trace whose `config_hash` differs from
  the freshly compiled one is REFUSED (exit 3): its logged decisions were made under a different
  configuration or compliance posture, not merely a different document; a trace with no
  `config_hash` at all is left to the per-decision `trace_missing` downgrade.
- `calibrate`: derive gate thresholds from a sample (a dataset dir of `*/case.json`). Runs the
  strategy's rung-1 backend over the sample, scores each result with the eval scorers, sweeps each
  gated threshold, and prints candidate operating points (threshold -> predicted escalation rate,
  cost/doc, scorer agreement) plus a ready-to-paste `escalate_if:` RECOMMENDATION for your
  `--target-escalation` / `--max-cost-per-doc`. It PROPOSES; it never rewrites the config
  (DECISIONS D-v3-21) -- the file you commit is the authority. Each case's rung-1 run is gated
  first (request, `--policy` and the file's own `policy:` block union most-restrictive-wins); a
  non-compliant case refuses with `ComplianceRefused` (exit 3) before the document is sent. A case
  whose `expected` names none of the scorer's five recognized dimensions is an ordinary "not
  labeled yet" case: excluded from `scorer_agreement` rather than silently required; the report's
  `n_scored` (next to `n_docs`) says how many contributed and a `[calibrate]` stderr advisory
  fires when some or all of the sample went unscored -- an unlabeled sample would otherwise yield a
  flat, precise-looking agreement number that measured nothing. Offline for local backends.
  Rung-1 runs under a fixed 60s deadline; `RetryableError` / `unsupported_feature` exit 3.

serve
-----
`serve [--host H] [--port P] [--cors-origin O ...]` runs the HTTP API (`openreading.server`;
needs the `[server]` extra, else exit 3). Default `127.0.0.1:8787`. (The web UI is not part of
this package: it ships as `openreading-ui` from the company's `openreading_webui` package, which
depends on this one.) Binding any host other than `127.0.0.1` prints a warning: anyone who can reach the socket spends your vendor keys, so put it
behind your own auth/proxy. The server never reads the working directory for a config -- pass
`OPENREADING_CONFIG`.

Exit codes
----------
  0  success.
  1  unexpected error (printed as `[tag] error: <Type>: <message>`); a batch in which nothing
     succeeded.
  2  usage: unknown `--backend` (argparse) or `--strategy` on a single document; `parse` with an
     unresolvable source, more files than `--max-items`, or more `--jobs` than `--max-jobs`;
     `compare` misuse (<2 subjects, unknown fan-out backend, `--format diff` with != 2 subjects,
     mixed subject kinds); `leaderboard` misuse (<2 backends, unknown id); `replay` with no
     strategy name anywhere.
  3  cannot run: missing credentials (names the exact vars + signup URL), `auth_rejected`,
     `unsupported_feature`, an unreadable `--config` / `--policy` / document / `--trace` /
     `explain` argument, a `--policy` file that is not a valid policy object (an unknown key, a
     non-object top level, or a value of the wrong type), a `ComplianceRefused` refusal (from
     `parse`, `strategy plan`, `replay`, `calibrate`, `compare`, `leaderboard`), a
     plan-exhausted `route --run`, `serve` without its extra or with a malformed
     `OPENREADING_API_KEYS` / `OPENREADING_API_KEY_SCOPES` (one `[serve] ...` line naming the
     bad entry's position, never its value), an unresolvable/empty `leaderboard` dataset, a
     `resume` refusal / unknown run / expired payloads, an unknown `backends --check` slug, or
     a `RetryableError` reaching a directly-named backend on `parse` / `compare` (rate-limit
     exhaustion, or a poll job past its deadline /
     `openreading.router.driver.MAX_CONSECUTIVE_FAULTS` -- a named backend has no next rung to
     fall back to).
  4  `route`: no compliant backend for the policy (the empty plan is printed as JSON);
     batch `parse`: partial -- some items failed.
  5  `compare`: inputs are not schema-valid responses, or `--from` on a run that kept no
     candidates.
  6  interrupted, resumable: `parse` was interrupted (Ctrl-C) while `OPENREADING_LEDGER` was
     armed. The run did not fail; it parked mid-walk. A single-document run names its own
     `RUN_ID` for `openreading resume`; a batch names none (batch-level resume is out of scope).
     Unarmed, an interrupt stays an ordinary interrupt.
"""

from __future__ import annotations

from openreading.cli.app import main

__all__ = ["main"]
