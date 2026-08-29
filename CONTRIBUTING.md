# Contributing to OpenReading

Thanks for your interest. This covers how to get a working environment, what the review bar
is, and the conventions specific to this codebase. Agents and humans follow the same rules:
[`AGENTS.md`](AGENTS.md) is the authoritative version of everything below.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting set up

Prerequisites: [uv](https://docs.astral.sh/uv/). Optional: the system `tesseract` binary for the
OCR backend (`brew install tesseract` / `apt install tesseract-ocr`).

```console
$ make sync                 # uv sync --all-extras --dev — every extra, every dev tool
$ uv run lefthook install   # one-time: pre-commit (ruff + docs policy), pre-push (make verify)
$ make verify               # the offline gate; no keys, no network
```

A venv synced without `--all-extras` is missing `uvicorn`, `boto3`, … and hosted/server paths
fail with `No module named …`. If you see that, `make sync`.

## Before you open a pull request

```console
$ make verify        # ruff + pyright + pytest (coverage floor) + schema/extras checks + smokes
$ make serve-smoke   # boots the real HTTP server on a socket; not part of verify
$ make verify-live   # only if you touched a hosted adapter and have its keys; skips otherwise
```

CI runs `make verify` on Python 3.11–3.14. Catching it locally is cheaper. The coverage floor is
`--cov-fail-under` in the Makefile. A test pins the README badge to it. The offline test count is
whatever `uv run pytest -m "not live" --collect-only` prints. The runbook is the docstring of
`tests/conftest.py`. It covers every surface, both lanes (offline and live), and the "is a new
backend tested well?" checklist.

## Conventions worth knowing

**Documentation lives in code.** Module docstrings, not markdown. If your change adds a
contract, an env var, an exit code, or a non-obvious decision, it goes in the docstring of the
module that owns it — with the failure the decision avoids. `tests/test_docs_policy.py` rejects
new markdown files. Ruff `D100`/`D104` reject modules without a docstring. The table under
[*Where a change gets documented*](AGENTS.md#where-a-change-gets-documented) in `AGENTS.md` says
which docstring, README, or `CHANGELOG.md` line each kind of change must touch, and which test
catches forgetting it.

**Tests come first.** New behaviour arrives with a test that fails before your change and
passes after. Hosted backends are tested against respx fixtures and injected faults; never add a
test that needs a key or the network to `make verify` — that is the `live` marker's job.

**Compliance is never relaxed.** Nothing you add to a strategy, route, or fallback may widen
the set of backends a compliance policy admits. Unverified compliance fails closed.

**Comment the why, not the what.** Keep rationale comments you find; update them if your change
makes them wrong.

**Adding a backend:** the runbook is the docstring of `openreading.adapters`
(`src/openreading/adapters/__init__.py`); `scripts/new_adapter.py` scaffolds the files.

## Commit messages and pull requests

Commits follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
`docs:`, `refactor:`, `test:`, `build:`, `ci:`. Subject under ~70 characters; body explains
*why*. Pull requests describe the problem before the solution and name any alternative you
rejected — that is usually the most valuable part of the review.

## Reporting bugs

Open an issue with what you expected, what happened, and the smallest input that reproduces it
(a document, a command, a policy file). Security vulnerabilities are the exception: follow
[`SECURITY.md`](SECURITY.md) instead of filing a public issue.

## Licensing of contributions

Unless you state otherwise, contributions are licensed under Apache-2.0, matching the project.
