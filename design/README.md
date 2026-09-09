# Design records

Proposals for work this repository has not shipped. `AGENTS.md` keeps them here, next to the code
they propose to change, so they are reviewed in the open. **A record is deleted in the pull
request that finishes its work**, with its durable facts moved into the module docstrings. A
record that outlives its feature is the authoritative-and-wrong document the whole rule exists to
prevent.

## The removal set has shipped

Six records and two checklists were written together in September 2026 and landed as one branch.
They shared a single test, kept here because the next sweep should apply it without rediscovering
it:

> **A fact core cannot verify must not change what core does.** It may be documentation, clearly
> marked and dated. It may not be a routing input, a gate, or a default.
>
> Two corollaries. Being wrong must produce an error, not a quieter success. And policy about the
> caller's own machine is the caller's.

Those records are deleted, as the rule above requires. What each removal avoids is written where
the code that replaced it lives: `openreading.router.router` for selection, `openreading.types.cost`
and `openreading.router.cost` for usage counters, `openreading.derive.mime` for type resolution,
`openreading.ledger` for what arming it copies. `CHANGELOG.md` under Unreleased carries the
reader-facing account of every one, including the schema cuts and the two behaviour changes that
outlive the argument (tie-breaks resolve on written order; the benchmark prompt fires on pages and
on an unbounded call count rather than on a dollar total).

## Open proposals

Each record proposes unbuilt work and has a product spec beside it in `product/specs/`.
The product spec defines what you get, while the design record defines the proposed implementation.

| Record | Proposes | Product spec |
|---|---|---|
| [`agentic.md`](agentic.md) | the agent surface: `openreading mcp` tools and `triage` | `agentic.product-spec.md` |
| [`decider-executor.md`](decider-executor.md) | the wire executor behind `DeciderPort` — the real LLM call, the caller's key, offline replay | `decider.product-spec.md` |
| [`intent.md`](intent.md) | the intent schema and its routing mechanics | `intent.product-spec.md` |
| [`run-stats-analytics.md`](run-stats-analytics.md) | run stats and routing analytics over the journal | `run-stats-analytics.product-spec.md` |
| [`http-file-uploads.md`](http-file-uploads.md) | multipart file uploads and client folder iteration | [`http-file-uploads.product-spec.md`](../product/specs/http-file-uploads.product-spec.md) |

`product/specs/hallucination-detection.product-spec.md` has product intent and no design record
yet.

**The first four records predate the removal set and have not been re-scoped since.** Every one was written
against a router with three stages, and there is one lookup now. `intent.md` is the worst
affected: its central lock reads "intent is read only by stage-3 scoring", and there is no stage
3 to read it. Each file carries a dated warning at its head saying so. Re-scope before
implementing, and read `CHANGELOG.md` under Unreleased first.

The HTTP file-upload proposal follows the current router and requires approval before any implementation work begins.

Nothing else in this directory is a proposal.
