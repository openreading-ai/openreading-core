# OpenReading

[![CI](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/multiversal-ventures/openreading-core/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-%E2%89%A591%25-brightgreen)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

**One unified API for document processing.** One request shape, one response schema over many
backends — hosted APIs, open-source libraries, self-hosted models — so you swap any backend with
zero code change. BYO-key pass-through: keys are read from your environment per request, held
only in memory, and the charge lands on your account. Compliance-aware routing that is never
relaxed by fallback. A fully-local tier (`pymupdf`, `tesseract`) that needs no keys and keeps
PHI on your machine.

Documentation lives in the code: every package's docstring is its reference
(`uv run python -c "import openreading.cli; help(openreading.cli)"`), the CLI explains itself
(`openreading --help`), and `openreading`'s own package docstring is a self-contained briefing
for an LLM or agent using the library. [`AGENTS.md`](AGENTS.md) is the working agreement.

## The 3×3

Three features × three ways in. The product grows by deepening cells, not by sprawling.

|              | **CLI** | **API** (Python · HTTP) | **LLM / agent** |
|--------------|---------|-------------------------|-----------------|
| **parse**    | `openreading parse` — file, directory, glob, URLs | `run()` / `run_batch()` · `POST /v1/parse`, `/v1/batch` | driven from the `openreading` package docstring |
| **compare**  | `openreading compare` — files, or two corpus runs | `compare()` · `POST /v1/compare` | typed verdicts, built to be branched on |
| **strategy** | `parse --strategy`, `strategy validate/plan`, `explain`, `replay`, `calibrate` | `run(strategy="x")` · `strategy:` on every run endpoint | every decision lands in a typed, replayable trace |

*Parse* gets you the envelope — any document in, one schema out, any backend. *Compare* tells
you whom to trust — what a backend missed, on one document or your whole corpus. *Strategy*
encodes the decision — compliance-first routing is its zero-config floor, `openreading.yaml`
its authored form, `calibrate` the feedback loop.

## Status

13 adapters (9 `hosted_api`, 3 `oss_library`, 1 `self_hosted_model`), a `.env`-driven
credential broker, the 3-stage compliance-first router with an executable fallback chain, three
surfaces (CLI, Python, HTTP server), async jobs + webhooks, strategies
(cascade / parallel / route / LLM decision points with offline replay), cross-backend compare
with corpus mode, batch intake, an execution ledger with replay and resume, and a benchmark
harness. `v0.3.0` is tagged; nothing is on PyPI yet. Milestone history:
[`CHANGELOG.md`](CHANGELOG.md).

## Install — bring your own key

```bash
pip install 'openreading[reducto]'            # + any backend extras you need; [all] for everything
cp .env.example .env                            # every supported variable, with signup URLs
echo 'REDUCTO_API_KEY=sk_...' >> .env           # fill only the backends you use

openreading backends                            # which backends are ready, which env vars are missing
openreading parse loan.pdf --backend reducto    # → normalized JSON (a hosted call, billed to your account)
```

Local backends need no keys. Each adapter is an optional extra; copyleft dependencies
(PyMuPDF, AGPL-3.0) are isolated in their own extra, imported lazily, and flagged in the
adapter's descriptor. The core package is Apache-2.0 with zero copyleft deps.
Credential resolution and the per-backend variable table: the docstring of
`openreading.credentials`; every operator knob: `.env.example`.

## Quickstart

```bash
openreading parse doc.pdf --backend pymupdf                 # one backend, one envelope
openreading route doc.pdf --policy phi.json --run           # compliance-first: plan, then execute the chain
openreading compare doc.pdf --backends pymupdf,tesseract,reducto --format diffs   # what actually differs
openreading parse invoices/ --backend pymupdf > runA.json   # a directory → one batch JSON
openreading parse doc.pdf --strategy cheap_first            # openreading.yaml orchestration
openreading serve                                           # HTTP API on 127.0.0.1:8787
```

Every backend returns the same envelope. Abbreviated, from the bundled test PDF through the
in-process `pymupdf` backend:

```json
{
  "schema_version": "0.1",
  "status": "succeeded",
  "backend": { "id": "pymupdf", "type": "oss_library", "output_paradigm": ["block_tree"] },
  "document": {
    "text": "OpenReading Test Document\nThis is the first paragraph…",
    "pages": [ { "page_number": 1, "width": 612.0, "height": 792.0, "unit": "pdf_point",
      "blocks": [ { "type": "title", "text": "OpenReading Test Document",
        "bbox": { "x": 0.1176, "y": 0.0739, "w": 0.4341, "h": 0.0347, "page": 1,
                  "bbox_native": { "coords": [72.0, 58.5, 337.66, 85.98], "origin": "top_left", "unit": "pdf_point" } },
        "reading_order": 0 } ] } ]
  },
  "warnings": [ { "code": "confidence_unavailable", "field": "block_confidence",
                  "message": "PyMuPDF is a deterministic parser; per-element confidence does not exist" } ]
}
```

The `warnings[]` entry is the contract in one line: a field a backend cannot produce is omitted
with an explanation, never fabricated. Geometry is canonical (`[0,1]`, top-left, y-down) with
the raw geometry preserved in `bbox_native`; confidence is `[0,1]` everywhere.

With `phi.json` = `{"require_baa": true, "no_train_on_data": true}`, `route` prints the chosen
backend, the ordered fallback chain, and *why* each dropped backend was dropped — and a backend
dropped at the compliance stage can never reappear later in the chain.

Python is the same contract:

```python
import openreading
resp = openreading.run("doc.pdf", backend="pymupdf")          # one envelope
plan = openreading.route("doc.pdf", policy={"require_baa": True})
delta = openreading.compare([resp, openreading.run("doc.pdf", backend="tesseract")])
```

## Layout

```
src/openreading/
  schemas/      vendored JSON Schemas — the source of truth — + validator   docstring: the contract, versions
  types/        pydantic models mirroring the schemas + control-plane dataclasses
  adapters/     one package per backend + the registry                     docstring: adding a backend
  router/       driver (sync/async/webhook) + 3-stage compliance-first router
  strategies/   optional openreading.yaml orchestration                     docstrings: grammar (model), execution
                                                                            (engine), signals, decider, cookbook (presets)
  comparison/   cross-backend delta, a pure function over responses        docstring: the semantics
  batch/        intake resolution + platform runner — many documents → one envelope
  derive/       shared normalization layer                                  docstring: channel contract C1–C11
  ledger/       execution journal, replay, resume
  evals/        benchmark harness: scorers, runner, leaderboard (labeled data is not here)
  cli/          `openreading …`                                             docstring: subcommands, exit codes
  server/       `openreading serve`                                         docstring: endpoints, status codes
  credentials.py  the BYO-key broker                                        docstring: precedence, per-backend vars
tests/          offline suite + keyed live lane                              conftest.py docstring: the runbook
scripts/        smoke runners, extras-parity check, adapter scaffold
```

## Testing

```bash
make sync            # uv sync --all-extras --dev — step zero
make verify          # the offline gate: ruff + pyright + pytest (91% coverage floor) + schema/extras checks + smokes
make serve-smoke     # boots the real server on a socket, POSTs a document, asserts a schema-valid response
make verify-live     # keyed live tests against real providers; each skips cleanly unless its keys are set
uv run lefthook install   # one-time: pre-commit (ruff + docs policy), pre-push (make verify)
```

`make verify` needs no credentials and no network; it is the gate every commit must pass and
what CI runs on Python 3.11–3.14. The coverage badge above states the enforced floor
(`--cov-fail-under` in the Makefile, pinned by a test); the measured number is in every CI run's
job summary. Offline test count: `uv run pytest -m "not live" --collect-only -q`. The runbook —
every surface, both lanes, the "is a new backend tested well?" checklist — is the docstring of
`tests/conftest.py`.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md). Company-internal
material, the web UI and enterprise packages live in the private `openreading` repo, which
consumes this one as a submodule. Security reports:
[`SECURITY.md`](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
