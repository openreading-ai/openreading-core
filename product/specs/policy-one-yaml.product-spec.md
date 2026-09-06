---
spec_format_version: "0.1"
title: "One File: Policy Lives in openreading.yaml"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-09-06T00:00:00Z"
updated_at: "2026-09-06T00:00:00Z"
applies_to:
  - path: "src/openreading/config.py"
  - path: "src/openreading/api.py"
  - path: "src/openreading/cli/"
  - path: "src/openreading/server/"
  - path: "src/openreading/strategies/loader.py"
  - path: "src/openreading/strategies/prune.py"
  - path: "src/openreading/schemas/strategy-config.v0.3.json"
  - component: "compliance-policy"
---

## Problem

A policy is the short list of things a backend must declare before it may read your document, such
as a signed BAA or a European data region. Today that list can be written four ways. A JSON file
behind `--policy` on seven subcommands. A `policy:` block in `openreading.yaml`. A `policy=` dict in
Python. Three environment variables that the server, and only the server, reads for three of the
keys. All four feed one validator and mean one thing, and nothing in the code says why any of them
exists beside the others.

The cost lands on the reader. The root README calls a policy "a short JSON file". The strategy guide
shows it as a YAML block. A person who has only ever written the block reads the README and
concludes the documentation is wrong. The person who wrote the README could not point at a schema
for the JSON file, because there is none. The `policy:` block in the strategy-config schema is
declared `additionalProperties: true` on purpose, so that the JSON file could stay the primary
spelling and the block its "superset". A hand-written validator stands in for the schema that
should exist.

The engine underneath is sound. Compliance is a filter that no fallback relaxes, a silent vendor
fails closed, and a typo in a key is refused by name. None of that changes here. What changes is the
number of places a person can write the filter down, from four to one.

## Hypothesis

If the only thing a user ever writes by hand is `openreading.yaml`, and its `policy:` block is the
only place a compliance policy can be spelled, then a reader can find the whole grammar in one
schema and one guide, and the "is it JSON or YAML" question cannot arise.

The falsifiable part: if library callers who build a policy in memory find `config={"version": 1,
"policy": {...}}` too heavy and ask for `policy=` back, the single-spelling rule cost more than the
confusion it removed.

## Product Summary

You write one file. It holds your strategies and your policy. Every command and every Python call
that decides which backends may run finds that file the same way and reads the same block.

```yaml
version: 1
policy:                       # what a backend must declare before it may run
  no_train_on_data: true
  data_region: eu
strategies:
  cheap_first:
    steps: [pymupdf, docling]
    escalate_if: default
defaults:
  strategy: cheap_first
```

`openreading parse doc.pdf` finds the file, drops every backend that fails the policy, and runs
`cheap_first` inside the survivors. `openreading route doc.pdf` prints who survived and who was
dropped and why. The server reads the same file from `OPENREADING_CONFIG` and applies it to every
request. Nothing else spells a policy.

A policy never names a backend. It names a requirement, and each backend's descriptor either meets
it or does not. A strategy names backends, in order, and runs inside the set the policy left. A
strategy step the policy dropped is pruned with a warning and never readmitted.

## Scope

**Increment 1. Move the container.**
- Remove `--policy PATH` from `route`, `strategy validate`, `strategy plan`, `replay`, `calibrate`,
  `benchmark run` and `leaderboard`. Add `--config PATH` to `route` and `leaderboard`, the two of
  those seven that lack it.
- Remove the `policy=` keyword from `build_request`, `route`, `run` and `run_batch`. `config=`
  accepts a path or a dict of the file's shape.
- Remove `OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE`, `OPENREADING_TRAIN_OPTOUT_CONFIRMED` and
  `OPENREADING_BAA_TIER_CONFIRMED`. The server builds its router configuration once at startup from
  the file's `policy:` block and applies that block to every request, not only strategy runs.
- Every path that routes discovers `openreading.yaml`: `parse`, `route`, `strategy *`, `replay`,
  `calibrate`, `benchmark run`, `leaderboard`, batch, resume, the server. Discovery order is
  unchanged. No file means no policy, which is today's behaviour byte for byte.
- Move discovery, reading and schema validation of the file out of `strategies/loader.py` into a
  top-level `openreading/config.py`, so a named-backend run can read `policy:` without importing
  the strategy engine. The union of request constraints with file constraints moves there too and
  runs once, before dispatch, on every path.
- Rewrite every guide line that shows a JSON policy. Add a section of worked policies to the
  routing guide, each one run and its `dropped` output pasted.

**Increment 2. Close the schema.**
- `strategy-config` v0.2 to v0.3. The `policy:` block becomes a closed, typed object: the five
  compliance keys typed as in `request.compliance`, `optimize_for` as its enum, and the three
  attestation keys as `boolean` and `array` of `string`. `doc_type_hint` leaves the policy grammar;
  it stays a request field, and no routing stage reads it.
- Delete `validate_policy`, `POLICY_KEYS`, `PolicyError` and `prune._validated_policy`. A malformed
  block is a `ConfigError` from the schema, exit 3, the rung it already occupies.
- One parity test pins the five compliance keys in `strategy-config` equal to `request.compliance`.

**Out of scope, deliberately.**
- Any new policy key, any rename, any change to what a key means or to the union rule.
- Selecting between several policies in one file, or mapping a policy to a backend. A different
  posture is a different file, chosen with `--config` or `OPENREADING_CONFIG`, both of which exist.
- Capability matching richer than "the file requires X, the descriptor declares X". That gets
  better with private knowledge of vendors and belongs in the company repository.
- A tombstone `--policy` flag that prints a migration hint. The package is pre-release with no
  tag, and the flag is removed outright.

## User Experience

```bash
# 1. Write the one file. This is the whole grammar a person needs.
cat > openreading.yaml <<'YAML'
version: 1
policy:
  no_train_on_data: true
  train_optout_confirmed: [aws-textract]   # you applied the opt-out in the AWS console
strategies:
  cheap_first:
    steps: [pymupdf, aws-textract]
    escalate_if: default
defaults:
  strategy: cheap_first
YAML

# 2. See who may run. No flag; the file in the working directory is found.
openreading route statement.pdf | jq '{chosen, dropped: (.dropped | keys)}'

# 3. Read the document under that policy and that strategy.
openreading parse statement.pdf > out.json

# 4. A different posture is a different file.
openreading route statement.pdf --config airgapped.yaml
```

From Python, the same file is found the same way, and a caller with no file passes the shape
inline:

```python
openreading.run("statement.pdf", config={"version": 1, "policy": {"require_local": True}})
```

The server reads `OPENREADING_CONFIG` at startup, never its working directory, and fails startup
on a malformed file. A request body may still carry `compliance` and `routing`, and those union
with the file, most-restrictive-wins, as they do today for strategy runs.

The routing guide carries a section of policies people write, in the form the router sees them.
Each one is run against the sample and its drop list pasted, so the reader learns the keys from
their effect rather than from a table. The section opens with the sentence a reader needs most: a
policy never names a backend.

## Acceptance Criteria

1. `grep -rn -- '--policy' src/ tests/ README.md` returns nothing. `grep -rn 'policy=' src/`
   returns nothing outside a comment that says the keyword was removed and why.
2. `openreading route doc.pdf` in a directory holding an `openreading.yaml` with a `policy:`
   block prints the same plan that `--policy` printed for the same keys before this change. The
   test asserts equality of `chosen`, `fallbacks` and `dropped` codes.
3. `openreading route doc.pdf` in a directory with no file prints the plan an empty policy
   printed before. No file means today's behaviour, and a test pins it.
4. A run that names a backend and finds a file holding only a `policy:` block never imports
   `openreading.strategies`. The existing subprocess guardrail test gains that case.
5. The server, started with `OPENREADING_CONFIG` pointing at a file whose `policy:` says
   `require_local: true`, drops every hosted backend on a request that names no strategy. Setting
   any of the three removed environment variables changes nothing, and a test says so.
6. `openreading.run(doc, config={...dict...})` and `config="path.yaml"` produce identical
   responses for identical content.
7. A `policy:` block with an unknown key, a quoted boolean, or a bare string where a list is
   required is refused before any backend is contacted, exit 3, naming the key. After increment
   2 the message comes from the schema and the hand validator is gone.
8. `tests/test_cli_help.py` passes: the `compliance` chapter heading and its `TOPICS` row moved
   together, and no help page names `policy.json`.
9. The routing guide's policy section holds at least ten policies, each with its pasted drop
   output, and `tests/test_docs_truth.py` executes every YAML block it can reach.
10. `make verify` is green at the end of each increment, with coverage at or above the floor.

## Success Metrics

- Zero questions of the form "is a policy JSON or YAML" after the guides land. One such question
  means a guide still shows the old spelling.
- No request to restore `policy=`. One request means the inline dict shape is too heavy and the
  hypothesis failed.
- A reader who has never used the tool can write a working policy from the guide's examples
  without opening a schema file.

## Risks

- **A directory file now gates a run that never saw one.** `parse --backend reducto` in a directory
  whose `openreading.yaml` says `require_local: true` is refused after this change and ran before.
  That is the correct outcome, and the reason the file exists, but it is a behaviour change for
  anyone who kept a strategy file next to documents they parse by name. Mitigated by the
  CHANGELOG entry and by the refusal naming the file and the key.
- **The engine import guardrail.** The strategies package imports its engine eagerly, so reading
  the file through `strategies.loader` on every path would pull the engine into every run. The
  move to `openreading/config.py` avoids that, and the subprocess test is the proof. If the move
  leaves a stray import behind, the test fails, not the user.
- **Server deployments that set the three environment variables.** They stop having an effect.
  Fail closed is the default without them, so the failure mode is a hosted backend dropped that
  used to be admitted, never the reverse.
- **Two increments, one schema.** Between increments the `policy:` block is still open in the
  schema and guarded by the hand validator. That is today's state, so nothing regresses in the
  gap.

## Alternatives considered

- **Let `--policy` accept YAML and keep every container.** One-line fix, no behaviour change.
  Rejected because it leaves four spellings and adds a fifth. The confusion was never the file
  extension. It was the count.
- **Named policies inside one file, selected by `--policy NAME`.** Keeps one file and allows two
  postures per project. Rejected because it adds a selection grammar, keeps a `--policy` flag
  alive, and answers a need `--config other.yaml` already meets.
- **Keep the server's environment variables as a deployment idiom.** Rejected because they are a
  third parser for three of nine keys, with their own truthiness rules, and the server already
  loads the file at startup and fails fast on it.
- **Keep `strategies/loader.py` where it is and accept the engine import.** Fewer files touched.
  Rejected because every `parse --backend pymupdf` would then import the strategy engine, and the
  guardrail that prevents it exists for a reason the test records.

## Decisions

1. **`optimize_for` stays in `policy:`.** It is a routing preference, not a constraint, and a
   `defaults:` block exists that could hold it. Moving it is a change to the grammar, and this
   spec only removes containers. Revisit if the block ever grows a second preference.

2. **`doc_type_hint` leaves the policy grammar in increment 2.** The routing guide records that no
   stage reads it. A key that does nothing in a file that gates compliance is a key a reader
   will try to rely on. It remains a request field for callers who send it on the wire.

3. **No tombstone flag.** A removed flag fails with argparse's own "unrecognized arguments" line
   and exit 2. The CHANGELOG names the replacement. The package has no tag and no PyPI release,
   so there is nobody on an old version to migrate.

4. **The union runs once, before dispatch, in `openreading/config.py`.** Today it runs inside
   `prune.compile_strategy`, which only strategy runs reach, and `api._apply_policy` covers the
   rest. One function on every path means the server's non-strategy requests get the file's
   constraints for the first time, and nobody has to remember two places.

5. **A dict passed to `config=` is validated exactly as file text is.** Same schema, same errors,
   source labelled `<dict>`. A shape that is refused from a file is refused from Python.

## Open Questions

None outstanding. The design record `design/policy-one-yaml.md` carries the line-level audit and
the test plan.

## Related Artifacts

- `design/policy-one-yaml.md`, the design record. Both files are deleted in the pull request that
  finishes increment 2, and their durable facts move into the `openreading.config`,
  `openreading.api`
  and `openreading.cli` docstrings.
- `src/openreading/router/README.md`, "How it decides", the key table this spec does not change.
- `src/openreading/strategies/model.py`, section 1.1, the `policy:` block as written today.
