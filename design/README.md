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

## Nothing is proposed right now

The last open item was `unverifiable-claims-sweep.md` section B, which proposed deleting the
descriptor fields nothing reads. It is resolved differently, and the record is gone with it
(Akshay, 2026-09-07): **the descriptor keeps its vendor claims as documentation, and core never
branches on one.** Deleting them would have thrown away something a person choosing a backend
actually reads. Behaving on them is what core has no business doing, because a claim about a
company this project does not control goes stale without notice and nothing here can detect it.
That is the enterprise product's problem, not this one's.

The rule is mechanized rather than remembered. `openreading.types.descriptor` states which fields
are load-bearing (facts about this machine, verified every run) and which are documentation, and
`tests/test_descriptor_is_documentation.py` asserts every vendor claim is read at zero sites, so a
change that starts branching on one fails `make verify` and has to argue for it. The refresh
procedure for keeping the claims current lives in the `openreading.adapters` runbook.

## Older records

[`agentic.md`](agentic.md), [`decider-executor.md`](decider-executor.md),
[`intent.md`](intent.md), [`run-stats-analytics.md`](run-stats-analytics.md) propose features this
repository has not built. They predate the removal set and describe a router with stages that no
longer exist, so read them against the code before implementing from them.
