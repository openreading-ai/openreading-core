<img src="assets/brand/icon.svg" alt="OpenReading" width="64" height="64" />

# OpenReading: an intelligent, policy-aware router for document processing

[![CI](https://github.com/openreading-ai/openreading-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/openreading-ai/openreading-core/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/badge/coverage-%E2%89%A594%25-brightgreen)](#status-and-versioning)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

OpenReading reads your documents with the backends your rules allow, and returns one JSON whose
shape does not depend on which backend did the work. A backend is the thing that does the reading,
such as PyMuPDF on your machine or Reducto's hosted API. Policy-aware means you write those rules
once instead of choosing a backend for each document. A policy is a block in your
`openreading.yaml` naming what a backend must guarantee before it may run, and no fallback relaxes
it ([how it decides](src/openreading/router/README.md#how-it-decides)). Intelligent means a strategy
acts on what a run reveals, so a local backend that returns almost no text escalates to a stronger
one. One command takes a single document or a whole folder, and a folder comes back as one envelope
holding one response per document.

OpenReading is a library and a command you run on your own machine. It is not a hosted service, a
user interface, or a model. If you call one parser and want its native output, call that parser
directly. OpenReading earns its place when you switch, compare, or route between parsers.

Most of this codebase is written by AI agents, under rules that assume an agent wrote it. An agent
produces a confident sentence as easily as a true one, so the checks here aim at the claims the
code makes rather than at its style. The JSON Schemas are pinned byte for byte. The strategy
examples in the documentation are executed rather than proofread. The whole gate runs offline,
with no key and no network. A human reviews every change and tests what a test cannot reach, such
as a real vendor API reading a real document. [How this repo is built and
verified](#how-this-repo-is-built-and-verified) names each check and the failure it catches.

## What it does

Whichever backends read your documents, you get one shape that you can read, compare, or use to
replay the run.

<!-- diagram:README-1 -->
<p align="center"><a href="assets/diagrams/README-1.svg"><img src="assets/diagrams/README-1.svg" alt="Your documents follow your rules to a chosen backend, then return one response shape for reading, comparison, and replay." /></a></p>

<details>
<summary>Logical flow (Mermaid)</summary>

```mermaid
%%{init: {"theme":"base","fontFamily":"Arial","deterministicIds":true,"deterministicIDSeed":"openreading","htmlLabels":false,"themeVariables":{"fontFamily":"Arial","fontSize":"17px","lineColor":"#8194ad","textColor":"#183451","primaryTextColor":"#183451","primaryColor":"#edf3fc","primaryBorderColor":"#9db4d0","edgeLabelBackground":"#ffffff","clusterBkg":"#f5f8fc","clusterBorder":"#d7e1ee","titleColor":"#183451","actorBkg":"#edf3fc","actorBorder":"#9db4d0","actorTextColor":"#183451","actorLineColor":"#9db4d0","signalColor":"#527095","signalTextColor":"#183451","labelBoxBkgColor":"#fff4de","labelBoxBorderColor":"#c6953a","labelTextColor":"#70501b","loopTextColor":"#527095","noteBkgColor":"#edf3fc","noteBorderColor":"#9db4d0","noteTextColor":"#183451","sequenceNumberColor":"#ffffff","activationBkgColor":"#e7f3ee","activationBorderColor":"#679780"},"flowchart":{"curve":"monotoneY","nodeSpacing":32,"rankSpacing":48,"padding":18,"useMaxWidth":true},"sequence":{"useMaxWidth":true,"actorMargin":65,"messageMargin":38,"mirrorActors":false}}}%%
flowchart TD
  D[/"Your documents<br>File, folder, or glob"/]:::src --> B["Choose the backend<br>Explicit name, policy, or strategy"]:::gate
  B --> P["The selected backend reads each document"]:::work
  P --> J(["One JSON per document<br>One envelope over the run"]):::hero
  J --> T["Read<br>Text and tables"]:::out
  J --> C["Compare<br>Across your corpus"]:::out
  J --> E["Inspect<br>Explain or replay"]:::out
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

**The problem.** Every document parser has its own API and its own output shape. Swapping one
parser for another means rewriting the code that reads its result. Comparing two parsers on your
own documents is guesswork, because their outputs do not line up. Some documents, such as a
medical record, must never leave the building at all.

**What you get.** You send one request and read one JSON, whichever parser did the work. That JSON
is the envelope, and it has the same shape for every backend, so code written against one backend
works against all of them. A backend that cannot fill a field leaves it out rather than inventing
a value. You can ask for four things.

- **[parse](src/openreading/cli/README.md)**: you have documents and want their text, their
  tables, and where each block sits on the page. One command takes a file, a folder or a glob, and
  returns one JSON.
- **[compare](src/openreading/comparison/README.md)**: you have two backends and want to know where
  their readings of your documents differ. You get a one-word verdict, `equivalent`, `mixed`, or
  `divergent`, and a list of findings naming each difference, for example a table one backend lost.
  Compare picks no winner unless you name one output as the baseline. When you want a ranking,
  `leaderboard` ranks backends against documents you labeled, which means a small `case.json` of
  expected values beside each one. `benchmark run` also measures backends and strategies with
  publisher-owned public scorers ([Evals](src/openreading/evals/README.md)).
- **[route](src/openreading/router/README.md)**: some documents may only go to vendors that meet
  a policy. The router applies the policy you name and drops every failing backend before anything
  runs.
- **[strategy](src/openreading/strategies/README.md)**: you want a cheap backend first and a
  stronger one only when the first result falls short. A strategy is a named plan that decides
  for you. Each run leaves a trace of which backends ran and why. `explain` prints that trace, and
  `replay` re-runs its decisions against the same document and config.

**What you need.** Bring a backend and documents it can read. Each backend declares its formats,
such as PDF, images, office files, HTML and EPUB.
[The adapter catalog](src/openreading/adapters/README.md) lists the formats per backend. An adapter
is the package that wraps one backend. The router skips a backend that cannot read your files and
records the drop as `unsupported_format`. PyMuPDF and Tesseract run locally with no key, while a
hosted backend needs your vendor key. If you have no document to hand, two synthetic ones ship in
[`examples/`](examples/README.md) and every parse on this page runs against them.

## Install

Four lines give you a working install with two local backends and no API keys. You need `git` and
[`uv`](https://docs.astral.sh/uv/), which fetches Python 3.11+ itself. The Tesseract OCR engine is a
separate system binary, needed only for the `tesseract` backend, and the next section installs it.
Python 3.11+ with `pip` also works. Nothing is on PyPI yet, so install from the clone.

```bash
git clone https://github.com/openreading-ai/openreading-core
cd openreading-core
uv sync --all-extras --dev       # every backend extra + the dev tools (same as `make sync`)
uv run openreading backends      # one row per backend, with a CONFIGURED column and the variables each backend still needs
```

Without uv, run `python3 -m venv .venv && .venv/bin/pip install -e '.[pymupdf,tesseract,server]'`
and type `.venv/bin/openreading …` wherever this page says `uv run openreading …`. In that venv,
`openreading backends` marks a hosted backend as missing an extra rather than a variable.

### The two local backends

Neither local backend calls anyone, so nothing you parse with them leaves your machine.

- **PyMuPDF** reads a PDF's own text layer. `uv sync` installs it, and there is nothing else to set
  up. It is the only copyleft component here, dual licensed as AGPL-3.0 or an Artifex commercial
  licence. Its own `[pymupdf]` extra keeps it separate, so you can leave it out. Its own docs are at
  [pymupdf.readthedocs.io](https://pymupdf.readthedocs.io/en/latest/installation.html).
- **Tesseract** renders each page to a bitmap and runs OCR over the pixels. That is what you need
  when a document is a scan or a photograph with no text layer. `uv sync` installs the Python
  wrapper. The OCR engine is a separate system binary you install once: `brew install tesseract`
  on macOS, `sudo apt install tesseract-ocr` on Debian or Ubuntu. Windows and other distributions
  are covered by
  [the Tesseract install guide](https://tesseract-ocr.github.io/tessdoc/Installation.html), and
  each extra language is its own `traineddata` package, such as `tesseract-lang` or
  `tesseract-ocr-deu`.

`uv run openreading backends` is how you check, and both local rows should read `yes` under
CONFIGURED once the OCR binary is installed. A missing engine shows up in the MISSING column by
name rather than as a crash later:

```text
BACKEND                        TYPE               CONFIGURED  MISSING
tesseract                      oss_library        no          tesseract binary (brew install tesseract / apt install tesseract-ocr)
```

## Your first parse

The response JSON is the interface your application builds against, not a backend-specific result you must decode yourself.
Read the [worked response guide](src/openreading/schemas/README.md#understanding-the-response-json) for an annotated example and a reusable Python consumer.
In a terminal, `uv run openreading help response` explains content, tables, fields, warnings, and provenance.

> **Want the guided version?** [The tutorial](https://openreading.ai/oss-tutorial) walks the whole tool
> in seventeen steps, from this first parse to a policy, a self-escalating strategy, a folder run
> and the HTTP server. It uses the documents in [`examples/`](examples/README.md) and needs no key.
> The sections below are the short tour.

Your first JSON is one command away, because the documents are already in the clone.
[`examples/`](examples/README.md) holds two synthetic one-page bank statements, with an invented
name, an invented bank and invented balances. Each has a header, an account block, a balance
summary and a dated transaction table. Parse the January one with PyMuPDF:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf > pymupdf.json
```

**You should see** one stderr line from PyMuPDF itself, suggesting the `pymupdf_layout` package for
better page layout analysis. It is not an error, and it does not reach your JSON. Here is
`pymupdf.json`, trimmed to the keys you read first (numbers rounded):

```json
{ "schema_version": "0.3", "status": { "state": "succeeded" },
  "backend": { "id": "pymupdf", "type": "oss_library", "output_paradigm": ["block_tree"] },
  "document": { "page_count": 1,
    "text": "First National Bank\n1000 Junction Highway, Kerrville, TX 78028\nACCOUNT STATEMENT\nAccount Holder:\nJohn Smith\n…",
    "markdown": "First National Bank\n\n1000 Junction Highway, Kerrville, TX 78028 ACCOUNT STATEMENT\n\nAccount Holder:\n\n…",
    "pages": [ { "page_number": 1, "width": 612.0, "height": 792.0, "unit": "pdf_point",
      "blocks": [ { "type": "text", "native_type": "text", "text": "First National Bank", "reading_order": 0,
        "bbox": { "x": 0.0588, "y": 0.0265, "w": 0.2085, "h": 0.0243, "page": 1,
                  "bbox_native": { "coords": [36.0, 21.02, 163.58, 40.3], "origin": "top_left", "unit": "pdf_point" } } } ] } ] },
  "usage": { "pages_processed": 1 },
  "warnings": [ { "code": "confidence_unavailable", "field": "block_confidence",
                  "message": "PyMuPDF is a deterministic parser; per-element confidence does not exist" } ] }
```

That page has 21 blocks, and one is shown. `schema_version` names the JSON contract rather than the
package you installed, and [Status and versioning](#status-and-versioning) separates the two. The
listing omits `backend_raw` and `channel_provenance`, which
[`src/openreading/schemas/README.md`](src/openreading/schemas/README.md) describes. `backend_raw`
carries the backend's own response untouched, so a vendor field the envelope does not model is
still there. Every backend returns this shape. When a backend cannot produce a field, OpenReading
leaves it out and never invents one, because a made-up confidence looks like a measured one. It
names some of those gaps in `warnings[]`, as PyMuPDF does for confidence here, and leaves others
unannounced. A channel is one kind of output inside the envelope, such as plain text, tables, or
per-block confidence. `channel_provenance` lists the channels this run produced, so read it rather
than waiting for a warning ([The channel
contract](src/openreading/derive/README.md#which-signal-to-trust-when-a-channel-is-missing)).

Now read the same page the other way. Tesseract ignores the text layer, renders the page to a
150-DPI bitmap and OCRs it. That is the work it would do on a photograph of the same statement:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend tesseract > tesseract.json
```
```json
{ "backend": { "id": "tesseract", "type": "oss_library", "output_paradigm": ["element_list"] },
  "document": { "page_count": 1,
    "pages": [ { "page_number": 1, "width": 1275.0, "height": 1650.0, "unit": "pixel",
      "blocks": [
        { "type": "text", "native_type": "line", "text": "First National Bank", "confidence": 0.96, "reading_order": 0, "text_type": "printed" },
        { "type": "text", "native_type": "line", "text": "; Account Hotder-",   "confidence": 0.0,  "reading_order": 3, "text_type": "printed" },
        { "type": "text", "native_type": "line", "text": "John Smith",          "confidence": 0.93, "reading_order": 4, "text_type": "printed" } ] } ] } }
```

Three things differ, and each one is the contract doing its job. The page is 1275×1650 `pixel`
rather than 612×792 `pdf_point`, because that is what Tesseract measured. The `bbox.x/y/w/h`
values stay page-relative fractions either way, so code that positions a block works against both.
Every block carries a `confidence`, and the envelope carries no `warnings[]` at all, because
Tesseract genuinely measures one per word. And OCR misread `Account Holder:` as
`; Account Hotder-` with `confidence: 0.0`, so it told you where it was unsure.

The guides under `src/openreading/` use a different, generated document, with tables, columns and
an image. Build it whenever a guide asks for `sample.pdf`:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
ls -l sample.pdf     # 8688 bytes: a title, two paragraphs, a 3×4 table, two columns, a tiny image
```

## The CLI explains itself

You do not have to come back here for a flag. The command line carries its own manual, and every
page of it is generated from the same reference the maintainers read, so it cannot drift from
what the code does.

```bash
uv run openreading help              # the topic index, grouped by what you want to do
uv run openreading help batch        # one chapter: folders, globs, many files at once
uv run openreading help chaining     # which command's output feeds which command
uv run openreading parse --help      # one command: examples, flags, exit codes
```

`openreading help` lists every chapter. Start with `quickstart`, then `batch` if you have a
folder of documents, then `exit-codes` before you put any of it in CI. A chapter answers to the
name you would reach for, so `help folder` and `help glob` both open the batch chapter.

Every `<command> --help` ends with the same four things: examples you can paste, the command that
consumes this one's output, the exit codes this command can actually return, and the chapter that
goes deeper.

## Compare, route, strategy

**Compare.** Compare shows where two backends disagree on your documents and names each
difference.
You have both readings of the January statement already. Now ask what differs between them:

```bash
uv run openreading compare examples/john_smith_1000_2026_01.pdf --backends pymupdf,tesseract --format diffs
```
```
DIFF — pymupdf vs tesseract   (1 page(s))

① CONTENT — real text/values either side missed
   ✗ DIVERGENT   content shared by all: 0.98
   pymupdf:
     MISSED — 1 line(s) others have that pymupdf lacks:
        - ; Account Hotder-
     ONLY pymupdf — 1 line(s) no other backend captured — mostly readable text:
        + Account Holder:
   tesseract:
     MISSED — 1 line(s) others have that tesseract lacks:
        - Account Holder:
     ONLY tesseract — 1 line(s) no other backend captured — mostly readable text:
        + ; Account Hotder-
…
④ GRANULARITY
   pymupdf 21 blocks  ·  tesseract 26 blocks
```

The two agree on 98% of the page and disagree on one line. The report names that line on both
sides instead of handing you a score to go investigate. Compare does not know which backend is
right, so it does not claim to. You can still see at a glance that the OCR line is the mangled
one. `✗ DIVERGENT` labels the CONTENT section rather than the whole run. The run's one-word
verdict is `equivalent`, because one misread label is not enough to call a winner.
`--format diffs` is where the disagreement itself lives.

**Route.** Route prints the chain that would run, before anything runs. A bank statement is the
everyday case. It names a person, an account and every place they spent money, and plenty of teams
may not ship one to an arbitrary vendor. So you say which vendors may see it:

```bash
cat > openreading.yaml <<'YAML'
version: 1
policy:
  backends: [pymupdf, tesseract]
YAML
uv run openreading route examples/john_smith_1000_2026_01.pdf
```
```json
{ "chosen": "pymupdf",
  "fallbacks": ["tesseract"],
  "dropped": {},
  "terminal_reason": null }
```

That printed a chain and read nothing, and no flag named the list: every command finds
`openreading.yaml` in the working directory the same way. Add `--run` to execute the chosen
backend and get the envelope back beside the chain. `fallbacks` is the order a `--run` tries next
if `pymupdf` fails, and reordering your list reorders it.

`policy:` has one key. Earlier versions had nine, asking the engine to enforce a compliance
posture by reading a per-vendor table it kept in its own source: whether each vendor signs a BAA,
trains on your data, or retains a document for so many hours. That table could not be true. Every
entry was a claim about a company this project does not control, published on a page that changes
without notice, and a stale entry did not fail loudly, it routed your document to a backend you
believed was excluded. You already know which vendors you hold agreements with, so `backends:` is
that conclusion written by the one party who can reach it. An empty list permits nothing, and a
key the loader does not recognise is refused rather than ignored.

**Strategy.** A strategy gives you the cheap result when it is good enough and the stronger one
when it is not. It checks each output against quality gates. A gate is one test on a result, for
example whether the backend detected scanned pages. Four presets ship: `cost_saver`, `fast`,
`max_accuracy`, and `offline_first`. Run the last one and print its trace:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --strategy offline_first > strat.json
uv run openreading explain strat.json
```
```
strategy offline_first  →  pymupdf (ok)
  root.steps[0]    pymupdf      succeeded                     41ms
      scanned_pages_detected     obs=False thr=True  ok
      garbled                    obs=0.0189 thr=True  ok
      empty_pages_over           obs=0.0 thr=0.2  ok
      confidence_below           obs=None thr=0.6  skipped
```

The trace shows PyMuPDF ran, three gates passed, and the run stopped there, so no second backend
was ever called. The fourth gate is `skipped` rather than failed, because PyMuPDF reports no
confidence. A missing measurement never counts as a passing one. Timings vary between machines.
Write your own strategy with `uv run openreading strategy --help`. Set `OPENREADING_LEDGER` to a
directory before a long `--strategy` run and every step is journaled there, so an interruption
resumes instead of restarting. Without that variable nothing is written and there is nothing to
resume. A `--backend` run writes no journal, and a batch has no resume of its own. The
[run ledger](src/openreading/ledger/README.md) guide explains the journal.

**A folder at a time.** Point `parse` at a directory and it batches, which is how you run a whole
corpus rather than one file:

```bash
uv run openreading parse examples/ --backend pymupdf --jobs 2 > batch.json
```

Progress goes to stderr, one line per file, so `batch.json` stays pure JSON.

`batch.json` carries one entry per file under `items[]`, each with the source's path and SHA-256.
Its `summary` tells you at a glance whether the sweep went as expected, and the duration varies
between machines:

```json
{ "total": 6, "succeeded": 5, "failed": 1, "duration_ms": 511.0,
  "pages_processed": 10, "backends": { "pymupdf": 5 } }
```

The total is six because `examples/README.md` is in that folder too. It comes back as a failed
item carrying PyMuPDF's own `unsupported_format` reason rather than being dropped in silence, so
the count you get back always accounts for every file you pointed at. `scripts/batch_demo.sh path/to/docs` runs the same
sweep with both local backends and compares the two corpora.

The command above names its backend explicitly. A `policy.backends` list supplies the default
chain only when a request names no backend.
[Routing and keys](src/openreading/router/README.md#recipes) runs it both ways.

## Bring your own key

A hosted backend works as soon as its vendor key is in `.env`. Without one, the command exits with
code 3 and names the variable:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend reducto
# [reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
```

To fix it, append the one key you hold to a fresh file with `echo 'REDUCTO_API_KEY=sk_…' >> .env`.
`uv run openreading backends` then shows `reducto … yes`. From then on every Reducto call is
billed to your account.

Never run `cp .env.example .env`. That file ships `DOCLING_SERVE_URL` and `QWEN_VL_ENDPOINT` with
values rather than blanks. A copy therefore marks `docling` and `qwen-vl` configured on a machine
where neither is running. What it costs you is a different data path rather than extra
configuration. Put `docling` in your `policy.backends` after that copy and the router sends your
scan to `http://localhost:5001`, and the envelope records `docling TerminalError (ConnectError)`.
Without the copy the same command records `docling skipped (missing_credentials)` and the document
never reaches a socket.

Two things about that `echo`. `.env` is already in this repo's `.gitignore`, so the file you just
wrote inside a clone is not committed by accident. Your shell records the line itself, which puts
the key in `~/.zsh_history` or `~/.bash_history` in plain text. Prefix the command with a space if
your shell is set to skip those. You can also open `.env` in an editor and type the key there
instead.

## Python and HTTP

From Python or over HTTP you get the same shapes that the command line prints. PyMuPDF writes two
advisory lines to stdout as well, one before the first result line and one before the last.

```python
import openreading
doc = "examples/john_smith_1000_2026_01.pdf"
resp = openreading.run(doc, backend="pymupdf")                    # dict
print(resp["status"]["state"], resp["backend"]["id"])            # succeeded pymupdf
plan = openreading.route(doc)                                    # ./openreading.yaml
print(plan.eligible_ids)                                         # ['pymupdf', 'tesseract']
delta = openreading.compare([resp, openreading.run(doc, backend="tesseract")])
print(delta["headline"]["verdict"])                              # equivalent
```
```bash
# Start the server in one terminal. The server extra is included in the install above.
uv run openreading serve
# In another terminal, upload a file from the machine running curl:
curl --fail-with-body -sS http://127.0.0.1:8787/v1/parse \
  -F 'file=@examples/john_smith_1000_2026_01.pdf' \
  --form-string 'request={"backend":{"id":"pymupdf"}}' \
  | jq -r '.status.state, .backend.id'
# succeeded
# pymupdf
curl --fail-with-body -sS http://127.0.0.1:8787/healthz
# {"status":"ok","version":"0.3.0"}
```

Uploads need no server path root. For another laptop, folder uploads, and a separate Docling process,
follow the [serving tutorial](https://openreading.ai/oss-tutorial#15-serving-the-same-engine).
The [server guide](src/openreading/server/README.md) covers explicit server filesystem access and authentication.

## Status and versioning

**Status: pre-release.** You can build on the JSON shape today, while CLI flags, the Python API
and the strategy grammar may still change before 1.0. The JSON Schemas are stable, pinned byte
for byte by `tests/test_schema_evolution.py`. Nothing is on PyPI and the repository has no git
tags, so install from a clone.

Five numbers travel with this project, and only one of them is the code you installed.

| Number | Where you read it | What it identifies | When it changes |
|---|---|---|---|
| `schema_version` `0.3` | every response envelope | the JSON contract that response obeys | a new version file lands in [`src/openreading/schemas/`](src/openreading/schemas/README.md) |
| package `0.3.0` | `uv run openreading --version`, `openreading.__version__`, `pyproject.toml` | the code you installed | the first tagged release, which has not happened |
| `"version": "0.3.0"` | `GET /healthz` on a running `openreading serve` | the package number of the process answering you | with the package number, never on its own |
| heading `[0.4.0]` | [`CHANGELOG.md`](CHANGELOG.md) | a development milestone merged to `main` | a milestone merges, so it runs ahead of the package number and meets it at the first tagged release |
| codename `Canon (v0.5)` | [`CHANGELOG.md`](CHANGELOG.md) | a branch that carried one body of work | never, because it is a label rather than a version |

Pin a commit SHA. None of the five numbers is a pin, because there are no git tags and no PyPI
release. A SHA is the only way to name the exact code you tested.

## How this repo is built and verified

Agents will write a large share of the software that gets written. The open question is what makes
any of it trustworthy. The bet this repository makes is that agents verify it too, and that a
human is spent where machine checking runs out. An agent that reviews a diff, writes the failing
test first, or tries to break a fix costs little enough to run on every change. Human attention
does not, so it goes to the places no check can reach.

An agent is good at producing work that looks right. A passing suite and a clean formatter do not
catch that on their own. Each check below therefore takes aim at one claim the code makes about
itself, and the middle column names the failure the check exists to prevent.

### The gate

`make verify` is the finish line for a change, and CI runs that same target on Python 3.11 through
3.14. It runs offline. It reads no credential and opens no socket, so a green result never depends
on a vendor being reachable or on the person running it holding an account. `uv run lefthook
install` adds local hooks that run ruff and the documentation policy before a commit, and the whole
gate before a push. Those hooks are a convenience, and CI is the gate. Coverage carries a floor
inside that target. The floor ratchets up and never down, and the README badge is pinned to the
number the Makefile enforces.

| What is checked | The failure it prevents | Where it lives |
|---|---|---|
| schema evolution | a released schema file changes under a caller, or a newer version stops validating an older response | `tests/test_schema_evolution.py` |
| documented examples | a YAML strategy example that the real grammar rejects, because the example was written instead of run | `tests/test_docs_truth.py` |
| where documentation lives | a new markdown file, a relative link to something that moved, or a coverage badge claiming more than the gate enforces | `tests/test_docs_policy.py` |
| numbers quoted in prose | a document citing a coverage floor the Makefile no longer sets | `tests/test_docs_freshness.py` |
| the CLI manual | a subcommand with no help topic, a help page taller than one screen, or a page naming a path a reader cannot open | `tests/test_cli_help.py` |
| adapter conformance | a backend that fabricates a channel it cannot produce, or that omits the warning it owes | `tests/test_conformance.py`, [`openreading.testing`](src/openreading/testing/) |
| unfinished scaffolding | a generated adapter merged with the generator's placeholder values still in it | `tests/test_scaffold_sentinel.py` |
| install extras | an adapter that every test exercises and that `pip install openreading[<slug>]` ships nothing for | `scripts/check_extras_parity.py` |
| hosted backends | broken normalization or error mapping, proven against intercepted HTTP calls plus injected faults, with no key | `tests/test_<slug>_faults.py`, `tests/test_<slug>_http.py` |
| invariants, not examples | a bounding-box conversion that holds for the glyphs someone pinned and fails on everything else | `tests/test_geometry_properties.py` |
| determinism | a strategy run that replays into a different journal than the one it recorded | `tests/test_strategy_determinism.py` |
| output purity | an import whose side effect prints a line in front of the JSON envelope | `tests/test_stdout_purity_imports.py` |

Two rules shape how those tests get written. The failing test comes first, before the change that
makes it pass. When the change is a bug fix, break the fix again and watch the test go red, then
restore it, because a test written afterwards often passes for a reason unrelated to the bug. No
document here quotes a test count, since that number moves with every commit that adds one. Read
it live with `uv run pytest -m "not live" --collect-only -q`.

### Where a human is spent

Four things resist automation, and they are where review time goes.

A rationale comment states the failure a decision avoids, and that failure is invisible in the code
the decision produced. No test reads English, so a reviewer is the only thing keeping such a
comment true. The same holds for a design record under `design/`, which has to be deleted in the
pull request that ships its feature, its durable facts moved into the module docstrings.

A mock proves this project's own normalization and error handling. It never proves that the
vendor's API behaves the way the mock claims. `make verify-live` runs the keyed lane against the
real APIs, and it skips cleanly for every backend whose keys are absent. A person reads those runs,
along with the output of `make serve-smoke` and of the four smokes inside the gate, which drive the
real CLI over a deterministic sample document.

Scope is the fourth. Whether an addition belongs in this repository at all is a judgement about
what an open engine owes its users, and [`AGENTS.md`](AGENTS.md) is where that judgement is
written down for agents and humans alike.

## Where the docs are

Because each package directory carries one guide, every question below leads to one guide or one
command.

| You want to know… | Run / open |
|---|---|
| **the full documentation, every guide, and how an agent uses it** | [`src/openreading/README.md`](src/openreading/README.md), then `uv run openreading --help` and `uv run openreading <cmd> --help` for every flag |
| **how to get from a fresh clone to a working strategy, one step at a time** | [The tutorial](https://openreading.ai/oss-tutorial), maintained in `openreading-web`, over the shipped documents |
| **how to build against the response JSON** | [Annotated response and Python consumer](src/openreading/schemas/README.md#understanding-the-response-json), or `uv run openreading help response` |
| what the shipped example documents contain and where they came from | [`examples/README.md`](examples/README.md) |
| each backend's variables, runtime location, and env-var precedence rules | [`src/openreading/adapters/README.md`](src/openreading/adapters/README.md), then `uv run python -m pydoc openreading.credentials` |
| the exact JSON shapes (the contract) | [`src/openreading/schemas/README.md`](src/openreading/schemas/README.md), then the `*.json` files beside it |
| how to cascade backends under quality gates, race them, or compare them from one file | [Strategies](src/openreading/strategies/README.md) |
| what `looks_bad` and the other escalation checks actually measure | `uv run openreading help gates`, or the [worked gate tutorial](https://openreading.ai/oss-tutorial#writing-escalation-checks) |
| what differs between two backends' readings of the same document | [Compare](src/openreading/comparison/README.md) |
| how `policy.backends` chooses the default chain and where each key comes from | [Routing and keys](src/openreading/router/README.md) |
| how to run a folder of documents and read one result | [Batch runs](src/openreading/batch/README.md) |
| how to resume an interrupted run, replay one offline, or erase what it recorded | [The run ledger](src/openreading/ledger/README.md) |
| how to retrieve local page evidence from an agent | [Local MCP tools](src/openreading/mcp_server/README.md) |
| how to put the same engine behind an HTTP API on your own machine | [The HTTP server](src/openreading/server/README.md) |
| how to run public benchmarks or rank backends on documents you labeled | [Evals](src/openreading/evals/README.md) |
| every command, its flags, and the exit code your script branches on | `uv run openreading help` for the manual's topic index, `uv run openreading help <topic>` for one chapter, and [The command line](src/openreading/cli/README.md) for the walkthrough |
| why a response leaves a field out instead of inventing it | [The channel contract](src/openreading/derive/README.md) |
| the Python API, every reference section, and how to add a backend | `uv run python -m pydoc openreading`, then the same command with `.<module>` appended. For a new backend, `uv run python -m pydoc openreading.adapters`, then `scripts/new_adapter.py` |
| the checks a change must pass | `make verify` runs lint, types, pytest at a 94% coverage floor, and the schema and smoke checks. `uv run pytest -m "not live" --collect-only` prints the offline test count. |

## Contributing · Security · License

[`CONTRIBUTING.md`](CONTRIBUTING.md) · [`SECURITY.md`](SECURITY.md) ·
[`AGENTS.md`](AGENTS.md), the conventions humans and agents both follow · Apache-2.0
([`LICENSE`](LICENSE)). The `[pymupdf]` extra is the one copyleft component, as the Install section
says.

`openreading-core` is the engine itself: the schemas, every backend adapter, the
router, strategies, compare, batch, the ledger, the local server, and the benchmark harness.
[`AGENTS.md`](AGENTS.md) states which additions belong in this repository and which do not.
