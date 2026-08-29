# AGENTS.md

Instructions for AI agents and humans working in this repository. Keep it short; the specs are
the code.

## What this is

One unified API for document processing: one request shape, one response schema over many
backends (hosted APIs, OSS libraries, self-hosted models). Three surfaces — CLI, Python
(`openreading.run` / `route` / `compare`), HTTP (`openreading serve`) — all speak the vendored
JSON Schemas in `src/openreading/schemas/` (the source of truth). The router never branches on
backend type; adapters self-describe via a static `AdapterDescriptor` + 8 methods.

## The one rule that shapes everything else

**Documentation lives in code.** Module docstrings, type annotations, inline comments at
decision points. No standalone markdown files beyond `README.md` in a directory and the root
project files (this file, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`,
`CHANGELOG.md`). If something needs explaining, explain it where the code is. Keep the
documentation in code and its README consistent, concise, and relevant.

The reason is not tidiness. A separate document that contradicts the code looks authoritative
and is wrong, and nothing forces anyone to notice. A module docstring that contradicts the
module below it is caught in review — and it is the one place an agent editing that module is
guaranteed to read. `tests/test_docs_policy.py` enforces the allowlist; ruff `D100`/`D104`
require every module and package to carry a docstring.

`docs/` is gitignored. Design specs and plans may live there while work is in flight; they are
working documents, not deliverables. When something in one turns out to be a durable fact about
the system, **move it into a module docstring**. When it is history worth keeping — a design
record, a research pack, a run log — it goes to the private `openreading` company repo (see
[the company repo](#the-company-repo)), never committed here.

## What belongs in a module docstring

Each package's `__init__.py` opens with enough context that someone landing on it cold
understands what the package is for and how it fits the whole:

- **What it is responsible for**, in a sentence.
- **The contract** it implements — request/response shapes, invariants, exit codes, env vars it
  reads and what happens when they are unset, status ladders and what each status means.
- **The design decisions that would otherwise look arbitrary.** Write down the failure each
  choice avoids, because that failure is invisible from the code that resulted. Examples this
  repo already carries: compliance is a hard filter never relaxed by fallback (a fallback that
  "helpfully" readmits a non-BAA backend leaks PHI silently); a channel a backend cannot produce
  is omitted with a `warnings[]` entry, never fabricated (a fabricated confidence is
  indistinguishable from a measured one downstream); the router never branches on backend type
  (one `if type == …` and every new backend needs a router change).
- **How it relates to its siblings** — name the module (`openreading.router.compliance`), not a
  markdown file.

Condense, don't transcribe. Narrative, build history, and sprint chatter do not belong.

## Comment conventions

Comment the **why**, never the **what**. A comment is written for someone reading the file two
years from now who has no idea the change ever happened. Do not write comments saying that you
changed something or that the change is correct — those go in the commit message. When you
touch code that carries a rationale comment, **keep it**; if your change makes it wrong, update
it. Ids like `D-v3-17` or `BL-161` in comments are stable references into
`internal/decisions/DECISIONS.md` and `internal/eng-council/`; leave them.

## Where a change gets documented

| New … | Must update | Existing guard |
|---|---|---|
| backend | its `AdapterDescriptor`; a row in `src/openreading/adapters/README.md`; a `# --- <slug> (signup: …) ---` block in `.env.example`; the adapter module docstring | `scripts/check_extras_parity.py`, `tests/test_descriptor_specs.py`, `tests/test_scaffold_sentinel.py` |
| CLI subcommand or flag | argparse `help=` (no `internal/` paths); its section in the `openreading.cli` docstring, exit codes included; the README's docs index only for a new *kind* of question | `tests/test_cli*.py` |
| schema version | copy to `family.vX.(Y+1).json`; the `*_SCHEMA_FILE` constant; a manifest row in `src/openreading/schemas/README.md`; the `openreading.types` default; a `CHANGELOG.md` line | `tests/test_schema_evolution.py` (byte pins), `tests/test_schema_versioning.py`, `tests/test_types_roundtrip.py` |
| strategy key or preset | grammar in `strategies/model.py` (and `plain.py` if Plain); a cookbook entry in `strategies/presets.py`; the `strategy --help` epilog if user-facing | `tests/test_docs_truth.py` (every docstring YAML block validates) |
| env var | one commented line in `.env.example`; the reading module's "Environment … this module reads" section; `credentials.py` only if it is a credential | none yet |
| exit code / HTTP status | the "Exit codes" ladder in the `openreading.cli` docstring; the "HTTP status codes" table in the `openreading.server` docstring | `tests/test_cli.py`, `tests/test_server.py` |
| Python API function or kwarg | "Exports and return shapes" in `api.py`; `__all__`; one recipe line in the `openreading` package docstring | `tests/test_api.py` |
| warning or finding code | the `warnings[]` known-codes list in the `openreading.schemas` docstring (open set); for `compare`, the closed set in the `openreading.comparison` docstring and the `comparison-report` schema enum | closed set: `tests/test_compare_core.py` (reports validate); open set: none |
| something a README example shows | re-run the README from a fresh clone; paste the new output | none yet |
| a Known-gaps line becomes false | delete the line where it lives (a directory README or a package docstring) | reviewer |
| design record / "not built" note | one line in the owning package docstring's Known gaps list; the prose goes to the company repo | `tests/test_docs_policy.py` |
| subsystem guide (`src/openreading/<pkg>/README.md`) | the same eight sections and nav line as the existing guides (the docs home, `src/openreading/README.md`, fixes the Prev/Next order); a row in the docs home map; the root README "Where the docs are" row for that need | `tests/test_docs_policy.py` (one README per directory); link and YAML-fence checks over guides: none yet |
| a CLI verb, flag or Python kwarg that changes what a guide's walkthrough shows | re-run that guide's commands from a fresh clone; paste the new output; bump nothing else | none yet |
| a "Not built yet" line becomes true | delete the line in the guide AND the docstring's "designed, not built" marker, in the same PR | reviewer |
| a new law / invariant in a package docstring (L*, M*, C*) | one line under that guide's "How it decides" naming the failure it avoids; never the full text | reviewer |
| a new package under `src/openreading/` | a docs-home map row (need → guide, or "reference only: pydoc") and a Layout line in this file | ruff `D104` (package docstring) |

A fact is documented where it is read: the flag next to its argparse definition, the var next to its
`os.environ` read, the field next to its schema. The README indexes those places. It never restates
them — and a guide demonstrates, never restates.

## Golden rules

- **`make verify` green is the finish line.** Lint + typecheck + tests at a 91% coverage floor
  (`uv run pytest -m "not live" --collect-only` for the offline count) + schema-validate +
  extras-parity + CLI/strategy/compare/leaderboard smoke. Offline: no keys, no network.
- **`make sync` before anything else** — `uv sync --all-extras --dev`. A venv synced without
  `--all-extras` silently lacks `uvicorn`, `boto3`, … and the server/hosted paths die with
  `No module named …`. If you see that error, sync first.
- **Fixtures over live calls.** Hosted backends are tested offline against respx mocks +
  injected faults. Real API behaviour is proven only in the keyed live lane
  (`make verify-live`), which skips cleanly without keys. Never add a network-dependent test to
  `make verify`.
- **Compliance is never relaxed by fallback.** Unverified compliance fails closed. No
  strategy/route construct may widen the compliance-eligible set.
- **Schemas are the contract.** Change `src/openreading/schemas/*.json` deliberately; the
  pydantic models in `openreading.types` mirror them and are round-trip tested.
- **Evals are a benchmark harness.** `openreading.evals` ships scorers, a runner, a leaderboard,
  and one synthetic sample. Labeled datasets — ground truth over real documents — never land in
  this repo; they live in `internal/data/` and are passed as a path.

## Testing expectations

Write the failing test first. When you fix a bug, prove the test detects it: break the fix,
watch the test fail, restore it. A test written after a fix often passes for reasons unrelated
to the bug. The runbook — every surface, both lanes, the "is a new backend tested well?"
checklist — is the module docstring of `tests/conftest.py`. Adding a backend: the runbook is
the docstring of `openreading.adapters`; `scripts/new_adapter.py` scaffolds it.

## Layout

```
src/openreading/
  schemas/     vendored JSON Schemas + validator              (docstring: the contract, versions)
  types/       pydantic models + control-plane dataclasses
  adapters/    one package per backend + registry              (docstring: adding a backend)
  router/      driver + 3-stage compliance-first router
  strategies/  optional openreading.yaml orchestration         (docstrings: grammar in model.py,
               execution in engine.py, signals, decider, cookbook in presets.py, Plain in plain.py)
  comparison/  cross-backend delta, pure over responses        (docstring: the semantics)
  batch/       intake resolution + platform runner
  derive/      shared normalization layer                      (docstring: channel contract C1–C11)
  ledger/      execution journal, replay, resume
  evals/       benchmark harness (scorers, runner, leaderboard)
  cli/         `openreading …`                                 (docstring: subcommands, exit codes)
  server/      `openreading serve`                             (docstring: endpoints, status codes)
  credentials.py  BYO-key broker                               (docstring: precedence, per-backend vars)
tests/         offline suite + keyed live lane                 (conftest.py docstring: the runbook)
scripts/       smoke runners, extras-parity, adapter scaffold
```

Reading a package's documentation: `uv run python -c "import openreading.cli; help(openreading.cli)"`,
or open the file. The CLI documents itself with `openreading --help`.

## The company repo

The private `multiversal-ventures/openreading` repo checks this repo out as `core/` and holds
everything *about* the engine that is not part of it: research packs, design specs, the
engineering-council process, product intent, run logs, the decision log, labeled data — and the
company's own packages that depend on this one, starting with the web UI (`openreading_webui`).
`internal/<path>` in docstrings and comments means that repo's top level
(`internal/design/ledger.md` → `openreading/design/ledger.md`). Read it for context; never copy
from it; never import from it — the dependency arrow is company → core, only.

## What belongs in this repo

This is the open-core engine. The test for a new feature: **could a competent engineer rebuild
it in a week from this repo?** Then it is a mechanism and it belongs here — mechanisms earn
adoption. What gets better only with private data, tuning, or hosting belongs in the company
repo, which depends on this one (never the reverse: nothing here may import, call, or assume
anything outside this package).

Here — the whole 3×3 grid (parse / compare / strategy × CLI / API / agent): the contract
(schemas, types, geometry), every adapter and the compliance-first router, the credential broker,
strategies (grammar, engine, signals, calibrate, explain, replay, decision points), compare,
batch, the ledger, `derive/`, the conformance kit, the thin JSON server, and the benchmark
**harness** with one synthetic case.

Also here when built: the agent surface (`openreading mcp` tools, `triage`); the decider wire
executor — the real LLM call behind `DeciderPort` with the caller's key, a generic prompt, and
offline replay; the intent schema and its routing mechanics; the translation stage and its profile
grammar; every new backend, signal, verdict, or channel.

Not here, ever: a web UI, labeled datasets (`tests/test_evals_benchmark_only.py` fails if one
lands in `evals/`), tuned prompts or calibration derived from private corpora, curated intent or
profile catalogs, anything hosted, multi-tenant, billed, or behind an account. A PR that adds one
of those is closed, not reviewed.

## Things not to do

- Do not add a markdown file outside the allowlist. Put it in a module docstring or in the
  company repo. `tests/test_docs_policy.py` fails otherwise.
- Do not commit run scratch (`GOAL*.md`, `PROGRESS.md`, prompts) at the root. That is the
  company repo's `runs/`.
- Do not widen the compliance-eligible set from a strategy, route, or fallback.
- Do not add a test that needs a key or the network to `make verify`.
- Do not lower `--cov-fail-under`. Raise it as coverage climbs, and the README badge with it.
- Do not run `uv sync` in a venv another account owns on a shared checkout; ask the owner to
  run `make sync`.

## Git

- Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `build:`, `ci:`); subject
  under ~70 characters; body says *why*.
- `uv run lefthook install` once per clone: pre-commit runs ruff + the docs policy on staged
  files, pre-push runs `make verify`. Skippable with `--no-verify`; CI on `main` is the real gate.
- Commit at natural boundaries; never commit red.
