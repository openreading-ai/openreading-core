---
spec_format_version: "0.1"
title: "Policy Enforcement Follow-up"
artifact_type: "prd"
spec_revision: 1
author: "Codex"
created_at: "2026-09-06T00:00:00Z"
updated_at: "2026-09-06T00:00:00Z"
applies_to:
  - path: "src/openreading/config.py"
  - path: "src/openreading/api.py"
  - path: "src/openreading/strategies/"
  - path: "src/openreading/ledger/"
  - component: "compliance-policy"
---

## Problem

OpenReading now gives policy one authoring format. Four execution paths can still disagree with
the promise that every policy is enforced before a backend receives document content.

A request can replace a stricter file retention ceiling or a different file region. A caller can
invoke public strategy compilation with a policy that compilation ignores. Resume can preserve a
removed file constraint as though the caller supplied it. A Python batch rereads its file for
each item, so one result can contain documents processed under different snapshots. Leaderboard
ignores file constraints, and publisher resume keys hash a path instead of its parsed content.

These failures are difficult to see from successful output. Each can admit a backend that one
policy source would reject, or can resume work under a changed routing order.

## Hypothesis

If every public execution boundary consumes one validated policy type, combines constraints by
field, and carries explicit provenance through batch and resume, then one YAML file will also
produce one enforcement result.

The hypothesis fails if any public call can widen a constraint, ignore a block, or observe two
file versions during one operation.

## Product Summary

File policy and request compliance form an intersection. Boolean requirements accumulate,
retention uses the shorter ceiling, and incompatible regions are refused before dispatch.

An explicit request routing preference overrides the file preference. That preference changes
ordering only, and resume records enough identity to detect a meaningful ordering change.

Public Python seams enforce the same typed policy without requiring a hidden loader sequence. A
batch reads one complete configuration snapshot and gives that snapshot to every item.

Leaderboard applies that snapshot to every dataset case. Publisher pipeline identity uses
canonical parsed content, so a meaningful edit cannot resume artifacts from an older policy.

## Scope

```productspec-scope
in:
  - "Define field-specific combination rules for compliance constraints, attestations, and routing preferences."
  - "Reject incompatible data-region requirements before any adapter is constructed."
  - "Use the stricter parsed max-retention ceiling from the file and request."
  - "Add a strict typed policy model that mirrors the vendored policy schema."
  - "Make direct strategy compilation and calibration enforce their StrategyConfig policy."
  - "Preserve original-request provenance for ledger resume."
  - "Include effective routing order inputs in resume identity."
  - "Read and validate one immutable configuration snapshot per Python batch."
  - "Apply file compliance constraints and attestations to every leaderboard case."
  - "Key publisher artifacts and resume on canonical configuration content."
out:
  - "Do not restore --policy, policy=, or the removed server environment variables."
  - "Do not add named policies or backend selection to the policy grammar."
  - "Do not change the HTTP request schema or response envelope."
  - "Do not add a policy history store or configuration reload service."
  - "Do not make optimize_for a compliance constraint."
```

## User Experience

The common file and commands stay unchanged:

```yaml
version: 1
policy:
  data_region: eu
  max_retention: zero
```

```console
$ openreading route statement.pdf
```

A server request that also asks for `data_region: us` receives a typed refusal before routing.
A request cannot silently replace the deployment's European requirement.

A Python batch observes the file once. Editing the file while the batch runs affects the next
batch, never later items in the current one.

## Acceptance Criteria

1. No request value can weaken a file compliance constraint, and no file value can weaken a
   request compliance constraint.
2. Two valid retention ceilings produce the lower parsed duration, independent of source order.
3. Two different non-empty regions produce a typed refusal before adapter construction.
4. `optimize_for` keeps request-over-file precedence and never changes backend eligibility.
5. `StrategyConfig.model_validate` strictly rejects malformed policy values and unknown keys.
6. `compile_strategy` enforces `StrategyConfig.policy` when called directly.
7. `calibrate_strategy` uses the same typed policy and combination function as normal execution.
8. `config.apply` and `router_config` cannot consume an unchecked widening dictionary.
9. Resume rejects removal or meaningful change of a file-supplied constraint or routing
   preference.
10. Resume accepts formatting-only changes whose parsed configuration is identical.
11. One Python batch performs configuration discovery, reading, and validation exactly once.
12. Every batch item uses the same policy, strategy, limits, defaults, and decider snapshot.
13. The native and platform batch paths produce identical policy verdicts.
14. Leaderboard applies all five file compliance constraints before each backend submission.
15. Publisher identity changes when configuration content changes at the same path.
16. Equivalent parsed mappings produce the same publisher identity despite key order or YAML
   formatting differences.
17. `make verify` passes with coverage at or above the existing floor.

## Success Metrics

- No accepted execution violates either its file policy or its request compliance.
- No public Python execution seam requires undocumented call ordering for policy enforcement.
- No returned batch contains items processed under different configuration snapshots.
- Every meaningful live policy change is detected before ledger resume dispatches new work.
- No leaderboard or publisher resume can silently use results from a different policy snapshot.

## Risks

- Region conflicts will become explicit refusals where request precedence previously chose one
  region silently. The refusal must name both sources and values.
- A strict typed model may expose Python callers that constructed malformed `StrategyConfig`
  objects. Those objects were never safe policy inputs.
- Changing ledger identity may make older interrupted runs non-resumable. Any version boundary
  must fail clearly before new work dispatches.
- Snapshotting the complete batch configuration adds internal plumbing. The public batch API does
  not change.

## Decisions

1. File policy and request compliance are peers in an intersection. Neither source has general
   precedence over the other.
2. A region conflict is refused because one string cannot represent two simultaneous hosted
   regions safely.
3. The request wins only for `optimize_for`, because it is an ordering preference.
4. Schema validation remains authoritative for files. A strict Pydantic mirror protects direct
   Python construction.
5. Batch snapshotting covers the whole configuration, because strategy and limits must remain
   consistent with policy.

## Open Questions

None. The design record defines the field algebra, public boundary, resume identity, and test
matrix.

## Related Artifacts

- `design/policy-enforcement-follow-up.md` contains the implementation contract and regression
  matrix.
- `src/openreading/config.py` contains the shipped one-file behavior this work tightens.
- `src/openreading/ledger/header.py` contains the current resume identity contract.
