# The command line: script OpenReading from a shell or CI

<sub>[Docs home](../README.md) · [Routing and keys →](../router/README.md)</sub>

> **In one sentence.** `openreading` prints one JSON envelope on stdout, keeps everything else on
> stderr, and exits with a code your script can branch on.

This page is the walkthrough. For a flag or a rule while you work, the CLI answers for itself:
`openreading help` lists the manual's chapters, `openreading help <topic>` prints one, and
`openreading <cmd> --help` gives one command's examples and exit codes.

## What this gives you

You want to call the parser from a shell script or a CI job and trust what comes back. The worry is
that a progress line or a library warning lands in the JSON you redirect to a file. A backend is one
parser, such as the local `pymupdf` library or a hosted API whose key you keep in the environment.

Every verb that returns a result prints exactly one JSON document on stdout and sends every other
line to stderr. A verb is one subcommand, such as `parse`. That document is the envelope, the one
response shape every backend returns. `> out.json` is therefore safe on `parse`, `route`, `resume`,
`replay`, and `compare` in its default JSON format. The reporting verbs are the exception, because
their output is for you rather than for a parser. `backends`, `explain`, `strategy show`,
`leaderboard`, and any `--format table` print a human table on stdout. The recipes below pipe those
into `grep` and `head`. The exit code tells your script what happened without reading the output.
For example, `3` means a missing key, invalid configuration, or replay refusal.

You need the install from the root README, and the first command below builds `sample.pdf` for you.
`uv run openreading --help` lists every verb, and this page is about scripting around them.

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
uv run openreading parse sample.pdf --backend pymupdf > out.json 2> err.txt; echo "exit=$?"; cat err.txt
```

```text
warning: The `fitz` API is deprecated and will be removed in future. Use `import pymupdf` instead.
exit=0
Consider using the pymupdf_layout package for a greatly improved page layout analysis.
```

## Mental model

Every run produces three outputs (stdout, stderr, and the exit code), and each can be read alone.
`stdout` carries the result and nothing else, so a redirect from a result verb captures exactly one
JSON document.
`stderr` carries progress, backend chatter, and every error line tagged with a label such as
`[preflight]`. Both library lines in the output above are PyMuPDF talking. The first comes from
the sample-building one-liner rather than from a verb. The second reaches stderr during the parse,
never stdout. That holds on every verb, `resume` included, so a recovery script may pipe straight
into `jq`. A strategy is a named plan over one or more backends, and it has
[its own guide](../strategies/README.md). Four presets ship inside the package: `cost_saver`,
`fast`, `max_accuracy`, and `offline_first`. Your own strategies load from `openreading.yaml`.
`parse` requires exactly one of `--backend SLUG`, `--strategy NAME`, or `--no-strategy`, and refuses
with exit `2` otherwise. `--no-strategy` walks `policy.backends` in written order, the same chain an
unnamed request takes. One file or URL prints a single response, while a directory, a glob, or
two or more sources print one batch-result over all of them.

## Walkthrough

### 1. Prove stdout purity

This step shows that a run which cannot start writes nothing at all to stdout.

```bash
uv run openreading parse sample.pdf --backend reducto 2>/dev/null | wc -c
uv run openreading parse sample.pdf --backend reducto; echo "exit=$?"
```

```text
       0
[reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
exit=3
```

**You should see** zero bytes, because a consumer never parses half an envelope. stderr carries
`[reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: …` and the
exit code is 3. The bracketed tag names what printed the line. A run error carries the backend or
strategy label. A usage error and an interrupt carry `[parse]`. The batch layer carries `[batch]` or
`[preflight]`, and progress carries `[i/N]`.

### 2. Branch on the exit code

Your script can branch on the exit code alone, without reading stdout.

Source: `src/openreading/cli/__init__.py` ("Exit codes", and the "Signals" paragraph under it).
Live truth: `uv run openreading help exit-codes`. This table adds the two columns a script
needs, whose fault a code is and whether retrying helps, plus a command that produces each one.
If the table and that chapter disagree, the chapter is right. Fix the table.

| Code | Means | Whose fault | Safe to retry | Triggered here by |
|---|---|---|---|---|
| `0` | envelope printed | nobody's | nothing to retry | `uv run openreading parse sample.pdf --backend pymupdf` |
| `1` | an unexpected error, or a batch where nothing succeeded | yours | no, fix the cause | `mkdir -p bad && printf 'not a pdf' > bad/bad.pdf && uv run openreading parse bad/ --backend pymupdf` |
| `2` | usage: bad selector, unknown backend or strategy, a source path that resolves to no document, over `--max-items` or `--max-jobs`, compare misuse, `leaderboard` misuse, `replay` with no strategy name | yours | no, fix the command | `uv run openreading parse sample.pdf` (no selector) |
| `3` | cannot run: missing key, `auth_rejected`, `unsupported_feature`, unreadable config, a document the backend cannot open, an armed ledger on an unwritable path, `serve` on a port already bound, a `RetryableError` on a directly named backend | read stderr, both happen | only the `RetryableError` and ledger lines | `uv run openreading parse sample.pdf --backend reducto` |
| `4` | batch partial, or `route` with no registered backend permitted by policy | per item, read `.items[]` | per failed item | `mkdir -p corpus && cp sample.pdf corpus/ && printf 'not a pdf' > corpus/bad.pdf && uv run openreading parse corpus/ --backend pymupdf > run.json` |
| `5` | `compare` inputs are not schema-valid responses | yours | no, fix the inputs | `echo '{"hello": 1}' > not-an-envelope.json && uv run openreading compare not-an-envelope.json out.json` |
| `6` | interrupted while `OPENREADING_LEDGER` was set. The run is resumable | whoever stopped it | yes, with `resume` | Ctrl-C or SIGTERM during a `parse` with the ledger armed ([Operations](#operations)) |
| `130` | interrupted by Ctrl-C with no ledger armed. A traceback, not a coded exit | whoever stopped it | yes, from the start | Ctrl-C during a `parse` with `OPENREADING_LEDGER` unset |
| `143` | terminated by SIGTERM with no ledger armed. One line says nothing was resumable | whoever stopped it | yes, from the start | `kill` during a `parse` with `OPENREADING_LEDGER` unset ([Operations](#operations)) |

```bash
uv run openreading parse corpus/ --backend pymupdf > run.json 2> run.log
case $? in
  0) echo "all succeeded" ;;
  4) echo "partial"; jq -c '.items[] | select(.state=="failed") | {relpath: .source.relpath, code: .error.code}' run.json ;;
  *) echo "cannot run or unexpected"; cat run.log ;;
esac
```

```text
partial
{"relpath":"bad.pdf","code":"FileDataError"}
```

**You should see** `partial` and the failed item. Exit 6 prints a run id and a `resume with:` line.
A run id is a UUIDv4, 36 characters, for example `dcb81857-018f-4e30-8e12-e91d915a1d64`.
`uv run openreading resume dcb81857-018f-4e30-8e12-e91d915a1d64` takes only that id
([The run ledger](../ledger/README.md)).
Without the ledger, `resume` exits 3 and prints this line on stderr:

```text
[resume] OPENREADING_LEDGER is not set, so there is no run to resume from
```

Exit 3 is the one code a script cannot act on by itself. It covers configuration refusal, which no
amount of retrying will change, and it covers a rate limit that the next hour clears. Nothing
machine-readable separates the two, because a failed single-document run writes zero bytes to
stdout by design. Read the tag and the message on stderr, or move that call to [the HTTP
server](../server/README.md). There the same conditions arrive as distinct statuses carrying an
`error.category` field.

### 3. Tell a wrong file type from a broken document

Exit 3 also covers a document the backend could not open, and the message names the format when it
can. A file whose extension this backend does not read is refused by name:

```bash
printf 'hello\n' > notes.txt
uv run openreading parse notes.txt --backend pymupdf; echo "exit=$?"
```

```text
[pymupdf] pymupdf cannot read notes.txt. It reads pdf, xps, epub, mobi, cbz, svg, and this file is not one of them.
exit=3
```

A file with a supported extension whose bytes are corrupt reads `PyMuPDF failed: Failed to open
stream`. The backend refuses either way, first-hand, and says which.

The same file inside a folder does not stop the run:

```bash
mkdir -p mixed && cp sample.pdf notes.txt mixed/
uv run openreading parse mixed/ --backend pymupdf 2>/dev/null | jq -c '[.items[] | {relpath: .source.relpath, state, code: .error.code}]'; echo "exit=$?"
```

```json
[{"relpath":"notes.txt","state":"failed","code":"unsupported_format"},{"relpath":"sample.pdf","state":"succeeded","code":null}]
exit=4
```

**You should see** the same file refused at exit 3 on its own, and a failed item at exit 4 inside a
folder. Every source is dispatched, so the count you get back accounts for every file you named.
[Backend adapters](../adapters/README.md) lists the formats each backend reads.

### 4. Pull what you need with jq, then compose verbs

A few `jq` lines pull the fields you need, and a saved envelope feeds the next verb.

```bash
jq -r .document.text out.json | head -1
jq '[.document.pages[].blocks[] | select(.type=="table")] | length' out.json
jq -c '[.warnings[]?.code]' out.json
uv run openreading parse sample.pdf --strategy offline_first 2>/dev/null > strat.json
jq -c '.orchestration.attempts[] | {node, backend, category}' strat.json
uv run openreading parse corpus/ --backend tesseract > tess.json 2>/dev/null
uv run openreading compare run.json tess.json --format table 2>/dev/null | head -2
```

```text
OpenReading Test Document
1
["confidence_unavailable"]
{"node":"root.steps[0]","backend":"pymupdf","category":"succeeded"}
CORPUS COMPARE: run vs tess
1 document(s): 0 equivalent · 0 divergent · 1 mixed · 0 unpaired
```

**You should see** text, one table, one warning code, one attempt per strategy step, and two batch
envelopes compared as a corpus. Blocks live under each page. Use `.warnings[]?` because a backend
with nothing to warn about omits `warnings` entirely (jq reads the missing key as `null`).
`explain` also renders a saved comparison of two single-document responses. Save one with
`uv run openreading compare out.json strat.json > report.json`, then run
`uv run openreading explain report.json`. A corpus report over two batch runs, like the one above,
is not one it reads.

## Recipes

**Gate a CI job on a parse.**
```bash
uv run openreading parse sample.pdf --backend pymupdf 2>/dev/null | jq -e '.status.state == "succeeded"' > /dev/null && echo "parse ok"
```
Prints `parse ok`. `jq -e` exits 1 on `false`, so the `&&` chain fails the job. Test the
orchestration outcome too on a `--strategy` run. A run where every rung gated still reports
`succeeded` and exits `0` ([Failures that exit 0](#failures-that-exit-0)):

```bash
uv run openreading parse sample.pdf --strategy offline_first 2>/dev/null | jq -e '.status.state == "succeeded" and (.orchestration.outcome // "ok") == "ok"' > /dev/null && echo "parse ok"
```

**Load keys from a file, never from a flag.**
```bash
printf 'REDUCTO_API_KEY=placeholder-not-a-real-key\n' > ci.env
uv run openreading backends --env-file ci.env | grep -E '^BACKEND|^reducto'
```
```text
BACKEND                        TYPE               CONFIGURED  MISSING
reducto                        hosted_api         yes         REDUCTO_WEBHOOK_SECRET
```
The MISSING column lists optional variables too, which is why `REDUCTO_WEBHOOK_SECRET` stays there
once the required key resolves. Without the flag the line reads `no  REDUCTO_API_KEY`. `./.env`
loads by itself, and a file never overrides a variable already exported. No verb takes a key flag.

**Parse some pages, or a folder with workers.**
```bash
uv run openreading parse sample.pdf --backend pymupdf --pages 1 2>/dev/null | jq -c '{pages: (.document.pages|length), page_count: .document.page_count}'
uv run openreading parse corpus/ --backend pymupdf --jobs 64; echo "exit=$?"
```
```text
{"pages":1,"page_count":2}
[batch] jobs=64 is over the max-jobs limit (32); raise it with --max-jobs / max_jobs= if this is intended
exit=2
```
`--jobs 4` runs four workers and records `request.jobs: 4` in the batch-result. The default is one
worker, run serially.

**Ask for extraction from a backend that cannot do it.**
```bash
uv run openreading parse sample.pdf --backend pymupdf --extract "totals and dates"; echo "exit=$?"
```
```text
[pymupdf] unsupported feature (custom_schema_extraction): pymupdf cannot perform schema-driven field extraction; route to an extraction-capable backend (e.g. google-document-ai, reducto)
exit=3
```
You get a refusal, never geometry-only output pretending to be an answer.

## How it decides

- stdout carries the result only, because a progress line there would break every `| jq` consumer.
  `openreading.cli.app` enforces this by redirecting stdout during the run.
- A printed envelope is schema-validated first, so a non-conforming document never reaches stdout.
- No flag widens the eligible set. The `policy:` block of your `openreading.yaml` sets it, and
  three of its keys widen it deliberately. A policy that leaves nothing to run is exit 3 from every verb that
  executes. Bare `route` prints the empty plan and exits 4 ([Routing and
  keys](../router/README.md)).
- Keys never travel on the command line, so they never land in shell history or `ps`.
- A batch never raises out of one item. The `[i/N]` stderr line carries a failed item's message
  instead.

## Operations

This section is for whoever runs `openreading` unattended and carries the pager. It answers what
the exit-code table cannot:

- what a stop signal leaves behind
- what the engine already retried before it gave up
- which failures never raise a code at all
- what a consumer pins to
- what the ledger costs in disk

### Stop a run, and know what it left behind

A strategy run with `OPENREADING_LEDGER` set parks a resumable journal, the on-disk record of its
steps that `resume` reads, when something stops it.
Send a real signal to a running parse and read the code:

```bash
export OPENREADING_LEDGER=./.openreading
uv run python -c 'import pymupdf; s=pymupdf.open("sample.pdf"); o=pymupdf.open(); [o.insert_pdf(s) for _ in range(8)]; o.save("slow.pdf")'
printf 'version: 1\nstrategies:\n  slow:\n    try: [tesseract, pymupdf]\n' > openreading.yaml
uv run openreading parse slow.pdf --strategy slow > parked.json & BG=$!
sleep 2; kill -TERM $BG; wait $BG; echo "exit=$?"
```

```text
warning: The `fitz` API is deprecated and will be removed in future. Use `import pymupdf` instead.
[parse] interrupted; run f3e1b27c-50c9-4695-89d0-49632384646d is resumable
[parse] resume with: openreading resume f3e1b27c-50c9-4695-89d0-49632384646d
exit=6
```

**You should see** exit 6, a run id, and an empty `parked.json`. Every row below was produced that
way, two seconds into the same run. If you see `exit=0` and a non-empty `parked.json`, the run
finished before the signal arrived. Rebuild `slow.pdf` with `range(32)` in place of `range(8)` and
run the block again. A rung is one step of the strategy, here the `tesseract` attempt.

| Signal | Ledger armed | Exit | stdout | stderr | Left in the journal |
|---|---|---|---|---|---|
| `SIGINT` (Ctrl-C) | yes | `6` | 0 bytes | the run id and a `resume with:` line | the in-flight rung recorded `cancelled` |
| `SIGTERM` (`kill`, `systemctl stop`, a pod eviction) | yes | `6` | 0 bytes | the run id and a `resume with:` line | the in-flight rung recorded `cancelled` |
| `SIGINT` | no | `130` | 0 bytes | a `KeyboardInterrupt` traceback | nothing is written |
| `SIGTERM` | no | `143` | 0 bytes | one `[openreading]` line naming `OPENREADING_LEDGER` | nothing is written |
| `SIGKILL` (`kill -9`, the OOM killer) | either | `137` | 0 bytes | a PyMuPDF library line | an `attempted` record with no terminal line, when armed |

Only the first stop signal acts, and it claims both kinds. Once a stop is under way, a further
SIGTERM or SIGINT is dropped, whichever kind it is. The same stop often arrives twice, from a
supervisor forwarding it or from `uv run` relaying it to this process. Reacting to the second one
separately can corrupt the shutdown already in progress. The exit code names whichever signal
started the stop. `kill` then Ctrl-C ends at `143`, and Ctrl-C then `kill` ends at `130`. With the
ledger armed, both end at `6`. Escalate with SIGKILL, never with another catchable signal. Two
SIGINTs with no SIGTERM between them are the one exception. Python's own "Ctrl-C twice to force out"
behaviour handles them. Taking SIGINT over before a run starts would cost every plain Ctrl-C its
safe shutdown path.

SIGKILL cannot be caught, so nothing prints and no rung is closed. That run is still resumable, and
no verb lists runs, so recovery means reading `$OPENREADING_LEDGER/*.header.json` and choosing by
modification time. Resuming it dispatches the rung that was in flight a second time. A
`cancelled` rung is not replayed either. The resumed run records that backend as skipped and moves
to the next entry in `try`. The answer can then come from a different backend than an uninterrupted
run would have used. On a hosted backend that is a second call on your key.

A batch takes the same signal paths and gives you less to work with. It exits 6 and names no run
id, while its per-item runs under the ledger root may still be individually resumable. [Batch
runs](../batch/README.md) covers what a batch does not save.

### Time budgets, and what the engine retries for you

One document gets 120 seconds, and a natively dispatched batch gets one hour. Those two numbers are
the whole timeout policy. Inside that budget the router retries a backend that returned a retryable
error, waiting 500 milliseconds and doubling to a 30 second ceiling. A backend that names its own
`retry_after` wins whenever its value is larger. The bound is the deadline rather than an attempt
count, so a slow vendor spends the budget instead of a fixed number of tries.

`--deadline SECONDS` moves that budget for a directly named `--backend`, and for a backend a batch
dispatches natively. It has no effect on `--no-strategy` and none on `--strategy`, which take their budget
from the strategy's own `budget.max_duration` or `limits.max_duration_per_doc`. Bound a strategy
run there rather than on the command line.

`--deadline 0` means fail fast, so a backend that would have to wait refuses instead. On `pymupdf`
it still prints `succeeded`, because a local library returns before any wait. On `reducto` with no
key it exits 3 on the credential first.

You still own the layer above. Nothing resumes a run that already ended, and nothing caps what a
run spends. A retry you add multiplies vendor calls the engine already made once, so choose your
outer retry count with that multiplier in mind.

Source: `src/openreading/credentials.py` (`DEFAULT_DEADLINE_MS`,
`DEFAULT_NATIVE_BATCH_DEADLINE_MS`) and `src/openreading/router/driver.py` (`backoff_ms`).
Live truth: `uv run openreading parse --help`.

### Failures that exit 0

An exit code catches a run that could not start or could not finish. Three failures end at exit 0
with an answer you may not want, so a script reading only the code will not see them.

- **A fallback answered instead.** `warnings[]` carries `fallback_used` while
  `orchestration.outcome` stays `ok`. A local binary missing from `PATH` reaches the trace as
  `error(provider_error)`. That reads as a transient vendor blip rather than the permanent host
  fault it is. An OCR job can quietly return text-layer output for a scan. Fail the job on the
  warning with `jq -e '[.warnings[]?.code] | index("fallback_used") == null' out.json`. To catch it
  before the run, gate on the `backends` table, because `backends --check` prints its verdict and
  always exits `0`:

  ```bash
  uv run openreading backends 2>/dev/null | awk '$1=="tesseract" && $3=="yes" {ok=1} END {exit !ok}'
  ```

- **A degraded answer.** A gate is a threshold a strategy sets for a result it will accept. A
  strategy that ends below its quality gate, or that runs out of its time budget, sets
  `orchestration.outcome` to `degraded` and still exits 0. That budget is `max_time` in the Plain
  spelling and `budget.max_duration` in the full grammar. The warning says which happened:
  `budget_exhausted` for the deadline, `quality_below_threshold` for the gate. Branch on
  `orchestration.outcome` rather than the code. A deadline wants a longer budget or a faster
  backend, while a quality miss wants a stronger one.
- **An unreadable file inside a folder.** The item is `failed`, carrying the backend's own
  `unsupported_format` reason, and the batch exits 4 as partial, as step 3 shows. Count
  `summary.failed` rather than trusting the code.

### Pin a version, and go back

Nothing is published to PyPI and no commit is tagged, so a consumer pins a commit SHA of this
repository and nothing else. Record that SHA next to whatever you deploy:

```bash
uv run openreading --version
```

```text
openreading 0.3.0
```

**You should see** the package version, which many commits share, and not the commit you are
running. `GET /healthz` reports the same number over HTTP, so neither surface answers "which build
is this" on its own.

Test a candidate by checking it out and running `make verify`, the offline check this repository's
own CI runs. Roll back by pinning the previous SHA, because nothing here migrates state between
versions. CLI flags, the Python API and the strategy grammar may still change before 1.0, so read
[`CHANGELOG.md`](../../../CHANGELOG.md) between two SHAs before you move.

### Where the disk goes

An armed ledger writes the document and the response for every strategy run. Three runs of
`offline_first` over the 8.7 KB sample left 87 KB behind, about 29 KB a run. Size the volume from
your own document and response sizes. An armed ledger that cannot write stops the parse at exit 3
rather than continuing unjournalled, so treat a full volume as an outage. [The run
ledger](../ledger/README.md) demonstrates this on an unwritable directory. Behaviour on a volume
that fills mid-run was not measured.

## Reference

- `uv run openreading help` lists the manual's chapters; `uv run openreading help <topic>` prints
  one. Start with `quickstart`, `batch` and `chaining`.
- `uv run openreading help datasets` shows the case inputs and expectations used for calibration
  and scoring. The `calibrate` chapter provides a complete cascade and shows where proposed gates go.
- `uv run openreading --help` for the task map, then `uv run openreading <verb> --help` for one
  command's flags, examples and exit codes.
- `uv run python -m pydoc openreading.cli` for the whole reference in source order, which is what
  to grep when you do not know the topic's name.
- `uv run python -m pydoc openreading.credentials` → env-file precedence, per-backend variables.
- What stdout carries: [JSON Schemas](../schemas/README.md).

## Not built yet

- Batch-level resume does not exist, so an interrupted batch names no run id. Source: the
  `openreading.cli` docstring, "Exit codes", 6: "a batch names none (batch-level resume is out of
  scope)".
- There is no coded exit for a non-conforming single-document response. Source: the
  `openreading.cli` docstring, "What lands on stdout, on stderr, and in the exit code", which
  explains why response validation can surface as an uncaught traceback.

Four inconsistencies between verbs are known and unfixed. Each is safe once you know it, and
each would be a breaking change to correct, so read the flag's own `--help` rather than assuming
a sibling's rule carries over.

- `--format` names different value sets. `compare` takes `json|table|diff|diffs|md`, `leaderboard`
  takes `table|json`, and `benchmark report` takes `text|json`, so `benchmark report
  --format table` exits 2.
- `--jobs 0` clamps to 1 on `parse` and exits 2 on `benchmark run`. `parse` also has a ceiling
  (`--max-jobs`) and `benchmark run` does not.
- An unknown backend id exits 2 from `compare` and `leaderboard`, and 3 from `backends --check`.
- On `strategy` and `benchmark`, `--env-file` belongs before the sub-verb, and passing it after
  prints the top-level usage line, which names neither the flag nor the sub-verbs.

## See also

- [Docs home](../README.md)
- [The HTTP server](../server/README.md) shows the same verbs as endpoints and status codes.
- [Routing and keys](../router/README.md) explains why a backend was dropped and how to bring a
  key.
- [The run ledger](../ledger/README.md) covers exit 6 and `resume`.
- [Backend adapters](../adapters/README.md) lists every slug and the variable it needs.

<sub>[Docs home](../README.md) · [Routing and keys →](../router/README.md)</sub>
