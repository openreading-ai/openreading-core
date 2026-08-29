# JSON Schemas

## What this is

The `*.json` files in this directory are the contract. Every surface (CLI, Python, HTTP) reads
and writes exactly these shapes. The pydantic models in `openreading.types` mirror them, and
the JSON file wins when the two disagree.

## Validate

Self-check every current schema file. Exit 0 means every file is valid, 1 means a fixture failed,
and 2 means no verb was given.

```bash
uv run python -m openreading.schemas validate
```

You should see:

```
schemas: request.v0.1.json OK, response.v0.3.json OK, adapter-descriptor.v0.7.json OK, … journal.v0.1.json OK
fixtures: 0 checked, 0 invalid
```

`fixtures: 0 checked` is expected today (see Known gaps). From Python, call `validate_<family>(instance)`,
for example `validate_response`. The families are the rows of the table below. Each function returns
`None` or raises `jsonschema.ValidationError`.

## Files

Source: `src/openreading/schemas/__init__.py` (the `*_SCHEMA_FILE` constants, lines 442–481).
Live truth: `uv run python -c "import openreading.schemas as s; print({n: getattr(s, n) for n in dir(s) if n.endswith('_SCHEMA_FILE')})"`. If this table and that output disagree, the output is right. Fix the table.

| family | current file | constant | `$id` | in-band version | validator |
|---|---|---|---|---|---|
| request | `request.v0.1.json` | `REQUEST_SCHEMA_FILE` | `https://openreading.ai/schemas/request/v0.1.json` | optional `schema_version` const `"0.1"` | `validate_request` |
| response | `response.v0.3.json` | `RESPONSE_SCHEMA_FILE` | `https://openreading.ai/schemas/response/v0.3.json` | required `schema_version` const `"0.3"` | `validate_response` |
| adapter-descriptor | `adapter-descriptor.v0.7.json` | `DESCRIPTOR_SCHEMA_FILE` | `https://openreading.ai/schemas/adapter-descriptor.v0.7.json` | none (filename and `$id` only) | `validate_descriptor` |
| strategy-config | `strategy-config.v0.2.json` | `STRATEGY_CONFIG_SCHEMA_FILE` | `https://openreading.ai/schemas/strategy-config/v0.2.json` | required integer `version` const `1` | `validate_strategy_config` |
| comparison-report | `comparison-report.v0.2.json` | `COMPARISON_REPORT_SCHEMA_FILE` | `https://openreading.ai/schemas/comparison-report.v0.2.json` | required `schema_version` const `"0.2"` | `validate_comparison_report` |
| batch-result | `batch-result.v0.1.json` | `BATCH_RESULT_SCHEMA_FILE` | `https://openreading.ai/schemas/batch-result.v0.1.json` | required `schema_version` const `"0.1"` | `validate_batch_result` |
| corpus-report | `corpus-report.v0.1.json` | `CORPUS_REPORT_SCHEMA_FILE` | `https://openreading.ai/schemas/corpus-report.v0.1.json` | required `schema_version` const `"0.1"` | `validate_corpus_report` |
| leaderboard-report | `leaderboard-report.v0.1.json` | `LEADERBOARD_REPORT_SCHEMA_FILE` | `https://openreading.ai/schemas/leaderboard-report.v0.1.json` | required `schema_version` const `"0.1"` | `validate_leaderboard_report` |
| liveness-report | `liveness-report.v0.1.json` | `LIVENESS_REPORT_SCHEMA_FILE` | `https://openreading.ai/schemas/liveness-report.v0.1.json` | required `schema_version` const `"0.1"` | `validate_liveness_report` |
| step | `step.v0.1.json` | `STEP_SCHEMA_FILE` | `https://openreading.ai/schemas/step.v0.1.json` | none | `validate_step` |
| journal | `journal.v0.1.json` | `JOURNAL_SCHEMA_FILE` | `https://openreading.ai/schemas/journal.v0.1.json` | none | `validate_journal_record` |

Older versions stay in this directory unchanged. `tests/test_schema_evolution.py` pins them byte for byte.

## What a response guarantees

The response is the JSON envelope every backend returns. Only its required fields, closed enums and
consts are listed here. Optional fields are named by the command below, never copied here.

Source: `response.v0.3.json` (top-level `required` lines 6–10, `additionalProperties` line 12, `$defs.BBox` lines 14–93, `$defs.Block` lines 94–238, `anyOf` lines 649–682).
Live truth: `uv run python -c "import json, openreading.schemas as s; print(json.dumps(s.response_schema(), indent=1))"`. If this table and that output disagree, the output is right. Fix the table.

| path | required | value rule | lines |
|---|---|---|---|
| top level | — | `additionalProperties: false`. Required keys: `schema_version`, `status`, `backend`, `document` | 6–12 |
| `schema_version` | yes | const `"0.3"` | 241–244 |
| `status` | yes | object, requires `state` | 245–275 |
| `status.state` | yes | one of `succeeded`, `partial`, `failed`, `processing` | 252–257 |
| `backend` | yes | object, requires `id` (string) and `type` | 276–316 |
| `backend.type` | yes | one of `hosted_api`, `oss_library`, `framework_loader`, `self_hosted_model` | 287–292 |
| `backend.output_paradigm[]` | no | items one of `markdown`, `typed_fields`, `element_list`, `block_tree`, `block_graph`, `token_stream` | 304–311 |
| `document` | yes | object. At least one of `document.markdown`, `document.text`, `document.pages` is present, or top-level `typed_fields` | 317–433, 649–682 |
| `document.pages[]` | see `anyOf` | each item requires `page_number`, an integer ≥ 1 counted in the source document | 357–368 |
| `document.pages[].unit` | no | one of `pdf_point`, `pixel`, `inch` | 377–381 |
| `document.pages[].blocks[]` | no | array of `Block`. Each `Block` requires `type` | 398–404, 94–99 |
| `Block.type` | yes | one of `title`, `section_header`, `header`, `footer`, `page_number`, `text`, `list`, `list_item`, `table`, `table_cell`, `figure`, `image`, `caption`, `formula`, `code`, `key_value`, `form_field`, `signature`, `selection_mark`, `barcode`, `table_of_contents`, `other`. An unmapped native label becomes `other` and `native_type` keeps the original | 105–129 |
| `Block.bbox` | no | a `BBox`. Absent when the backend has no real geometry, never invented | 147–149 |
| `Block.text_type` | no | one of `printed`, `handwriting`, `unknown` | 230–234 |
| `BBox` | — | object, requires `x`, `y`, `w`, `h`, `page` | 14–23 |
| `BBox.x` `y` `w` `h` | yes | numbers in `[0, 1]`, relative to the page, origin top-left, y grows downward | 25–44 |
| `BBox.page` | yes | integer ≥ 1 | 45–49 |
| `BBox.bbox_native` | no | the raw source geometry, untouched | 62–91 |
| `BBox.bbox_native.origin` | no | one of `top_left`, `bottom_left` | 73–76 |
| `BBox.bbox_native.unit` | no | one of `normalized`, `pdf_point`, `pixel`, `inch` | 79–84 |
| `warnings[]` | no | items are `{code, message, field}`, all strings. `code` is an open set | 580–597 |
| `channel_provenance` | no | map of channel name to `native` or `derived`. Marked `x-stability: experimental` | 632–642 |

`warnings[].code` is an open set: switch on the codes you know and tolerate the rest. The first string
argument of every `add_warning(` call is a code. List today's with:

```bash
grep -rn -A1 "add_warning(" src/openreading
```

## Versions

A request carries `schema_version: "0.1"` and a response carries `"0.3"`. They are different
families with different numbers. Sending `"0.3"` in a request to `openreading serve` returns HTTP 400.
Response history: `CHANGELOG.md` and the "Versioning rules" section of `uv run python -m pydoc openreading.schemas`.

## Notes

- `document.page_count` is the source page count even when you request a subset. Check: `uv run openreading parse sample.pdf --backend pymupdf --pages 2` prints `page_count: 2` and one page.
- `backend_raw` is outside the versioned contract and may change shape without a version bump.
- `channel_provenance` is experimental and excluded from backward-compatibility guarantees.
- A `pymupdf` run also carries `usage`, `backend_raw` and `channel_provenance` today. Check: `uv run openreading parse sample.pdf --backend pymupdf | python3 -c "import json,sys; print(list(json.load(sys.stdin)))"`.

## Known gaps

- `openreading.SCHEMA_VERSION` prints `0.1`, the request family's number, unlabelled. Reproduce: `uv run python -c "import openreading; print(openreading.SCHEMA_VERSION)"`.
- `validate` reports `fixtures: 0 checked` because its fixture glob names a directory that does not exist. Reproduce: `uv run python -m openreading.schemas validate`.
- An out-of-range page range returns the whole document with no warning. Reproduce: `uv run openreading parse sample.pdf --backend pymupdf --pages 99` (exit 0, both pages, only `confidence_unavailable`).

## See also

- `uv run python -m pydoc openreading.types` — the pydantic mirror of these files.
- `uv run python -m pydoc openreading.server` — HTTP status codes per error.
- [`../adapters/README.md`](../adapters/README.md) — every backend, its env vars and compliance posture.
- [`tests/test_schema_evolution.py`](../../../tests/test_schema_evolution.py) — the byte pins on older files.

## Maintenance

To cut a new schema version: copy the current file to `<family>.vX.(Y+1).json`. Point its
`*_SCHEMA_FILE` constant at the new file. Add a row to the Files table above. Update the default in
`openreading.types`. Add a `CHANGELOG.md` line. Leave the old file untouched: its hash is pinned.
A new required field, enum value or const goes in the response table above. A new warning code needs
no edit here. When a Known-gaps line stops being true, delete it. The full table of what to update for
each kind of change is under *Where a change gets documented* in [`AGENTS.md`](../../../AGENTS.md).
