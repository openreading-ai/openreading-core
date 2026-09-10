---
spec_format_version: "0.1"
title: "Remaining General Agent Surface"
artifact_type: "prd"
spec_revision: 2
author: "Akshay"
created_at: "2026-08-03T00:00:00Z"
updated_at: "2026-09-10T00:00:00Z"
---

## Problem

General agent integrations still implement their own interpretation of routing, batch failures, and comparison results.
The fixed local document profile does not provide these general operations or automated triage.

## Hypothesis

Typed general tools and observable verdicts could reduce duplicated result handling in agent integrations.
That hypothesis requires evidence beyond successful local document retrieval and bounded page citations.

## Product Summary

This revision preserves the unbuilt general surface while separating the implemented local document proof.
The [MCP guide](../../src/openreading/mcp_server/README.md) documents the actual three-tool local profile.
The [remaining design](../../design/agentic.md) identifies future authorization and triage work.
Client installation and token measurement belong in the separate `openreading-agent-tools` repository.

## Scope

General parse, batch, route, compare, readiness, triage, warning registries, and orchestration schemas remain proposed.
Removed compliance policy keys are excluded; future authorization must use mechanisms current core can actually enforce.
No acceptance criterion below is marked complete by the fixed local document profile.

## Acceptance Criteria

```productspec-acceptance-criteria
- id: AC-1
  criterion: An agent can discover which backends are ready, parse a document, read the verdict, and act on it, without a human reading any output at any step.
- id: AC-2
  criterion: When an agent passes a policy setting that would widen which backends are eligible, the call fails with an error naming the rejected setting; a narrowing policy setting is honored.
- id: AC-3
  criterion: Installing without the agent extra leaves every existing response and decision record byte-identical to the previous release, apart from the class added to failed batch documents.
- id: AC-4
  criterion: Every value in a returned receipt is copied or computed from the run it describes, text previews are truncations rather than summaries, and the verdict inside a receipt matches the verdict produced for that same result on its own.
- id: AC-5
  criterion: Any single result, batch result, or failure can be turned into exactly one of accept, retry, escalate, or reject, and every reason given cites the field it was read from.
- id: AC-6
  criterion: A verdict never names a backend to try next, so it cannot route around an authorization decision the run already made.
- id: AC-7
  criterion: An agent can ask for a comparison of two runs it already made and get a verdict back, and asking for that comparison never runs a backend.
- id: AC-8
  criterion: An agent can load the warning codes and the routing-trace shape as data from the server instead of reading prose documentation.
- id: AC-9
  criterion: A backend that writes to standard output during a run does not corrupt the agent session.
- id: AC-10
  criterion: Every failed document in a batch carries a class from a fixed set that says whether re-running it could succeed.
- id: AC-11
  criterion: The receipt returned from an agent-surface call stays small enough to read in an agent's context no matter how large the artifact it describes is, and that full artifact stays retrievable by the identifier the receipt carries.
```

## Success Metrics

External integration adoption and reductions in manual result handling need baselines before a general surface ships.
No numerical target or measured result is claimed by this proposal.

## Risks

A general tool can spend provider credentials and therefore requires explicit authorization and bounded execution.
A triage verdict must cite observed evidence and cannot silently select an unauthorized backend.

## Related Artifacts

- [Remaining engineering design](../../design/agentic.md).
- [Implemented local evidence guide](../../src/openreading/artifacts/README.md).
