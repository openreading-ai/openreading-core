.PHONY: verify lint typecheck typecheck-mypy test schema-validate extras-parity smoke strategy-smoke compare-smoke leaderboard-smoke serve-smoke audit verify-live sync clean

# `make verify` is the gate. CI runs it on every pull request and on every push to main.
verify: lint typecheck test schema-validate extras-parity smoke strategy-smoke compare-smoke leaderboard-smoke

# Install every extra and every dev tool into `.venv`, once per clone and again whenever
# `uv.lock` moves. A venv synced without `--all-extras` lacks `uvicorn` and `boto3`, so
# `openreading serve` and the API backends fail with `No module named`.
sync:
	uv sync --all-extras --dev

lint:
	uv run ruff check .
	uv run ruff format --check .

# BL-170: this runs pyright through `python -m`, not the installed console-script `uv run pyright`.
# A relocated or copied .venv leaves the console-script shims carrying a stale shebang, so spawning
# the script fails. The interpreter still resolves the module, so `python -m` keeps working.
typecheck:
	uv run python -m pyright || uv run python -m mypy   # pyright primary, mypy fallback (DECISIONS D1)

# BL-89: `verify` runs mypy only when pyright fails, so a mypy regression has no signal while
# pyright is green. This target runs mypy on its own, so drift is caught before the day the
# fallback is needed. It is not in `verify`, because D1 keeps pyright primary and mypy fallback.
# Run it explicitly, or wire it into CI as a non-blocking step.
typecheck-mypy:
	uv run python -m mypy

# This target runs the offline test suite and enforces the coverage floor. Coverage below
# `--cov-fail-under` fails `make verify` the same way a red test does. Ratchet the floor up as
# coverage climbs, and never lower it to make a build pass. The recipe calls
# `scripts/run_test_suite.py`, which wraps the real `pytest --cov` call for two reasons. First, it
# deletes any stale `.coverage` or `.coverage.*` file before the run starts (BL-61). pytest-cov
# ends every run with a combine() step over the coverage data files on disk. That step fails with
# an INTERNALERROR when a leftover file uses the other coverage schema. This repo measures branch
# coverage, because `pyproject.toml` sets `branch = true` for the whole tree. A statement-only file
# from a differently configured concurrent run is the incompatible one here. Second, the script
# retries once when a concurrent run writes a fresh incompatible file mid-run (BL-69), which the
# sweep alone cannot prevent. The retry fires only on that combine()-time INTERNALERROR signature,
# never on any other failure. The script's own module docstring carries the full mechanism.
test:
	uv run python scripts/run_test_suite.py -m "not live" --cov=openreading --cov-report=term-missing --cov-fail-under=91

schema-validate:
	uv run python -m openreading.schemas validate

# Extras parity gate (BL-158): `openreading.adapters.registry.BUILTIN_ADAPTERS` and
# `pyproject.toml`'s `[project.optional-dependencies]` are two hand-maintained lists of the same
# thing, which adapter needs which install extra. The dev dependency group already installs every
# adapter's runtime dependency for the tests. An adapter missing or misspelled in the extras
# therefore passes `make verify` green, while `pip install openreading[<slug>]` ships nothing for
# a real user. This check reads `pyproject.toml` and imports the registry module, with no network
# and no subprocess, which is why it sits next to schema-validate.
extras-parity:
	uv run python scripts/check_extras_parity.py

# CLI smoke: this builds the deterministic test PDF and parses it through the local PyMuPDF
# backend, using the CLI rather than the Python API. The CLI validates before it prints, so
# schema-valid JSON on stdout is the assertion.
smoke:
	@uv run python -c "import os; from openreading.testing.sample_pdf import build_sample_pdf; open(os.path.join(os.environ.get('TMPDIR','/tmp'),'read_smoke.pdf'),'wb').write(build_sample_pdf())"
	@uv run python -m openreading.cli parse "$${TMPDIR:-/tmp}/read_smoke.pdf" --backend pymupdf --pages 1 \
	  | uv run python -c "import sys,json; d=json.load(sys.stdin); assert d['document']['pages'], 'no pages'; print('smoke: OK. pymupdf parsed the sample PDF into schema-valid JSON (%d blocks)' % len(d['document']['pages'][0].get('blocks',[])))"

# Strategy smoke: this builds the scanned fixture and runs a pymupdf-then-tesseract cascade
# through `parse --strategy`. It asserts that the quality gate fired and that the response is
# schema-valid and carries its orchestration block. It uses local backends only, so it stays green
# with or without the tesseract binary.
strategy-smoke:
	uv run python scripts/strategy_smoke.py

# Compare smoke: this parses the sample PDF through pymupdf, then compares that result against a
# second backend. The second backend is tesseract when the binary is present, and a deterministic
# fixture otherwise. It asserts a schema-valid pairwise report that carries findings. It uses
# local backends only, so it is green on any machine.
compare-smoke:
	uv run python scripts/compare_smoke.py

# Leaderboard smoke (BL-160): this runs the real `openreading leaderboard` CLI entry point, not
# the Python API, over the deterministic sample dataset. It uses two local backends, needs no
# keys, and asserts a schema-valid BenchmarkReport with both backends ranked and a non-empty
# per-case table. A missing tesseract binary is fine, because every tesseract case then scores a
# tallied error rather than crashing. This smoke treats that ranking as legitimate rather than as
# a failure.
leaderboard-smoke:
	uv run python scripts/leaderboard_smoke.py

# Server smoke: this starts the HTTP server on an ephemeral local port and posts the sample PDF to
# /v1/parse. It checks the response against the schema and then shuts the server down. It is not
# part of `verify`, because it opens a localhost socket, so run it explicitly.
serve-smoke:
	uv run python scripts/serve_smoke.py

# Dependency vulnerability audit: this queries the OSV/PyPI advisory database against the
# third-party dependencies in the resolved lock. It is not part of `verify`, because it needs the
# network, and it blocks the build on any hit. A vulnerable lock with no CI signal is how
# PYSEC-2026-3655/3656 (pypdf) and PYSEC-2026-3552 (cryptography) sat unnoticed. Dependabot opens
# a pull request for each one, but it never blocks the green build that ships the lock. The audit
# reads `uv export`'s output rather than the live environment for one reason. pip-audit's
# `--strict` treats any skipped dependency as fatal, and the live environment always skips this
# project itself. uv installs the checkout as an editable package, and PyPI cannot look up an
# unpublished local package. `--no-emit-project` keeps that self-reference out of the exported
# list, so `--strict` fires only on a real third-party finding. An advisory with no released fix
# gets an explicit `--ignore-vuln <ID>` in a commit whose body says why, never a silent skip.
audit:
	uv export --format requirements.txt --all-extras --no-emit-project > "$${TMPDIR:-/tmp}/audit-requirements.txt"
	uv run --with pip-audit pip-audit --strict -r "$${TMPDIR:-/tmp}/audit-requirements.txt"

# Live lane: this runs only the tests marked `@pytest.mark.live`, and nothing else. Each one skips
# cleanly unless its backend's environment keys are set, and `-rs` prints the skip reasons. It is
# never part of the offline `verify` gate, which stays keyless and offline.
verify-live:
	uv run pytest -m live -rs

# Remove the tool caches and build output this repo's own commands write. The `.venv` directory
# and any output files you generated stay where they are.
clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage .coverage.* coverage.xml htmlcov dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
