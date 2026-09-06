# Contributing to OpenReading

You want a change merged into this repository, and this page gets you there. It sets up a
working environment, states what a review checks, and names the conventions specific to this
codebase. [`AGENTS.md`](AGENTS.md) is the longer and authoritative version, and it binds agents
and humans alike.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## What this repository takes

Check the scope before you start, not after a week of work. This repository holds mechanisms,
the parts a competent engineer could rebuild in a week from what is already here. A pull request
that adds a web UI, a labeled dataset, or prompts tuned on a private corpus is closed without
review. A pull request that adds accounts, tenancy, or billing is closed the same way. Adapters
for vendor APIs are welcome, because an adapter runs on your own machine against your own key.
[`AGENTS.md`](AGENTS.md#what-belongs-in-this-repo) carries the full list and the reasoning
behind it.

## Getting set up

You need [uv](https://docs.astral.sh/uv/), and every command below assumes it is on your path.
A backend is the parser that does the reading, such as PyMuPDF on your machine or a vendor's
API. The `tesseract` binary is optional, and it enables the OCR backend. Install it with `brew
install tesseract` on macOS or `apt install tesseract-ocr` on Debian and Ubuntu.

```bash
make sync                 # uv sync --all-extras --dev: every extra, every dev tool
uv run lefthook install   # one-time: pre-commit (ruff + docs policy), pre-push (make verify)
make verify               # the offline gate, no keys, no network
```

A venv synced without `--all-extras` lacks `uvicorn`, `boto3`, and other optional packages. The
backends that call a hosted API, and the local `openreading serve` process, then fail with
`No module named …`. Run `make sync` when you see that error, and it goes away.

## Before you open a pull request

Run these locally before you push, so CI does not tell you something you could have learned in
two minutes.

```bash
make verify        # ruff + pyright + pytest (coverage floor) + schema/extras checks + smokes
make serve-smoke   # boots the real HTTP server on a socket, not part of verify
make verify-live   # only if you touched a hosted adapter and have its keys, skips otherwise
```

CI runs `make verify` on Python 3.11–3.14. A failure you catch locally costs less than one that
comes back from CI. While you work, run a single file with `uv run pytest tests/test_cli.py`, and
save the full gate for the end.

The coverage floor lives in the Makefile as `--cov-fail-under`, and `tests/test_docs_policy.py`
pins the README badge to the same number. Never lower the floor to get a change past the gate.
Raise it as coverage climbs, and raise the badge in the same commit. The offline test count is
whatever `uv run pytest -m "not live" --collect-only` prints.

The testing runbook is the docstring of `tests/conftest.py`. It covers every surface, both lanes
(offline and live), and the "is a new backend tested well?" checklist.

## Changing a dependency

You changed a dependency, so the lockfile changes with it. `uv.lock` is committed to the
repository. CI installs from it with `uv sync --locked`, never re-resolving on the runner. A
lockfile that disagrees with `pyproject.toml` fails the build before a single test runs. Run
`uv lock` and commit the changed `uv.lock` in the same pull request. Dependabot opens its own
bump pull requests every week, so expect to rebase on those.

`pyproject.toml` pins `ruff` to an exact version and `.github/dependabot.yml` tells Dependabot to
leave it alone. `ruff format` changes its output between minor versions. A ruff bump therefore
lands in a commit that reformats the whole tree, by hand and deliberately.

CI also runs `make audit` on every pull request. A published advisory against anything in the
lock fails the build. That step queries an advisory database over the network, so it is not part
of `make verify`. Run `make audit` before you open a pull request that touches dependencies.

## Conventions worth knowing

The conventions below decide most review comments here, so read them once before your first
pull request.

**Documentation lives in code.** A new contract, env var, exit code, or non-obvious decision
goes in the docstring of the module that owns it, not in a markdown file. Write down the failure
that decision avoids, because that failure is invisible from the code it produced.
`tests/test_docs_policy.py` rejects new markdown files, and ruff `D100`/`D104` reject modules
without a docstring. The table under
[Where a change gets documented](AGENTS.md#where-a-change-gets-documented) in `AGENTS.md` names
the docstring, README, or `CHANGELOG.md` line each kind of change must touch. That table also
names the test that catches the change when you forget.

**Tests come first.** New behaviour arrives with a test that fails before your change and passes
after. Hosted backends are tested offline with `respx`, a library that intercepts HTTP calls and
returns canned responses, plus injected faults. A test that needs a key or the network carries
the `live` marker, and `make verify` never runs it.

**Compliance is never relaxed.** Nothing you add to a strategy, route, or fallback may widen the
set of backends a compliance policy admits. Unverified compliance fails closed.

**Comment the why, not the what.** Keep the rationale comments you find, and update one when
your change makes it wrong.

**The CLI docstring is user-facing help.** `openreading help [TOPIC]` prints chapters of the
`openreading.cli` package docstring verbatim, so a sentence you write there is a sentence a user
reads at their prompt. That buys one source of truth and costs three rules, all enforced by
`tests/test_cli_help.py`: keep every line at 79 columns or fewer, because the renderer never
reflows; give each section exactly one topic slug in `openreading.cli.help`; and name no
`internal/` path, because a reader cannot open one. A new subcommand owes a docstring section, a
`TOPICS` row, and an `epilog` carrying examples, the command that consumes its output, its exit
codes, and a pointer onward.

**Adding a backend.** `scripts/new_adapter.py` scaffolds the files, and the adapter runbook is
the docstring of `openreading.adapters` (`src/openreading/adapters/__init__.py`).

## Commit messages and pull requests

Your pull request lands on `main` as a single squash commit, not as the commits you pushed. Its
title becomes that commit's subject, so the title is where the naming rule applies. Write it the
way [Conventional Commits](https://www.conventionalcommits.org/) asks: a type, an optional scope,
a colon, then an imperative description. The types in use here are `feat`, `fix`, `docs`,
`refactor`, `test`, `build`, and `ci`. Keep the subject under about 70 characters and start the
description in lowercase. A branch name in sentence case, such as `Feature/pdf-tables`, is not a
title.

The body says why the change exists, because the subject alone never can. State the problem
first, then the solution, then the alternative you rejected and the reason. That last part is
usually the most valuable thing a reviewer reads. Write the pull request description the same
way, for someone who did not follow the discussion.

## Reporting bugs

Open an issue with what you expected, what happened, and the smallest input that reproduces it
(a document, a command, an `openreading.yaml`). Security vulnerabilities are the exception: follow
[`SECURITY.md`](SECURITY.md) instead of filing a public issue.

## Licensing of contributions

Unless you state otherwise, contributions are licensed under Apache-2.0, matching the project.
