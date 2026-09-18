# Remaining general agent surface

This proposal covers capabilities beyond implemented general execution and local document profiles.
The [MCP guide](../src/openreading/mcp_server/README.md) documents implemented local evidence tools and scoped route planning.
The [artifact guide](../src/openreading/artifacts/README.md) documents retention and physical page evidence.
The [remaining product intent](../product/specs/agentic.product-spec.md) preserves the unbuilt acceptance identifiers.

## Scope boundary

Scoped resume, truth-scored and corpus comparison, readiness discovery, and triage tools remain unbuilt.
Scoped backend route planning is documented in the MCP guide; strategy planning remains proposed.
The local profile does not satisfy those requirements by exposing differently named equivalents.
It deliberately selects one backend, one explicit input grant, and bounded retained evidence.
Client packaging and token measurement belong in `openreading-agent-tools`, which depends on core.
Its approved scope is [local document proof revision 1](https://github.com/openreading-ai/openreading-agent-tools/blob/main/product/specs/local-document-proof.product-spec.md).

## Contracts requiring a future design

Internal execution preflight is documented in `openreading.mcp_server.execution` and the MCP guide.
Remaining execution tools must preserve this authority through admission and every backend dispatch.
Current core authorization narrows backend dispatch through configured backend sets and HTTP API-key scope.
The removed vendor compliance tables and their policy keys must not return through an agent interface.
No claim about a vendor's training, retention, or agreement can become an unverifiable routing guarantee.

General result retention and bounded retrieval are documented in the artifact and MCP guides.
The internal execution_process module supplies granted source copying, explicit child environments, isolated stdout and private journal/output storage.
The execution_jobs supervisor documents durable status, cancellation, reconnect and retained-result publication.
Remaining tools must retain measured reply budgets and the standalone launcher authorization boundary.
Every dispatch must retain backend scope; preflight cannot replace execution-time enforcement.
Retained normalized-response comparison is documented in the MCP guide.
Truth scoring and corpus comparison still need their own MCP input contracts.
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
