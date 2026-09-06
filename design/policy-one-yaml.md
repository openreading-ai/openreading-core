# One File: Policy Lives in openreading.yaml

**Status:** DESIGN, revision 1. Proposal for review. No implementation is in scope for this branch.
**Product intent:** `product/specs/policy-one-yaml.product-spec.md`, revision 1.
**Reviewed against:** this repo at `96d0c8f`, 2026-09-06. Every line reference below was read in
that tree.
**Decision requested:** approve the removals in section 2, the module move in section 3, and the
two-increment order in section 9, before the build prompt runs.

A policy is the short list of things a backend must declare before it may read a document. A
backend is one parser, such as the local `pymupdf` library or a hosted API. A descriptor is the
static record in which a backend declares what it reads, what it needs and its compliance posture.
The router drops every backend whose descriptor fails the policy, and no fallback readmits one.

## 1. Laws

Each law names the failure it prevents. A reviewer checks the pull request against these, not
against the prose that follows.

- **P1. One file.** `openreading.yaml` is the only file a person writes. Its `policy:` block is the
  only place a policy is spelled. Failure prevented: two documents describing one grammar in two
  syntaxes, and a reader concluding one of them is wrong.
- **P2. No hand-written JSON input.** JSON remains as output, as the wire, and as the schema
  language. Nothing a person authors is JSON. Failure prevented: a policy file with no schema.
- **P3. A policy never names a backend.** It names a requirement. The descriptor meets it or does
  not. A strategy names backends and runs inside the survivors. Failure prevented: a per-backend
  exception that widens the set by naming its way around a constraint.
- **P4. The union never widens.** Request constraints and file constraints combine
  most-restrictive-wins, in one function, on every path. Failure prevented: a path that forgot to
  union, which is the server's non-strategy path today.
- **P5. No file is byte-identical to today.** A directory with no `openreading.yaml` and no
  `OPENREADING_CONFIG` routes exactly as it does now. Failure prevented: a silent change to every
  user who never wrote a file.
- **P6. Reading the file never imports the engine.** A named-backend run that finds a file holding
  only `policy:` does not import `openreading.strategies`. Failure prevented: guardrail T10 in
  `tests/test_strategy_surface.py:47` turning red, and every plain run paying for a package it does
  not use.

## 2. Audit: the four spellings today

| Spelling | Read by | Validated by | Lines |
|---|---|---|---|
| `--policy p.json` on seven verbs | `cli/app.py:177` `_load_policy`, via `_read_json_or_fail` at `:157` | `api.validate_policy` | flags at `cli/app.py:2862, 2972, 2997, 3111, 3130, 3270, 3342` |
| `policy:` block in `openreading.yaml` | `strategies/loader.py:166` `load_config`, applied in `strategies/prune.py:127-128` | `prune._validated_policy` at `:306`, which calls `api.validate_policy` | grammar at `strategies/model.py:66-70` |
| `policy=` dict in Python | `api.py:466` `build_request`, `:629` `route`, `:1170` `run`, `:1364` `run_batch`, `:1510` `_run_native` | `api._apply_policy` at `:432`, `api.router_config` at `:446` | docstring at `api.py:69-94` |
| three env vars, server only | `server/app.py:467` `_server_router_config`, per request | none beyond `"1"/"true"/"yes"` and a comma split | documented at `server/app.py:108-126`, `.env.example:110-125` |

Facts that shape the design:

- `openreading.yaml` is discovered only for `auto` and `strategy:` requests (`api.py:58-61`,
  `:1205-1212`). `route` and a named-backend `parse` never open it.
- `strategies/__init__.py:292-334` imports the engine, the loader, prune and validate at package
  import. Any `from openreading.strategies.loader import ...` runs that file first. That is why
  the loader cannot serve every path from where it is.
- The union already exists as two pure functions, `prune._union_compliance` at `:334` and
  `prune._merge_router_config` at `:350`. They run only inside `compile_strategy`.
- `resume` calls `load_config(None)` and `router_config(None)` (`api.py:1299-1303`). The file's
  policy already reaches a resume through `compile_strategy`. A resume's behaviour does not change.
- The `strategy-config` schema declares `policy` as `{"type": "object", "additionalProperties":
  true}`. `prune.py:309-317` records that this is deliberate, so the JSON file could be primary
  and the block its superset. The hand validator exists to close what the schema left open.
- The seven `--policy` verbs and the nine `--config` verbs overlap on five. `route` (`:2853`) and
  `leaderboard` (`:3312`) carry `--policy` and no `--config`.
- Nine keys survive after `doc_type_hint` leaves: `require_baa`, `no_train_on_data`, `data_region`,
  `require_local`, `max_retention`, `optimize_for`, `allow_unverified_compliance`,
  `train_optout_confirmed`, `baa_tier_confirmed`. `src/openreading/router/README.md`, "How it
  decides", says of `doc_type_hint`: "Carried on the request; none of the three stages reads it."

## 3. The module: `openreading/config.py`

One new top-level module, beside `credentials.py` and `readiness.py`. It owns the file. Nothing in
it imports `openreading.strategies`.

What moves in, unchanged in behaviour:

| From | Symbol | Note |
|---|---|---|
| `strategies/loader.py:76-78` | `_ENV_VAR`, `_DEFAULT_FILENAME`, `_ALT_FILENAME` | |
| `strategies/loader.py:81` | `ConfigError` | `strategies.loader` re-exports it so existing imports hold |
| `strategies/loader.py:99` | `discover()` | signature unchanged, including `allow_cwd` |
| `strategies/loader.py:123-144` | the read and `yaml.safe_load` half of `parse_config_raw` | `yaml` stays a lazy import |
| `strategies/loader.py:201` | `_format_schema_error` | |
| `strategies/prune.py:334` | `_union_compliance` | becomes public within the package |
| `strategies/prune.py:350` | `_merge_router_config` | same |
| `api.py:446` | `router_config` | takes the `policy:` dict, not a caller's policy |

What it exposes:

- `load(config: str | PathLike | dict | None, *, allow_cwd: bool = True) -> LoadedFile | None`.
  Discovers, reads, schema-validates. A dict is validated as file text is, source `<dict>`. Returns
  the raw validated mapping, the `policy:` sub-dict (or `None`), the path, and the text hash.
  Returns `None` when nothing is found. Raises `ConfigError` on a file that will not parse or
  fails the schema.
- `apply(req: OpenReadingRequest, policy: dict | None, base: RouterConfig) ->
  tuple[OpenReadingRequest, RouterConfig]`. The union, most-restrictive-wins, plus the router
  configuration fold. Called once per request on every path. Idempotent, so a request that
  already carries the file's values is unchanged by a second call.

What stays in `strategies/loader.py`: `LoadedConfig`, `parse_config`, `load_config`,
`strip_strategy_prefix`, `resolve_strategy`, and the desugar-and-build half of `parse_config_raw`.
`load_config` calls `config.load` and then builds the `StrategyConfig`. Its callers do not change.

Increment 1 keeps `api.validate_policy` and calls it from `config.load` on the `policy:` sub-dict,
because the schema is still open. Increment 2 deletes it.

## 4. Every path, before and after

| Path | Today | After |
|---|---|---|
| `parse --backend X` | no file read; `policy=` from nowhere | `config.load(args.config)`; `config.apply`; `Router.check_eligible` on the named backend as today |
| `parse --strategy` / `auto` | `load_config`; union inside `compile_strategy` | `config.load`; `config.apply` before dispatch; `compile_strategy` reads the unioned request and stops unioning |
| `route` | `--policy` JSON; `_apply_policy` | gains `--config`; `config.load`; `config.apply` |
| `strategy validate` / `plan` | `--config` plus `--policy`; both unioned | `--config` only; the file's own block flags unreachable steps, as it already does |
| `replay` / `calibrate` | `--config` plus `--policy` | `--config` only |
| `benchmark run` / `leaderboard` | `--policy` on every request | `--config`; the file's block on every request |
| `run_batch` | `policy=` checked once for the whole batch (`api.py:1409`) | `config=` loaded once; `config.apply` per item, as `build_request` is today |
| `resume` | `load_config(None)`, `router_config(None)` | `config.load(None)`; `config.apply` on the reconstructed request |
| Python `run` / `route` | `policy=` | `config=` path or dict |
| server, strategy request | file block via `compile_strategy`; env vars per request | file block via `config.apply`; router configuration from the file, built at startup |
| server, named-backend request | request body only; env vars per request | request body unioned with the file block; router configuration from the file |

Discovery order is unchanged: explicit path or dict, then `OPENREADING_CONFIG`, then
`./openreading.yaml` or `./openreading.yml`. The server passes `allow_cwd=False` and never probes
its working directory (`server/app.py:1041-1044` already does this).

## 5. Server

`server/app.py:467-487` `_server_router_config` reads three environment variables per request.
It is deleted. At startup, beside the existing `_load_strategy_config(None, allow_cwd=False)`
call, the server stores the loaded file's `policy:` on `app.state` and builds one `RouterConfig`
from it. Every request handler calls `config.apply(req, app.state.policy, app.state.router_config)`
before routing. A malformed block still fails startup; today it surfaces on the first strategy
request (`server/__init__.py:221`), which is later than it should.

`server/__init__.py:313-316` and `server/app.py:49-53, 108-126` describe the environment variables.
Those paragraphs are rewritten to name the file. `credentials.py` mentions one of the variables
once;
the reference is removed. The three lines in `.env.example:110-125` and their preamble at
`:100-109` are deleted. `.env.example:237` (`OPENREADING_CONFIG`) gains the words "read by every
command, not only strategy runs".

## 6. Schema, increment 2

`strategy-config.v0.2.json` is copied to `strategy-config.v0.3.json`. The `policy` property
becomes:

```json
{
  "type": "object",
  "additionalProperties": false,
  "description": "What a backend must declare before it may run. Constraints union with the request, most-restrictive-wins. The three *_confirmed / allow_* keys are attestations and are the only keys that widen the eligible set.",
  "properties": {
    "require_baa": {"type": "boolean"},
    "no_train_on_data": {"type": "boolean"},
    "data_region": {"type": "string"},
    "require_local": {"type": "boolean"},
    "max_retention": {"type": "string"},
    "optimize_for": {"type": "string", "enum": ["accuracy", "cost", "latency", "offline"]},
    "allow_unverified_compliance": {"type": "boolean"},
    "train_optout_confirmed": {"type": "array", "items": {"type": "string"}},
    "baa_tier_confirmed": {"type": "array", "items": {"type": "string"}}
  }
}
```

The five compliance properties carry the same `description` strings as `request.v0.2.json`
`compliance`, copied verbatim. The config file's own `version` stays `1`: no file that was valid
and meaningful becomes invalid, because a key outside this set was already refused by the hand
validator.

With the block closed, a quoted `"false"` fails `type: boolean` and a bare `reducto` fails
`type: array`. Those are the two failures `prune.py:309-317` records the hand validator for. The
validator, `POLICY_KEYS`, `PolicyError` and `_validated_policy` are deleted. `PolicyError` leaves
`api.__all__` and the package's top-level exports. A malformed block raises `ConfigError`, which the
CLI already maps to exit 3 (`cli/__init__.py:857-859`).

One parity test replaces `test_policy_keys_are_derived_from_the_models_they_feed`: the property
names and types of the five compliance keys in `strategy-config` v0.3 `policy` equal those in
`request` v0.2 `compliance`. The schema evolution and versioning tests take their usual rows.

## 7. Tests

Changed in increment 1, by mechanical substitution of `--policy p.json` for `--config p.yaml` and
`policy={...}` for `config={"version": 1, "policy": {...}}`:

`tests/test_cli.py` (13 sites), `test_strategy_validate.py` (11),
`test_benchmark_publisher_contract.py`
(11), `test_api.py` (5), `test_cli_replay_calibrate.py` (3), `test_anthropic_batch.py` (3),
`test_strategy_surface.py` (2), `test_auth_rejected_surfaces.py` (2), `conftest.py` (2),
`test_ledger_replay.py`, `test_cli_leaderboard.py`, `test_benchmark_targets.py`,
`test_benchmark_official.py` (1 each). `tests/test_server.py:513-528` sets the three environment
variables; those tests are rewritten to point `OPENREADING_CONFIG` at a file.

`tests/test_policy_validation.py`: in increment 1 the `--policy` and `policy=` cases become
`--config` and `config=` cases and keep their assertions. In increment 2 the derivation test at
`:65` is replaced by the parity test, and the assertions on message text move from
`invalid policy` to the schema error format.

New in increment 1:

- `route` with no flag, in a directory holding a file with a `policy:` block, prints the plan the
  JSON policy printed for the same keys. Assert `chosen`, `fallbacks`, and the `dropped` codes.
- `route` in an empty directory prints the plan an empty policy printed. P5.
- Guardrail T10, second case: a named-backend run with a file holding only `policy:` leaves
  `openreading.strategies` out of `sys.modules`. P6.
- `parse --backend reducto` under `require_local: true` is refused naming the key. P3.
- Server with `OPENREADING_CONFIG` at a `require_local` file drops every hosted backend on a
  request that names no strategy. Setting any removed variable changes nothing.
- `run(doc, config=dict)` equals `run(doc, config=path)` for identical content.
- `run_batch` under a file block refuses a hosted backend on every item.

## 8. Documentation touchpoints

Every line is read where the fact lives. This list is exhaustive for the tree at `96d0c8f`.

**Root.** `README.md:9-15` (the definition sentence links
`src/openreading/router/README.md#how-it-decides` and says YAML), `:265-300` (the Route walkthrough
writes a YAML file), `:351-354` (`parse` and policy), `:394` (Python example). `CHANGELOG.md`
Unreleased: one Removed line naming the flag, the keyword and the three variables; one Changed
line naming discovery on every path. `AGENTS.md` Layout: one line for `config.py`.

**Package docstrings.** `api.py:8-10, 24-37, 58-61, 69-105` (recipes, exports, discovery, policy
grammar, exceptions). `cli/__init__.py:206, 311, 450, 499-530, 692, 825, 856-878, 931, 942, 1024`;
the `route` chapter heading becomes `route <file|url> [--config FILE] [--run]` and
`cli/help.py:91` moves with it. `strategies/model.py:66, 91-94, 134-146, 521-522, 933-942,
1052-1055`. `strategies/__init__.py:57`. `server/__init__.py:221, 313-316`. `server/app.py:49-53,
108-126`. `openreading/__init__.py:159` (recipe) and the Known gaps list, which drops the line
this record added. Increment 2 adds `schemas/__init__.py:524` and the manifest row at
`schemas/README.md:67`.

**Guides.** `src/openreading/router/README.md` is the owner. Walkthrough steps 1 to 7 (`:70-270`)
write YAML files and pass `--config`. Recipes (`:275-328`) lose `echo '{...}' > opt.json`. "How it
decides" keeps its key table and changes its "Source" and "Live truth" lines. A new section,
"Policies people write", sits between Recipes and "How it decides". The other guides with a
`--policy` or `policy.json` mention (35 lines across `src/openreading/*/README.md` at `96d0c8f`)
are found by grep and rewritten in place.

**The new section.** Eleven policies, each in the form the router sees, each run against
`examples/john_smith_1000_2026_01.pdf` and its drop output pasted. The measured results at
`96d0c8f`, for the implementer to re-run and paste, not to copy:

| Policy | Keeps | Drops, by code |
|---|---|---|
| `require_local: true` | 4 | 11 `not_local` |
| `no_train_on_data: true` | 8 | aws-textract, chunkr `trains_on_data`; gemini, mistral, nuextract, open-ocr, pulse `trains_unverified` |
| `data_region: eu` | 8 | anthropic, textract, chunkr `region_mismatch`; 4 `region_unverified` |
| `max_retention: zero` | 6 | azure, google-document-ai `retention_exceeds`; 7 `retention_unverified` |
| `max_retention: 48h` | 8 | 7 `retention_unverified` |
| `optimize_for: cost` | 15 | none; the chain is reordered |
| `no_train_on_data` + `train_optout_confirmed: [aws-textract]` | 9 | as "no training" with textract readmitted |
| `require_baa` + `baa_tier_confirmed: [reducto]` | 9 | chunkr, gemini, mistral, nuextract, open-ocr, pulse `no_baa` |
| `data_region: eu` + `allow_unverified_compliance: true` | 12 | the 3 `region_mismatch` only |
| the three constraints composed, `optimize_for: accuracy` | 7 | each constraint drops independently |
| no `policy:` block | 15 | none |

The section opens with P3 in one sentence, then shows a strategy step the policy dropped and the
warning it earns. It closes with how to hold two postures: two files, `--config`.

## 9. Increments and the finish line

**Increment 1, one pull request.** Sections 3, 4, 5, 7 (increment-1 rows) and 8 (all but the
schema rows). `api.validate_policy` stays and is called from `config.load`. `make verify` green.

**Increment 2, one pull request.** Section 6, the increment-2 test rows, the schema docs rows, and
the deletion of this record and its product spec. `make verify` green.

Both increments hold P1 to P6. Increment 1 satisfies acceptance criteria 1 to 6, 8, 9 and 10 of
the product spec. Increment 2 satisfies 7 in full.

## 10. What does not change

The meaning of every key. The drop codes. The union rule and the direction it can move. The three
attestation keys as the only wideners. `Router.check_eligible` on a named backend. HTTP
`request.compliance` and `request.routing` on the wire. The response envelope. The `route` plan
shape. Discovery order. `OPENREADING_CONFIG`. The four presets, which work with no file.
