> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. It was written before the monorepo
> split and predates protocol v2, adapter-descriptor v0.7 and the current `.env.example` posture,
> so verify every fact against this repository's code before implementing from it. Delete this
> file in the pull request that finishes the work, moving its durable facts into the module
> docstrings.

---
spec_format_version: "0.1"
title: "Agent Surface"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-08-03T00:00:00Z"
updated_at: "2026-08-03T00:00:00Z"
applies_to:
  - path: "src/openreading/mcp_server/"
  - path: "src/openreading/triage.py"
  - component: "llm-agent-surface"
---

## Problem

Teams putting document processing into unattended pipelines still need a person in the loop at
the one place it is most expensive: reading the result and deciding what to do about it. The
output is already machine-decidable — a closed status vocabulary, an honest error taxonomy, a
trace of every routing decision — but the meaning of those fields lives in prose documentation,
so the judgment gets re-implemented by hand in every consumer, or a human keeps doing it.

An agent has no seat at all. It can shell out to the CLI or post to the HTTP server, borrowing a
surface built for a person or a program, and then it must parse prose to know whether a warning
matters. Nothing tells it which documents in a failed batch are worth retrying, and nothing lets
it act on a bad result other than to stop and escalate to a human.

## Hypothesis

If agents get a native seat — tools they call directly, a verdict they can branch on, and the
output contracts loadable as data instead of prose — then document processing gets operated
unattended end to end, because the judgment a human currently supplies is available to the
caller as a typed answer. If that holds, the agent seat is not a convenience on top of the
product; it is the reason a team picks it.

## Product Summary

Three things ship together. The output contracts become machine-loadable: the set of warning
codes and the shape of the routing trace stop being prose and become schemas an agent can read,
and a failed document in a batch carries a class that says whether retrying could help. A
verdict function turns any result — one document, a batch, or an outright failure — into one of
four words with the reasons that produced it. And an MCP server gives an agent six tools and
three families of readable resources, so parsing, routing, comparing, and judging are things it
calls rather than things it scrapes.

Nothing here spends money on an LLM. The judgment is deterministic, and the one part of the
product that would call a model is a separate bet.

## Scope

```productspec-scope
in:
  - Publish the warning codes and the routing-trace shape as loadable contracts in this version.
  - Give every failed batch document a machine-readable class saying whether a retry could help.
  - Ship a verdict function that turns a result, a batch, or a failure into one of four words with its reasons.
  - Ship an MCP server exposing six tools and three families of readable resources in this version.
  - Return a small receipt from every agent-surface call instead of an envelope too large to read, with the verdict already inside it.
out:
  - Do not build the in-run LLM decider in this version; it is a separate bet with its own spending decision.
  - Do not expose the verdict over the HTTP server in this version.
  - Do not expose long-running jobs through the agent surface in this version.
  - Do not extend corpus comparison to the library and HTTP surfaces in this version.
  - Do not send progress notifications during long batches in this version.
cut:
  - Cut splitting the review-escalation warning from the quality-escalation warning, which would silently change what existing gates fire on.
  - Cut consolidating the near-duplicate channel warning codes, which would break gates users have already written.
```

## User Experience

The externally observable shape of this work is an agent transcript with no human turn in it.
An agent asks which backends are ready, parses a document, and reads a verdict off the receipt
it gets back. On `accept` it consumes the text. On `escalate` it parses the same document with a
second backend and asks for a comparison of the two runs it just made, then branches on whether
they actually disagree. On a batch, a `retry` verdict hands back the list of documents worth
re-running, which it feeds straight back in. At any point it can read the contracts
themselves — the schemas, the warning registry — to check its own interpretation.

Operator-facing documentation for the surface ships with it, at `docs/mcp.md`: launch
configuration, the tool list, what the receipt contains, and where the keys get spent.

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
  criterion: A verdict never names a backend to try next, so it cannot route around a compliance decision the run already made.
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

```productspec-success-metrics
- id: SM-1
  metric: external_agent_integrations
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: 90 days after the agent surface ships
- id: SM-2
  metric: runs_acted_on_without_human_review
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: 60 days after the agent surface ships
- id: SM-3
  metric: agent_extra_install_share
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: 90 days after the first published release
```

## Risks

Agents holding an operator's keys at one remove is the central trust question, and the surface is
built to fail loudly rather than quietly: a request that would widen compliance is rejected by
name rather than dropped, and run artifacts land in a private per-process location rather than in
the user's project.

Spend confusion is a real risk in an agent surface, and this version answers it by containing no
path that calls a model at all. This version documents that its own tools spend a provider key;
documenting what an armed in-run decider additionally costs belongs to that separate milestone,
which revises this wording when it ships.

The largest risk is not technical. This work assumes the agent seat is what makes teams adopt the
product, and it would be built before the product is distributed at all — see Open Questions.

## Open Questions

- **Is this bet worth building before distribution?** The package is not yet published, so an
  agent surface is reachable only by operators who have already installed from source. Building
  it first assumes the agent seat drives adoption rather than being something adopters ask for
  once they arrive. Decide: build the agent surface now, or publish first and let demand order
  the work.
- **What pre-launch proof would validate the hypothesis?** The acceptance criteria prove the
  surface works. None of them prove anyone wants it. Decide what evidence would count — one real
  integration outside this repository, one worked end-to-end run against a real corpus, or an
  explicit decision to build on conviction without that proof.
- **Which success metric is the real scoreboard, and what number would mean it worked?** All
  three targets are provisional with no baseline behind them. Decide which metric the bet is
  actually judged on and what value counts as success, or record that it ships without a
  committed target and why that is acceptable.

## Related Artifacts

```productspec-related-artifacts
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Agentic design — the milestone specification"
- type: engineering_spec
  url: "docs/design/agentic-prompt.md"
  title: "Agentic build-loop prompt — phase-by-phase execution"
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design section 14 — the closed agent loop acceptance"
  section_id: acceptance_criteria
  item_id: AC-1
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-2 — narrowing-only in-band policy"
  section_id: acceptance_criteria
  item_id: AC-2
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design section 0 and test layer T1 — additive, byte-identical bare install"
  section_id: acceptance_criteria
  item_id: AC-3
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-3 — receipts never fabricate"
  section_id: acceptance_criteria
  item_id: AC-4
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design section 6 — the triage rules and verdict precedence"
  section_id: acceptance_criteria
  item_id: AC-5
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-6 — triage never names a backend"
  section_id: acceptance_criteria
  item_id: AC-6
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-9 and section 9.1a — the compare tool never runs a backend"
  section_id: acceptance_criteria
  item_id: AC-7
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-5 and sections 4 to 5 — the contracts as vendored schemas"
  section_id: acceptance_criteria
  item_id: AC-8
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-7 — structural standard-output purity"
  section_id: acceptance_criteria
  item_id: AC-9
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design invariant AG-4 and section 5 — machine-readable batch failure classes"
  section_id: acceptance_criteria
  item_id: AC-10
- type: engineering_spec
  url: "docs/design/agentic.md"
  title: "Design section 9.3 — the run receipt and payload discipline"
  section_id: acceptance_criteria
  item_id: AC-11
- type: product_spec
  product_spec_path: "./decider.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "In-Run Decider — the separate spending bet this version excludes"
```
