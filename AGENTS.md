# AGENTS.md

Instructions for AI agents and humans working in this repository. Keep it short. The specs are
the code.

## What this is

One unified API for document processing: one request shape, one response schema over many
backends (hosted APIs, OSS libraries, self-hosted models). Three surfaces speak the vendored
JSON Schemas in `src/openreading/schemas/`, which are the source of truth: the CLI, Python
(`openreading.run` / `route` / `compare`), and HTTP (`openreading serve`). The router never
branches on backend type. Adapters self-describe with a static `AdapterDescriptor` plus 8
methods.

## The one rule that shapes everything else

**Documentation lives in code.** Module docstrings, type annotations, inline comments at
decision points. No standalone markdown files *describing code that exists*, beyond `README.md`
in a directory, and the root project files (this file, `CONTRIBUTING.md`,
`SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`). If something needs explaining, explain it
where the code is. Keep the documentation in code and its README consistent, concise, and
relevant.

The reason is not tidiness. A separate document that contradicts the code looks authoritative
and is wrong, and nothing forces anyone to notice. A module docstring that contradicts the
module below it is caught in review. It is the one place an agent editing that module is
guaranteed to read. `tests/test_docs_policy.py` enforces the allowlist. Ruff `D100`/`D104`
require every module and package to carry a docstring.

**Work that is not built yet is the exception.** A design record or product spec for a feature
this repo has not shipped lives in `design/` and `product/specs/`. It is tracked and reviewed
here, in the open, next to the code it proposes to change. That does not weaken the rule above.
The rule exists because a document contradicting the code looks authoritative and is wrong, and
a proposal has no code to contradict. The risk begins the day it ships. When the work lands, its
durable facts move into the module docstrings and **the design file is deleted in the same PR**.
Nothing mechanizes that. A reviewer has to.

The marketing site, OSS pages, and guided tutorial live in `multiversal-ventures/openreading-web`.
The tutorial at `https://openreading.ai/oss-tutorial` runs commands against the documents in this
repository's `examples/` directory. The website validates its tutorial configurations against
core, while `tests/test_docs_truth.py` validates only this repository's guides and module docstrings.
Core never imports the website or requires its checkout. When a demonstrated command changes,
update the walkthrough and run its validation in the web repository.

`docs/` stays gitignored. It holds scratch, working notes and in-flight plans, nothing a reader
depends on. Research packs, run logs and the decision log go to the private `openreading` company
repo (see [the company repo](#the-company-repo)).

## What belongs in a module docstring

Each package's `__init__.py` opens with enough context that someone landing on it cold
understands what the package is for and how it fits the whole:

- **What it is responsible for**, in a sentence.
- **The contract** it implements: request/response shapes, invariants, exit codes, env vars it
  reads and what happens when they are unset, status ladders and what each status means.
- **The design decisions that would otherwise look arbitrary.** Write down the failure each
  choice avoids, because that failure is invisible from the code that resulted. Three this repo
  already carries:
  - Core holds no fact it cannot verify. A compliance filter reading a per-vendor table of BAAs
    and training postures looked authoritative and could not be true, so a stale entry routed a
    document to a backend the operator believed was excluded and the run succeeded.
  - A channel is one named part of the response, such as text, tables, or confidence. A channel a
    backend cannot produce is omitted with a `warnings[]` entry, never fabricated, because a
    fabricated confidence is indistinguishable from a measured one downstream.
  - The router never branches on backend type. Write one `if type == …` and every new backend
    needs a router change.
- **How it relates to its siblings**: name the module (`openreading.router.router`), not a
  markdown file.

Condense, don't transcribe. Narrative, build history, and sprint chatter do not belong.

## Comment conventions

Comment the **why**, never the **what**. A comment is written for someone reading the file two
years from now who has no idea the change ever happened. Do not write comments saying that you
changed something or that the change is correct. Those notes go in the commit message. When you
touch code that carries a rationale comment, **keep it**. If your change makes it wrong, update
it. Ids like `D-v3-17` or `BL-161` in comments are stable references into
`internal/decisions/DECISIONS.md` and `internal/eng-council/`. Leave them in place. An
`internal/` path names a file in the private company repo, not a directory in this tree, as
[The company repo](#the-company-repo) explains.

## Prose rules

These apply to every word this repo ships: markdown, docstrings, comments, argparse `help=`, and
error messages.

- No em dashes. Use a period, a comma, or parentheses. En dashes belong in numeric ranges only.
- One main clause per sentence, 12 to 25 words. Split a sentence that wants a semicolon.
- Say what the reader gets before you say how it works.
- Define a term in a full sentence the first time a page uses it. Backend, channel, gate, verdict,
  journal, descriptor and BAA all need one.
- Give one concrete example per abstract claim.
- Second person for what the reader does, present tense for what the software does. No "we", no
  "simply", no "just", no "note that".
- Never enumerate document types as a fixed list. Say that each backend reads the formats its
  descriptor claims, and link `src/openreading/adapters/README.md`.
- `openreading serve` is a process the reader starts on their own machine. Nothing here describes
  a service someone else runs for them.

## Where a change gets documented

| New … | Must update | Existing guard |
|---|---|---|
| backend | its `AdapterDescriptor`; a row in `src/openreading/adapters/README.md`; a `# --- <slug> (signup: …) ---` block in `.env.example`; the adapter module docstring | `scripts/check_extras_parity.py` (the install extra), `tests/test_descriptor_specs.py` (the descriptor), `tests/test_scaffold_sentinel.py` (leftover scaffold markers). The catalog row and the `.env.example` block: none yet |
| CLI subcommand or flag | argparse `help=` (no `internal/` paths, no `uv run`) and, for a subcommand, an `epilog` of runnable lines carrying Examples / Then / Exits / More; its own underlined section in the `openreading.cli` docstring, exit codes included; for a subcommand, a `TOPICS` row in `openreading.cli.help`; the README's docs index only for a new *kind* of question | `tests/test_cli_help.py` (every subcommand resolves to a topic, every page has a description and a four-part epilog inside one screen, no page names a private path or `uv`), `tests/test_cli*.py` (behaviour and exit codes) |
| `openreading help` topic | one underlined section in the `openreading.cli` docstring, written where the fact is read and wrapped at 79 columns; a `TOPICS` row in `openreading.cli.help` giving the slug, that exact heading, and any reader-side aliases; nothing in a markdown file | `tests/test_cli_help.py` (heading and slug stay a bijection, the index lists every primary slug, rendering stays verbatim, no chapter names a private path, no docstring line exceeds 79 columns) |
| schema version | copy to `family.vX.(Y+1).json`; the `*_SCHEMA_FILE` constant; a manifest row in `src/openreading/schemas/README.md`; the `openreading.types` default; a `CHANGELOG.md` line | `tests/test_schema_evolution.py` (byte pins), `tests/test_schema_versioning.py`, `tests/test_types_roundtrip.py` |
| strategy key or preset | grammar in `strategies/model.py` (and `plain.py` if Plain); a cookbook entry in `strategies/presets.py`; the `strategy --help` epilog if user-facing | `tests/test_docs_truth.py` (config-shaped YAML in the seven `openreading.strategies` modules it lists, nothing outside them) |
| env var | one commented line in `.env.example`; a section headed "Environment variables this module reads" in the module that reads it, created if the module has none; `credentials.py` only if it is a credential | none yet |
| exit code / HTTP status | the "Exit codes" ladder in the `openreading.cli` docstring; the "HTTP status codes" table in the `openreading.server` docstring | `tests/test_cli.py`, `tests/test_server.py` |
| Python API function or kwarg | "Exports and return shapes" in `api.py`; `__all__`; one recipe line in the `openreading` package docstring | `tests/test_api.py` (what the function does). `__all__` and the recipe line: none yet |
| warning or finding code | the `warnings[]` known-codes list in the `openreading.schemas` docstring (open set); for `compare`, the closed set in the `openreading.comparison` docstring and the `comparison-report` schema enum | closed set: `tests/test_compare_core.py` (reports validate); open set: none |
| something a README example shows | re-run the README from a fresh clone; paste the new output | none yet |
| a Known-gaps line becomes false | delete the line where it lives (a directory README or a package docstring) | reviewer |
| design record / "not built" note | the record in `design/` (plus `product/specs/` when it has product intent); one line in the owning package docstring's Known gaps list naming it | `tests/test_docs_policy.py` |
| subsystem guide (`src/openreading/<pkg>/README.md`) | the same eight sections and nav line as the existing guides (the docs home, `src/openreading/README.md`, fixes the Prev/Next order); a row in the docs home map; the root README "Where the docs are" row for that need | `tests/test_docs_policy.py` (one README per directory); link and YAML-fence checks over guides: none yet |
| a CLI verb, flag or Python kwarg that changes what a guide's walkthrough shows | re-run that guide's commands from a fresh clone; paste the new output; bump nothing else | none yet |
| a CLI verb, flag, output or example document that the hosted tutorial shows | update `tutorial/README.md` in `openreading-web`; re-run its commands from a fresh core clone | the web repository's tutorial validation; core never reads that checkout |
| a "Not built yet" line becomes true | delete the line in the guide AND the docstring's "designed, not built" marker, in the same PR | reviewer |
| a new law / invariant in a package docstring (L*, M*, C*) | one line under that guide's "How it decides" naming the failure it avoids; never the full text | reviewer |
| a new package under `src/openreading/` | a docs-home map row (need → guide, or "reference only: pydoc") and a Layout line in this file | ruff `D104` (package docstring) |

A fact is documented where it is read: the flag next to its argparse definition, the var next to its
`os.environ` read, the field next to its schema. The README indexes those places. It never restates
them. A guide demonstrates, never restates.

## Golden rules

- **`make verify` green is the finish line.** Lint + typecheck + tests at a 94% coverage floor
  (`uv run pytest -m "not live" --collect-only` for the offline count) + schema-validate +
  extras-parity + CLI/strategy/compare/leaderboard smoke. Offline: no keys, no network.
- **`make sync` before anything else.** That target runs `uv sync --all-extras --dev`. A venv
  synced without `--all-extras` silently lacks `uvicorn`, `boto3`, … and the server/hosted paths
  die with `No module named …`. If you see that error, sync first.
- **Fixtures over live calls.** Hosted backends are tested offline against respx mocks +
  injected faults. Real API behaviour is proven only in the keyed live lane
  (`make verify-live`), which skips cleanly without keys. Never add a network-dependent test to
  `make verify`.
- **Core holds no fact it cannot verify.** A constraint core cannot check is a constraint core
  must not appear to enforce. `policy.backends` supplies the default chain, while an explicitly
  named backend runs directly. Server API-key scope is the caller authorization boundary. Being
  wrong about a capability costs one round trip, because the
  backend refuses and the chain moves on; being wrong about a vendor claim cost a silent
  exclusion nothing recovered from.
- **Schemas are the contract.** Change `src/openreading/schemas/*.json` deliberately. The
  pydantic models in `openreading.types` mirror them and are round-trip tested.
- **Evals are a benchmark harness.** `openreading.evals` ships scorers, a runner, a leaderboard,
  and one synthetic sample. Labeled datasets are ground truth over real documents. They never
  land in this repo. They live in `internal/data/` and are passed as a path.

## Testing expectations

Write the failing test first. When you fix a bug, prove the test detects it: break the fix,
watch the test fail, restore it. A test written after a fix often passes for reasons unrelated
to the bug. The module docstring of `tests/conftest.py` is the runbook. It covers every surface,
both lanes, and the "is a new backend tested well?" checklist. For a new backend, the runbook is
the docstring of `openreading.adapters`. `scripts/new_adapter.py` scaffolds it.

## Layout

```
src/openreading/
  schemas/     vendored JSON Schemas + validator              (docstring: the contract, versions)
  types/       pydantic models + control-plane dataclasses
  adapters/    one package per backend + registry              (docstring: adding a backend)
  router/      driver + backend resolution (the caller's list, in the caller's order)
  strategies/  optional openreading.yaml orchestration         (docstrings: grammar in model.py,
               execution in engine.py, signals, decider, cookbook in presets.py, Plain in plain.py)
  comparison/  cross-backend delta, pure over responses        (docstring: the semantics)
  batch/       intake resolution + platform runner
  derive/      shared normalization layer                      (docstring: channel contract C1–C11)
  ledger/      execution journal, replay, resume
  evals/       benchmark harness (scorers, runner, leaderboard)
  testing/     conformance kit, sample PDF, fixture scrubber   (docstring: what adapter authors get)
  cli/         `openreading …` + `openreading help`, which serves  (docstring: subcommands,
               that docstring back as the CLI's own manual          exit codes, the manual)
  server/      `openreading serve`                             (docstring: endpoints, status codes)
  api.py          `run` / `route` / `compare` / `resume`       (docstring: exports and return shapes)
  config.py       the one reader of `openreading.yaml`         (docstring: discovery, the nine
                                                                policy keys, the union, P4-P6)
  credentials.py  BYO-key broker                               (docstring: precedence, per-backend vars)
  readiness.py    is a backend runnable here, and which vars are missing
  liveness.py     is a backend answering right now
tests/         offline suite + keyed live lane                 (conftest.py docstring: the runbook)
scripts/       smoke runners, extras-parity, adapter scaffold
examples/      two synthetic bank statements the READMEs parse (README.md: what they are)
```

To read a package's documentation, run `uv run python -m pydoc openreading.cli`, or open the file.

The CLI documents itself three ways, and they are one source. `openreading <cmd> --help` is the
flag page, and its epilog carries examples, the command that consumes its output, the exit codes
it can return, and a pointer onward. `openreading help [TOPIC]` prints one chapter of the manual,
and every chapter is a section of the `openreading.cli` docstring, located by its heading and
printed verbatim. `pydoc` prints that whole docstring in source order. Editing that docstring
therefore edits user-facing help, which is why `tests/test_cli_help.py` holds it to 79 columns and
one slug per heading. A new verb owes a docstring section, a `TOPICS` row and an epilog.

## The company repo

The private `multiversal-ventures/openreading` repo checks this repo out as `core/`. It holds
everything *about* the engine that is not part of it: research packs, design specs, the
engineering-council process, product intent, run logs, the decision log, and labeled data. It
also holds the company's own packages that depend on this one, a web UI among them.
`internal/<path>` in docstrings and comments names a file at that repo's top level, so
`internal/design/ledger.md` is `openreading/design/ledger.md` there. Read it for context. Never
copy from it and never import from it, because the dependency arrow runs company → core only.

## What belongs in this repo

This is the open-core engine. The test for a new feature: **could a competent engineer rebuild
it in a week from this repo?** Then it is a mechanism and it belongs here, because mechanisms
earn adoption. What gets better only with private data, tuning, or hosting belongs in the company
repo, which depends on this one. The reverse never holds, because nothing here may import, call,
or assume anything outside this package.

Here lives the whole 3×3 grid, which is parse, compare and strategy across the CLI, the Python
API and the agent surface. That means the contract (schemas, types, geometry), every adapter and
the router, the credential broker, strategies (grammar, engine, signals,
calibrate, explain, replay, decision points), compare, batch, the ledger, `derive/`, the
conformance kit, the thin JSON server, and the benchmark **harness** with one synthetic case.

These belong here too, once they are built:

- the agent surface (`openreading mcp` tools, `triage`)
- the decider wire executor, which is the real LLM call behind `DeciderPort` with the caller's
  key, a generic prompt, and offline replay
- the intent schema and its routing mechanics
- the translation stage and its profile grammar
- every new backend, signal, verdict, or channel

Not here, ever: a web UI, labeled datasets (`tests/test_evals_benchmark_only.py` fails if one
lands in `evals/`), tuned prompts or calibration derived from private corpora, curated intent or
profile catalogs, anything hosted, multi-tenant, billed, or behind an account. A PR that adds one
of those is closed, not reviewed.

## Things not to do

- Do not add a markdown file outside the allowlist. Documentation of code that exists goes in a
  module docstring. A design record for unbuilt work goes in `design/` or `product/specs/`.
  `tests/test_docs_policy.py` fails otherwise.
- Do not leave a design record in `design/` once its feature ships. Move the durable facts into
  the module docstrings and delete the file in the PR that finishes the work. A stale spec for
  shipped code is exactly the authoritative-and-wrong document the rule exists to prevent.
- Do not commit run scratch (`GOAL*.md`, `PROGRESS.md`, prompts) at the root. That is the
  company repo's `runs/`.
- Do not let `routing.fallback` add to the default chain. It reorders within the resolved set and
  never widens it. Do not reintroduce a node that resolves its own backend: every strategy leaf
  names the backend it runs, which is why a file can be read. Server API-key scope narrows every
  dispatch, including explicitly named strategy leaves.
- Do not add a test that needs a key or the network to `make verify`.
- Do not lower `--cov-fail-under`. Raise it as coverage climbs, and the README badge with it.

## Git

- Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `build:`, `ci:`). Keep the
  subject under ~70 characters. The body says *why*.
- `uv run lefthook install` once per clone: pre-commit runs ruff + the docs policy on staged
  files, pre-push runs `make verify`. Skippable with `--no-verify`. CI on `main` is the real gate.
- Commit at natural boundaries. Never commit red.
