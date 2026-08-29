# OpenReading

[![CI](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-%E2%89%A591%25-brightgreen)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

OpenReading turns PDFs, scans and images into one predictable JSON, using whichever parser you
point it at — a local library, a hosted OCR API, or your own model.

## What it does

In: a PDF, an image, or an office document. Out: one JSON with `document.text`, `document.markdown`,
`document.pages[].blocks[]` (page-relative coordinates) and optional `typed_fields`. A backend is
the parser doing the work. The JSON is the same for every backend. Three verbs:

- **parse**: one document or a folder, one backend, one JSON.
- **compare**: what two backends' outputs differ on.
- **route**: pick a backend under a compliance policy.

OpenReading is for developers who need text or structure from many sources without one integration
per vendor. It is not a hosted service, a UI, or a model: you bring a local backend or a vendor key.

## Install

You need `git`, the `tesseract` binary (`brew install tesseract` / `apt install tesseract-ocr`), and
either [`uv`](https://docs.astral.sh/uv/), which fetches Python 3.11+ itself, or Python 3.11+ with `pip`.
No API keys. Nothing is on PyPI yet, so a plain `pip install` of the package fails. Install from the clone.

```bash
git clone https://github.com/multiversal-ventures/openreading-core
cd openreading-core
uv sync --all-extras --dev       # every backend extra + the dev tools (same as `make sync`)
uv run openreading backends      # one row per backend: pymupdf and tesseract yes; hosted backends no, most naming the var they need
```

Without uv, run `python3 -m venv .venv && .venv/bin/pip install -e '.[pymupdf,tesseract,server]'`.
Then type `.venv/bin/openreading …` wherever this page says `uv run openreading …`. In that venv,
`backends` lists a missing *extra* under MISSING (`httpx (pip install …)`) rather than a variable.

## Your first parse

If the `tesseract` row says `no` with `tesseract binary` under MISSING, install the binary now.
Generate a two-page sample document (or `cp ~/some.pdf sample.pdf`), then parse it:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
ls -l sample.pdf     # 8688 bytes: a title, two paragraphs, a 3×4 table, two columns, a tiny image
uv run openreading parse sample.pdf --backend pymupdf > pymupdf.json
```

You should see one stderr line, `Consider using the pymupdf_layout package …`. That is PyMuPDF's
advice, not an error. Here is `pymupdf.json`, trimmed to the keys you will read first (numbers rounded):

```json
{ "schema_version": "0.3", "status": { "state": "succeeded" },
  "backend": { "id": "pymupdf", "type": "oss_library", "output_paradigm": ["block_tree"] },
  "document": { "page_count": 2,
    "text": "OpenReading Test Document\nThis is the first paragraph rendered at twelve points. …",
    "markdown": "# OpenReading Test Document\n\n…| Region | Units | Revenue |\n| --- | --- | --- |\n| North | 120 | 4400 |…",
    "pages": [ { "page_number": 1, "width": 612.0, "height": 792.0, "unit": "pdf_point",
      "blocks": [ { "type": "title", "text": "OpenReading Test Document", "reading_order": 0,
        "bbox": { "x": 0.1176, "y": 0.0739, "w": 0.4323, "h": 0.0347, "page": 1,
                  "bbox_native": { "coords": [72.0, 58.5, 336.56, 85.98], "origin": "top_left", "unit": "pdf_point" } } } ] } ] },
  "warnings": [ { "code": "confidence_unavailable", "field": "block_confidence",
                  "message": "PyMuPDF is a deterministic parser; per-element confidence does not exist" } ] }
```

`usage`, `backend_raw` and `channel_provenance` are omitted here. [`src/openreading/schemas/README.md`](src/openreading/schemas/README.md) describes them.
Check: `grep -c confidence_unavailable pymupdf.json` prints `1`. Every backend returns this shape. A field
a backend cannot produce is left out and named in `warnings[]`, never invented (`uv run python -m pydoc openreading`).

## Compare, route, strategy

**Compare.** Parse the same file with OCR, then diff the two outputs. Abbreviated, you should see:

```bash
uv run openreading parse sample.pdf --backend tesseract > tesseract.json
uv run openreading compare sample.pdf --backends pymupdf,tesseract --format diffs
```
```
DIFF — pymupdf vs tesseract   (2 page(s))
① CONTENT — real text/values either side missed
   ✔ EQUIVALENT   content shared by all: 1.00  ·  0 real misses
② TABLES — 1 table(s)
   table counts differ: {'pymupdf': 1, 'tesseract': 0}
…
```

Both got every word. OCR lost the table's structure (`uv run python -m pydoc openreading.comparison`).
If you see `[tesseract] tesseract failed: tesseract is not installed…` (exit 3), install the binary.

**Route.** A compliance policy is a JSON file of requirements a backend must meet. `require_baa`
demands a BAA, the HIPAA contract a vendor signs before handling health data. Abbreviated, you should see:

```bash
echo '{"require_baa": true, "no_train_on_data": true}' > phi.json
uv run openreading route sample.pdf --policy phi.json
```
```json
{ "chosen": "pymupdf",
  "fallbacks": ["docling", "azure-document-intelligence", "google-document-ai", "tesseract", "qwen-vl", "anthropic-claude"],
  "dropped": { "reducto": { "stage": 1, "code": "no_baa", "reason": "require_baa set but hipaa_baa='tier_gated' and 'reducto' is not in baa_tier_confirmed" },
               "…": "5 more" },
  "terminal_reason": null }
```

A backend dropped here never comes back through a fallback (`uv run python -m pydoc openreading.router.compliance`).

**Strategy.** A strategy is a named plan that picks backends and checks their output. You should see (timings vary):

```bash
uv run openreading parse sample.pdf --strategy offline_first > strat.json
uv run openreading explain strat.json
```
```
strategy offline_first  →  pymupdf (ok)
  root.steps[0]    pymupdf      succeeded                     66ms  $0
      scanned_pages_detected     obs=False thr=True  ok
      …
```

Presets: `cost_saver`, `fast`, `max_accuracy`, `offline_first`. Write your own with `uv run openreading strategy --help`.

## Bring your own key

Hosted backends need a key from the vendor. Without one, the exit code is 3, not a crash:

```bash
uv run openreading parse sample.pdf --backend reducto
# [reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
```

Fix: `echo 'REDUCTO_API_KEY=sk_…' > .env`, and only that line. Do not copy `.env.example` whole: it
pre-fills two localhost endpoints. Then `uv run openreading backends` shows `reducto … yes`, and
calls are billed to your account. Every backend's variables: [`src/openreading/adapters/README.md`](src/openreading/adapters/README.md).

## Python and HTTP

```python
import openreading
resp = openreading.run("sample.pdf", backend="pymupdf")          # dict
print(resp["status"]["state"], resp["backend"]["id"])            # succeeded pymupdf
plan = openreading.route("sample.pdf", policy={"require_baa": True, "no_train_on_data": True})
print(plan.eligible_ids[0], plan.dropped["reducto"].code)        # pymupdf no_baa
delta = openreading.compare([resp, openreading.run("sample.pdf", backend="tesseract")])
print(delta["headline"]["verdict"])                              # mixed
```
```bash
uv run openreading serve         # one terminal; listens on http://127.0.0.1:8787 ([server] extra, included above)
curl -s -X POST http://127.0.0.1:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "pymupdf"}}' | head -c 80   # another
# {"schema_version":"0.3","status":{"state":"succeeded"},"backend":{"id":"pymupdf"
curl -s http://127.0.0.1:8787/healthz     # {"status":"ok","version":"0.3.0"}
```

## Status and versioning

**Status: pre-release.** The package version is `0.3.0` (`pyproject.toml`, `openreading.__version__`). It is not on
PyPI and this repository has no git tags yet, so install from a clone as shown above. `CHANGELOG.md` records
development milestones. Its newest numbered heading, `0.4.0`, is a milestone label, not a published release. The two numbers
are reconciled when the first release is tagged. Stable now: the vendored JSON Schemas are frozen once cut
(`tests/test_schema_evolution.py` pins them byte for byte). May still change before 1.0: CLI flags, the Python API,
the strategy grammar.

## Where the docs are

| You want to know… | Run / open |
|---|---|
| every command and flag | `uv run openreading --help`, `uv run openreading <cmd> --help` (`parse` needs exactly one of `--backend` / `--strategy` / `--no-strategy`) |
| which backends are ready on this machine | `uv run openreading backends` |
| each backend's variables and compliance posture; the env-var precedence rules | [`src/openreading/adapters/README.md`](src/openreading/adapters/README.md); `uv run python -m pydoc openreading.credentials` |
| the exact JSON shapes (the contract) | [`src/openreading/schemas/README.md`](src/openreading/schemas/README.md), then the `*.json` files beside it |
| the Python API; strategies, compare, batch, routing in depth | `uv run python -m pydoc openreading`, then `.strategies` · `.comparison` · `.batch` · `.router.compliance` |
| how to add a backend | `uv run python -m pydoc openreading.adapters`, then `scripts/new_adapter.py` |
| the test gate | `make verify`: lint, types, pytest at a 91% coverage floor, schema and smoke checks; test count: `uv run pytest -m "not live" --collect-only` |
| what changed | [`CHANGELOG.md`](CHANGELOG.md) |

## Contributing · Security · License

[`CONTRIBUTING.md`](CONTRIBUTING.md) · [`SECURITY.md`](SECURITY.md) · Apache-2.0 ([`LICENSE`](LICENSE)). Only the `[pymupdf]` extra is AGPL-3.0, isolated in its own extra.
