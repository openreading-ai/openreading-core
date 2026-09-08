"""pytest configuration, and the testing runbook for every surface and both lanes.

The only global hook: load `.env` for the live lane so `make verify-live` (and any `-m live` run)
sees the caller's real keys without a manual export. GUARDED to the live marker — the offline
suite (`make verify`, which runs `-m "not live"`) NEVER loads real credentials, so it can never
accidentally hit the network.

The one sentence: `make verify` is green on every surface offline; the live lane is the ONLY
thing that proves a hosted backend against its real API, and it needs that backend's keys.

Step zero — `make sync` (`uv sync --all-extras --dev`)
======================================================
`--all-extras` is not optional. A venv synced without it silently lacks the `server` extra
(`uvicorn`), `boto3`, the Google protos, `jiter`, ...; the failure only surfaces at the surface
that needs them (`openreading serve` / `make serve-smoke` dies with `No module named uvicorn`).
An import error from a server or hosted-backend command means: run `make sync` before debugging.
Optional system dep: the `tesseract` binary (`brew install tesseract` / `apt install
tesseract-ocr`) for the OCR backend; its tests skip cleanly without it. Local-library adapters'
runtime deps (pymupdf, pytesseract, ...) live in the `dev` dependency group so tests can EXECUTE
them, never in core `dependencies` (`internal/decisions/DECISIONS.md` D9: the AGPL pymupdf is in
dev and in its own extra, never in core. The user install path is the per-adapter extra).

Lane 1 — the offline gate, `make verify`
========================================
The bar for every commit. No credentials, no network. Sub-targets:

  lint              `ruff check` + `ruff format --check`
  typecheck         pyright primary, mypy fallback (DECISIONS D1). Invoked as `python -m pyright`
                    / `python -m mypy`, not the console-script shim: a relocated or copied .venv
                    can carry shims whose shebang points at another interpreter and fails to
                    spawn, while the module form works on any venv. `make typecheck-mypy` runs
                    mypy unconditionally, outside `verify`, because the `||` fallback never runs
                    mypy on the green path — mypy drift stayed invisible for months that way.
  test              full pytest suite at the 94% coverage floor (`--cov-fail-under=94`):
                    coverage below the floor fails the build like a red test. Ratchet up, never
                    down. Test counts are never quoted anywhere, because they change with every
                    commit that adds a test. Read the live count with
                    `uv run pytest -m "not live" --collect-only -q`.
  schema-validate   request / response / descriptor / strategy JSON Schemas + every captured
                    fixture (`python -m openreading.schemas validate`). The vendored schemas are
                    the authority; pydantic models in `openreading.types` must round-trip through
                    them (DECISIONS D4 — where they drift, the schema wins).
  extras-parity     `openreading.adapters.registry.BUILTIN_ADAPTERS` vs pyproject's
                    optional-dependencies: an adapter fully tested via the dev group but missing
                    or misspelled as an extra ships nothing to `pip install openreading[<slug>]`
                    users while `verify` stays green.
  smoke             builds the sample PDF, parses it through `pymupdf` via the REAL CLI, asserts
                    schema-valid JSON.
  strategy-smoke    runs a `[pymupdf, tesseract]` cascade, asserts the quality gate fires.
  compare-smoke     pymupdf live vs tesseract (or a fixture); asserts a schema-valid delta report.
  leaderboard-smoke the real `openreading leaderboard` CLI over the sample dataset with two local
                    backends; a missing tesseract binary scores honest errors, never a crash.

Every hosted adapter is exercised here against respx mocks + injected faults, never live. Nine of
the ten adapters that build their own real HTTP client (`reducto`, `nuextract`, `pulse`,
`chunkr`, `open-ocr`, `azure-document-intelligence`, `qwen-vl`, `google-gemini`, `mistral-ocr`)
also have a `test_<slug>_http.py`
that drives the real `_Httpx*Client` class directly under `@respx.mock` (`respx.post(url).mock(
return_value=httpx.Response(...))`), not only the higher-level fake in its main test file —
otherwise the real client has zero coverage anywhere. The tenth, `docling`'s
`_HttpxDoclingClient`, is the standing exception: it carries `# pragma: no cover`, no offline test
references it, and it runs only in the live lane behind `DOCLING_SERVE_URL`
(`tests/test_docling.py::test_live_convert`). The http tests prove OUR normalization and error
mapping; they are NOT proof the provider's live API behaves as the mock claims (see Lane 2).

Targeted runs during development:

  uv run pytest tests/test_open_ocr.py tests/test_open_ocr_faults.py   # one backend + faults
  uv run pytest -m "not live"                                          # offline, no coverage gate
  uv run pytest tests/test_conformance.py                              # every adapter, conformance

`-q` stacking: pyproject's `addopts = "-q"` STACKS with a caller's explicit `-q` (verbosity -2),
the level at which pytest suppresses its own final `=== N passed in Ns ===` line. A missing
summary line after an explicit `-q` is this, not a hang or a crash; do not pass `-q` again when
you need the summary. Neither the Makefile's `test:` target nor the wrapper adds one.

`make test` runs through `scripts/run_test_suite.py`, not bare pytest. It passes the real argv
(`-m "not live" --cov=openreading --cov-report=term-missing --cov-fail-under=94`) straight
through, streaming output live, and adds two things. Before the run it deletes any stale
`.coverage` / `.coverage.*` file, because pytest-cov calls `Coverage.combine()` unconditionally at
the end of every `--cov` run and a leftover data file from a differently-configured run (branch vs
statement schema) makes that combine raise `DataError` -> pytest `INTERNALERROR` (exit 3) after
every test already passed. If a second, genuinely concurrent `pytest --cov` on the same checkout
writes a fresh incompatible file mid-run, the wrapper recognizes that exact exit-3 + INTERNALERROR
+ DataError signature, sweeps again, and retries the whole invocation ONCE. Nothing else is ever
retried: a red test or a floor breach (both exit 1), or any other INTERNALERROR, exits with
pytest's unaltered code. `tests/test_run_test_suite.py` covers the selectivity
(`is_stray_combine_failure`, `clear_stray_coverage_data_files`) and the retry-once flow by
stubbing `run_pytest_once`; the script's own docstring has the exact message signatures.

Outside `verify`, deliberately:
  - `make serve-smoke` opens a localhost socket (server surface, below).
  - `make verify-live` needs keys (Lane 2).

Surface: CLI + Python (local backends `pymupdf` / `tesseract`, no keys)
=======================================================================
  uv run python -c "from openreading.testing.sample_pdf import build_sample_pdf; ..."  # sample
  uv run openreading backends                     # readiness table + the MISSING env vars
  uv run openreading parse sample.pdf --backend pymupdf     # schema-validated JSON on stdout
  uv run openreading parse sample.pdf --backend tesseract   # OCR path; graceful without binary
  uv run openreading route sample.pdf --run       # compliance-first plan, then run the chain
     (openreading.yaml: `version: 1` + `policy: {require_baa: true, no_train_on_data: true}`)

  resp = openreading.run(pdf_bytes, backend="pymupdf", mime_type="application/pdf")  # dict
  assert resp["status"]["state"] == "succeeded"
  plan = openreading.route(pdf_bytes, config={...}, mime_type=...)   # RoutePlan dataclass:
  plan.chosen.descriptor.id; [a.descriptor.id for a in plan.fallbacks]   # chosen, fallbacks,
                                                                          # dropped, terminal_reason

API-shape gotcha: `run` / `route` take a PATH or `bytes` first, not a request dict. Passing a
dict raises a confusing "File name too long" `OSError` (the dict's repr is opened as a path).

Surface: HTTP server (`openreading serve`)
==========================================
Same request/response schema over HTTP. `tests/test_server.py` (in `verify`) drives the ASGI app
through FastAPI's `TestClient` — no socket. `make serve-smoke` boots uvicorn on an ephemeral port,
POSTs the sample PDF through `/v1/parse`, asserts schema-valid, shuts down; run it explicitly.
Manual: `uv run openreading serve &` binds 127.0.0.1:8787; `GET /healthz`; `GET /v1/backends`
(readiness, same data as the CLI); `POST /v1/parse` with content-type application/json and body
`{"document": {"bytes_base64": ..., "mime_type": "application/pdf"}, "backend": {"id": "pymupdf"}}`.
The server reads credentials from its OWN process environment (or `--env-file`), never from a
request body, and strategy config from `OPENREADING_CONFIG` (not a flag). Caller auth is opt-in
and OFF by default: with `OPENREADING_API_KEYS` unset every endpoint is open, so keep it on
localhost or behind your own gateway. When set (comma-separated bearer tokens, read once at
startup, never from a request body or a flag), every endpoint except `GET /healthz` and
`POST /v1/webhooks/{backend_id}` requires `Authorization: Bearer <token>` (401 `unauthorized`),
and `OPENREADING_API_KEY_SCOPES` (`token=backend1|backend2`) narrows a token to a backend
allow-list (403 `scope_denied`, raised before any vendor credential is resolved). Endpoint,
env-var and status-code reference: `openreading.server` / `openreading.server.app`.

The web UI is not in this repo: it lives in the private company repo as `openreading_webui`
with its own test suite, and consumes this package through `openreading.api` only.

Lane 2 — the live lane, `make verify-live` (`uv run pytest -m live -rs`)
========================================================================
Every hosted backend's real behavior is proven ONLY here. Each `@pytest.mark.live` test skips
cleanly unless that backend's full credential set is present, so the lane is safe with any subset
of keys; `-rs` prints the exact missing var per skip. This file loads `./.env` only when the run
targets the live marker (see `_live_selected`), so keys arrive automatically here and never in the
offline gate. Gating: `tests/live_helpers.py` `gate_env(slug)` takes the descriptor's explicit
`live_gate_env` (ambient-chain backends whose spec keys are all optional), else the required
credential env names; `skip_unless_creds(slug)` skips naming the missing vars
(`AWS_REGION` | `AWS_DEFAULT_REGION` interchangeable). `run_live` resolves creds, does submit ->
drive -> normalize against the real backend, asserts the response schema-valid, and records a
fixture in record mode. Live coverage: per-backend parse, pulse/nuextract structured extraction,
a real directory batch per keyed backend (`tests/test_batch_live.py`, reducto/pulse), and a local
pymupdf-vs-tesseract corpus run.

Local-service gate caveat: `gate_env` / `skip_unless_creds` only check that a var is SET, never
that its value is valid or reachable. Harmless for a hosted API key (a stale key fails loudly like
any auth error), but `docling` (`DOCLING_SERVE_URL`) and `qwen-vl` (`QWEN_VL_ENDPOINT`) gate on a
LOCAL SERVICE ADDRESS, and `.env.example` ships both pointed at localhost. A `.env` still carrying
either var from an old setup will not skip: `make verify-live` attempts a real connection to
whatever is (or is not) listening. Remove or comment the var unless that service is running.

Keying a hosted backend: append only the credential lines you hold to `.env`, one per line, for
example `echo 'NUEXTRACT_API_KEY=...' >> .env`. Never run `cp .env.example .env`. That file ships
`DOCLING_SERVE_URL` and `QWEN_VL_ENDPOINT` with values, so a copy marks `docling` and `qwen-vl`
configured on a machine where neither is running. Var names are not derived from the slug: the
`open-ocr` backend reads `OPENOCR_API_KEY`, and `azure-document-intelligence`, `aws-textract` and
`google-document-ai` each take a multi-var credential set. `.env.example` and
`uv run openreading backends` list the real names. Confirm `uv run openreading backends` flips the
backend from "no / MISSING ..." to "ready", then run `make verify-live` (billed to your account).
Also drive it through both product
surfaces: `openreading parse sample.pdf --backend <slug>` and a `POST /v1/parse` against
`openreading serve` with `"backend": {"id": "<slug>"}`.

Fixture recording: `OPENREADING_RECORD_FIXTURES=1 make verify-live` ALSO writes each live
response, scrubbed, to `tests/fixtures/<slug>/<name>.json` (`live_helpers.record_if_enabled`; a
no-op otherwise, so it never runs in the offline suite). Scrubbing is
`openreading.testing.scrub_fixture`: secret-looking keys (authorization, *_key, *_token,
*_secret, password, signature, account ids, ...) become `"<scrubbed>"` and signed/presigned URL
query strings are stripped — over-redaction is fine (the offline replay catches a corrupted
fixture), a leaked secret is not. Re-run `make verify` afterward so `schema-validate` proves the
refreshed fixtures still validate and the offline mock stays honest.

Checklist — is a NEW hosted backend tested well?
================================================
Done when ALL are green (the conformance kit and spec tests enforce most of it, so a gap is a
red build, not a silent hole):

  [ ] `tests/test_<slug>.py` — offline happy path against respx mocks (submit -> drive ->
      normalize)
  [ ] `tests/test_<slug>_faults.py` — every error-taxonomy branch (auth, rate-limit, 5xx, bad
      body, timeout)
  [ ] `tests/test_<slug>_http.py` if the adapter builds its own real `_Httpx*Client` (the
      `# pragma: no cover` on the class hides its absence from the coverage floor — `docling` is
      the shipped example of that gap, so the checklist, not `verify`, is what enforces this row)
  [ ] a `@pytest.mark.live` test in `tests/test_<slug>.py` — skips without keys, real call with
      them
  [ ] `tests/test_conformance.py` + `tests/test_descriptor_specs.py` pass — the descriptor is
      honest and the 8 methods conform
  [ ] `make verify` green, including the 94% coverage floor
  [ ] `openreading backends` shows it, with the correct MISSING vars when unkeyed
  [ ] AT LEAST ONCE: a live run with real keys + `OPENREADING_RECORD_FIXTURES=1`, driven through
      BOTH the CLI and the server
  [ ] compare it: `openreading compare doc.pdf --backends <new>,pymupdf` — a real cross-backend
      delta (`openreading.comparison`) is the quickest way to eyeball whether its normalization
      is sane
  [ ] liveness — EITHER the adapter declares `descriptor.liveness` AND implements
      `probe_liveness` (offline test against an injected fake client; one `@pytest.mark.live`
      probe test; deployment states simulated with respx per `tests/test_liveness_http.py`), OR
      it declares no `liveness` block and takes the platform's `configured_unverified`
      inference. A probe that is a billed request, or an offline probe test that opens a socket,
      is a defect either way: nothing reachable from `make verify` may call `probe_liveness` on
      a real adapter (`openreading.liveness`; design rationale in the private context repo,
      internal/design/liveness.md §4, §8).
  [ ] native-batch adapters only (`descriptor.batch.native` truthy): `normalize_many`'s per-item
      mapping step is crash-isolated, not just its vendor-reported-failure branch — a mapping
      crash on item k of n (k > 0) must not lose items 0..k-1's already-mapped results (see
      `tests/test_anthropic_batch.py::
      test_normalize_many_isolates_a_per_item_mapping_crash_and_redacts_it`).

Building the adapter itself: the `openreading.adapters` docstring
(`src/openreading/adapters/__init__.py`) — copy the closest template adapter + its two test files,
fill the descriptor honestly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _live_selected(config: pytest.Config) -> bool:
    """True only when the run explicitly targets the live marker (`-m live`, `-m "live or …"`),
    never for the offline default (`-m "not live"`) or an unmarked run."""
    markexpr = str(config.getoption("markexpr") or "")
    return "live" in markexpr and "not live" not in markexpr


def pytest_configure(config: pytest.Config) -> None:
    # runs once at startup, before collection — so os.environ is populated before any live test's
    # skip_unless_creds() check reads it.
    if _live_selected(config):
        from openreading.credentials import load_dotenv

        load_dotenv(".env")


# --- publisher benchmark corpora ---------------------------------------------------------------
# Both benchmark test modules build these, so they live here rather than in one of them. The
# shapes are the publishers' own, taken from `parse_bench.test_cases.loader`: ParseBench reads a
# JSONL corpus whose `pdf` key is a path relative to the corpus root, and ExtractBench reads a
# sidecar corpus of `<group>/<stem>.pdf` beside `<stem>.test.json`. Building them offline keeps
# the benchmark tests out of the live lane, where a HuggingFace download does not belong.


def jsonl_corpus(root: Path, *, per_category: int = 3) -> Path:
    """A ParseBench-shaped corpus: rules in {category}.jsonl, PDFs at a root-relative path."""
    from openreading.testing.sample_pdf import build_sample_pdf

    pdf = build_sample_pdf()
    expected = {}
    for category in ("chart", "layout", "table", "text_content"):
        rows = []
        for index in range(per_category):
            relative = f"pdfs/{category}/doc{index}.pdf"
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(pdf)
            expected[relative] = f"# {category} {index}"
            # Two rules per document, so grouping by (category, pdf) is exercised: a document
            # asserted twice is still one inference and must be counted once.
            for rule in ("present", "table"):
                rows.append(
                    json.dumps(
                        {
                            "pdf": relative,
                            "page": 1,
                            "category": category,
                            "id": f"{category}-{index}-{rule}",
                            "type": rule,
                            "rule": {},
                        }
                    )
                )
        (root / f"{category}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "expected_markdown.json").write_text(json.dumps(expected), encoding="utf-8")
    return root


def sidecar_corpus(root: Path, *, per_group: int = 2) -> Path:
    """An ExtractBench-shaped corpus: <group>/<stem>.pdf beside <stem>.test.json."""
    from openreading.testing.sample_pdf import build_sample_pdf

    pdf = build_sample_pdf()
    for group in ("short", "medium", "long"):
        (root / group).mkdir(parents=True, exist_ok=True)
        for index in range(per_group):
            (root / group / f"doc{index}.pdf").write_bytes(pdf)
            (root / group / f"doc{index}.test.json").write_text(
                json.dumps({"data_schema": {"type": "object"}}), encoding="utf-8"
            )
    return root
