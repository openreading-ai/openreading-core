# Commented `openreading.yaml` examples

Choose a complete configuration, run it unchanged, then edit the comments' suggested controls.
The numbered files progress from one default parser to composed workflows with routing and parallel execution.

A backend is one document parser. A strategy is a named recipe for selecting and combining backends.
A gate is a result check that decides whether another attempt is needed.

## Choose a starting point

Each file stands alone. You do not need to combine earlier files or fill in missing strategy definitions.

| File | What you learn | Processing destination |
|---|---|---|
| [01-single-backend.yaml](01-single-backend.yaml) | Choose one default backend with `policy.backends` | Local |
| [02-default-fallbacks.yaml](02-default-fallbacks.yaml) | Order a default chain for failure recovery | Local |
| [03-named-strategies.yaml](03-named-strategies.yaml) | Reuse named recipes, select a default, and override it | Local |
| [04-local-quality.yaml](04-local-quality.yaml) | Try OCR when text extraction looks deficient | Local |
| [05-local-then-hosted.yaml](05-local-then-hosted.yaml) | Add a hosted fallback after local quality checks | Local, then hosted |
| [06-tuned-quality.yaml](06-tuned-quality.yaml) | Tune individual `looks_bad` checks for your documents | Local |
| [07-race.yaml](07-race.yaml) | Take the first success from concurrent parsers | Local |
| [08-compare.yaml](08-compare.yaml) | Select a scored winner and retain alternatives for inspection | Local |
| [09-compare-then-escalate.yaml](09-compare-then-escalate.yaml) | Use disagreement or poor quality to trigger another parser | Local, then hosted |
| [10-advanced-gates.yaml](10-advanced-gates.yaml) | Combine AND/OR checks and gate the final result | Local |
| [11-errors-and-budgets.yaml](11-errors-and-budgets.yaml) | Choose error handling and nested time budgets | Hosted, then local |
| [12-conditional-routing.yaml](12-conditional-routing.yaml) | Route by filename, page count, MIME type, or a caller's hint | Local |
| [13-delayed-hedge.yaml](13-delayed-hedge.yaml) | Delay a second parallel attempt until a latency threshold | Hosted |
| [14-sampled-shadow.yaml](14-sampled-shadow.yaml) | Audit a deterministic sample without replacing the selected result | Local, with sampled hosted work |
| [15-page-escalation.yaml](15-page-escalation.yaml) | Escalate deficient pages and understand capability fallback | Local |
| [16-composed-workflow.yaml](16-composed-workflow.yaml) | Combine routing, reusable checks, and a hosted comparison | Local, then hosted |

## Run one

Start from the [Core installation instructions](../../README.md#install), then open a terminal at the repository root.
These commands assume `openreading` is on your PATH. In a development checkout, use `uv run openreading` instead.

Validate and inspect a local recipe before parsing a document:

```bash
openreading strategy validate --config examples/configs/04-local-quality.yaml
openreading strategy plan examples/john_smith_1000_2026_01.pdf \
  --strategy main --config examples/configs/04-local-quality.yaml
openreading parse examples/john_smith_1000_2026_01.pdf --strategy main \
  --config examples/configs/04-local-quality.yaml > run.json
openreading explain run.json
```

Try `examples/1040-1988.pdf` next to exercise OCR. Its missing text layer illustrates why successful extraction can still need another parser.

Every strategy example sets `defaults.strategy` for Python and HTTP requests that name no backend.
The parse CLI requires an explicit choice. Use `--strategy main` for these strategy examples or `--no-strategy` for the first two configurations.
Use `openreading route` to inspect the default backend chain without executing it.

To adopt a file, copy it to an unused `openreading.yaml` in your working directory.
The CLI and Python API discover that filename automatically. An explicit `--config` path selects a different file without overwriting yours.
For a server you start yourself, set `OPENREADING_CONFIG` to the file's absolute path before running `openreading serve`.
See the [HTTP server guide](../../src/openreading/server/README.md) for installation, authentication, and startup.

## Install the backends you choose

Configuration selects backends. It does not install their packages, executables, model assets, or credentials for you.
Follow the [local setup walkthrough](../../src/openreading/adapters/README.md#local-setup-walkthrough) for PyMuPDF and Tesseract.
Use the [backend catalog](../../src/openreading/adapters/README.md) for hosted extras, variables, and setup links.
Each backend reads the formats its descriptor claims. A configured name does not expand those capabilities.

Local examples need no vendor credentials. Hosted examples can upload documents and incur charges whenever their hosted steps execute.
Put credentials in the environment or your `.env` file, never in these YAML files or source control.
Validation checks configuration, not credentials, connectivity, provider behavior, or your permission to send a document.

`policy.backends` supplies a default chain. It is not an access-control boundary for explicit backend or strategy requests.
Review each strategy's actual leaves before running it. Use server API-key scopes when you need caller-level restrictions.

## Extend a recipe

The comments explain the tradeoffs beside each control. The complete grammar and runtime semantics remain in the implementation's documentation.

- [Strategy walkthrough](../../src/openreading/strategies/README.md) for working commands and composition.
- `openreading help gates` for the measurements behind quality checks.
- `python -m pydoc openreading.strategies.plain` for readable `try`, `race`, and `compare` recipes.
- `python -m pydoc openreading.strategies.model` for advanced syntax and explicitly unimplemented controls.

After editing, run `openreading strategy validate --config your-file.yaml` before processing documents.
These files are checked by [test_config_examples.py](../../tests/test_config_examples.py) through the real loader, validator, and normalizer without network calls.
That check proves configuration validity, not extraction accuracy. Test your chosen thresholds on representative documents and inspect the resulting traces.
