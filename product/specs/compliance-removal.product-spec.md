---
spec_format_version: "0.1"
title: "Remove compliance from openreading-core"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-09-07T00:00:00Z"
updated_at: "2026-09-07T00:00:00Z"
applies_to:
  - path: "src/openreading/router/compliance.py"
  - path: "src/openreading/schemas/"
  - path: "src/openreading/types/policy.py"
  - path: "src/openreading/types/descriptor.py"
  - path: "src/openreading/config.py"
  - path: "src/openreading/ledger/retention.py"
  - component: "compliance-filter"
---

## Problem

This repository ships a compliance filter that decides which backends a document may go to. It
reads a per-vendor table it keeps in code: whether each vendor signs a business associate
agreement (BAA), whether the vendor trains on customer data, which regions it offers, and how many
hours it retains a document. Fifteen adapters each declare a twelve-field `ComplianceProfile`.
Stage 1 of the router reads those declarations and drops backends against a caller's constraints.

The table cannot be true. Every field in it is a claim about a company this project does not
control, published on a page that changes without notice, and a reader has no way to tell a fact
verified last week from one copied at import time in 2026. The repository has no mechanism that
detects drift, and cannot have one: nothing here can observe whether a vendor still signs a BAA.
A wrong entry does not fail loudly. It routes a document to a backend the operator believed was
excluded, and the run succeeds.

Three properties make this worse than an ordinary stale document.

**It looks authoritative.** `require_baa: true` reads as a guarantee. What it means today is "a
value someone typed into a Python file survived review", and the operator relying on it cannot see
that distinction from the outside.

**It is load-bearing in the wrong places.** The ledger derives its own blob retention ceiling from
`min(max_retention_hours)` across hosted descriptors, so a vendor number this repo cannot verify
sets how long the caller's own documents persist on the caller's own disk.

**It grows.** The filter already carries nine drop codes, three operator attestation keys, a
`tier_gated` tri-state, an `unverified` tri-state and an `allow_unverified_compliance` escape
hatch. Each was added because the previous shape could not express a real deployment. The next
deployment will need the next one. The surface is 64 source files and 842 test lines today.

The recent alias defect is the shape of the whole feature. A `credentials_ref` alias could select
a remote endpoint while `require_local` still passed, because the gate read one environment source
and execution read another. That bug was real and the fix was correct, but it existed only because
core promised something core cannot know.

## Hypothesis

The engine does not need to know what a vendor permits. It needs to do what the caller says.

A caller who cares about compliance already knows their own posture: which vendors they hold
agreements with, which regions their contracts cover, what their auditors accepted. That knowledge
is theirs, it is current, and it is the only version of it that is correct. Core's job is to obey
it, not to second-guess it from a table.

The mechanism that expresses "only these backends" already exists and is already enforced on every
surface: `backend_allowlist`, threaded through `prune`, `engine`, `executor`, `router` and the
server's API key scopes. A caller who writes an allow-list of four vendors they have agreements
with gets exactly the guarantee `require_baa` pretends to give, sourced from the one party who can
know it.

## Product Summary

`openreading-core` becomes unaware of and agnostic to compliance. It obeys the caller's laws.

Remove the compliance filter, the per-vendor compliance table, the request `compliance` block, the
five compliance keys and three attestation keys in the `openreading.yaml` `policy:` block, the
`ComplianceRefused` error, the `compliance_refused` 403, the nine stage-1 drop codes, and the
`BAA_TIER_CONFIRMED_WARNING`.

What a caller uses instead is the backend allow-list they already have. Naming the backends you
permit is a statement core can honour exactly, forever, with no table to rot.

The vendor mapping moves to the commercial product, where it can be maintained as a dated,
sourced, reviewed dataset with someone accountable for its accuracy. That is a better home for it
on the merits, not only a commercial convenience: a claim about a vendor needs an owner, a
verification date and a change feed, and none of those things belong in a library's source code.

## Scope

In scope:

- Delete `openreading.router.compliance` and the router's stage 1.
- Delete `ComplianceProfile` from `AdapterDescriptor` and from all fifteen adapters.
- Delete the `compliance` block from the request schema and `OpenReadingRequest`.
- Reduce the `policy:` block from nine keys to one (`optimize_for`).
- Delete `ComplianceRefused`, `compliance_refused`, `BAA_TIER_CONFIRMED_WARNING` and the nine
  stage-1 drop codes.
- Give the ledger's retention ceiling an operator-set source instead of a vendor-derived one.
- Bump `request`, `adapter-descriptor` and `strategy-config` to new major-breaking versions.

Out of scope:

- The backend allow-list and API key scopes. They stay exactly as they are; they are the
  replacement, and they need no change to become it.
- `ScopeRefused` and the 403 `scope_denied`. A caller-declared allow-list is a caller's law, so it
  survives untouched.
- Whether a container endpoint resolves to loopback. That check exists only to serve
  `require_local` and goes with it.
- Any attempt to ship a compliance mapping elsewhere in this repository, in any form, including a
  data file, an optional extra or a documented example that would be read as authoritative.

## User Experience

Before, an operator wrote a posture in `openreading.yaml` and trusted core's table to enforce it:

```yaml
policy:
  require_baa: true
  no_train_on_data: true
```

After, the operator writes the conclusion that posture leads to, which only they can reach:

```yaml
policy:
  backends: [aws-textract, azure-document-intelligence, pymupdf]
```

The second is shorter, needs no attestation keys, cannot silently drift, and says a true thing.
The work of deciding which three vendors belong in that list is work the operator was always
doing; core was only pretending to do it for them.

## Acceptance Criteria

1. `grep -rniE "hipaa|baa|trains_on_customer|data_region_options|max_retention_hours|require_local|
   no_train_on_data|allow_unverified_compliance" src/` returns nothing.
2. No adapter declares any compliance field, and `AdapterDescriptor` has no compliance member.
3. The router has two stages, and no stage reads a vendor claim.
4. A request carrying a `compliance` block is refused by the request schema as an unknown field,
   not silently ignored.
5. The ledger's retention ceiling comes from `OPENREADING_LEDGER_RETENTION_HOURS` and a documented
   default, with no term derived from any descriptor.
6. `backend_allowlist` remains enforced on every surface, with its existing tests unchanged.
7. `make verify` is green, and the coverage floor does not drop.
8. `CHANGELOG.md` carries a `Removed` section naming every deleted key, code and error, with the
   one-line migration for each.

## Success Metrics

- Source files touching compliance: 64 to 0.
- `policy:` keys: 9 to 1.
- Test lines asserting on vendor claims: 842 to 0.
- Vendor facts this repository asserts and cannot verify: 180 (15 adapters x 12 fields) to 0.

## Risks

**A user reads the removal as the engine becoming unsafe.** It is the opposite: the engine stops
making a promise it could not keep. The `Removed` changelog entry and the migration line have to
say that plainly, in the operator's terms, or the change will be read as a regression.

**Someone depends on the filter today.** The migration is mechanical (name your backends) but it
is not automatic, and a deployment that upgrades without editing its `openreading.yaml` loses a
constraint it thought it had. This is a breaking change across three schemas and must ship as one,
loudly, never as a quiet default change.

**The temptation to keep "just `require_local`".** It is the one constraint core could arguably
honour, since `runs_fully_local` is structural rather than a vendor claim. Keeping it keeps the
block, the drop code, the endpoint resolution and the fail-closed posture, which is most of the
surface for one flag. An allow-list of local backends expresses the same thing with no mechanism.

## Open Questions

1. Does anything outside this repository read `AdapterDescriptor.compliance` off the wire? The
   descriptor is a published schema, so the company repo and any third-party adapter author are
   the callers to check before the field is deleted rather than deprecated.
2. Should `policy:` gain a `backends:` key in this change, or is the existing `backend_allowlist`
   plumbing already reachable from the file? If it is not, the replacement is not actually
   available to the operator on the day the filter is removed, and the two must ship together.
3. Does the leaderboard, calibrate or evals path lose a behaviour that had nothing to do with
   vendor claims, and that a reader would miss?

## Related Artifacts

- `design/compliance-removal.md`, the migration and the file-by-file inventory.
- `src/openreading/router/README.md`, whose "How it decides" section describes the three stages.
- `AGENTS.md`, whose golden rules currently name compliance twice and must be edited in the same
  pull request.
