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

`fixtures: 0 checked` is expected today (see "Not built yet"). From Python, you validate one
instance by calling `validate_<family>(instance)`, for example `validate_response`. The families
are the rows of the table below. Each function returns `None` or raises
`jsonschema.ValidationError`.

## Files

This table tells you which file each schema family lives in and which function validates it. A
family is one kind of JSON document, such as a request or a response, with its own numbered file.

Source: `src/openreading/schemas/__init__.py` (the `*_SCHEMA_FILE` constants). Live
truth: `uv run python -c "import openreading.schemas as s; print({n: getattr(s, n) for n in dir(s)
if n.endswith('_SCHEMA_FILE')})"`. If the table and that output disagree, the output is right and
the table needs fixing.

The in-band version is the marker an instance itself carries, and it is the only version signal a
consumer gets. The adapter-descriptor family describes a descriptor, which is the static record in
which a backend declares its formats, its environment variables and its compliance posture.

| family | current file | constant | validator | in-band version |
|---|---|---|---|---|
| request | `request.v0.1.json` | `REQUEST_SCHEMA_FILE` | `validate_request` | optional `schema_version` const `"0.1"` |
| response | `response.v0.3.json` | `RESPONSE_SCHEMA_FILE` | `validate_response` | required `schema_version` const `"0.3"` |
| adapter-descriptor | `adapter-descriptor.v0.7.json` | `DESCRIPTOR_SCHEMA_FILE` | `validate_descriptor` | none, filename and `$id` only |
| strategy-config | `strategy-config.v0.2.json` | `STRATEGY_CONFIG_SCHEMA_FILE` | `validate_strategy_config` | required integer `version` const `1` |
| comparison-report | `comparison-report.v0.2.json` | `COMPARISON_REPORT_SCHEMA_FILE` | `validate_comparison_report` | required `schema_version` const `"0.2"` |
| batch-result | `batch-result.v0.1.json` | `BATCH_RESULT_SCHEMA_FILE` | `validate_batch_result` | required `schema_version` const `"0.1"` |
| corpus-report | `corpus-report.v0.1.json` | `CORPUS_REPORT_SCHEMA_FILE` | `validate_corpus_report` | required `schema_version` const `"0.1"` |
| leaderboard-report | `leaderboard-report.v0.1.json` | `LEADERBOARD_REPORT_SCHEMA_FILE` | `validate_leaderboard_report` | required `schema_version` const `"0.1"` |
| liveness-report | `liveness-report.v0.1.json` | `LIVENESS_REPORT_SCHEMA_FILE` | `validate_liveness_report` | required `schema_version` const `"0.1"` |
| step | `step.v0.1.json` | `STEP_SCHEMA_FILE` | `validate_step` | none |
| journal | `journal.v0.1.json` | `JOURNAL_SCHEMA_FILE` | `validate_journal_record` | none |

Each file's `$id` is the URL inside it that names it, and it is that filename under
`https://openreading.ai/schemas/`. Two shapes coexist and both are permanent. The request,
response and strategy-config families put a slash before the version, as in
`https://openreading.ai/schemas/response/v0.3.json`. Every other current file keeps the dot, as in
`https://openreading.ai/schemas/journal.v0.1.json`. Code that matches on `$id` must accept both.

Older versions stay in this directory unchanged. `tests/test_schema_evolution.py` pins them byte for
byte.

## What a response guarantees

This table tells you which response fields you can rely on. The response is the JSON envelope,
meaning the one JSON object every backend returns in the same shape. The table lists every
required field, every const, and every enum. A const is a field with exactly one allowed value.
An enum is a field whose value must come from a fixed list.

You can tell an open set from a closed one by how the schema declares it, and the *may gain
values* column reports what it found. A field declared as a JSON Schema `enum` is closed. A value
outside its list fails validation today, and only a MAJOR release may add to the list, so you may
switch on such a field exhaustively. A field declared as a plain string carries its known values
in prose instead, and a MINOR release may add to that set at any time, so switch on the values you
know and send the rest to a default branch. `warnings[].code` and `backend.id` are the two open
sets in a response.

`additionalProperties: false` applies to the top level alone. An unknown key is rejected there,
and accepted inside `backend`, `usage`, `document`, `status`, a page, and a block. A nested object
can therefore gain a field in a MINOR release without failing validation. A loader that
materializes nested objects should ignore keys it does not recognize rather than reject them.

Optional fields are described in the Response section of
`uv run python -m pydoc openreading.schemas`, one entry each with the condition under which it
appears.

Source: `response.v0.3.json` (the top-level `required` and `additionalProperties` keys, the
`$defs.BBox` and `$defs.Block` definitions, and the top-level `anyOf`). Live truth: `uv run
python -c "import json, openreading.schemas as s; print(json.dumps(s.response_schema(),
indent=1))"`. If the table and that output disagree, the output is right and the table needs
fixing. In the required column, n/a marks a row that names a whole object rather than one key.

| path | required | value rule | may gain values | lines |
|---|---|---|---|---|
| top level | n/a | `additionalProperties: false`, and only here. Required keys: `schema_version`, `status`, `backend`, `document` | n/a | 6–12 |
| `schema_version` | yes | const `"0.3"`. Changing it is itself the version bump | n/a | 241–244 |
| `status` | yes | object, requires `state` | n/a | 245–275 |
| `status.state` | yes | one of `succeeded`, `partial`, `failed`, `processing` | closed, `MAJOR` only | 252–257 |
| `status.error` | no | object with optional `code`, `message` and `backend_code`, every one a plain string with no enum. Read [Reading an error](#reading-an-error) before branching on `code` | open and undocumented | 259–273 |
| `backend` | yes | object, requires `id` (string) and `type` | n/a | 276–316 |
| `backend.type` | yes | one of `hosted_api`, `oss_library`, `framework_loader`, `self_hosted_model` | closed, `MAJOR` only | 287–292 |
| `backend.output_paradigm[]` | no | items one of `markdown`, `typed_fields`, `element_list`, `block_tree`, `block_graph`, `token_stream` | closed, `MAJOR` only | 304–311 |
| `backend.version` | no | the backend, model or library version actually run. See [Lineage](#lineage) for who populates it | n/a | 297–300 |
| `document` | yes | object. At least one of `document.markdown`, `document.text`, `document.pages` is present, or top-level `typed_fields` | n/a | 317–433, 649–682 |
| `document.pages[]` | see `anyOf` | each item requires `page_number`, an integer ≥ 1 counted in the source document | n/a | 357–368 |
| `document.pages[].unit` | no | one of `pdf_point`, `pixel`, `inch` | closed, `MAJOR` only | 377–381 |
| `document.pages[].blocks[]` | no | array of `Block`. Each `Block` requires `type` | n/a | 398–404, 94–99 |
| `Block.type` | yes | one of `title`, `section_header`, `header`, `footer`, `page_number`, `text`, `list`, `list_item`, `table`, `table_cell`, `figure`, `image`, `caption`, `formula`, `code`, `key_value`, `form_field`, `signature`, `selection_mark`, `barcode`, `table_of_contents`, `other`. An unmapped native label becomes `other` and `native_type` keeps the original | closed, `MAJOR` only | 105–129 |
| `Block.bbox` | no | a `BBox`. Absent when the backend has no real geometry, never invented | n/a | 147–149 |
| `Block.text_type` | no | one of `printed`, `handwriting`, `unknown` | closed, `MAJOR` only | 230–234 |
| `BBox` | n/a | object, requires `x`, `y`, `w`, `h`, `page` | n/a | 14–23 |
| `BBox.x` `y` `w` `h` | yes | numbers in `[0, 1]`, relative to the page, origin top-left, y grows downward | n/a | 25–44 |
| `BBox.page` | yes | integer ≥ 1 | n/a | 45–49 |
| `BBox.bbox_native` | no | the raw source geometry, untouched | n/a | 62–91 |
| `BBox.bbox_native.origin` | no | one of `top_left`, `bottom_left` | closed, `MAJOR` only | 73–76 |
| `BBox.bbox_native.unit` | no | one of `normalized`, `pdf_point`, `pixel`, `inch` | closed, `MAJOR` only | 79–84 |
| `usage.cost_usd` | no | a number, and `null` for every local backend. Never sum it without checking `cost_basis` first | n/a | 539–542 |
| `usage.cost_basis` | no | `billed` means the backend charged this run, so the number is real money. `estimated` means a published rate was applied to a page count, so it is a projection and not spend. `infra_only` means a local backend ran and `cost_usd` is `null`, so your only cost is your own compute. `unknown` means the backend reported no basis at all | closed, `MAJOR` only | 543–550 |
| `warnings[]` | no | items are `{code, message, field}`, all strings. The key is **absent** when nothing warned, never present and empty | `code` is open, `MINOR` | 580–597 |
| `channel_provenance` | no | map of channel name to `native` or `derived`. Marked `x-stability: experimental` | experimental, so outside the guarantees entirely | 632–642 |

`warnings[].code` is an open set, so switch on the codes you know and tolerate the rest. The first
string argument of every `add_warning(` call is a code. This command lists today's codes.

```bash
grep -rn -A1 "add_warning(" src/openreading
```

### Absent is not empty, and not null

A key this table marks optional is missing from the JSON altogether when it has no value. It is
not present holding `null`, and `warnings` is not present holding `[]`. Reach for it with a
default rather than by subscript, so `resp.get("warnings", [])` in Python and `.warnings[]?` in
jq. A `resp["warnings"]` loader crashes on the ordinary happy path, because a backend with nothing
to warn about omits the key. Compare two local backends on the same document:

```bash
uv run openreading parse sample.pdf --backend pymupdf | jq -c 'keys'
uv run openreading parse sample.pdf --backend tesseract | jq -c 'keys'
```
```json
["backend","backend_raw","channel_provenance","document","schema_version","status","usage","warnings"]
["backend","backend_raw","channel_provenance","document","schema_version","status","usage"]
```

**You should see** `warnings` on the pymupdf run, which reports `confidence_unavailable`, and no
`warnings` key at all on the tesseract run. The same rule governs `usage.cost_usd`, which is
`null` on a local run rather than absent, and `typed_fields`, which is absent when no backend
produced any.

### Reading an error

There is no error vocabulary you can branch on from a response today. `status.error.code` is an
unconstrained string with no enum, so nothing constrains what a backend may put there. One
shipped adapter populates it, `nuextract`, which sets the literal `backend_validation` when the
model answers and the answer fails its own template validation. Neither local backend ever sets
it.

A terminal failure on the CLI gives you no envelope to read at all. The run exits 3, writes zero
bytes to stdout, and names the reason on stderr instead:

```bash
uv run openreading parse sample.pdf --backend anthropic-claude > out.json; echo "exit=$?"; wc -c < out.json
```
```text
[anthropic-claude] missing required credentials/config: ANTHROPIC_API_KEY. Sign up / configure: https://console.anthropic.com
exit=3
       0
```

So branch on the exit code on the CLI, on the `error.category` field over HTTP
([The HTTP server](../server/README.md)), and on `items[].error.code` in a batch result, where a
failed item keeps its own error beside the items that succeeded
([Batch runs](../batch/README.md)).

> [!IMPORTANT]
> The words `timeout`, `rate_limited`, `auth`, `provider_error`, `unsupported_feature`,
> `invalid_input`, `exhausted`, `budget_exhausted`, `transient` and `any` are not response values
> and never appear in `status.error.code`. They are the keys of the `on_error` map you write in
> `openreading.yaml`, a closed set that `strategy-config` validates when it loads your config
> ([Strategies](../strategies/README.md)). They are a vocabulary for authoring a config, not one
> for reading a result.

## Lineage

Lineage is the record of what produced a row, and you need it the first time someone asks why last
March's number looks wrong. Read this section before you store responses one per file, because a
single-document response answers less about its own origin than its shape suggests.

### A response does not identify its input

A response carries no identity of the document it parsed. There is no filename, no path, no
content hash, and no echo of the request you sent. The eight top-level keys a local run produces
are `schema_version`, `status`, `backend`, `document`, `usage`, `warnings`, `backend_raw` and
`channel_provenance`, and not one of them names the input. Two responses in a folder are therefore
indistinguishable except by their content, and `parse --backend pymupdf > out.json` gives you a
file with no provenance at all. Record the path and the hash yourself at the moment you write the
response, or use one of the two surfaces that record them for you.

Input identity lives in exactly two places. A batch result carries `items[].source` with
`filename`, `path`, `relpath`, `size_bytes` and `sha256` per document ([Batch
runs](../batch/README.md)). A journaled strategy run carries the document's digest and filename in
its ledger header ([The run ledger](../ledger/README.md), which explains what that header keeps
after a run is erased).

### The join keys

Those two artifacts join to each other, and to a strategy response, on values that are byte
identical rather than merely similar.

```bash
uv run openreading parse corpus/ --backend pymupdf | jq -r '.items[0].source | "\(.relpath)  \(.sha256)"'
jq -r '.document.digest' .openreading/$RUN_ID.header.json
```
```text
a.pdf  ef51b93f5ac23790cfa3055b4b749ca622c3c12281aececb95ee58166a503916
sha256:ef51b93f5ac23790cfa3055b4b749ca622c3c12281aececb95ee58166a503916
```

**You should see** the same digest twice, once bare and once with a `sha256:` prefix the journal
adds. Strip the prefix and a batch row joins to a journal record on the document. The second join
is the config: a strategy response's `orchestration.config_hash` equals the ledger header's
`config_hash` exactly, prefix included, so a stored response tells you which compiled strategy
produced it.

### What names the producer

| field | where it lives | populated by |
|---|---|---|
| `backend.id` | response | every backend, always |
| `backend.type` | response | every backend, always |
| `backend.version` | response | no backend shipped today, including both local ones |
| `schema_url` | response | no backend shipped today |
| `orchestration.config_hash` | response, strategy runs only | every strategy run |
| `resolved_version` | a journal or step record, never a response | recorded per step when a backend reports one |
| `registry_fingerprint` | ledger header only | every journaled run |
| `pinned_eligible` | ledger header only | written on the first arm, read on resume |

> [!WARNING]
> `backend.version` is the field that answers "which version of the parser produced this row", and
> nothing populates it yet. Treat it as absent, and record the version of the `openreading`
> package yourself if you need to reproduce a result later.

### Clocks and byte stability

No response and no batch result carries a wall-clock timestamp. Neither one tells you the date a
run happened, so stamp your own time at the moment you ingest a row. The `duration_ms` fields
measure an interval and never name an instant, so they cannot stand in for one.

A journal record does carry the time. Its `started_epoch_ms` and `ended_epoch_ms` are absolute UTC
epoch milliseconds, and so is the retention stamp's `expires_epoch_ms`, so a loader can read them
as timestamps directly. Those values name a moment because they cross a process boundary, while
the engine measures its own durations against a monotonic clock that no clock adjustment can move.
[The run ledger](../ledger/README.md) is where a journal comes from.

Whether two runs of the same input produce the same bytes depends on which envelope you have.

| envelope | byte stable | what moves |
|---|---|---|
| `parse --backend <id>` response | yes | nothing, verified over three runs |
| `parse --strategy <name>` response | no | `orchestration.attempts[].duration_ms` |
| `batch-result` | no | `summary.duration_ms` |
| `comparison-report` | yes | nothing, and [Compare](../comparison/README.md) explains why |

Hash the whole envelope only for the first and last rows. For the other two, hash `.document`, or
hash the envelope with the timing fields removed, so a change-detection job does not fire on every
run.

## Versions

A request and a response carry different version numbers, so never copy one into the other. A
request carries `schema_version: "0.1"` and a response carries `"0.3"`. They are different
families with different numbers. Sending `"0.3"` in a request to `openreading serve` returns
HTTP 400.

Which versions shipped when is the response *history*, and it is in
[`CHANGELOG.md`](../../../CHANGELOG.md). What a future version may do to code you have already
written is the *policy*, and it is the next section.

### Compatibility policy

This is what a release may change under a consumer that has already shipped. The full text is the
"Versioning rules" section of `uv run python -m pydoc openreading.schemas`, which is the authority
if it and this summary disagree.

| A release may | in a | what it means for your code |
|---|---|---|
| add an optional property, at the top level or nested | MINOR | ignore keys you do not recognize, because validation will not reject a nested one |
| add a value to an open set, meaning one declared as a plain string | MINOR | give every switch over `warnings[].code` or `backend.id` a default branch |
| widen a type where absence was already handled | MINOR | keep handling absence the way you already do |
| add a whole schema file | MINOR | nothing |
| remove or rename a field | MAJOR | a column disappears, so read the migration note |
| make an optional field required | MAJOR | nothing breaks on read |
| tighten a type or a constraint you can see | MAJOR | a value you accepted may stop arriving |
| change what an existing field means | MAJOR | your stored history and your new rows stop being comparable |
| add a value to any `enum` in a released schema | MAJOR | an exhaustive switch over an enum stays exhaustive until you upgrade |

Growing an enum is a MAJOR because the vocabulary is normalized across every backend, so one
backend adopting a new value while another leaves the same concept where it was would be a worse
contract than no new value at all. A new native concept does not need one. An adapter maps it to
the catch-all value for that field, such as `Block.type: other`, and keeps the vendor's own label
in `native_type`, so the concept reaches you without a schema change.

Two rules sit underneath that table and catch people out. The first is that a documented invariant
is part of the contract even when the shape does not change. Ask whether a legitimate assertion
against the old behaviour would fail on new output, and if it would, that is a breaking change
whichever fields moved. While the package is below `1.0` those changes ride the MINOR slot, and
each one is named in the CHANGELOG "Changed" table with its invariant id rather than shipped
quietly. The second is that the in-band `schema_version` const is the only version signal you get,
because no HTTP header carries it. Assert on that const in your loader and you will see a bump the
day it arrives.

You cannot pin a response version. There is no central service to down-convert for you, so the
package you install decides the version you receive. A MAJOR ships a forward-only
`migrate(instance, to_version)` for instances you have already stored. Before anything is removed
it is marked `deprecated` for at least one MINOR, and using it adds a `warnings[]` entry naming
it, so the warning reaches you before the removal does.

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

## Not built yet

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
code needs no edit here. When a "Not built yet" line stops being true, delete it. The full table
of what to update for each kind of change is under *Where a change gets documented* in
[`AGENTS.md`](../../../AGENTS.md).

<sub>[Docs home](../README.md) · [← Evals](../evals/README.md) · [Backend adapters →](../adapters/README.md)</sub>
