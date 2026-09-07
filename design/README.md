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

## Still proposed

- [`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) **section B**: twelve descriptor
  fields read at zero sites. Mechanical, no behaviour change, lands alone as a schema cleanup. The
  rest of that record shipped and is trimmed to a pointer.

## Older records

[`agentic.md`](agentic.md), [`decider-executor.md`](decider-executor.md),
[`intent.md`](intent.md), [`run-stats-analytics.md`](run-stats-analytics.md) propose features this
repository has not built. They predate the removal set and describe a router with stages that no
longer exist, so read them against the code before implementing from them.
