# Design records

Proposals for work this repository has not shipped. `AGENTS.md` keeps them here, next to the code
they propose to change, so they are reviewed in the open. **A record is deleted in the pull
request that finishes its work**, with its durable facts moved into the module docstrings. A
record that outlives its feature is the authoritative-and-wrong document the whole rule exists to
prevent.

## The removal set (2026-09-07)

Six records, one argument, plus two checklists. They were written together and share a single
test:

> **A fact core cannot verify must not change what core does.** It may be documentation, clearly
> marked and dated. It may not be a routing input, a gate, or a default.
>
> Two corollaries. Being wrong must produce an error, not a quieter success. And policy about the
> caller's own machine is the caller's.

[`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) states the test, lists everything
that fails it, and records four things checked and found honest so the next sweep does not
re-litigate them.

[`documentation-surface.md`](documentation-surface.md) is what every row owes the docs, and which
guards catch a stale document automatically. In a repository where the documentation is the code,
a removal that leaves its chapter behind ships a manual describing a flag the binary rejects.
**No row defers its documentation to a follow-up.**

[`test-surface.md`](test-surface.md) is the same question asked of the suite: 1,249 test lines
across 69 of 162 files name something being deleted. It carries the one item with an ordering
requirement, ahead of row 0. **Extend `tests/golden/` into a characterization suite before any
code moves**, because the risk in six sweeping removals is not the behaviour anyone argued about,
it is the coupling nobody thought to mention.

### Implementation order

Each row is independently shippable and green on its own. Later rows assume earlier ones.

| # | Record | What lands | Depends on |
|---|---|---|---|
| pre | [`test-surface.md`](test-surface.md) §1 | the characterization suite: pinned envelopes per surface, local backends only | **must precede row 0** |
| 0 | [`explicit-backends.md`](explicit-backends.md) §2 | the `page_range_selection` dead-gate fix, alone, as an ordinary bug fix | nothing |
| 1 | [`format-agnostic-intake.md`](format-agnostic-intake.md) part 2 | one MIME resolver, `puremagic>=1.30,<2`, no PDF default | nothing |
| 2 | [`format-agnostic-intake.md`](format-agnostic-intake.md) part 1 | delete the format gate, `batch-result` v0.2 | 1 |
| 3 | [`ledger-policy-removal.md`](ledger-policy-removal.md) | delete retention, the reaper and encryption at rest; drop `cryptography` | nothing |
| 4 | [`compliance-removal.md`](compliance-removal.md) + [`explicit-backends.md`](explicit-backends.md) | **one atomic change**: the compliance filter, `optimize_for` and the scorer, the capability gate, `auto`, and the `policy.backends` that replaces all of them | 3 |
| 5 | [`cost-removal.md`](cost-removal.md) | delete the price tables and every dollar figure; keep the counters the vendor returned | 4 |

Row 4 is deliberately not split. Every intermediate state is a repository that lies in a new way:
a half-removed filter, or a replacement that exists while the thing it replaces still runs. The
reason to stage it was to avoid stranding callers, and shipping the replacement in the same commit
removes that window entirely.

Row 3 goes before row 4 because the ledger's retention ceiling and its `zdr` branch read
`max_retention_hours` and `zdr_flag`, so it removes two of the compliance table's consumers first.

Two guards make the documentation non-optional rather than merely expected.
`tests/test_docs_truth.py` executes the YAML in `examples/tutorial.md`, so its step 8
`policy: {require_local: true}` block fails the build the moment that key leaves the schema. And
`tests/test_cli_help.py` holds `cli/help.py`'s `TOPICS` and the `openreading.cli` docstring to a
bijection, so the doomed `compliance` and `cost` chapters cannot be left behind. Both break in
row 4 and row 5 respectively, in the same commit as the code.

### What row 4 deletes, in one place

- The compliance filter, `ComplianceProfile` (180 vendor claims across 15 adapters), the request
  `compliance` block, nine drop codes, `ComplianceRefused`, `compliance_refused`,
  `BAA_TIER_CONFIRMED_WARNING`.
- `optimize_for`, `_WEIGHTS`, `_QUALITY_BY_PRIORITY`, `integration_priority`, `priority_reason`,
  and stage 3 entirely.
- The capability gate, `_FEATURE_CAPABILITY`, `_truthy_cap`, the `missing_<cap>` drop codes.
- `auto`, at 67 source sites, 3 schema properties and 132 test lines.
- Added in the same commit: `policy.backends`, a flat list in preference order, reaching the
  `backend_allowlist` machinery that only the server's API key scope can set today.

Selection afterwards is a lookup with no inference in it: the backend the caller named, else
`policy.backends` in written order, else `pymupdf`.

### Schema versions this set moves

| schema | from | to | why |
|---|---|---|---|
| `request` | v0.2 | v0.3 | remove `compliance`, `optimize_for`, `auto` |
| `adapter-descriptor` | v0.7 | v0.8 | remove `compliance`, `cost`, `integration_priority`, `priority_reason`, the unread fields |
| `strategy-config` | v0.3 | v0.4 | `policy` drops eight keys and gains `backends`; `when` drops the `compliance` fact |
| `batch-result` | v0.1 | v0.2 | remove `skip_reason`, `skipped` state, `summary.skipped`, `cost_usd`, `cost_bases[]` |
| `response` | v0.3 | v0.4 | remove `usage.cost_usd`, `usage.cost_basis` |
| `comparison-report` | v0.2 | v0.3 | remove `cost_outlier` from the closed finding enum, drop the cost facts row |

If rows 1 to 5 ship in one release, bump each schema **once** and carry every change. Shipping
`adapter-descriptor` v0.8 and then v0.9 a week apart costs two migrations for one intent.

Frozen historical files stay on disk byte for byte (`tests/test_schema_evolution.py`), so they
still contain the removed properties. No acceptance check may be a blanket `grep` over `src/`.

### Not decided yet

- **Twelve descriptor fields read at zero sites**
  ([`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) section B). Mechanical, no
  behaviour change, can ride along with any row above.

### Settled since the first draft

- **`Cost`** is row 5, deleted ([`cost-removal.md`](cost-removal.md)). Core keeps the counters a
  vendor returned and never converts them to money, because the price that is true for a caller is
  the one on their own invoice.
- **`openreading route` and `POST /v1/route`** are repointed at readiness rather than deleted
  (Akshay, 2026-09-07). With nothing dropped or scored they would only echo the caller's list
  back; answering "here is your chain, and here is which of these are configured on this machine"
  is a real question with a verifiable answer.

## Older records

[`agentic.md`](agentic.md), [`decider-executor.md`](decider-executor.md),
[`intent.md`](intent.md), [`run-stats-analytics.md`](run-stats-analytics.md) propose features this
repository has not built. They predate the removal set and describe a router with stages that
these records delete, so read them against the code before implementing from them.
