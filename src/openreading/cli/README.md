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
reaches stdout. A strategy is a named plan in `openreading.yaml` over one or more backends, and it
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

Source: `src/openreading/cli/__init__.py` ("Exit codes", lines 372–398). Live truth: `uv run python
-m pydoc openreading.cli` → "Exit codes". If this table and that text disagree, the text is right.
Fix the table.

| Code | Means | Triggered here by |
|---|---|---|
| `0` | envelope printed | `uv run openreading parse sample.pdf --backend pymupdf` |
| `1` | unexpected error; a batch where nothing succeeded | `mkdir -p bad && printf 'not a pdf' > bad/bad.pdf && uv run openreading parse bad/ --backend pymupdf` |
| `2` | usage: bad selector, unknown backend or strategy, over `--max-items` or `--max-jobs`, compare misuse | `uv run openreading parse sample.pdf` (no selector) |
| `3` | cannot run: missing key, `auth_rejected`, `unsupported_feature`, compliance refusal, unreadable policy or config | `uv run openreading parse sample.pdf --backend reducto` |
| `4` | batch partial (some items failed); `route` with no compliant backend | `mkdir -p docs && cp sample.pdf docs/ && printf 'not a pdf' > docs/bad.pdf && uv run openreading parse docs/ --backend pymupdf > run.json` |
| `5` | `compare` inputs are not schema-valid responses | `echo '{"hello": 1}' > not-an-envelope.json && uv run openreading compare not-an-envelope.json out.json` |
| `6` | interrupted while `OPENREADING_LEDGER` was set; the run is resumable | Ctrl-C during a `parse` with the ledger armed (shape only, not triggered here) |

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
`uv run openreading resume r_01J8QK` takes only that id ([The run ledger](../ledger/README.md)).
Without the ledger, `resume` exits 3 and prints this line on stderr:

```text
[resume] OPENREADING_LEDGER is not set — there is no run to resume from
```

### 3. Pull what you need with jq, then compose verbs

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
