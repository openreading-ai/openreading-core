# Remaining general agent surface

This proposal covers work beyond the implemented fixed local document profile.
The [MCP guide](../src/openreading/mcp_server/README.md) documents the implemented import, search, and read tools.
The [artifact guide](../src/openreading/artifacts/README.md) documents retention and physical page evidence.
The [remaining product intent](../product/specs/agentic.product-spec.md) preserves the unbuilt acceptance identifiers.

## Scope boundary

General parse, batch, route, compare, readiness discovery, and triage tools remain unbuilt.
The local profile does not satisfy those requirements by exposing differently named equivalents.
It deliberately selects one backend, one explicit input grant, and bounded retained evidence.
Client packaging and token measurement belong in `openreading-agent-tools`, which depends on core.
Its approved scope is [local document proof revision 1](https://github.com/openreading-ai/openreading-agent-tools/blob/main/product/specs/local-document-proof.product-spec.md).

## Contracts requiring a future design

A general tool surface must define authorization before accepting backend selection or strategy execution.
Current core authorization narrows backend dispatch through configured backend sets and HTTP API-key scope.
The removed vendor compliance tables and their policy keys must not return through an agent interface.
No claim about a vendor's training, retention, or agreement can become an unverifiable routing guarantee.

General tool results need independently bounded receipts and explicit retained artifact access.
The existing normalized response remains the extraction contract, while tool receipts use separate schemas.
A compare operation over retained results must not execute either backend again.
A general extraction operation must identify any provider calls before the caller authorizes their execution.

## Triage remains proposed

A triage function would classify existing results as accept, retry, escalate, or reject.
Every reason must cite an observed field instead of inventing confidence or provider capabilities.
A verdict cannot select another backend or widen the operator's authorization.
Its semantics must cover single results, batch failures, and comparison findings consistently.

The warning registry, orchestration schema, and batch failure classification remain separate unbuilt contracts.
Their versions, compatibility rules, and cross-surface behavior require review before implementation.
The decider executor remains a separate proposal in [its design](decider-executor.md).

## Acceptance and evidence

Future implementation must cover ProductSpec AC-1 through AC-11 against the actual general tools.
The current local profile's tests provide no evidence for hosted authorization, triage, or general batch behavior.
Offline fixtures must cover provider failures without requiring keys or network access.
Real provider and client checks remain explicit lanes with recorded versions and reviewed evidence.

When a future capability lands, move its durable contract beside the code and remove its completed proposal here.
