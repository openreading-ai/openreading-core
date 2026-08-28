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
record, a research pack, a run log — it goes to the private `openreading-internal` repo (see
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

## Golden rules

- **`make verify` green is the finish line.** Lint + typecheck + tests at a 91% coverage floor
  (`uv run pytest -m "not live" --collect-only -q` for the offline count) + schema-validate +
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
