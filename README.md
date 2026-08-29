# OpenReading

[![CI](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-%E2%89%A591%25-brightgreen)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

OpenReading hands your document to a backend and gives you back one JSON whose shape does not
depend on the backend. A backend is the parser that does the reading, such as PyMuPDF on your
machine or Reducto's hosted API.

## What it does

Whichever backend reads your document, you get one shape that you can read, compare, or replay.

```mermaid
flowchart LR
  D["your document"] --> B["choose a backend<br>yourself, by policy, or by strategy"]
  B --> P["the backend reads it"] --> J["one JSON<br>same shape every time"]
  J --> T["read the text and tables"] & C["compare two backends"] & E["explain or replay a run"]
```

**The problem.** Every document parser has its own API and its own output shape. Swapping one
parser for another means rewriting the code that reads its result. Comparing two parsers on your
own documents is guesswork, because their outputs do not line up. Some documents, such as a
medical record, must never leave the building at all.

**What you get.** You send one request and read one JSON, whichever parser did the work. The
JSON has the same keys for every backend, so code written against one backend works against all
of them. You can ask for four things, each of which answers a problem you already have.

- **[parse](src/openreading/cli/README.md)**: you have a document and want its text, its tables,
  and where each block sits on the page. One command returns one JSON.
- **[compare](src/openreading/comparison/README.md)**: you have two backends and want to know
  which one reads your documents better. You get a verdict naming what differs, for example a
  table that one backend lost.
- **[route](src/openreading/router/README.md)**: some documents may only go to vendors that meet
  a policy. A policy is a short JSON file that lists what a backend must guarantee before it may
  run. The router applies it and drops every failing backend before anything runs.
- **[strategy](src/openreading/strategies/README.md)**: you want a cheap backend first and a
  stronger one only when the first result falls short. A strategy is a named plan that decides
  for you. Each run leaves a trace of which backends ran and why, for you to print or replay.

**What you need.** Bring a backend and a document it can read. Each backend declares its formats,
such as PDF, images, office files, HTML and EPUB.
[The adapter catalog](src/openreading/adapters/README.md) lists the formats per backend. The
router skips a backend that cannot read your file and records the drop as `unsupported_format`.
PyMuPDF and Tesseract run locally with no key, while a hosted backend needs your vendor key. If
you have no document to hand, two synthetic ones ship in
[`examples/`](examples/README.md) and every command on this page runs against them.
OpenReading is not a hosted service, a UI, or a model.

## Install

Two commands give you a working install with two local backends and no API keys. You need `git`,
[`uv`](https://docs.astral.sh/uv/), which fetches Python 3.11+ itself, and the `tesseract` binary.
Python 3.11+ with `pip` also works. Nothing is on PyPI yet, so install from the clone.

```bash
git clone https://github.com/multiversal-ventures/openreading-core
cd openreading-core
uv sync --all-extras --dev       # every backend extra + the dev tools (same as `make sync`)
uv run openreading backends      # one row per backend: pymupdf and tesseract yes; hosted backends no, most naming the var they need
```

Without uv, run `python3 -m venv .venv && .venv/bin/pip install -e '.[pymupdf,tesseract,server]'`
and type `.venv/bin/openreading …` wherever this page says `uv run openreading …`. In that venv,
`openreading backends` marks a hosted backend as missing an extra rather than a variable.

### The two local backends

Neither local backend calls anyone, so nothing you parse with them leaves your machine.

- **PyMuPDF** reads a PDF's own text layer. `uv sync` installs it; there is nothing else to set
  up. It is the only AGPL-3.0 component here, kept in its own `[pymupdf]` extra so that you can
  leave it out. Its own docs are at
  [pymupdf.readthedocs.io](https://pymupdf.readthedocs.io/en/latest/installation.html).
- **Tesseract** renders each page to a bitmap and runs OCR over the pixels, which is what you
  need when a document is a scan or a photograph and has no text layer to read. `uv sync`
  installs the Python wrapper, but the OCR engine itself is a separate system binary you install
  once: `brew install tesseract` on macOS, `sudo apt install tesseract-ocr` on Debian or Ubuntu.
  Windows and other distributions are covered by
  [the Tesseract install guide](https://tesseract-ocr.github.io/tessdoc/Installation.html), and
  each extra language is its own `traineddata` package, such as `tesseract-lang` or
  `tesseract-ocr-deu`.

`uv run openreading backends` is how you check: both rows should read `yes` under CONFIGURED. A
missing engine shows up there by name rather than as a crash later.

```
tesseract                      oss_library        no          tesseract binary (brew install tesseract / apt install tesseract-ocr)
```

## Your first parse

Your first JSON is one command away, because the document is already in the clone.
[`examples/`](examples/README.md) holds two synthetic one-page bank statements, with an invented
name, an invented bank and invented balances. Each has a header, an account block, a balance
summary and a dated transaction table. Parse the January one with PyMuPDF:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf > pymupdf.json
```

You should see one stderr line, `Consider using the pymupdf_layout package …`. That is PyMuPDF's
advice, not an error. Here is `pymupdf.json`, trimmed to the keys you read first (numbers rounded):

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
  "usage": { "pages_processed": 1, "cost_basis": "infra_only" },
  "warnings": [ { "code": "confidence_unavailable", "field": "block_confidence",
                  "message": "PyMuPDF is a deterministic parser; per-element confidence does not exist" } ] }
```

That page has 21 blocks; one is shown. The listing omits `backend_raw` and `channel_provenance`,
which [`src/openreading/schemas/README.md`](src/openreading/schemas/README.md) describes. Every
backend returns this shape. When a backend cannot produce a field, OpenReading leaves it out and
names it in `warnings[]`. It never invents one, because a made-up confidence looks like a measured
one, and confidence is exactly the field PyMuPDF is warning about here.

Now read the same page the other way. Tesseract ignores the text layer, renders the page to a
150-DPI bitmap and OCRs it, which is the work it would do on a photograph of the same statement:

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

Three differences, and each one is the contract doing its job. The page is 1275×1650 `pixel`
rather than 612×792 `pdf_point`, because that is what Tesseract actually measured. The
`bbox.x/y/w/h` values stay page-relative fractions either way, so code that positions a block works
against both. Every block carries a `confidence`, and no `confidence_unavailable` warning appears,
because Tesseract genuinely measures one per word. And OCR misread `Account Holder:` as
`; Account Hotder-` with `confidence: 0.0`, so it told you where it was unsure.

The generated document the subsystem guides use is a different file, with tables, columns and an
image. Build it whenever a guide asks for `sample.pdf`:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
ls -l sample.pdf     # 8688 bytes: a title, two paragraphs, a 3×4 table, two columns, a tiny image
```

## Compare, route, strategy

**Compare.** Compare tells you which backend read a document better and what the other missed.
You have both readings of the January statement already; now ask what differs between them:

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
…
④ GRANULARITY
   pymupdf 21 blocks  ·  tesseract 26 blocks
```

The two agree on 98% of the page and disagree on one line, which the report names on both sides
instead of handing you a score to go investigate. Compare does not know which backend is right,
so it does not claim to, and you can still see at a glance that the OCR line is the mangled one. The
one-word headline for this pair is `equivalent`, because a single misread label is not enough to
call one backend better; `--format diffs` is where the disagreement itself lives.

**Route.** Route hands a sensitive document only to backends that meet your policy. A bank
statement is the everyday case: it names a person, an account and every place they spent money,
and plenty of teams may not ship one to an arbitrary vendor. `require_baa` keeps out every vendor
that does not publish a BAA, the HIPAA contract a vendor signs before handling regulated data.
`no_train_on_data` refuses vendors that train on what you send:

```bash
echo '{"require_baa": true, "no_train_on_data": true}' > phi.json
uv run openreading route examples/john_smith_1000_2026_01.pdf --policy phi.json
```
```json
{ "chosen": "pymupdf",
  "fallbacks": ["docling", "azure-document-intelligence", "google-document-ai", "tesseract", "qwen-vl", "anthropic-claude"],
  "dropped": { "reducto": { "stage": 1, "code": "no_baa", "reason": "require_baa set but hipaa_baa='tier_gated' and 'reducto' is not in baa_tier_confirmed" },
               "…": "5 more" },
  "terminal_reason": null }
```

Reducto is dropped because its BAA is offered only on some tiers and none is confirmed here. The
three hosted vendors that survive are there because each one publishes a BAA, which its descriptor
records. That is a vendor's advertised offer read on a date, not an agreement you hold, so
`require_baa` narrows the field without finishing the job. Confirm your own signed paperwork before
real data moves, and see [the catalog](src/openreading/adapters/README.md#catalog) for where each
claim came from.

The `fallbacks` list is the order OpenReading tries next if `pymupdf` fails. A dropped backend
never joins that list, because a fallback that readmits it would leak the statement silently. Your
policy file is the only thing that sets the eligible set, and three of its keys widen that set on
purpose, which [Routing and keys](src/openreading/router/README.md#how-it-decides) names. A key the
router does not recognise is refused rather than ignored, so a typo cannot leave you with a clean
exit code and no filter.

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
  root.steps[0]    pymupdf      succeeded                     41ms  $0
      scanned_pages_detected     obs=False thr=True  ok
      garbled                    obs=0.0189 thr=True  ok
      empty_pages_over           obs=0.0 thr=0.2  ok
      confidence_below           obs=None thr=0.6  skipped
```

The trace shows PyMuPDF ran, three gates passed, and the run stopped there, with no second backend
and no cost. The fourth gate is `skipped` rather than failed: PyMuPDF reports no confidence, so
there is nothing to test, and a missing measurement never counts as a passing one. Timings vary
between machines. Write your own strategy with `uv run openreading strategy --help`. An interrupted
run resumes from its journal, which records every step so far. The
[run ledger](src/openreading/ledger/README.md) guide explains it.

**A folder at a time.** Point `parse` at a directory and it batches, which is how you run a whole
corpus rather than one file:

```bash
uv run openreading parse examples/ --backend pymupdf --jobs 2 > batch.json
```

`batch.json` carries one entry per file under `items[]`, each with the source's path and SHA-256,
plus the `summary` that tells you at a glance whether the sweep went as expected:

```json
{ "total": 3, "succeeded": 2, "failed": 0, "skipped": 1,
  "pages_processed": 2, "cost_bases": ["infra_only"], "backends": { "pymupdf": 2 } }
```

Three, because `examples/README.md` is in that folder too. It is skipped with
`skip_reason: "unsupported_format"` rather than dropped in silence, so the count you get back
always accounts for every file you pointed at. `scripts/batch_demo.sh path/to/docs` runs the same
sweep with both local backends and compares the two corpora.

`parse` takes no `--policy`, so the command above runs with no compliance filter in force. A
corpus reaches the router's policy gate two other ways: a `policy:` block in `openreading.yaml`
under `parse <dir> --strategy <name> --config openreading.yaml`, or
`openreading.run_batch(paths, policy={…})` from Python.
[Routing and keys](src/openreading/router/README.md#recipes) runs both.

## Bring your own key

A hosted backend works as soon as its vendor key is in `.env`. Without one, the command exits with
code 3 and names the variable:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend reducto
# [reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
```

To fix it, run `echo 'REDUCTO_API_KEY=sk_…' > .env` rather than copying `.env.example`, because
that file pre-fills two localhost endpoints. Then `uv run openreading backends` shows
`reducto … yes`. From then on every Reducto call is billed to your account.

## Python and HTTP

From Python or over HTTP you get the same shapes that the command line prints.

```python
import openreading
doc = "examples/john_smith_1000_2026_01.pdf"
resp = openreading.run(doc, backend="pymupdf")                    # dict
print(resp["status"]["state"], resp["backend"]["id"])            # succeeded pymupdf
plan = openreading.route(doc, policy={"require_baa": True, "no_train_on_data": True})
print(plan.eligible_ids[0], plan.dropped["reducto"].code)        # pymupdf no_baa
delta = openreading.compare([resp, openreading.run(doc, backend="tesseract")])
print(delta["headline"]["verdict"])                              # equivalent
```
```bash
uv run openreading serve         # one terminal; listens on http://127.0.0.1:8787 ([server] extra, included above)
# in another terminal, from the same clone:
curl -s -X POST http://127.0.0.1:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/examples/john_smith_1000_2026_01.pdf"}, "backend": {"id": "pymupdf"}}' | head -c 80
# {"schema_version":"0.3","status":{"state":"succeeded"},"backend":{"id":"pymupdf"
curl -s http://127.0.0.1:8787/healthz     # {"status":"ok","version":"0.3.0"}
```

## Status and versioning

**Status: pre-release.** You can build on the JSON shape today, while CLI flags, the Python API
and the strategy grammar may still change before 1.0. The JSON Schemas are stable, pinned byte
for byte by `tests/test_schema_evolution.py`. The package version is `0.3.0` in `pyproject.toml`
and `openreading.__version__`. It is not on PyPI and has no git tags yet, so install from a
clone. [`CHANGELOG.md`](CHANGELOG.md) records development milestones. Its newest heading,
`0.4.0`, is a milestone label, not a published release. The two numbers meet when the first
release is tagged.

## Where the docs are

Because each package directory carries one guide, every question below leads to one guide or one
command.

| You want to know… | Run / open |
|---|---|
| **the full documentation, every guide, and how an agent uses it** | [`src/openreading/README.md`](src/openreading/README.md), then `uv run openreading --help` and `uv run openreading <cmd> --help` for every flag |
| what the shipped example documents contain and where they came from | [`examples/README.md`](examples/README.md) |
| each backend's variables and compliance posture, and the env-var precedence rules | [`src/openreading/adapters/README.md`](src/openreading/adapters/README.md), then `uv run python -m pydoc openreading.credentials` |
| the exact JSON shapes (the contract) | [`src/openreading/schemas/README.md`](src/openreading/schemas/README.md), then the `*.json` files beside it |
| strategies · compare · routing and keys · batch · the ledger · the server · evals · the CLI · the channel contract | [Strategies](src/openreading/strategies/README.md) · [Compare](src/openreading/comparison/README.md) · [Routing and keys](src/openreading/router/README.md) · [Batch runs](src/openreading/batch/README.md) · [The run ledger](src/openreading/ledger/README.md) · [The HTTP server](src/openreading/server/README.md) · [Evals](src/openreading/evals/README.md) · [The command line](src/openreading/cli/README.md) · [The channel contract](src/openreading/derive/README.md). The docs home links them all. |
| the Python API, every reference section, and how to add a backend | `uv run python -m pydoc openreading`, then the same command with `.<module>` appended. For a new backend, `uv run python -m pydoc openreading.adapters`, then `scripts/new_adapter.py` |
| the test gate | `make verify` runs lint, types, pytest at a 91% coverage floor, and the schema and smoke checks. `uv run pytest -m "not live" --collect-only` prints the offline test count. |

## Contributing · Security · License

[`CONTRIBUTING.md`](CONTRIBUTING.md) · [`SECURITY.md`](SECURITY.md) · Apache-2.0
([`LICENSE`](LICENSE)). The `[pymupdf]` extra is the only AGPL-3.0 component, kept separate so
that you can leave it out.
