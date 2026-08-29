# The command line: script the engine from a shell or CI

<sub>[Docs home](../README.md) · [Routing and keys →](../router/README.md)</sub>

> **In one sentence.** `openreading` prints one JSON envelope on stdout, keeps everything else on
> stderr, and exits with a code your script can branch on.

## What this gives you

You want to call the parser from a shell script or a CI job and trust what comes back. The worry is
that a progress line or a library warning lands in the JSON you redirect to a file. A backend is one
parser, such as the local `pymupdf` library or a hosted API whose key you keep in the environment.
Every `openreading` verb prints exactly one JSON document on stdout and sends every other line to
stderr. That document is the envelope, the one response shape every backend returns, so `> out.json`
is always safe. The exit code tells your script what happened without reading the output. For
example, `3` means a missing key or a compliance refusal. You need the install from the root
README, and the first command below builds `sample.pdf` for you. `uv run openreading --help` lists
every verb, and this page is about scripting around them.

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
uv run openreading parse sample.pdf --backend pymupdf > out.json 2> err.txt; echo "exit=$?"; cat err.txt
```

```text
exit=0
Consider using the pymupdf_layout package for a greatly improved page layout analysis.
```

## Mental model

Every run produces three streams, stdout, stderr, and the exit code, and each can be read alone.
`stdout` carries the envelope and nothing else, so a redirect captures exactly one JSON document.
`stderr` carries progress, backend chatter, and every error line tagged with a label such as
`[preflight]`. The `pymupdf_layout` line in the output above is the library talking, and it never
reaches stdout. That holds on every verb, `resume` included, so a recovery script may pipe straight
into `jq`. A strategy is a named plan in `openreading.yaml` over one or more backends, and it
has [its own guide](../strategies/README.md). `parse` requires exactly one of `--backend SLUG`,
`--strategy NAME`, or `--no-strategy`, and refuses with exit `2` otherwise. One file or URL prints a
single response, while a directory, a glob, or two or more sources print one batch-result over all
of them.

## Walkthrough

### 1. Prove stdout purity

This step shows that a run which cannot start writes nothing at all to stdout.

```bash
uv run openreading parse sample.pdf --backend reducto 2>/dev/null | wc -c
```

```text
       0
```

**You should see** zero bytes, because a consumer never parses half an envelope. stderr carries
`[reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: …` and the
exit code is 3. The tag is the run label, not the verb.

### 2. Branch on the exit code

Your script can branch on the exit code alone, without reading stdout.

Source: `src/openreading/cli/__init__.py` ("Exit codes", and the "Signals" paragraph under it).
Live truth: `uv run python -m pydoc openreading.cli` → "Exit codes". If this table and that text
disagree, the text is right. Fix the table.

| Code | Means | Whose fault | Safe to retry | Triggered here by |
|---|---|---|---|---|
| `0` | envelope printed | nobody's | nothing to retry | `uv run openreading parse sample.pdf --backend pymupdf` |
| `1` | unexpected error; a batch where nothing succeeded; a document this process may not read; an armed ledger on an unwritable path | yours | no, fix the cause | `mkdir -p bad && printf 'not a pdf' > bad/bad.pdf && uv run openreading parse bad/ --backend pymupdf` |
| `2` | usage: bad selector, unknown backend or strategy, a source path that resolves to no document, over `--max-items` or `--max-jobs`, compare misuse | yours | no, fix the command | `uv run openreading parse sample.pdf` (no selector) |
| `3` | cannot run: missing key, `auth_rejected`, `unsupported_feature`, compliance refusal, unreadable policy or config, a document the backend cannot open, `serve` on a port already bound, a `RetryableError` on a directly named backend | read stderr, both happen | only the `RetryableError` line | `uv run openreading parse sample.pdf --backend reducto` |
| `4` | batch partial (some items failed); `route` with no compliant backend | per item, read `.items[]` | per failed item | `mkdir -p docs && cp sample.pdf docs/ && printf 'not a pdf' > docs/bad.pdf && uv run openreading parse docs/ --backend pymupdf > run.json` |
| `5` | `compare` inputs are not schema-valid responses | yours | no, fix the inputs | `echo '{"hello": 1}' > not-an-envelope.json && uv run openreading compare not-an-envelope.json out.json` |
| `6` | interrupted while `OPENREADING_LEDGER` was set; the run is resumable | whoever stopped it | yes, with `resume` | Ctrl-C or SIGTERM during a `parse` with the ledger armed ([Operations](#operations)) |
| `130` | interrupted by Ctrl-C with no ledger armed; a traceback, not a coded exit | whoever stopped it | yes, from the start | Ctrl-C during a `parse` with `OPENREADING_LEDGER` unset |
| `143` | terminated by SIGTERM with no ledger armed; one line says nothing was resumable | whoever stopped it | yes, from the start | `kill` during a `parse` with `OPENREADING_LEDGER` unset ([Operations](#operations)) |

```bash
uv run openreading parse docs/ --backend pymupdf > run.json 2> run.log
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
[resume] OPENREADING_LEDGER is not set — there is no run to resume from
```

Exit 3 is the one code a script cannot act on by itself. It covers a compliance refusal, which no
amount of retrying will change, and it covers a rate limit that the next hour clears. Nothing
machine-readable separates the two, because a failed single-document run writes zero bytes to
stdout by design. Read the tag and the message on stderr, or move that call to [the HTTP
server](../server/README.md), where the same conditions arrive as distinct statuses carrying an
`error.category` field.

### 3. Tell a wrong file type from a broken document

Exit 3 also covers a document the backend could not open, and the message does not say which kind
you have. A text file and a truncated PDF produce the identical line:

```bash
printf 'hello\n' > notes.txt
uv run openreading parse notes.txt --backend pymupdf; echo "exit=$?"
```

```text
[pymupdf] PyMuPDF failed: Failed to open stream
exit=3
```

The same file inside a folder is not an error at all:

```bash
mkdir -p mixed && cp sample.pdf notes.txt mixed/
uv run openreading parse mixed/ --backend pymupdf 2>/dev/null | jq -c '[.items[] | {relpath: .source.relpath, state, skip_reason}]'; echo "exit=$?"
```

```json
[{"relpath":"notes.txt","state":"skipped","skip_reason":"unsupported_format"},{"relpath":"sample.pdf","state":"succeeded","skip_reason":null}]
exit=0
```

**You should see** the same file refused at exit 3 alone and skipped at exit 0 in a folder. Check
the type before you name a file directly, because the single-document message names neither the
file's format nor the fix. [Backend adapters](../adapters/README.md) lists the formats each backend
reads.

### 4. Pull what you need with jq, then compose verbs

A few `jq` lines pull the fields you need, and a saved envelope feeds the next verb.

```bash
jq -r .document.text out.json | head -1
jq '[.document.pages[].blocks[] | select(.type=="table")] | length' out.json
jq -c '[.warnings[]?.code]' out.json
uv run openreading parse sample.pdf --strategy offline_first 2>/dev/null > strat.json
jq -c '.orchestration.attempts[] | {node, backend, category, cost_usd}' strat.json
uv run openreading parse docs/ --backend tesseract > tess.json 2>/dev/null
uv run openreading compare run.json tess.json --format table 2>/dev/null | head -2
```

```text
OpenReading Test Document
1
["confidence_unavailable"]
{"node":"root.steps[0]","backend":"pymupdf","category":"succeeded","cost_usd":null}
CORPUS COMPARE — run vs tess
1 document(s): 0 equivalent · 0 divergent · 1 mixed · 0 unpaired
```

**You should see** text, one table, one warning code, one attempt per strategy step, and two batch
envelopes compared as a corpus. Blocks live under each page. Use `.warnings[]?` because a backend
with nothing to warn about omits `warnings` entirely (jq reads the missing key as `null`).
`explain report.json` renders a saved comparison.

## Recipes

**Gate a CI job on a parse.**
```bash
uv run openreading parse sample.pdf --backend pymupdf 2>/dev/null | jq -e '.status.state == "succeeded"' > /dev/null && echo "parse ok"
```
Prints `parse ok`. `jq -e` exits 1 on `false`, so the `&&` chain fails the job. Contributors run
`make verify`, the offline gate for this repo.

**Load keys from a file, never from a flag.**
```bash
printf 'REDUCTO_API_KEY=placeholder-not-a-real-key\n' > ci.env
uv run openreading backends --env-file ci.env | grep -E '^BACKEND|^reducto'
```
```text
BACKEND                        TYPE               CONFIGURED  MISSING
reducto                        hosted_api         yes         -
```
Without the flag the line reads `no  REDUCTO_API_KEY`. `./.env` loads by itself, and a file never
overrides a variable already exported. No verb takes a key flag.

**Parse some pages, or a folder with workers.**
```bash
uv run openreading parse sample.pdf --backend pymupdf --pages 1 2>/dev/null | jq -c '{pages: (.document.pages|length), page_count: .document.page_count}'
uv run openreading parse docs/ --backend pymupdf --jobs 64; echo "exit=$?"
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
You get a refusal, never geometry-only output pretending to be an answer. `--deadline 0` is the
same idea for time on a named backend. On `pymupdf` it still prints `succeeded`, because a local
library returns before any wait. On `reducto` with no key it exits 3 on the credential first.

## How it decides

- stdout is the envelope only, because a progress line there would break every `| jq` consumer.
  `openreading.cli.app` enforces this by redirecting stdout during the run.
- A printed envelope is schema-validated first, so a non-conforming document never reaches stdout.
- No flag widens compliance. A policy that leaves nothing to run is exit 3 from every verb that
  executes. Bare `route` prints the empty plan and exits 4 ([Routing and
  keys](../router/README.md)).
- Keys never travel on the command line, so they never land in shell history or `ps`.
- A batch never raises out of one item. The `[i/N]` stderr line carries a failed item's message
  instead.

## Operations

This section is for whoever runs `openreading` unattended and carries the pager. It answers what
the exit-code table cannot: what a stop signal leaves behind, what the engine already retried before
it gave up, which failures never raise a code at all, what a consumer pins to, and what the ledger
costs in disk.

### Stop a run, and know what it left behind

A strategy run with `OPENREADING_LEDGER` set parks a resumable journal when something stops it.
Send a real signal to a running parse and read the code:

```bash
export OPENREADING_LEDGER=./.openreading
uv run python -c 'import fitz; s=fitz.open("sample.pdf"); o=fitz.open(); [o.insert_pdf(s) for _ in range(8)]; o.save("slow.pdf")'
printf 'version: 1\nstrategies:\n  slow:\n    try: [tesseract, pymupdf]\n' > openreading.yaml
uv run openreading parse slow.pdf --strategy slow > parked.json & BG=$!
sleep 2; kill -TERM $BG; wait $BG; echo "exit=$?"
```

```text
[parse] interrupted; run f3e1b27c-50c9-4695-89d0-49632384646d is resumable
[parse] resume with: openreading resume f3e1b27c-50c9-4695-89d0-49632384646d
exit=6
```

**You should see** exit 6, a run id, and an empty `parked.json`. Every row below was produced that
way, two seconds into the same run.

| Signal | Ledger armed | Exit | stdout | stderr | Left in the journal |
|---|---|---|---|---|---|
| `SIGINT` (Ctrl-C) | yes | `6` | 0 bytes | the run id and a `resume with:` line | the in-flight rung recorded `cancelled` |
| `SIGTERM` (`kill`, `systemctl stop`, a pod eviction) | yes | `6` | 0 bytes | the run id and a `resume with:` line | the in-flight rung recorded `cancelled` |
| `SIGINT` | no | `130` | 0 bytes | a `KeyboardInterrupt` traceback | nothing is written |
| `SIGTERM` | no | `143` | 0 bytes | one `[openreading]` line naming `OPENREADING_LEDGER` | nothing is written |
| `SIGKILL` (`kill -9`, the OOM killer) | either | `137` | 0 bytes | 0 bytes | an `attempted` record with no terminal line, when armed |

A second SIGTERM while the first is still shutting down is ignored, not queued: the same stop
signal often arrives twice (a supervisor forwarding it, or `uv run` relaying it to this process),
and reacting to each one separately can corrupt the shutdown in progress. Escalate with SIGKILL if
the process has not exited.

SIGKILL cannot be caught, so nothing prints and no rung is closed. That run is still resumable, and
no verb lists runs, so recovery means reading `$OPENREADING_LEDGER/*.header.json` and choosing by
modification time. Resuming it dispatches the rung that was in flight a second time, where a
`cancelled` rung would have replayed from the journal for free. On a hosted backend that is a
second billed call.

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
dispatches natively. It has no effect on `auto` and none on `--strategy`, which take their budget
from the strategy's own `budget.max_duration` or `limits.max_duration_per_doc`. Bound a strategy
run there rather than on the command line.

You still own the layer above. Nothing resumes a run that already ended, nothing caps what a run
spends, and a retry you add multiplies vendor calls the engine already made once. Choose your outer
retry count with that multiplier in mind.

Source: `src/openreading/credentials.py` (`DEFAULT_DEADLINE_MS`,
`DEFAULT_NATIVE_BATCH_DEADLINE_MS`) and `src/openreading/router/driver.py` (`backoff_ms`).
Live truth: `uv run openreading parse --help`.

### Failures that exit 0

An exit code catches a run that could not start or could not finish. Three failures end at exit 0
with an answer you may not want, so a script reading only the code will not see them.

- **A fallback answered instead.** `warnings[]` carries `fallback_used` while
  `orchestration.outcome` stays `ok`. A local binary missing from `PATH` reaches the trace as
  `error(provider_error)`, which reads as a transient vendor blip rather than the permanent host
  fault it is, so an OCR job can quietly return text-layer output for a scan. Fail the job on the
  warning with `jq -e '[.warnings[]?.code] | index("fallback_used") == null' out.json`, and run
  `uv run openreading backends --check tesseract` beforehand to catch it earlier.
- **A degraded answer.** A strategy that ends below its quality gate, or that runs out of its
  `max_time`, sets `orchestration.outcome` to `degraded` and still exits 0. The warning says which
  happened: `budget_exhausted` for the deadline, `quality_below_threshold` for the gate. Branch on
  `orchestration.outcome` rather than the code, because a deadline wants a longer budget or a
  faster backend while a quality miss wants a stronger one.
- **An unreadable file inside a folder.** The item is `skipped` with
  `skip_reason: unsupported_format` and the batch still exits 0, as step 3 shows. Count
  `summary.skipped` rather than trusting the code.

Two more belonged on this list and no longer do. A compliance policy the router cannot read is a
refusal rather than an empty filter ([Routing and keys](../router/README.md)), and the three
strategy keys that validated green while enforcing nothing are refused by `strategy validate`
([Strategies](../strategies/README.md)).

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

Test a candidate by checking it out and running `make verify`, the offline gate this repository's
own CI runs. Roll back by pinning the previous SHA, because nothing here migrates state between
versions. CLI flags, the Python API and the strategy grammar may still change before 1.0, so read
[`CHANGELOG.md`](../../../CHANGELOG.md) between two SHAs before you move.

### Where the disk goes

An armed ledger writes the document and the response for every strategy run. Three runs of
`offline_first` over the 8.7 KB sample left 87 KB behind, about 29 KB a run. Size the volume from
your own document and response sizes. An armed ledger that cannot write stops the parse at exit 1
rather than continuing unjournalled, which [The run ledger](../ledger/README.md) demonstrates on an
unwritable directory, so treat a full volume as an outage. Behaviour on a volume that fills
mid-run was not measured.

## Reference

- `uv run openreading --help`, then `uv run openreading <verb> --help` for every flag.
- `uv run python -m pydoc openreading.cli` → "Invariants shared by every subcommand" and
  "Exit codes".
- `uv run python -m pydoc openreading.credentials` → env-file precedence, per-backend variables.
- What stdout carries: [JSON Schemas](../schemas/README.md).

## Not built yet

- `parse --policy` does not exist. `route`, `strategy plan`, `replay`, `calibrate`, and
  `leaderboard` take a policy, and `parse` does not (`openreading.cli` docstring, "Batch: a
  directory, a glob, or two or more sources": "`parse` has no `--policy` flag").
- Batch-level resume does not exist, so an interrupted batch names no run id (`openreading.cli`
  docstring, "Exit codes", 6: "a batch names none (batch-level resume is out of scope)").
- There is no coded exit for a non-conforming single-document response (`openreading.cli`
  docstring, "Invariants shared by every subcommand": "surfaces as an uncaught traceback, not a
  coded exit").

## See also

- [Docs home](../README.md)
- [The HTTP server](../server/README.md) shows the same verbs as endpoints and status codes.
- [Routing and keys](../router/README.md) explains why a backend was dropped and how to bring a
  key.
- [The run ledger](../ledger/README.md) covers exit 6 and `resume`.
- [Backend adapters](../adapters/README.md) lists every slug and the variable it needs.

<sub>[Docs home](../README.md) · [Routing and keys →](../router/README.md)</sub>
