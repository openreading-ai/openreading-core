# Batch runs: a folder in, one JSON out

<sub>[Docs home](../README.md) · [← Compare](../comparison/README.md) · [The run ledger →](../ledger/README.md)</sub>

> **In one sentence.** Point `parse` at a folder, a glob, or several files and get one
> `batch-result` JSON holding a full response per document.

## What this gives you

You have a folder of two hundred documents and one parser that works well on a single file. Running
it in a shell loop means two hundred output files, a crash halfway that stops everything, and no
summary at the end. A backend is one parser, such as the local `pymupdf` library or a hosted API.
`openreading parse corpus/ --backend pymupdf > batch.json` runs the whole folder in one invocation
and prints one JSON document. Each document runs the ordinary single-document pipeline on its own,
so one failure never stops the rest. Every source is dispatched: a file the backend cannot read
comes back as a FAILED item carrying that backend's own reason, such as `unsupported_format`,
rather than being filtered out before it was ever offered. The result is one envelope, meaning one JSON document with a fixed shape, and
this one is called `batch-result`. It carries a complete `response` per succeeded item, a summary
with counts, and warnings. Run the same folder with a second backend and `compare` the two
envelopes to see, document by document, where the two backends disagree. Deciding which one is
right needs documents you labeled yourself, which is what [Evals](../evals/README.md) scores. You
need `sample.pdf` from the root README, a folder to copy it into, and no key for the local
backends.

## Mental model

The batch layer wraps the single-document path and never changes what that path does. Intake is the
first step, and it expands your sources into one sorted list of files. Each item then runs exactly
as `parse one.pdf` would, with its own routing and error isolation.

A policy is the default backend chain, written once in the `policy.backends` block of your
`openreading.yaml`. Each null-backend item resolves that list independently. A batch that names a
backend runs that backend directly, while a named strategy follows its own explicit nodes. From
Python, `openreading.run_batch(paths, config="openreading.yaml")` reads the same file, and
`config={"version": 1, "policy": {…}}` passes the same shape inline. [Routing and
keys](../router/README.md#recipes) runs both.

<!-- diagram:src-openreading-batch-1 -->
<p align="center"><img src="../../../assets/diagrams/src-openreading-batch-1.svg" alt="Mental model" /></p>

<details>
<summary>Diagram source (Mermaid)</summary>

```mermaid
%%{init: {"theme":"base","fontFamily":"Arial","deterministicIds":true,"deterministicIDSeed":"openreading","htmlLabels":false,"themeVariables":{"fontFamily":"Arial","fontSize":"17px","lineColor":"#8194ad","textColor":"#183451","primaryTextColor":"#183451","primaryColor":"#edf3fc","primaryBorderColor":"#9db4d0","edgeLabelBackground":"#ffffff","clusterBkg":"#f5f8fc","clusterBorder":"#d7e1ee","titleColor":"#183451","actorBkg":"#edf3fc","actorBorder":"#9db4d0","actorTextColor":"#183451","actorLineColor":"#9db4d0","signalColor":"#527095","signalTextColor":"#183451","labelBoxBkgColor":"#fff4de","labelBoxBorderColor":"#c6953a","labelTextColor":"#70501b","loopTextColor":"#527095","noteBkgColor":"#edf3fc","noteBorderColor":"#9db4d0","noteTextColor":"#183451","sequenceNumberColor":"#ffffff","activationBkgColor":"#e7f3ee","activationBorderColor":"#679780"},"flowchart":{"curve":"monotoneY","nodeSpacing":32,"rankSpacing":48,"padding":18,"useMaxWidth":true},"sequence":{"useMaxWidth":true,"actorMargin":65,"messageMargin":38,"mirrorActors":false}}}%%
flowchart TD
  S[/"sources<br>dir, glob, files, URLs"/]:::src --> I["intake<br>expand, sort, skip by format"]:::work
  I --> R1["item 1<br>the single-document pipeline"]:::work
  I --> R2["item N<br>the same pipeline again"]:::work
  R1 --> E(["one batch-result envelope"]):::hero
  R2 --> E
  E --> C["corpus compare<br>pair by relpath, then filename,<br>then sha256"]:::gate
  E2[/"second envelope, same folder"/]:::src --> C
  C --> V["verdict per document<br>plus a rollup"]:::out
  classDef src fill:#f5f8fc,stroke:#a7b9d0,stroke-width:1px,color:#29445f;
  classDef work fill:#edf3fc,stroke:#9db4d0,stroke-width:1px,color:#183451;
  classDef gate fill:#fff4de,stroke:#c6953a,stroke-width:1px,color:#70501b;
  classDef good fill:#e7f3ee,stroke:#679780,stroke-width:1px,color:#245740;
  classDef bad fill:#fbeeee,stroke:#c78686,stroke-width:1px,color:#803d3d;
  classDef store fill:#e7f3ee,stroke:#679780,stroke-width:1px,color:#245740;
  classDef out fill:#edf3fc,stroke:#9db4d0,stroke-width:1px,color:#183451;
  classDef hero fill:#164bc5,stroke:#164bc5,stroke-width:1px,color:#ffffff;
  linkStyle default stroke-width:1.4px;
```

</details>

Whether a run is a batch is decided by the form of the input and never by the count. A directory or
a glob that expands to a single file is still a batch with one item. A single named file always
yields the ordinary single `response`, byte for byte.

## Walkthrough

Start from the root README's sample, then build a folder with one file the backend cannot read:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
mkdir -p corpus && cp sample.pdf corpus/a.pdf && cp sample.pdf corpus/b.pdf && echo hi > corpus/c.txt
```

### 1. Parse a directory and read the envelope

`--save-dir out` writes each item's own `response` under `out/`, next to the envelope on stdout.

```bash
uv run openreading parse corpus/ --backend pymupdf --save-dir out > batch.json
```
```text
[1/3] a.pdf succeeded
[2/3] b.pdf succeeded
[3/3] c.txt failed unsupported_format: pymupdf cannot read c.txt. It reads pdf, xps, epub, mobi, cbz, svg, and this file is not one of them.
```

**You should see** one progress line per file on stderr, exit 0, and pure JSON in `batch.json`.
The `.txt` is a skip with a reason, never a crash. PyMuPDF also writes two lines of its own to
stderr, a `fitz` deprecation notice and a `pymupdf_layout` advisory. Neither changes what the run
produces. Here `out/a.pdf.json` and `out/b.pdf.json` hold those two responses, with subdirectories
preserved.

Those files appear once, after the last item finishes and the envelope is assembled, rather than as
each item completes. An interrupted run therefore writes none of them, however many items already
succeeded, so `--save-dir` is a convenience and not a crash-safety mechanism. [Sizing a large
run](#sizing-a-large-run) has the shard recipe that does protect a long run.

`batch.json` is a `batch-result.v0.2`. Here it is with the two inner responses cut out:

```json
{ "schema_version": "0.2", "status": { "state": "partial" },
  "request": { "backend": "pymupdf", "jobs": 1, "source_args": ["corpus/"] },
  "items": [
    { "source": { "filename": "a.pdf", "format": "pdf", "path": "corpus/a.pdf", "relpath": "a.pdf", "size_bytes": 8688, "sha256": "…" },
      "state": "succeeded", "response": { "schema_version": "0.3", "…": "…" }, "transport": "platform" },
    { "source": { "relpath": "b.pdf", "…": "…" }, "state": "succeeded", "response": "…", "transport": "platform" },
    { "source": { "filename": "c.txt", "format": "txt", "path": "corpus/c.txt", "relpath": "c.txt", "size_bytes": 1, "sha256": "…" },
      "state": "failed", "error": { "code": "unsupported_format", "message": "pymupdf cannot read c.txt. It reads pdf, xps, epub, mobi, cbz, svg, and this file is not one of them." } } ],
  "summary": { "total": 3, "succeeded": 2, "failed": 1, "duration_ms": 49.0, "pages_processed": 4, "backends": { "pymupdf": 2 } } }
```

The envelope holds one item per file, in argument order. A directory's files come sorted by
relative path, whatever order the progress lines finished in, and that ordering is rule `M1` under
[How it decides](#how-it-decides). Each item has a `relpath`, the path relative to the folder you
passed, and corpus compare pairs documents by it. Each succeeded item also carries a `transport`,
which names who fanned the work out. `platform` means this process ran every document through the
ordinary single-document pipeline itself. `native` means a backend's own bulk endpoint took the
whole folder as one job.

Check it with `jq -r '.items[] | "\(.source.relpath) \(.state)"' batch.json`.

### 2. A single file, a glob, several files, `--jobs`, the size guard, a strategy

```bash
uv run openreading parse corpus/a.pdf --backend pymupdf | jq -c '{schema_version, state: .status.state}'
uv run openreading parse 'corpus/*.pdf' --backend pymupdf --jobs 4 | jq -c '{jobs: .request.jobs, total: .summary.total}'
uv run openreading parse corpus/a.pdf corpus/b.pdf --backend pymupdf | jq -c '.summary | {total, succeeded}'
uv run openreading parse corpus/ --backend pymupdf --max-items 1; echo "exit=$?"
uv run openreading parse corpus/ --strategy offline_first | jq -c '.request, [.items[] | select(.state=="succeeded") | .response.orchestration.outcome]'
```
```text
{"schema_version":"0.3","state":"succeeded"}
{"jobs":4,"total":2}
{"total":2,"succeeded":2}
[batch] batch expansion is 3 files, over the max-items limit (1); raise it with --max-items / max_items= if this is intended
exit=2
{"backend":"strategy:offline_first","strategy":"offline_first","jobs":1,"source_args":["corpus/"]}
["ok","ok"]
```

**You should see** a `0.3` single response for the named file and batch envelopes for the glob and
the pair. Quote the glob, because the shell expands an unquoted one into two files, which is also a
batch. `--jobs 0` clamps to 1, and a value above `--max-jobs` (32) exits 2. Under `--strategy` each
succeeded item carries its own `orchestration` block, because the strategy runs per document
([Strategies](../strategies/README.md)).

`--jobs N` runs items in a thread pool of N workers, so how much it buys you depends on the
backend. A backend that spends its time waiting on a network call gains most of the N. A backend
that spends its time computing in Python gains nothing, because the workers take turns on the
interpreter lock. Measured here on 64 twelve-page PDFs, `pymupdf` took 4.62 s at `--jobs 1` and
4.67 s at `--jobs 16`, which is no speedup at all. Measured on 16 of the same documents,
`tesseract` fell from 43.4 s to 15.6 s and then stopped improving above `--jobs 4`.

It stops there because a backend may declare its own concurrency ceiling, and `tesseract` declares
4. Asking for more workers than that ceiling changes nothing, and the run tells you so on stderr.

```bash
uv run openreading parse corpus/ --backend tesseract --jobs 8 | jq -c .request
```
```text
[preflight] --jobs 8 requested; tesseract caps platform concurrency at 4 (descriptor.batch.max_concurrency), so this run uses 4
…
{"backend":"tesseract","jobs":4,"source_args":["corpus/"]}
```

**You should see** the notice on stderr and `jobs: 4` in the envelope. That field always echoes the
value the run used, never the one you asked for. Every backend's ceiling is in the [limits
table](../adapters/README.md#the-ceilings-on-one-request), and the
useful ceiling sits far below the `--max-jobs` limit of 32. The notice stays silent under the
router's own choice (`--no-strategy` on the command line, `backend=None` in Python) and under
`--strategy`. Both resolve a backend per item, so no single ceiling is knowable up front.

`--max-items` (default 200) refuses before any file is read. It guards against a mistyped path and
against unintended spend, and it is also the only thing bounding the run's memory. The batch holds
every item's full response until the last one finishes, so peak memory grows with the corpus and
never levels off. Raising the limit for a large corpus is therefore not free, and
[Sizing a large run](#sizing-a-large-run) gives the numbers for choosing a safe value.

### 3. The payoff: compare two runs of the same folder

Two batch envelopes of the same folder show you where the backends disagree, document by document.

```bash
uv run openreading parse corpus/ --backend tesseract > batchB.json
uv run openreading compare batch.json batchB.json --format table
```
```text
CORPUS COMPARE — batch vs batchB
2 document(s): 0 equivalent · 0 divergent · 2 mixed · 0 unpaired

  [     mixed] a.pdf
  [     mixed] b.pdf

FINDINGS (across paired documents)
     4  type_conflict
     2  block_unique
     2  table_shape_mismatch
```

**You should see** one verdict line per document and a rollup of finding codes. A verdict is the
compare judgement on one pair, such as `mixed` when the texts agree but the tables do not.
`--format diffs` prints what each side captured that the other missed. `--format json` is the
schema-valid `corpus-report`, with the same numbers under `.rollup`. A document in one run but not
the other is `unpaired`, which is a finding and not a crash. Mixing a batch envelope with a single
response exits 2. The verdicts and codes are in [Compare](../comparison/README.md).

## Recipes

**Find out which documents failed, and why.**
```bash
mkdir -p broken && cp sample.pdf broken/good.pdf && echo 'not a pdf' > broken/bad.pdf
uv run openreading parse broken/ --backend pymupdf > broken.json; echo "exit=$?"
jq -c '.items[] | select(.state=="failed") | {relpath: .source.relpath, error}' broken.json
```
```text
[1/2] bad.pdf failed FileDataError: PyMuPDF failed: Failed to open stream
[2/2] good.pdf succeeded
exit=4
{"relpath":"bad.pdf","error":{"code":"FileDataError","message":"PyMuPDF failed: Failed to open stream"}}
```
Exit 4 means `partial`. Some items failed, and the good document still has its full response.

**Load a batch result into a warehouse table.**
Flatten one row per item from `items[]`, and run three assertions on every load before anything
queries the result.

```bash
jq -e '.schema_version == "0.2"' batch.json > /dev/null || echo "schema bumped, re-check the loader"
jq -r '[.items[] | select(.state != "succeeded") | .source.relpath] | @csv' batch.json
jq -r '.summary.pages_processed' batch.json
```

Assert `schema_version` first, because a bump is the one signal that the shape may have moved
under you. Triage every item whose `state` is not `succeeded` next: a failed item carries an
`error` instead of a response. Read `summary.pages_processed` last, which sums the page counts
the backends themselves reported and is absent when none did. There is no money in the envelope
to load. `cost_usd` and `cost_bases` were removed along with the per-vendor price tables that
filled them ([JSON Schemas](../schemas/README.md#what-a-response-guarantees)), so a spend column
comes from your provider invoice joined on your own run ids. Three columns are absent rather than
null when they have no value:
`warnings`, per-block `confidence`, and `typed_fields`. Read them with a default and make the
column nullable.

**Run from Python.**
```python
import openreading
env = openreading.run_batch(["corpus/"], backend="pymupdf", jobs=2)
print(env["status"]["state"], env["summary"]["succeeded"], env["summary"]["backends"])   # succeeded 2 {'pymupdf': 2}
```
Python returns the same envelope under the same intake rules. `max_items=` and `max_jobs=` are the
keyword forms of the flags.

**Batch over HTTP.**
```bash
# document.path is refused over HTTP unless rooted (openreading.server docstring, "Security")
OPENREADING_SERVER_PATH_ROOT="$PWD" uv run openreading serve &      # http://127.0.0.1:8787
until curl -s http://127.0.0.1:8787/healthz > /dev/null; do sleep 0.2; done
curl -s -X POST http://127.0.0.1:8787/v1/batch -H 'content-type: application/json' \
  -d '{"documents": [{"path": "'"$PWD"'/corpus/a.pdf"}, {"path": "'"$PWD"'/corpus/b.pdf"}], "backend": "pymupdf", "jobs": 2}' \
  | jq -c '{state: .status.state, request, transports: [.items[].transport]}'
kill %1
```
```json
{"state":"succeeded","request":{"backend":"pymupdf","jobs":2,"source_args":["doc-0","doc-1"]},"transports":["platform","platform"]}
```
`backend` is a plain string here, such as `"pymupdf"`, not the `{"id": …}` object `/v1/parse`
takes. The object form is refused with a 400 naming the fix, not run. Expand directories on the
client side. See [The HTTP server](../server/README.md).

**Mix files, folders, and URLs.** (shape shown, not run)
Run `uv run openreading parse https://example.com/loan.pdf corpus/ extra/w2.png --backend pymupdf`.
A URL passes through to its item as `document.url`, with no `sha256` at intake. Hidden files and
symlinks inside a directory are skipped.

**Use a hosted backend's native batch.** (shape shown, not run)
With a hosted key, `--backend anthropic-claude` over a directory sends one vendor batch job, and
each item reports `"transport": "native"`. `--deadline SECONDS` overrides its one-hour wait.

**Read the scope preflight before a hosted run.** More than 10 live items on a directly named
hosted backend print a `[preflight]` line on stderr first, and never a prompt. It names how many
calls are about to leave your machine, to which backend, on whose key.

`c16/` is any folder of sixteen documents.

```bash
uv run openreading parse c16/ --backend reducto > /dev/null
```
```text
[preflight] 16 items on hosted backend reducto: 16 call(s) on your own key, one per item and more if a document is paged
…
```

**You should see** one line naming the call count as a floor. It quotes no price. The rates it
used to print came from `descriptor.cost`, a rate card this package had written down about
someone else and could not verify, and they are gone with the rest of core's money. Intake opens
no files either, so no page count exists yet. The threshold is 10 live items, counting neither a
failed item nor a directly named single file. The preflight stays silent under `--no-strategy`
and under `--strategy`, which is the shape a production backfill usually takes. Price any run
from your provider's own invoice. With no `REDUCTO_API_KEY` set, every item fails on missing
credentials and the batch exits 1, after the preflight has printed.

### Sizing a large run

A corpus larger than a few hundred documents needs a number before it needs a command. One
invocation holds every response in memory until the last item finishes. Peak memory is therefore a
straight line in the item count. On this machine it measured 81 MB of base plus 1.47 MB per
twelve-page PDF. That line predicted 269.2 MB at 128 documents where the run measured 269.1 MB, so
it is worth planning against. Each document also adds about 274 KiB to the single JSON document on
stdout. A large batch is therefore awkward to read back as well as to run.

Turn that into a ceiling by subtracting the base from your budget and dividing by the per-document
cost. For a 4 GB budget, `(4096 - 81) / 1.47` is about 2,700 documents. For 16 GB it is about
11,000. Halve those if your documents run to twenty-five pages rather than twelve. Better still,
measure your own figure over a few hundred of your real files. Use `/usr/bin/time -l` on macOS and
`/usr/bin/time -v` on Linux. Nothing warns you when you pass the ceiling, and `--max-items 100000`
is accepted in silence. The ceiling you compute is the only one there is.

**Shard the corpus, and treat one shard as the retry unit.** Batch-level resume does not exist, so
an interrupted run loses every item it had finished. Splitting the corpus into shards small enough
to re-run whole turns that from a lost day into a lost shard.

```bash
mkdir -p shards shard-out
find corpus/ -name '*.pdf' | split -l 2000 - shards/shard_
for s in shards/shard_*; do
  uv run openreading parse $(tr '\n' ' ' < "$s") --backend pymupdf --max-items 2000 \
    > "shard-out/$(basename "$s").json"
done
```

Pick the shard size from the arithmetic above, and keep it well under your ceiling so a long
document cannot push one shard over. The loop hands each shard's paths to `parse` as separate
words, so it assumes no file name contains a space. Each shard writes its own envelope, so a shard
that fails re-runs on its own while the shards that succeeded keep their JSON. Keeping the file
lists and the outputs in separate directories makes the loop safe to run again over the same
shards.

> [!WARNING]
> An interrupted shard has already spent whatever a hosted backend charged for the items it
> parsed, and the re-run pays for them a second time. Size shards small on a hosted backend for
> that reason, not only for memory.

## How it decides

These rules are why a batch never surprises you with a different envelope shape, a crash, or a
widened backend set. Each rule names the failure it avoids. The full set is M1–M10 in the
package docstring.

Source: `src/openreading/batch/__init__.py` (the M1–M10 invariants). Live truth: `uv run python -m
pydoc openreading.batch`. If this table and that text disagree, the text is right. Fix the table.

| Rule | What it avoids | Where |
|---|---|---|
| `M2` envelope by input form, not count | An envelope type that flips with how many files a folder holds | `batch.sources.looks_batch` |
| `M1` sorted, recursive, deterministic expansion | Two runs of one folder that pair differently | `batch.sources.resolve_intake` |
| `M3` unsupported format is a skip with a reason | A file the chosen backend cannot read crashing the run, or vanishing silently | `batch.sources.resolve_intake` |
| `M4` item and jobs ceilings refuse before reading | A home directory, an accidental hosted spend, or a corpus whose responses exhaust memory | `batch.sources`, `batch.runner.bound_jobs` |
| `M6` per-item isolation | One bad file taking the corpus down | `batch.runner` |
| `M7` per-item routing, no batch-level cache | A cached decision widening the caller's list | `batch.runner`, `openreading.api` |

The batch status has three values. `succeeded` means at least one item succeeded and none failed
(exit 0). `partial` means some of each (exit 4). `failed` means nothing succeeded (exit 1). An
empty batch is `failed`, with an `empty_batch` warning saying why. Silence is never mistaken for
a hang.

## Reference

- `uv run python -m pydoc openreading.batch` covers the layer, M1–M10, the envelope, native batch,
  surfaces, and exits.
- `uv run python -m pydoc openreading.comparison.corpus` covers pairing precedence and verdicts.
- `uv run openreading parse --help` lists every batch flag.
- `src/openreading/schemas/batch-result.v0.2.json` and `corpus-report.v0.1.json` are described in
  [JSON Schemas](../schemas/README.md). `scripts/batch_demo.sh corpus/` runs `pymupdf` and
  `tesseract` over a folder you name, writes both envelopes under `<folder>/.runs/`, and compares
  them.

## Not built yet

- `--retry-failed <batch.json>`, a merge-rerun of failed items (`openreading.batch` docstring,
  "Deferred").
- Webhook-mode batches, and server-side directory upload as a multipart bundle (same).
- Corpus-level evals with `--truth` per document, and native batch for providers that stage through
  GCS or blob containers (same).
- Batch-level resume. Ctrl-C with `OPENREADING_LEDGER` set exits 6 and names no run id
  (`openreading.cli` docstring, exit codes). That holds even for a `--backend` batch, which writes
  nothing to the journal, the on-disk record of a run that `resume` replays. Shard the corpus
  instead, as [Sizing a large run](#sizing-a-large-run) shows.

## See also

- [Docs home](../README.md)
- [Compare](../comparison/README.md): the verdicts a corpus report rolls up.
- [The run ledger](../ledger/README.md): one journaled run per item.
- [The HTTP server](../server/README.md): `POST /v1/batch`.
- [Backend adapters](../adapters/README.md) · [JSON Schemas](../schemas/README.md)

<sub>[Docs home](../README.md) · [← Compare](../comparison/README.md) · [The run ledger →](../ledger/README.md)</sub>
