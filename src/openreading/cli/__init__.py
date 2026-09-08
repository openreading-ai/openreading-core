r"""`openreading` command-line reference: every subcommand, the flags that matter,
the exit codes.

The implementation is `openreading.cli.app` (`main` is re-exported here). The
CLI is a thin shell over the public API (`openreading.api`): a file or URL
becomes one request, credentials resolve from the environment (never from the
command line -- see `openreading.credentials`), the request goes to one
backend, a strategy, or the router, and the one response schema is printed.

Quickstart
----------
Four commands parse and compare local documents with no key and no account.
A backend is the parser or extraction engine that reads your documents.
Install the clone's dependencies first with `make sync`, as the root README
explains. The comparison also needs the `tesseract` executable on your PATH.

    F=examples/john_smith_1000_2026_01.pdf

    openreading backends
    openreading parse $F --backend pymupdf > out.json
    openreading parse examples/ --backend pymupdf > all.json
    openreading compare $F --backends pymupdf,tesseract --format table

`backends` says what runs on this machine and what each other backend still
needs. The first `parse` reads one document and prints one response envelope.
The second points the same command at a FOLDER and prints one batch-result
over every document in it, which is the one-argument difference that most
readers miss. `compare` runs two backends over one page and shows you the
lines they read differently.

    $ jq -r .status.state out.json
    succeeded
    $ jq -c .summary all.json
    {"total":6,"succeeded":5,"failed":1,"pages_processed":10,...}

`examples/` holds five PDFs and a README, so `total` is 6 and the README is a
FAILED item carrying PyMuPDF's own `unsupported_format` reason. Every source is
offered to the backend, so nothing is filtered out before it is tried, and the
count always accounts for every file you pointed at.

From a clone the command is `uv run openreading`; an installed package puts
`openreading` on your PATH. Read the topics with `openreading help`.

Want the guided version? https://openreading.ai/oss-tutorial walks the tool
in seventeen steps, from this first parse to a policy, an escalating strategy,
a folder run and the HTTP server. The walkthrough lives in `openreading-web`
and uses the documents in this clone's `examples/` directory without a key.
The chapters below are the reference; the hosted tutorial is the tour.

Your next step is `openreading help response`, which explains the JSON you
saved, the content you can consume, and the fields that may be absent.

Understanding the response JSON
-------------------------------
You can switch backends without rewriting the code that reads their results.
A backend is the parser or extraction engine that reads your document.
The response envelope is one JSON object containing its content and outcome.
The field names stay consistent, but available content depends on the backend.

Start with the bundled statement, then inspect the result with `jq`:

    F=examples/john_smith_1000_2026_01.pdf
    openreading parse "$F" --backend pymupdf > response-mu.json
    jq -r '.status.state, .backend.id' response-mu.json
    jq -r '.document.text // empty' response-mu.json

The first query prints `succeeded` and `pymupdf` on separate lines. The second
prints the statement's text, beginning with `First National Bank`.
`// empty` prints nothing for an absent field. It does not prove completeness.

Choose the content your application needs:

    document.text              plain text for search and text processing
    document.markdown          formatted content for display or model input
    document.pages[].blocks[]   page elements in their reading order
    blocks[].table             table cells, spans, and a convenient rows grid
    typed_fields               named extracted values, when produced
    chunks[]                   chunks linked to source blocks, when produced

`blocks[]` above lives beneath `document.pages[]`, not at the top level.
For example, this prints page numbers beside each available text block:

    jq -r '.document.pages[]? | .page_number as $p |
      .blocks[]? | [$p, .type, (.text // "")] | @tsv' response-mu.json

Four top-level keys are required: `schema_version`, `status`, `backend`, and
`document`. Version `0.3` identifies the JSON contract, not the package.
At least one of `document.text`, `document.markdown`, `document.pages`, or
top-level `typed_fields` is present. Presence does not imply nonempty content.
A field extractor can return `document: {}` with `typed_fields` instead.

Read quality separately from content. `succeeded` says the operation completed,
not that it read every word correctly. `partial` carries incomplete content.
Do not treat `failed` or `processing` as a finished reading. Some failures
raise an error instead of producing a response at all; read `help output`.

A channel is one kind of output, such as text, tables, or block confidence.
`channel_provenance` records produced channels as `native` or `derived`.
Native means the backend supplied it. Derived means core computed it from
that output. This map is experimental; check the content fields themselves.
Warnings explain some limitations, but no warnings does not mean no gaps.

    jq '.channel_provenance // {}' response-mu.json
    jq -r '.warnings[]? | [.code, .message] | @tsv' response-mu.json

PyMuPDF produces no measured confidence and omits that field. Tesseract
produces OCR confidence but no structured table cells. Try the same consumer:

    openreading parse "$F" --backend tesseract > response-te.json
    jq -r '.backend.id, .document.pages[0].blocks[0].text' \
      response-mu.json response-te.json

Both runs print `First National Bank` in this captured example. They can still
disagree elsewhere in the document. OCR values can vary across installations.

Optional does not mean zero, empty, or null. Use `response.get("warnings", [])`
in Python and `.warnings[]?` in jq. Missing confidence is not confidence zero.
In Python, use `is not None` when zero is a valid measured value.
The value of an extracted field can contain false, zero, or nested nulls.
Covered positions in a table's `rows` grid can also contain null.

Bounding boxes use `x`, `y`, `w`, `h` in [0,1], with a top-left origin.
`page` is one-based in the source document. Multiply by the page dimensions
to draw an overlay. Geometry is optional; never invent a box when absent.
`bbox_native` preserves the original coordinates and their units.

`usage` reports counters such as `pages_processed`, `input_tokens`, and
`output_tokens`, not a dollar cost. Missing counters are unknown, not free.
`backend_raw` holds the native payload for vendor-specific inspection. Its
contents are outside the versioned contract; prefer normalized content first.

Do not confuse the outer shapes:

    one-document parse       this response JSON
    strategy parse           this response plus orchestration details
    folder or multi-input    batch-result, with items[].response when available
                             and per-item errors for failed documents
    compare                  comparison-report, not another parsed document
    POST /v1/jobs             async handle; its response appears when available

A strategy can retain a successful parse with `orchestration.outcome` set to
`degraded`. Inspect that outcome and the warnings before accepting its quality.
`orchestration` is permissive control-plane data, not a closed schema inside
the response. `openreading explain run.json` is its human-readable view.

For validation and Python consumers, read the worked response guide:
`src/openreading/schemas/README.md`. The web tutorial explains the same shape:
https://openreading.ai/oss-tutorial#3-understanding-the-response-json
`python -m pydoc openreading.schemas` lists the contract beside its validator.

help [TOPIC]
------------
Print one chapter of this manual, or the topic index when you omit TOPIC.
Every chapter comes from the `openreading.cli` docstring, so `pydoc` carries
the same text in source order.

    openreading help
    openreading help batch
    openreading help folder
    openreading help exit-codes | grep 143

Topic names and aliases ignore case, so `folder` and `BATCH` open the same
chapter. Pipe a chapter to `less` when you want a pager. For nested command
flags, run `openreading strategy show --help` or `openreading benchmark run
--help`. Their longer chapters are `help strategy` and `help benchmark`.

Exits: 0 an index or chapter; 2 an unknown topic, with suggestions on stderr;
3 when `python -OO` discarded the docstrings. Use a normal interpreter then.

Invariants shared by every subcommand
-------------------------------------
- `--env-file PATH` is accepted by every verb (default `./.env` when present).
  On `strategy` and `benchmark` it belongs before the sub-verb. For example:
  `openreading strategy --env-file ci.env validate`. An exported process
  variable always beats the file.
- `openreading --version` prints `openreading <version>` on stdout and exits 0,
  with no subcommand -- the same string as `openreading.__version__` and the
  `version` field of the server's `GET /healthz`, so an incident's first
  question has an answer that does not require a running server.
- The three streams have their own chapter, `openreading help output`.
  Output formats depend on the command. For example, `parse` prints JSON while
  `strategy show` prints YAML. Read that chapter before building a pipeline.
- A `<file>` starting with `http(s)://` is a URL: backends that ingest URLs
  natively get it as-is, the rest download to bytes first.
- No fallback widens the policy's default backend chain (D7, D7a).
  A policy with no registered backend prevents dispatch. `route`, including
  `route --run`, prints the empty plan and exits 4. Other commands report the
  refusal through their own exit codes or per-case results.
- The CLI passes no result cache (DECISIONS D-v3-3): silent memoization inside
  a library call is a footgun; a caller who wants it constructs one in Python.
- `OPENREADING_LEDGER` (a directory) arms the run journal that `resume` and
  exit 6 depend on. There is deliberately no CLI flag for it (arming is
  environment/config only, so `parse --help` gains nothing). Which runs
  actually journal: `openreading.cli.app`.

What lands on stdout, on stderr, and in the exit code
-----------------------------------------------------
`parse`, `resume` and `replay` print JSON envelopes on stdout. Their progress,
backend chatter and errors go to stderr, so a successful `> out.json` parses.
`help`, `backends`, `explain` and strategy inspection print human-readable text
or YAML. `benchmark run` prints its preflight and publisher report on stdout.
Use `benchmark report --format json` to obtain JSON from its saved artifacts.
For the fields inside a parsed document, run `openreading help response`.

    openreading parse examples/ --backend pymupdf > all.json   # JSON only
    openreading parse examples/ --backend pymupdf 2> run.log   # the story

A printed envelope is schema-validated before it is printed
(`schemas.validate_response` / `validate_batch_result`), so a non-conforming
document never reaches stdout. Only the batch path turns a conformance failure
into a coded exit (its validation sits inside the try, BL-84); in
single-document `parse`, `resume` and `replay` the call sits after every except
clause, so a non-conforming response surfaces as an uncaught traceback rather
than a coded exit.

stderr lines carry a bracket tag. `[<command>]` (`[route]`, `[resume]`,
`[compare]`, `[calibrate]`, ...) is the common shape. A single-document `parse`
tags its error lines with the run label instead of the command: the backend
slug, `strategy:<name>` or `router` (`[pymupdf] missing credentials ...`). On
that path `[parse]` appears only on selector misuse, a slug that fails catalog
lookup, and the interrupt lines. A batch `parse` prints `[batch]` for usage and
unexpected errors and for the `empty_batch` warning, `[i/N]` for progress, and
`[preflight]` when `--jobs` exceeds the named backend's descriptor cap.
Its exit-3 cannot-run line carries the run
label, not `[batch]`. A `compare` fan-out tags `[<backend>]` on a fanned-out
backend's FAILURE; a fan-out that succeeds prints nothing per backend.

Some lines carry no tag. Argument-parser usage errors and server logs use
their own formats. A backend's own chatter is redirected from
stdout to stderr and arrives exactly as that library wrote it, so PyMuPDF's
layout advisory shows up untagged in front of your own lines. And `strategy
validate`'s grammar error prints untagged on purpose, so every line of that
report shares one `LEVEL source:path: message` shape.

The exit code is the third stream, and it is the one a script reads. Branch on
it before you parse anything: see the "Exit codes" ladder below, or run
`openreading help exit-codes`. Exit 0 does not mean every document was read,
and on a strategy run it does not mean nothing was gated. Read `summary.failed`
on a batch, and `orchestration.outcome` and `warnings[]` on a single response.

Environment variables this module reads
---------------------------------------
Credentials never come from the command line. They come from the process
environment or a `.env` file, so a key cannot land in your shell history or in
a `ps` listing. `--env-file PATH` picks the file (default `./.env` when
present) and every verb accepts it. On `strategy` and `benchmark` it belongs
BEFORE the sub-verb, because it is declared on the parent parser:

    openreading strategy --env-file ci.env validate

A file never overrides a variable already set in the process, so an exported
value always beats the file. `.env.example` at the repo root is the
per-variable reference, one commented line each, and `openreading.credentials`
is the contract for per-backend keys and their precedence.

These are the variables the CLI itself changes behaviour on.

  OPENREADING_CONFIG        an explicit `openreading.yaml` path. Discovery
                            order, first hit wins: `--config`, then this, then
                            `./openreading.yaml`. Two files are never merged.
                            Export it before `resume`, because the journal
                            records the config's hash and not its path.
  OPENREADING_LEDGER        a directory, and setting it arms the run journal
                            that `resume` and exit 6 depend on. There is no
                            flag for it on purpose: arming is an environment
                            decision, and a per-run flag would put document
                            payloads on disk by accident. Only a strategy
                            dispatch journals, so a named `--backend` run
                            writes nothing while appearing armed.
  OPENREADING_LLM_DECIDER   the second enablement key for the LLM decider.
                            Set `1`, `true`, `yes` or `on` to enable it.
                            Unset, a configured decision point downgrades to
                            the engine default with reason `env_disabled`. Two
                            keys are deliberate, so a checked-in YAML cannot
                            start spending tokens on its own.
  OPENREADING_ALLOW_PRIVATE_URLS
                            allow a document URL whose host resolves to a
                            private or loopback address. Any nonempty value
                            enables it, including `0`. Unset refuses one
                            before the request leaves this process, which is
                            what stops a supplied URL from probing your
                            internal network.

`serve` reads its own set, and they are environment only because they are read
once at startup: `OPENREADING_API_KEYS` turns authentication on,
`OPENREADING_API_KEY_SCOPES` narrows one token to named backends, and
`OPENREADING_SERVER_PATH_ROOT` is the one directory beneath which a request may
name a local file by path. Without it set, a request sends `bytes_base64` or a
URL and a `document.path` is refused. `openreading help serve` and
`openreading.server` carry the rest.

Chaining one verb into the next
-------------------------------
`parse` is the hub. Every other verb either decides what `parse` should do or
reads what `parse` wrote. Two things pick the chain: the input FORM decides
which envelope you get back, and the FLAG decides which verb can read that
envelope next.

    parse FILE                     one response JSON
    parse DIR | 'GLOB' | A B       one batch-result JSON over all of them

    parse --save-dir DIR       -> DIR/<relpath>.json -> compare a.json b.json
    parse --keep-candidates    -> candidates[]       -> compare --from run.json
    parse --strategy NAME      -> orchestration{}    -> explain, replay
    parse + OPENREADING_LEDGER -> a run id on Ctrl-C -> resume RUN_ID

Decide before you run.

    openreading backends --check pymupdf         # is it reachable right now
    openreading route doc.pdf                    # which backends may see it
    openreading strategy plan doc.pdf --strategy main   # the pruned tree

Read what a run did.

    openreading parse doc.pdf --strategy main > run.json
    openreading explain run.json                       # gate by gate
    openreading replay doc.pdf --trace run.json        # take the same path

Find where two backends differ. `compare` reads what `parse` wrote, so a saved
response is a first-class subject and so is a whole folder run.

    openreading parse examples/ --backend pymupdf   > mu.json
    openreading parse examples/ --backend tesseract > te.json
    openreading compare mu.json te.json --format table   # a corpus verdict

Two batch-results pair their documents by `relpath`, so name each run after the
backend that produced it; the labels come from the filenames.

Compare a strategy's winner against completed parallel alternatives. A cascade
of backend steps retains no candidates, even when it escalates. The `fast` race
can cancel every loser before its response exists. Put this comparison
strategy in `openreading.yaml` to wait for both backends:

    version: 1
    strategies:
      duel:
        compare: [pymupdf, tesseract]

    openreading parse doc.pdf --strategy duel --keep-candidates > run.json
    openreading compare --from run.json --format table

Both backends must complete successfully to produce a winner and a retained
alternative. `--from` reads one response, so save a batch's individual
responses with `parse DIR --strategy duel --keep-candidates --save-dir out`.
Then compare one with `openreading compare --from out/invoice.pdf.json`.

Pick a run back up. Arm the journal first, because a run that journalled
nothing cannot be resumed.

    export OPENREADING_LEDGER=./.openreading
    openreading parse big.pdf --strategy main   # Ctrl-C prints a run id
    openreading resume 7dbf6b71-adb5-4e90-9188-a184fdba9d05

A resumed run reads its options from the journal, and the journal records the
config's HASH rather than its path. When your config is not
`./openreading.yaml` export `OPENREADING_CONFIG` before you resume, or the
resumed run cannot find the strategy by name.

An interrupted batch prints no single run ID. Its per-document journals may
be resumable individually. `openreading help resume` explains where IDs live.

Tune a gate from your own documents, then check what you pasted.

    openreading calibrate samples/ --strategy main --target-escalation 0.15
    # paste the printed escalate_if: into openreading.yaml, then
    openreading strategy validate

One pair that looks like a chain and is not. `leaderboard` does not read a
`--save-dir` tree: it takes a labeled dataset of `<case>/case.json` and runs
the backends itself, where `compare` needs no labels and scores nothing. They
answer different questions, so no arrow joins them.

`explain` reads a folder run as well as a single one, naming each document:

    openreading parse examples/ --strategy fast > all.json
    openreading explain all.json

What a run uses, and how to use less
------------------------------------
Every response carries a `usage` block, and it reports what the backend
consumed in the unit that backend meters in: `pages_processed`, `credits`,
`input_tokens`, `output_tokens`, `duration_ms`. A counter the backend did not
report is absent rather than zero.

    $ jq -c .usage out.json
    {"pages_processed":1}

There is no dollar figure anywhere in this package. `usage.cost_usd` and
`usage.cost_basis` were removed along with the per-vendor price tables that
filled them. Turning a page count into money needed a rate read off a vendor
page and typed into this source, which nothing here could verify and nothing
could tell had gone stale. The derived figure then sat on `usage` beside
counters that were genuinely measured, and no reader could tell which was
which. Multiply these counters by the prices on your own invoice, which is the
one rate card carrying your tier and your negotiated rate.

Four commands do more work than you may expect, each for its own reason.

  parse <folder>      one run per document. A hosted backend with more than ten
                      live items prints a `[preflight]` line to stderr first,
                      naming how many calls leave your machine and on whose
                      key.
  compare --backends  one full run per subject. Three backends on one document
                      is three runs of that document, not one.
  leaderboard         cases times backends. `--all-ready` over a real dataset
                      is the largest run in this CLI.
  benchmark run       someone else's corpus, so it runs two documents unless
                      you say otherwise, prints pages and a call count first,
                      and asks before a large or unbounded hosted run. CI
                      passes `--yes`.

How to use less.

Start local. `pymupdf` and `tesseract` need no key and no network, so a
pipeline is worth debugging on them before a hosted backend ever sees it. Ask
first. `openreading route doc.pdf` and `openreading strategy
plan doc.pdf --strategy main` print what WOULD run and execute nothing. Cascade
instead of fanning out. A `try:` strategy reaches the expensive backend only on
the documents the cheap one could not read, where `compare:` calls both on
every document. Keep the sample small. `calibrate` and `benchmark run` both
take a subset, and a threshold found on 30 documents transfers to 3000. Raise
`--jobs` freely. Concurrency changes how long a folder takes and never how many
calls it makes.

Every source is dispatched, so a file a backend cannot read still costs one
call: it comes back as a failed item carrying that backend's own reason.
`openreading help batch` has the shape.

parse <file|url|dir|glob ...>
-----------------------------
Usage: `parse SOURCE... (--backend SLUG | --strategy NAME | --no-strategy)`.
Run one backend, a named strategy or preset from `openreading.yaml`, or the
router, and print the normalized response. Exactly one of the three selectors
is required (else exit 2).

    openreading parse loan.pdf --backend pymupdf --pages 1
    openreading parse https://example.com/loan.pdf --backend reducto
    openreading parse invoice.pdf --backend anthropic-claude \
      --extract "totals and dates"
    openreading parse loan.pdf --strategy cheap_first

Single-document flags:
  --backend SLUG      one built-in backend (`openreading backends` lists them).
                      The slug must also resolve in the adapter catalog; a
                      divergence is exit 2, not a traceback.
  --strategy NAME     a strategy or preset; the response then carries an
                      `orchestration` block. An unknown name is exit 2
                      (`unknown_strategy`, DECISIONS D-v3-2).
  --no-strategy       force the router's configured chain, ignoring
                      `defaults.strategy` (`strategy:none`, the reserved escape
                      hatch back to the plain router).
  --config PATH       an `openreading.yaml`; else `OPENREADING_CONFIG`, else
                      `./openreading.yaml`.
  --operation OP      backend sub-operation (e.g. `AnalyzeLending`,
                      `prebuilt-invoice`).
  --pages N [N ...]   1-based page numbers.
  --extract [TEXT]    schema-driven field extraction (default instructions when
                      TEXT is omitted). A directly-named backend that cannot do
                      it raises `unsupported_feature` (exit 3) instead of
                      silently returning geometry-only output, which would deny
                      the ask (DECISIONS D13); the router pre-filters such
                      backends. `--pages` and `--extract` take a variable
                      number of values, so name FILE before them or close the
                      flag list with `--`.
  --keep-candidates   retain completed parallel alternatives under
                      `orchestration.candidates[]` (what `compare --from`
                      reads). Off by default (payload bloat); no effect on a
                      direct backend run; in batch mode it applies per item
                      (DECISIONS D-v4-14).
  --deadline SECONDS  absolute wall-clock budget for a directly-named
                      (`--backend`) backend. Default 120s
                      (`openreading.credentials.DEFAULT_DEADLINE_MS`): enough
                      for a synchronous single-page backend, sometimes too
                      short for a hosted async backend's ordinary workload
                      (e.g. Textract on a large document) -- raise it rather
                      than re-running into the same wall. No effect on
                      `--strategy` / `--no-strategy`, which manage their own
                      per-node budget. `0` or negative means fail fast: do not
                      wait at all.

Single-document exits: 0 printed; 2 selector misuse, unknown backend/strategy,
unresolvable source; 3 cannot run (missing credentials -- the message names the
exact vars and signup URL -- `auth_rejected` with its `check <VAR>` hint,
`unsupported_format` for a named backend, `unsupported_feature`,
`ScopeRefused`, an exhausted router plan, or a
`RetryableError` reaching a directly-named backend: rate-limit exhaustion or a
poll job past its deadline / `MAX_CONSECUTIVE_FAULTS`
(`openreading.router.driver`, 120), which has no next rung to fall back to the
way a resolved chain does); 6 interrupted while the ledger was armed
(below); 1 anything
else. Every other backend error message has any resolved secret value redacted
to `***`.

Batch: a directory, a glob, or two or more sources
...................................................
The envelope is decided by input FORM (invariant M2): a directory, a glob, or
>=2 arguments produce ONE `batch-result` JSON over every document; a single
explicit file/URL stays the single-document `response` above, byte-identical.
Sources may mix files, dirs, globs and URLs; a directory expands recursively
(sorted; hidden files and symlinks skipped). Quote a glob so your shell hands
it over whole, and `**` matches every depth. Each glob selects a file once,
even when its directory matches overlap. Repeating a source in separate
arguments still requests repeated processing. Each item's `relpath` is measured
from the directory you named, or from the fixed part of the pattern before the
first wildcard, so two files with one name under different parents stay two
records and `--save-dir` writes two files. Each succeeded item carries a full
`response` envelope, so a batch is a first-class `compare` subject. Under
`--no-strategy` routing is per file (`summary.backends` tallies which backend
took what).

    openreading parse invoices/ --backend pymupdf > run.json
    openreading parse invoices/ extra/w2.png --no-strategy
    openreading parse 'scans/**/*.png' --backend tesseract --jobs 4

Batch flags (in addition to the single-document ones):
  --jobs N            concurrent workers. Default 1 (serial: deterministic,
                      rate-limit-safe); `N <= 0` clamps to 1; above
                      `--max-jobs` exits 2.
  --max-jobs N        ceiling on `--jobs` (default 32,
                      `openreading.batch.runner.MAX_BATCH_JOBS`) -- the same
                      floor-clamp / ceiling-reject the server enforces on `POST
                      /v1/batch`, with a caller escape hatch the server's
                      untrusted boundary lacks.
  --max-items N       hard cap on expanded files (default 200); exceeding it
                      exits 2.
  --deadline SECONDS  in batch mode, overrides the deadline of a directly-named
                      NATIVE-batch backend's one vendor `submit_many`
                      (currently `anthropic-claude`); no effect on the platform
                      fan-out path. The default here is NOT the 120s above but
                      an adapter-appropriate budget (1h for `anthropic-claude`,
                      per its descriptor's "most <1h"). `0` or negative means
                      fail fast.
  --save-dir DIR      also write each succeeded item's response to
                      `DIR/<relpath>.json`.

A file whose format the backend cannot take is a SKIPPED item with a reason
(the backend's own `unsupported_format`) -- never a crash, never a silent
omission. Per-item isolation (M6) means a failure never raises out of the
batch, so the stderr progress line (`[i/N] <path> <state> <code>: <message>`)
is the only place a failed item's message is read. A `[preflight]` advisory
prints when requested concurrency exceeds a named backend's declared
limit. A
`--jobs N` above that backend's
`descriptor.batch.max_concurrency`, naming N and the cap, because only the
capped value survives into `request.jobs`. A source list that resolves to zero
documents prints the envelope's `empty_batch` warning to stderr so silence is
never mistaken for a hang. Exits: 0 nothing FAILED; 4 partial (some items
failed); 1 nothing succeeded (an unknown `--strategy` becomes a failed item per
file, so it lands here with the same hint on stderr AND each item's `error` --
`code: unknown_strategy` -- in the envelope); 2 unresolvable source,
`--max-items` or `--max-jobs` exceeded; 3 cannot run at all, which on a batch
means only a native-batch backend's one vendor call refusing before any item
ran (missing credentials, `ScopeRefused`, or a `RetryableError` / deadline
from `submit_many`, which has no next rung and does not retry), or an
`openreading.yaml` that will not load; 6 interrupted while
`OPENREADING_LEDGER` was set -- per-item runs may be individually resumable,
but batch-level resume is not supported, so no single run id is named.

Exit 0 does NOT mean every document was read. Every source is dispatched, so a
folder of 40 PDFs and one stray `.txt` sends all 41 and the `.txt` comes back
as a FAILED item carrying the backend's own reason. That batch is `partial` and
exits 4. Branch on `summary.failed`, never on exit 0 alone.

The same missing key that exits 3 on one file exits 1 on a folder. Per-item
isolation is the point of a batch: one item's failure never stops the rest, so
a missing credential becomes a failed item per document rather than a refusal
of the run. The envelope names the cause on every item; the exit code only says
how much survived.

resume RUN_ID
-------------
Re-drive a run recorded under `OPENREADING_LEDGER` from its own journal: every
step already terminal there replays byte-identical with zero network calls;
anything genuinely unreached executes for real. Takes ONLY `RUN_ID` -- every
other option comes from the ledger, not the command line. A run is resumable
once the ledger was armed for it, whether the original `parse` went on to
succeed, was interrupted (exit 6) or crashed; the id is a UUIDv4, 36
characters, printed by `parse` on interrupt or read from the run's own header
under `$OPENREADING_LEDGER`.

    export OPENREADING_LEDGER=./.openreading
    openreading parse big-batch.pdf --strategy cheap_first  # Ctrl-C: exit 6
    openreading resume 7dbf6b71-adb5-4e90-9188-a184fdba9d05

    [parse] interrupted; run 7dbf6b71-adb5-4e90-9188-a184fdba9d05 is resumable
    [parse] resume with: openreading resume 7dbf6b71-adb5-4e90-9188-...

A resumed run REFUSES BY NAME (exit 3) rather than falling back to a fresher
config when `openreading.yaml` (`config_hash`), the compiled plan, or the
journal's record format no longer match what the original run saw -- a resumed
run replays recorded decisions; a changed config would silently mean a
different run:

    [resume] refused: openreading.yaml changed since 7dbf6b71-adb5-...
             (sha256 3f9a... -> c21b...)
    [resume] a resumed run replays recorded decisions; start a new run instead

The `policy:` block counts as part of that config. Editing its default backend
chain refuses the resume because it changes the compiled plan.

Also exit 3: an unknown `RUN_ID`, `OPENREADING_LEDGER` unset, or a run whose
payload files are missing or fail their recorded digest check.
Python: `openreading.resume(id)`.

route <file|url> [--config FILE] [--run]
----------------------------------------
Print the chain that would run; with `--run`, execute it (chosen, then
fallbacks). The list comes from `openreading.yaml`, found in the working
directory or named with `--config`.

    cat > openreading.yaml <<'YAML'
    version: 1
    policy:
      backends: [pymupdf, tesseract, aws-textract]
    YAML
    openreading route loan.pdf
    openreading route loan.pdf --run > plan.json
    jq '.result' plan.json > out.json

`route --run` puts the response inside its plan's `result` field. Check the
exit code before extracting it for `compare` or other response consumers.
`parse` reads the same file and the same list, so a chain you print here is the
chain a `parse` in that directory runs. A different list is a different file:
`openreading route loan.pdf --config airgapped.yaml`.

`policy:` has one key, `backends`: the default backend ids for an unnamed
request, in preference order. A request or strategy may name another backend
explicitly. Any other policy key is refused by name (with a `did you mean` for
a near miss), and a value of the wrong type is refused too. A malformed block
is exit 3 before a backend is contacted.

Nine keys used to live here, asking the engine to enforce a compliance posture
from a per-vendor table it kept in its own source: whether each vendor signs a
BAA, trains on customer data, or retains a document for so many hours. Nothing
in this tool can observe any of that, so a stale entry did not fail loudly. It
routed a document to a backend the operator believed was excluded, and the run
succeeded. You already know which vendors you hold agreements with; `backends:`
is that conclusion, written by the one party who can reach it.

Selection with no list is a lookup, not a guess: the backend you named, else
`policy.backends` in order, else `pymupdf`, which needs no key and no config.
An EMPTY list permits nothing and refuses.

Output is `{chosen, fallbacks, dropped, terminal_reason}` plus, with `--run`, a
`result`. `dropped` carries a backend your own allow-list excluded, never a
judgement about a vendor. `--run` never widens the chain; a fallback actually
used is recorded in the result's `warnings[]`. Exits: 0; 4 no backend left in
scope (the empty plan is still printed as JSON); 3 an unloadable config file,
an unreadable document, or a plan-exhausted `--run` (the plan is still printed;
the stderr trail names each backend's failure and a `check <VAR>` hint for
every rejected key).

backends [--check SLUG[,SLUG...]|all] [--timeout SECONDS]
---------------------------------------------------------
Show every backend and whether it is CONFIGURED to run here (extra installed,
credentials found), with the exact env vars still missing. Bare `openreading
backends` is offline and free: it never touches the network.

    BACKEND                        TYPE               CONFIGURED  MISSING
    pymupdf                        oss_library        yes         -
    reducto                      hosted_api       no        REDUCTO_API_KEY

"Configured" is not "reachable" -- a `DOCLING_SERVE_URL` pointing at a dead
port is configured. `--check` answers the other question by actually probing,
and it is NEVER implicit: on a CLI, a flag the user typed is the consent a page
load can never be. Reports print as they complete, so a slow backend never
hides the ones that already answered.

    BACKEND  PROBE     STATUS          MEASURED  LATENCY  DETAIL
    pymupdf  local     live            yes       0ms      responding, the...
    chunkr   none      not_configured  no        -        not configured:...
    docling  endpoint  not_configured  no        -        not configured:...
                                          (columns abridged for this page)

Status ladder (`openreading.types.liveness.LivenessStatus`), seven states
ascending in what is known: `not_supported` (no probe AND nothing declared to
infer from -- "every requirement resolves" is vacuous there, so it declines to
guess) < `not_configured` (the same `backend_readiness().ready` judgement as
the table above, so "configured" cannot mean two things; short-circuits the
probe) < `configured_unverified` (inferred) < `live` (measured), with
`unreachable`, `unauthorized` (key rejected) and `error` (the probe itself
raised -- reported as a finding, never a crash) as the measured negatives.
`MEASURED` is the column to read: `yes` means the backend was called, `no`
means the status was inferred from the environment with no round trip (so no
latency). Inference is a first-class state distinct from measurement (DECISIONS
D-v7-3) because rendering an inference as `live` is exactly the "configured
wearing the word ready" defect the column exists to fix. A probe is never a
billed request (D-v7-4): a vendor with no free liveness call declares no probe
and reports `configured_unverified` rather than spending your money. `--check
all` probes only backends that DECLARE a probe (probing the rest would re-print
the free inference). `--timeout` omitted means the adapter's own declared
`liveness.timeout_s`, else 5s (`openreading.liveness.
DEFAULT_PROBE_TIMEOUT_S`); any value is clamped to [0.1, 30] (clamped, not
rejected, so nobody parks a worker indefinitely). Liveness is a diagnostic,
never routing input (D-v7-6). An unknown slug exits 3.

compare <subjects...>
---------------------
Usage: `compare a.json b.json ...`, or one document with `--backends a,b`,
or `--all-ready`, or `--from run.json`.
Compare backends' outputs and print the delta (fields / text / blocks). Three
ways to get the N subjects: response JSON files (the primitive); fan-out over
one document (`--backends a,b` or `--all-ready`, optionally `--save-dir DIR` to
keep each envelope as `DIR/<backend>.json`); or a strategy run's retained
losers (`--from run.json` after `parse --keep-candidates`, the winner compared
against `orchestration.candidates[]`). The comparison is pure; fan-out is
CLI-only sugar over independent direct parses, run SERIALLY (deterministic
subject order, one hosted call in flight, so a wide `--all-ready` cannot
stampede provider rate limits).

    openreading compare a.json b.json --format table          # delta-first
    openreading compare a.json b.json --format table --show-agreements
    openreading compare doc.pdf --backends pymupdf,tesseract  # fan out first
    openreading compare a.json b.json --format diff       # exactly 2 subjects
    openreading compare a.json b.json --baseline pymupdf  # a.json's backend
    openreading compare a.json b.json --truth golden.json   # score vs golden
    openreading compare --from run.json
    openreading explain report.json                 # render a saved report

Flags: `--format json` (default, schema-valid, pipeable) | `table` | `md` |
`diff` (2-way git-style text diff plus field deltas; exactly two subjects) |
`diffs` (N-way content verdict + structure view, no packaging noise; see
`openreading.comparison`). `--show-agreements` lists agreeing fields in the
table and Markdown formats (hidden by default). `--baseline LABEL` signs deltas
against one subject. Labels come from `backend.id`, not the response filename.
Read `subjects[].label` in the JSON report when backend IDs repeat. A response
path instead adds a new baseline subject. `--truth golden.json`
scores each subject against a golden in the evals `expected` shape. `--deadline
SECONDS` overrides the 120s single-document deadline for every fanned-out
backend (the same escape hatch as `parse --backend --deadline`; `0` or negative
means fail fast).

Corpus mode: when EVERY subject is a `batch-result` envelope (from `parse
<dir>`), documents are paired across runs by identity (`relpath`, then
`filename`, then `sha256`) and a corpus report is emitted -- a per-document
verdict (`equivalent` / `divergent` / `mixed` / `unpaired`) with a rollup.
`--format json` is the schema-valid `corpus-report`; `table` prints one verdict
line per document; `diffs` is VALUE-FIRST: under each divergent document it
prints the actual content each backend captured that the other missed (real
lines, token-coverage matched so packaging never fakes a delta), capped at a
few per side with a `... N more` tail, plus a footer one-liner to dump one
document's full text. It is not counts and not structure (table shapes / types
/ granularity live in `table` and the single-pair drill), because a corpus-wide
four-section diff is a wall. Mixing batch and single-response subjects is a
usage error (exit 2); `--format diff` is 2-way text only and is refused in
corpus mode. Corpus mode also refuses `--baseline`, `--truth` and
`--show-agreements` at exit 2. Those options apply to single-response subjects.

Exits: 0; 2 misuse (<2 subjects, fan-out with more than one document, <2 or
unknown fan-out backends, `--format diff` with != 2 subjects, mixed subject
kinds); 3 a fanned-out backend cannot run (missing credentials,
`ScopeRefused`, `unsupported_feature`, `RetryableError`); 5 inputs are not
schema-valid responses (unreadable file, invalid envelope) or `--from` on a run
that kept no candidates (the message explains completed parallel alternatives
and `--keep-candidates`, DECISIONS D-v4-14); 1 anything else.

Datasets for calibrate, leaderboard and rules
--------------------------------------------
A dataset groups documents with their expected results, one `case.json` per
case directory. `calibrate`, `leaderboard` and `rules` read this layout.
A folder of documents from `parse` or `--save-dir` is not a dataset.

Save this example as `samples/one/case.json` to use the generated local sample:

    {
      "name": "one",
      "input": {"builtin_sample": true, "pages": [1]},
      "expected": {"text_contains": ["OpenReading Test Document"]}
    }

For your own document, replace `input` with `{"path": "invoice.pdf"}` and
put that file beside `case.json`. Each backend reads the formats its
descriptor claims. A descriptor is the backend's static capability record.
See `src/openreading/adapters/README.md` for the catalog and installation.

`expected` holds the labels used for scoring. Omit it, or use `{}`, for an
unlabeled calibration case. Those cases still inform escalation rates, but
do not contribute to scorer agreement. `leaderboard` needs expectations for
meaningful scores, and `rules` needs existing expectations to generate rules.

    openreading leaderboard samples/ --backends pymupdf,tesseract
    openreading calibrate samples/ --strategy main
    openreading rules samples/

The calibration command also needs the cascade from `openreading help
calibrate`. `openreading.evals.dataset` documents the remaining case fields.
For `compare --truth golden.json`, put only the `expected` object in that
file, such as `{"text_contains": ["OpenReading Test Document"]}`.

leaderboard <dataset_dir>
-------------------------
Usage: `leaderboard DIR (--backends a,b | --all-ready) [--config FILE]
[--format table|json]`.
Rank N registered backends on ONE dataset -- measured, not vendor-claimed. Runs
the same `case.json` corpus (`openreading.evals.dataset`; the repo ships one
under `src/openreading/evals/sample`) through the unchanged
`openreading.evals.runner.run_case` path for every named backend. It uses the
same five-dimension scorer and prints one ranked `BenchmarkReport`: measured
mean score,
per-dimension breakdown, per-case result table, and error tally.

Each named backend is an explicit benchmark target. `--config` supplies shared
runtime settings.

    openreading leaderboard datasets/paystubs/ \
      --backends aws-textract,google-document-ai
    openreading leaderboard src/openreading/evals/sample \
      --backends pymupdf,tesseract --format json
    openreading leaderboard datasets/paystubs/ --all-ready

`--format table` (default) prints the dataset's own identity (path, case count,
case names) above the ranking so a screenshot is never read as a universal
verdict rather than "on these N documents"; `--format json` is the schema-valid
`BenchmarkReport` (`leaderboard-report.v0.1.json` in `openreading.schemas`) a
script or CI job consumes.

The table carries `scored` (`n_scored/n_cases`) beside `mean`, and prints
`mean` as an em dash when `n_scored` is 0, because a mean over no scored case
is not a measurement and a printed `0.000` is indistinguishable from a backend
that measured 0.00 on every case it ran. `errors` does not separate them
either: a case naming no recognized `expected` dimension is unscored without
erroring. A non-deterministic backend's row is marked in place -- its mean is
one labeled sample. The per-case block states `winner=`, `tie=`, `no winner
(every scored backend got 0.00)` or `no result (no backend produced a score)`,
and totals the four, because the report's own `winner` field breaks a tie
alphabetically for byte-stability and printing that as a result turns ties and
mutual failures into a clean sweep for anyone tallying the block. The JSON
`winner` is unchanged. Any backend per-case fault
per-case fault, is that backend's own scored, error-carrying case -- in its
error tally, excluded from its mean -- never a silently skipped case, never a
crash. Every backend makes a REAL call per case: `--all-ready` over a large
dataset is N x M billable calls, not N + M. The numbers are evidence a human
reads and are never fed back into the router's scoring or any adapter's
`integration_priority`. Exits: 0; 2 <2 backends or an unknown `--backends` id;
3 an openreading.yaml that will not load, an unresolvable/empty dataset
directory, or a cannot-run fault.

rules <dataset>
---------------
Turn the expectations a dataset's cases already carry into ParseBench rule
objects, so nobody hand-authors another company's JSON to get started.

    openreading rules mydata            # print what it would add
    openreading rules mydata --write    # edit the case.json files in place

`text_contains` becomes a `present` rule per string, passed through untouched.
Each table cell becomes a `table` rule carrying its right neighbour and its
column heading, which is what makes it structural rather than a second presence
check. `text`, `markdown` and `typed_fields` generate nothing: a whole-document
string is a similarity measure, and forcing it into a rule would demand a
character-exact reproduction no backend passes.

Printing is the default because this rewrites files a person hand-labeled. A
case that already has `rules` is skipped unless `--force`. The rule worth
having most, `absent`, cannot be generated at all, because nothing in a case
says what must NOT appear; the command says so when it finishes. Scoring the
result needs the `parsebench` extra (`openreading.evals.rules`). Exits: 0; 3 a
dataset directory with no `<case>/case.json`, or a `case.json` that will not
parse.

benchmark <list|show|prepare|estimate|run|report>
--------------------------------------------------
A benchmark profile connects one public dataset and official scorer to
OpenReading. A target is one backend or strategy evaluated on that dataset.
Discovery is offline and requires no optional package.

    openreading benchmark list
    openreading benchmark show parsebench
    openreading benchmark prepare parsebench --preset smoke
    openreading benchmark estimate parsebench --preset full \
      --target backend:pymupdf
    openreading benchmark run parsebench --target backend:pymupdf  # 2 docs
    openreading benchmark run parsebench --target backend:pymupdf \
      --limit 0 --yes                                          # all of them

`list` displays runnable and cataloged profiles with separate dataset-terms
lanes. `show` prints publisher sources, data and code licenses, published
scale, metric dimensions, and the install extra. Runnable profiles are
ParseBench and ExtractBench. Their official packages require Python 3.12 or
newer and stay outside the base installation.

`prepare` uses the publisher's downloader and writes beneath
`~/.cache/openreading/benchmarks` by default. `run` prepares the same cache,
executes every repeated `--target`, and writes publisher artifacts beneath
`./benchmark-results`. `--jobs` controls document concurrency. `--force`
replaces complete publisher artifacts, while an ordinary rerun resumes by
letting the official harness skip valid results.

What a benchmark run uses, and how to use less
.............................................
`run` prints the comparison when it finishes, ranked by the publisher's own
numbers, and writes `openreading-run.json` beside the artifacts so a later
reader can tell which pipeline was which target. `report` prints that same
table again from a finished run without re-running anything, with `--format
json` for a script. Neither computes a score: both read the publisher's
`_evaluation_report.json` back, so the terminal and the publisher's dashboard
cannot disagree.

`run` touches **two documents** unless you say otherwise, because it calls
someone else's API on your key. `--limit N` runs N, `--limit 0` runs the
whole prepared corpus, and `--doc NAME` (repeatable) runs documents you name by
id (`table/doc1`) or file stem. A limited run is written out as a corpus in the
publisher's own format, so every metric and report behaves exactly as it does
over the full set. A run that covers everything uses the prepared cache in
place.

Documents under `--limit` are chosen round-robin across the corpus's
categories, so two documents span two categories rather than two charts. The
order is stable, so the same `--limit` picks the same documents and a rerun
resumes instead of running them again.

Before a target runs, `run` prints the documents it chose, their PAGE count,
and the call count per target. Pages expose volume that document counts hide:
ExtractBench is 370 documents and 4,869 pages. It quotes no price: the rates it
used to multiply were a vendor rate card typed into this package's own source,
unverifiable from here. One target reports its calls as a floor rather than a
count: a `strategy:` target escalates, so one document is one or more calls,
and nothing here knows how many rungs fire.

A hosted run above twenty-five pages, or one whose call count cannot be stated
at all, asks before it starts. `--yes` answers in advance. With no terminal
attached the question is not asked and the run refuses, naming `--yes`, because
a CI job hung on stdin is worse than one that stops.

Every document calls `openreading.run`, including a `strategy:NAME` target
selected with `--config`. The file's `policy.backends` remains the default
chain for null-backend
requests. The raw publisher artifact retains the complete
OpenReading response. The official normalized artifact receives Markdown and
layout for ParseBench,
or typed values and citations for ExtractBench. Two or more successful targets
also produce the publisher's cross-pipeline leaderboard.

A commercial lane needs no CLI acknowledgement. Research-only terms require
`--allow-research-only`. Missing, mixed, or source-specific terms require the
separate `--allow-unverified-terms` flag. Neither flag makes a cataloged
profile runnable or claims a use is lawful. `estimate` refuses a cataloged
profile too, because its published scale would otherwise read as a run you
could start.

A per-document fault is the publisher's to record, not this command's to raise.
The official harness catches whatever one document's provider call throws,
marks that case failed, and keeps going, so a `ScopeRefused` or a missing
key on document 40 of 300 surfaces as exit 1 with a failed case in the
publisher's report, never as exit 3. Read the report to find out which
documents fell over and why. Exit 3 is left for a fault raised outside that
per-document boundary. Exits: 0 complete; 1 publisher inference, scoring, or
comparison failure, including documents the publisher recorded as failed; 2
invalid profile, target, preset, `--jobs`, terms acknowledgement, optional
package, preparation, an unknown `--doc`, or a run stopped at the spending
confirmation; 3 an OpenReading cannot-run fault raised before or around the
publisher run.

strategy <verb>: list, show, validate, normalize, plan
------------------------------------------------------
Inspect and drive `openreading.yaml` strategies. `openreading.strategies` maps
the package and `openreading.strategies.model` is the grammar reference. Every
verb takes `--config PATH` (else the discovery order above). `validate`,
`plan`, `normalize`,
`replay` and `calibrate` need a config and exit 3 without one ("no
openreading.yaml found"); `list` and `show` run config-free on the built-in
presets (an unparseable config is exit 3 for every verb).

    openreading strategy validate
    openreading strategy plan doc.pdf --strategy NAME
    openreading strategy show NAME [--longhand]
    openreading strategy list
    openreading strategy normalize
    openreading explain response.json
    openreading replay doc.pdf --trace response.json [--strategy NAME]
    openreading calibrate samples/ --strategy NAME [--target-escalation 0.15]

- `validate`: grammar (schema) + world-consistency check of the whole file,
  then per strategy a dialect badge (`dialect: plain` or `dialect: advanced
  (first advanced key: ...)`), the body as written, a plain-English summary,
  and a glossary of the criterion words used. Errors go to stderr and warnings
  to stdout; exit 3 on any error, 0 otherwise -- warnings never fail (DECISIONS
  D-v3-8). Issues are located by file + node path, not line number. Steps
  unreachable under the file's own `policy:` block are flagged. A grammar error
  prints untagged (`ERROR <source>: <detail>`) so every line shares one `LEVEL
  source:path: message` shape.
- `plan`: the Terraform-style speculative plan -- the pruned tree for THIS
  document + policy (`{strategy, config_hash, eligible, dropped[], tree}`), no
  execution. Exit 3 on an unreadable document or config file, an unknown
  strategy, or `ScopeRefused`.
- `show NAME`: a strategy or preset body AS WRITTEN (a Plain strategy prints
  Plain); `--longhand` prints the canonical desugared full-grammar tree
  instead. Unknown name: exit 3.
- `list`: built-in presets, then the configured strategies (with the config
  path).
- `normalize`: the whole config as canonical longhand YAML (the `docker compose
  config` analog).

explain <response.json | batch-result.json | comparison-report.json>
..................................................................
Render a response's `orchestration` block as a story (strategy, chosen backend
and outcome; each attempt's node, backend, category and duration; gate
rows -- grouped under their Plain source word when present -- with
observed/threshold and FIRED/skipped/ok; decisions with `decider=` and, when a
point resolved to something other than its configured choice, `downgraded=`;
dropped backends by stage and code). A file that is a comparison report
(`subjects` + `fields` + `findings`) renders as the compare table instead. No
orchestration block: exit 3. A batch-result renders each item's orchestration
and names skipped, failed or directly parsed items too. If no item carries
orchestration, it exits 3. An unreadable or invalid JSON file also exits 3.
A corpus comparison report is not a supported `explain` input. Re-run
`compare` over the saved batch-results with `--format table` to render it.

replay <file|url> --trace <response.json>
.........................................
Re-execute the strategy for the document but take, at each decision point, the
choice logged in `--trace` (a saved response or bare orchestration JSON) -- the
TraceDecider (`decider: "trace"` in the new records, DECISIONS D-v3-18). A
decision absent from the trace takes the engine default (`trace_missing`).
Deterministic and offline for local backends; it consults no LLM, so the
LLM-enablement gate is moot. The strategy name comes
from `--strategy` or the trace (neither: exit 2). A trace whose `config_hash`
differs from the freshly compiled one is REFUSED (exit 3): its logged decisions
were made under a different configuration, not merely a
different document; a trace with no `config_hash` at all is left to the
per-decision `trace_missing` downgrade.

calibrate <dataset> --strategy NAME
...................................
Derive gate thresholds from a sample (a dataset dir of `*/case.json`). Runs the
strategy's rung-1 backend over the sample, scores each result with the eval
scorers, sweeps each gated threshold, and prints candidate operating points
(threshold -> predicted escalation rate and scorer agreement) plus a
ready-to-paste `escalate_if:` recommendation for `--target-escalation`. It
never rewrites the config (DECISIONS D-v3-21). A case whose `expected` names
none of the
scorer's five recognized dimensions is an ordinary "not labeled yet" case:
excluded from `scorer_agreement` rather than silently required; the report's
`n_scored` (next to `n_docs`) says how many contributed and a `[calibrate]`
stderr advisory fires when some or all of the sample went unscored -- an
unlabeled sample would otherwise yield a flat, precise-looking agreement number
that measured nothing. Offline for local backends. Rung-1 runs under a fixed
60s deadline; `RetryableError` / `unsupported_feature` exit 3.

Calibration needs a cascade whose first step names one backend. A race such
as `fast`, or a cascade starting with a parallel step, exits 3. Only numeric
first-step predicates can be swept. For example, save this `openreading.yaml`:

    version: 1
    strategies:
      main:
        steps:
          - backend: pymupdf
            escalate_if:
              chars_per_page_below: 100
          - tesseract

    openreading calibrate samples/ --strategy main \
      --target-escalation 0.15 > calibration.json
    jq '.recommended.escalate_if' calibration.json

`openreading help datasets` shows how to create `samples/`. Stdout is a JSON
report, with the proposed gate under `recommended.escalate_if`. Copy those
thresholds into the first step's `escalate_if`, then run `strategy validate`.
For a Plain strategy, use `strategy show NAME --longhand` to obtain an advanced
body before adding advanced gate keys. Mixing both dialects in one body fails
validation. An empty `recommended` means there is no numeric gate to propose.

serve
-----
`serve [--host H] [--port P] [--cors-origin O ...]` runs the HTTP API
(`openreading.server`; needs the `[server]` extra, else exit 3). Default
`127.0.0.1:8787`. (This package ships no web UI. `serve` exposes the JSON API
only.) Binding any host other than `127.0.0.1` prints a warning: anyone who can
reach the socket spends your vendor keys, so put it behind your own auth/proxy.
The server never reads the working directory for a config -- pass
`OPENREADING_CONFIG`.

A request may not name a local file by path unless
`OPENREADING_SERVER_PATH_ROOT` is set to a directory. With it set,
`document.path` may resolve beneath that directory and nowhere else. Without
it, a client sends `bytes_base64` or a URL. Authentication is OFF until
`OPENREADING_API_KEYS` holds a comma-separated list of bearer tokens; every
endpoint but `GET /healthz` and `POST /v1/webhooks/{backend_id}` then answers
401 without an `Authorization: Bearer` header. `OPENREADING_API_KEY_SCOPES`
(`token=backend1|backend2`, comma-separated) narrows one token to an allow-list
of backends, and a token absent from it reaches every backend. Mint one with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`. All three are
environment only and are read once at startup, so rotating a token means
restarting the server.

Startup and readiness. The listening socket is claimed BEFORE uvicorn is handed
control, so a port conflict is one `[serve] cannot bind ...` line and exit 3
with nothing served, and every uvicorn startup line that follows is true when
it prints. (Left to uvicorn, the order is lifespan-then-bind: `INFO:
Application startup complete.` is logged before the port is claimed, so a
readiness gate grepping the log passed a server that was about to die of a
conflict.) The one line this CLI prints is `[serve] listening on
http://HOST:PORT. Readiness: GET /healthz`, after the bind, naming the port the
kernel actually gave (`--port 0` resolves to a real one). Do not gate on any
log line: poll `GET /healthz` until it answers 200. Logging otherwise is
uvicorn's own -- access logs on stdout, lifecycle on stderr, no request id, no
level knob.

Signals. SIGTERM is uvicorn's while the server runs: it drains in-flight
requests, logs the shutdown, and the process exits 143. `serve` is the one
command excluded from this CLI's own SIGTERM handling, which would otherwise
fire after that clean shutdown.

Exit codes
----------
  0  success.
  1  unexpected error (printed as `[tag] error: <Type>: <message>`); a batch in
     which nothing succeeded.
  2  usage: unknown `--backend` (argparse) or `--strategy` on a single
     document; `parse` with an unresolvable source, more files than
     `--max-items`, or more `--jobs` than `--max-jobs`; `compare` misuse (<2
     subjects, unknown fan-out backend, `--format diff` with != 2 subjects,
     mixed subject kinds); `leaderboard` misuse (<2 backends, unknown id);
     `benchmark` profile (unknown, or cataloged where a runnable one is
     required), target, preset, `--jobs`, terms, package, or preparation
     errors; `replay` with no strategy name anywhere; `help` with an unknown
     topic.
  3  cannot run: missing credentials (names the exact vars + signup URL),
     `auth_rejected`, `unsupported_format` on a named single-document parse,
     `unsupported_feature`, an unreadable `--config` / document / `--trace` /
     `explain` argument, a `policy:` block that is not a policy (an unknown
     key, a non-object block, or a value of the wrong type), a
     `ScopeRefused` refusal (from
     `parse`, `strategy plan`, `replay`, `calibrate`, `compare`, `leaderboard`;
     under `benchmark` only when it is raised outside the publisher's own
     per-document boundary, which otherwise records the refusal as a failed
     case at exit 1), a plan-exhausted `route --run`, `serve` without its extra
     or with a malformed `OPENREADING_API_KEYS` / `OPENREADING_API_KEY_SCOPES`
     (one `[serve] ...` line naming the bad entry's position, never its value),
     an unresolvable/empty `leaderboard` dataset, a `rules` dataset with no
     `<case>/case.json` or an unparseable `case.json`, `resume` refusal /
     unknown run / expired payloads, an `OPENREADING_LEDGER` pointing at a path
     this process cannot journal to (`ledger_unavailable`; an armed ledger is a
     hard dependency, so the run fails rather than parsing unjournalled), an
     unknown `backends --check` slug, a `serve` port already bound (one
     `[serve] cannot bind ...` line), or a `RetryableError` reaching a
     directly-named backend on `parse` / `compare` (rate-limit exhaustion, or a
     poll job past its deadline /
     `openreading.router.driver.MAX_CONSECUTIVE_FAULTS` -- a named backend has
     no next rung to fall back to); `help` under `python -OO`, which discarded
     the manual's docstrings.
  4  `route`: no registered backend permitted by policy (the empty plan is
     printed as JSON); batch `parse`: partial -- some items failed.
  5  `compare`: inputs are not schema-valid responses, or `--from` on a run
     that kept no candidates.
  6  interrupted, resumable: `parse` was interrupted while `OPENREADING_LEDGER`
     was armed -- Ctrl-C or SIGTERM, which the CLI turns into the same
     interrupt so a supervisor's stop signal parks a run the way an interactive
     one does. The run did not fail; it parked mid-walk. A single-document run
     names its own `RUN_ID` for `openreading resume`; a batch names none
     (batch-level resume is out of scope). Only a strategy dispatch journals,
     and the two paths test that differently. A single document tests what
     actually armed, so an interrupted `--backend` run exits 143 and says
     nothing was resumable, which is right. A batch tests only whether the
     variable is set, so an interrupted `--backend` batch exits 6 and names no
     id, and there is nothing for `resume` to replay. Arm the ledger for the
     strategy runs you mean to resume.
143  terminated by SIGTERM with no ledger armed: nothing was resumable, so one
     `[openreading]` line says so and names `OPENREADING_LEDGER`. Unarmed
     Ctrl-C is unchanged -- it stays an ordinary `KeyboardInterrupt`
     (traceback, 130), byte-for-byte the pre-ledger behaviour.

Signals, and what a stopped run leaves behind
---------------------------------------------
SIGINT and SIGTERM both reach the interrupt path above; SIGKILL cannot be
caught and journals nothing. Only the FIRST stop signal acts, and that first
one claims BOTH signals: once a stop is under way, a further SIGTERM or SIGINT
is dropped, whichever kind started it. The exit code belongs to the signal that
arrived first, so `kill` followed by Ctrl-C is 143 (or 6 when armed) and Ctrl-C
followed by `kill` is 130 (or 6). Two stop signals are in practice one stop
arriving twice -- a forwarding parent such as `uv run` or a container init
shim, a `killpg` that reaches both a wrapper and the process it wraps, or a
responder who runs `kill` and then reaches for Ctrl-C -- and raising a second
interrupt into the shutdown the first one started is what strands the run
mid-teardown at exit 1. A stop that must not wait escalates to SIGKILL, never
to another catchable signal. The one pair not covered is two SIGINTs with no
SIGTERM involved: that is asyncio's own "Ctrl-C twice to force out" escalation,
deliberately left alone, because taking SIGINT over before a run starts would
cost every Ctrl-C the safe cancellation path. A process started with SIGINT
already ignored -- a shell's asynchronous `&` job in a non-interactive shell,
`nohup`, a masking supervisor -- keeps ignoring it, because a parent that
shielded this process said so on purpose and reinstalling a handler over that
shield would break it for everyone downstream. Send SIGTERM to such a process,
or run it in the foreground."""

from __future__ import annotations

from openreading.cli.app import main

__all__ = ["main"]
