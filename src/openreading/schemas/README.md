# JSON Schemas

<sub>[Docs home](../README.md) · [← Evals](../evals/README.md) · [Backend adapters →](../adapters/README.md)</sub>

You want to know exactly which fields you can rely on in the JSON that OpenReading returns, and
which fields are optional. This page answers that from the schema files themselves. A JSON Schema
is a file that states which fields a JSON document must carry and which it may carry. It also
states what values each field may hold. The files in this directory are the contract for every
request and response, so code written against them keeps working whichever backend produced the
output.

You get a table of the schema families and the file each one lives in. You get a table of what
every response guarantees, and the commands that print the live truth from the installed package.
You need the package installed as the root README describes, and `sample.pdf` from the root README
for the checks under Notes. OpenReading accepts any document some backend can read, and each
backend lists its formats in its descriptor. The [Backend adapters](../adapters/README.md) page
carries that list.

## What this is

Code written against these files keeps working on every surface, because the `*.json` files in
this directory are the contract. Every surface (CLI, Python, HTTP) reads and writes exactly these
shapes. The pydantic models in `openreading.types` mirror them. When the two disagree, the JSON
file wins.

## Validate

One command proves every current schema file is valid. Exit 0 means every file is valid. Exit 1
means a fixture failed, and exit 2 means no verb was given.

```bash
uv run python -m openreading.schemas validate
```

**You should see:**

```
schemas: request.v0.1.json OK, response.v0.3.json OK, adapter-descriptor.v0.7.json OK, … journal.v0.1.json OK
fixtures: 0 checked, 0 invalid
```

`fixtures: 0 checked` is expected today (see Known gaps). From Python, you validate one instance
by calling `validate_<family>(instance)`, for example `validate_response`. The families are the
rows of the table below. Each function returns `None` or raises `jsonschema.ValidationError`.

## Files

This table tells you which file each schema family lives in and which function validates it. A
family is one kind of JSON document, such as a request or a response, with its own numbered file.

Source: `src/openreading/schemas/__init__.py` (the `*_SCHEMA_FILE` constants, lines 442–481). Live
truth: `uv run python -c "import openreading.schemas as s; print({n: getattr(s, n) for n in dir(s)
if n.endswith('_SCHEMA_FILE')})"`. If the table and that output disagree, the output is right and
the table needs fixing.

| family | current file | constant | validator |
|---|---|---|---|
| request | `request.v0.1.json` | `REQUEST_SCHEMA_FILE` | `validate_request` |
| response | `response.v0.3.json` | `RESPONSE_SCHEMA_FILE` | `validate_response` |
| adapter-descriptor | `adapter-descriptor.v0.7.json` | `DESCRIPTOR_SCHEMA_FILE` | `validate_descriptor` |
| strategy-config | `strategy-config.v0.2.json` | `STRATEGY_CONFIG_SCHEMA_FILE` | `validate_strategy_config` |
| comparison-report | `comparison-report.v0.2.json` | `COMPARISON_REPORT_SCHEMA_FILE` | `validate_comparison_report` |
| batch-result | `batch-result.v0.1.json` | `BATCH_RESULT_SCHEMA_FILE` | `validate_batch_result` |
| corpus-report | `corpus-report.v0.1.json` | `CORPUS_REPORT_SCHEMA_FILE` | `validate_corpus_report` |
| leaderboard-report | `leaderboard-report.v0.1.json` | `LEADERBOARD_REPORT_SCHEMA_FILE` | `validate_leaderboard_report` |
| liveness-report | `liveness-report.v0.1.json` | `LIVENESS_REPORT_SCHEMA_FILE` | `validate_liveness_report` |
| step | `step.v0.1.json` | `STEP_SCHEMA_FILE` | `validate_step` |
| journal | `journal.v0.1.json` | `JOURNAL_SCHEMA_FILE` | `validate_journal_record` |

The second table gives each family's `$id` and the version marker an instance carries. The `$id`
is the URL inside the schema file that names it. The adapter-descriptor family describes a
descriptor, which is the static record in which a backend declares its formats, its environment
variables and its compliance posture.

| family | `$id` | in-band version |
|---|---|---|
| request | `https://openreading.ai/schemas/request/v0.1.json` | optional `schema_version` const `"0.1"` |
| response | `https://openreading.ai/schemas/response/v0.3.json` | required `schema_version` const `"0.3"` |
| adapter-descriptor | `https://openreading.ai/schemas/adapter-descriptor.v0.7.json` | none (filename and `$id` only) |
| strategy-config | `https://openreading.ai/schemas/strategy-config/v0.2.json` | required integer `version` const `1` |
| comparison-report | `https://openreading.ai/schemas/comparison-report.v0.2.json` | required `schema_version` const `"0.2"` |
| batch-result | `https://openreading.ai/schemas/batch-result.v0.1.json` | required `schema_version` const `"0.1"` |
| corpus-report | `https://openreading.ai/schemas/corpus-report.v0.1.json` | required `schema_version` const `"0.1"` |
| leaderboard-report | `https://openreading.ai/schemas/leaderboard-report.v0.1.json` | required `schema_version` const `"0.1"` |
| liveness-report | `https://openreading.ai/schemas/liveness-report.v0.1.json` | required `schema_version` const `"0.1"` |
| step | `https://openreading.ai/schemas/step.v0.1.json` | none |
| journal | `https://openreading.ai/schemas/journal.v0.1.json` | none |

Older versions stay in this directory unchanged. `tests/test_schema_evolution.py` pins them byte for
byte.

## What a response guarantees

This table tells you which response fields you can rely on. The response is the JSON envelope,
meaning the one JSON object every backend returns in the same shape. Only its required fields,
closed enums and consts are listed here. A closed enum is a field whose value must come from a
fixed list, and a const is a field with exactly one allowed value. Optional fields are named by
the command below, never copied here.

Source: `response.v0.3.json` (top-level `required` lines 6–10, `additionalProperties` line 12,
`$defs.BBox` lines 14–93, `$defs.Block` lines 94–238, `anyOf` lines 649–682). Live truth: `uv run
python -c "import json, openreading.schemas as s; print(json.dumps(s.response_schema(),
indent=1))"`. If the table and that output disagree, the output is right and the table needs
fixing. In the required column, n/a marks a row that names a whole object rather than one key.

| path | required | value rule | lines |
|---|---|---|---|
| top level | n/a | `additionalProperties: false`. Required keys: `schema_version`, `status`, `backend`, `document` | 6–12 |
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
| `BBox` | n/a | object, requires `x`, `y`, `w`, `h`, `page` | 14–23 |
| `BBox.x` `y` `w` `h` | yes | numbers in `[0, 1]`, relative to the page, origin top-left, y grows downward | 25–44 |
| `BBox.page` | yes | integer ≥ 1 | 45–49 |
| `BBox.bbox_native` | no | the raw source geometry, untouched | 62–91 |
| `BBox.bbox_native.origin` | no | one of `top_left`, `bottom_left` | 73–76 |
| `BBox.bbox_native.unit` | no | one of `normalized`, `pdf_point`, `pixel`, `inch` | 79–84 |
| `warnings[]` | no | items are `{code, message, field}`, all strings. `code` is an open set | 580–597 |
| `channel_provenance` | no | map of channel name to `native` or `derived`. Marked `x-stability: experimental` | 632–642 |

`warnings[].code` is an open set, so switch on the codes you know and tolerate the rest. The first
string argument of every `add_warning(` call is a code. This command lists today's codes.

```bash
grep -rn -A1 "add_warning(" src/openreading
```

## Versions

A request and a response carry different version numbers, so never copy one into the other. A
request carries `schema_version: "0.1"` and a response carries `"0.3"`. They are different
families with different numbers. Sending `"0.3"` in a request to `openreading serve` returns
HTTP 400. The response history is in `CHANGELOG.md` and in the "Versioning rules" section of
`uv run python -m pydoc openreading.schemas`.

## Notes

- `document.page_count` is the source page count even when you request a subset. To check, run
  `uv run openreading parse sample.pdf --backend pymupdf --pages 2`, which prints `page_count: 2`
  and one page.
- `backend_raw` is the backend's own response body, copied into the envelope untouched. It travels
  wherever the envelope travels, so it lands in every saved file, batch result, comparison input,
  and ledger blob. For a run over regulated data that is a second copy of vendor output, shaped by
  the vendor rather than by this contract, in every artifact. It is outside the versioned contract
  and may change shape without a version bump. To drop it, set `outputs.include_backend_raw` to
  `false` in a Python `run()` call or an HTTP request body, which every adapter honors and no CLI
  flag exposes today.
- `channel_provenance` is experimental and excluded from backward-compatibility guarantees.
- A `pymupdf` run also carries `usage`, `backend_raw` and `channel_provenance` today. To see them,
  run `uv run openreading parse sample.pdf --backend pymupdf | python3 -c "import json,sys;
  print(list(json.load(sys.stdin)))"`.

## Known gaps

- `openreading.SCHEMA_VERSION` prints `0.1`, the request family's number, unlabelled. Reproduce it
  with `uv run python -c "import openreading; print(openreading.SCHEMA_VERSION)"`.
- `validate` reports `fixtures: 0 checked` because its fixture glob names a directory that does not
  exist. Reproduce it with `uv run python -m openreading.schemas validate`.
- An out-of-range page range returns the whole document with no warning. Reproduce it with `uv run
  openreading parse sample.pdf --backend pymupdf --pages 99`, which exits 0 with both pages and
  only `confidence_unavailable`.

## See also

- [Docs home](../README.md) is the documentation home. It lists every guide and explains how to
  use OpenReading from an agent.
- `uv run python -m pydoc openreading.types` prints the pydantic mirror of these files.
- `uv run python -m pydoc openreading.server` prints the HTTP status code for each error.
- [Backend adapters](../adapters/README.md) lists every backend with its formats, its env vars and
  its compliance posture.
- [`tests/test_schema_evolution.py`](../../../tests/test_schema_evolution.py) holds the byte pins
  on older files.

## Maintenance

To cut a new schema version, copy the current file to `<family>.vX.(Y+1).json`. Point its
`*_SCHEMA_FILE` constant at the new file. Add a row to the Files table above. Update the default in
`openreading.types`. Add a `CHANGELOG.md` line. Leave the old file untouched, because its hash is
pinned. A new required field, enum value or const goes in the response table above. A new warning
code needs no edit here. When a Known-gaps line stops being true, delete it. The full table of what
to update for each kind of change is under *Where a change gets documented* in
[`AGENTS.md`](../../../AGENTS.md).

<sub>[Docs home](../README.md) · [← Evals](../evals/README.md) · [Backend adapters →](../adapters/README.md)</sub>
