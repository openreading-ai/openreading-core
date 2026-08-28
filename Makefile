.PHONY: verify lint typecheck typecheck-mypy test schema-validate extras-parity smoke strategy-smoke compare-smoke leaderboard-smoke serve-smoke verify-live sync clean

# `make verify` is the gate: nothing red gets committed.
verify: lint typecheck test schema-validate extras-parity smoke strategy-smoke compare-smoke leaderboard-smoke

sync:
	uv sync --all-extras --dev

lint:
	uv run ruff check .
	uv run ruff format --check .

# BL-170: invoke via `python -m`, not the installed console-script (`uv run pyright`) — a venv
# whose console-script shims carry a stale shebang (e.g. after the .venv directory was relocated
# or copied) fails to spawn the script but still resolves the module fine through the interpreter.
typecheck:
	uv run python -m pyright || uv run python -m mypy   # pyright primary; mypy fallback (DECISIONS D1)

# BL-89: the `||` fallback above only ever runs mypy when pyright itself fails, so mypy's own
# health — does it still pass clean, independent of whether pyright happens to be up — has no
# signal on the green path (pyright passing short-circuits `||` every time, whether mypy would
# have passed too or not). This target runs mypy unconditionally so drift is caught on its own,
# not only the day pyright's fallback is actually needed. Not in `verify` (D1 keeps pyright
# primary/mypy fallback as policy — see D1's status note); run this explicitly, or wire it into CI
# as its own non-blocking step.
typecheck-mypy:
	uv run python -m mypy

# Offline suite + coverage floor. --cov-fail-under is the gate: coverage below the floor fails
# `make verify` just like a red test. Ratchet the floor up as coverage climbs; never lower it.
# `scripts/run_test_suite.py` wraps the real `pytest --cov` invocation: it clears any stale
# .coverage(.*) file before starting (BL-61 — pytest-cov's own combine() step runs unconditionally
# at the end of every run, and hard-fails with an INTERNALERROR if a leftover parallel data file,
# e.g. branch-mode schema from a differently-configured concurrent `pytest --cov` run on this
# shared checkout, doesn't match this repo's statement-mode schema), and retries once — only for
# that exact combine()-time INTERNALERROR signature, never any other failure — when a second,
# genuinely concurrent `pytest --cov` invocation writes a fresh incompatible file mid-run, a gap
# the before-the-run sweep alone can't close (BL-69). See the script's own docstring for the full
# mechanism.
test:
	uv run python scripts/run_test_suite.py -m "not live" --cov=openreading --cov-report=term-missing --cov-fail-under=91

schema-validate:
	uv run python -m openreading.schemas validate

# Extras parity gate (BL-158): openreading.adapters.registry.BUILTIN_ADAPTERS and
# pyproject.toml's [project.optional-dependencies] are two independent, hand-maintained sources
# describing the same "install this to run this adapter" relationship. [dependency-groups] dev
# already supplies every adapter's runtime dep for tests, so an adapter that's fully tested but
# missing or misspelled here still sees a green `make verify` while `pip install
# openreading[<slug>]` ships nothing (or the wrong thing) for a real user. Fast, offline,
# structural — same shape as schema-validate, next to which it's wired in. No network, no
# subprocess: reads pyproject.toml and imports the registry module only.
extras-parity:
	uv run python scripts/check_extras_parity.py

# CLI smoke: generate the deterministic test PDF, parse it through the local PyMuPDF backend via
# the CLI, and confirm it emits schema-valid JSON (the CLI validates before printing).
smoke:
	@uv run python -c "import os; from openreading.testing.sample_pdf import build_sample_pdf; open(os.path.join(os.environ.get('TMPDIR','/tmp'),'read_smoke.pdf'),'wb').write(build_sample_pdf())"
	@uv run python -m openreading.cli parse "$${TMPDIR:-/tmp}/read_smoke.pdf" --backend pymupdf --pages 1 \
	  | uv run python -c "import sys,json; d=json.load(sys.stdin); assert d['document']['pages'], 'no pages'; print('smoke: OK — pymupdf parsed sample PDF, schema-valid JSON (%d blocks)' % len(d['document']['pages'][0].get('blocks',[])))"

# Strategy smoke: generate the scanned fixture, run a [pymupdf, tesseract] cascade through
# `parse --strategy`, assert the quality gate fired + a schema-valid orchestration-carrying
# response. Fully offline (local backends); green with or without the tesseract binary.
strategy-smoke:
	uv run python scripts/strategy_smoke.py

# Compare smoke: parse the sample PDF through pymupdf live, compare against a second real backend
# (tesseract live if present, else a deterministic fixture), assert a schema-valid pairwise report
# with findings. Fully offline (local backends); green on any machine.
compare-smoke:
	uv run python scripts/compare_smoke.py

# Leaderboard smoke (BL-160): run `openreading leaderboard` itself (the real CLI entry point, not
# the Python API) over the deterministic sample dataset with two local backends, assert a
# schema-valid BenchmarkReport with both backends ranked and a non-empty per-case table. Fully
# offline (local backends, no keys); green with or without the tesseract binary — a missing binary
# just means every one of tesseract's cases scores an honest, tallied error, never a crash, which
# this smoke also treats as a legitimate (if uninteresting) ranking rather than a failure.
leaderboard-smoke:
	uv run python scripts/leaderboard_smoke.py

# Server smoke: boot the real HTTP API on an ephemeral port, POST the sample PDF through /v1/parse,
# assert schema-valid, shut down. Not in `verify` (uses a localhost socket); run explicitly.
serve-smoke:
	uv run python scripts/serve_smoke.py

# Live lane: run ONLY the @pytest.mark.live tests. Each skips cleanly unless its backend's env
# keys are present (`-rs` surfaces the skip reasons). Never part of the offline `verify` gate.
verify-live:
	uv run pytest -m live -rs

clean:
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
