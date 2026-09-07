# Design record: remove compliance from openreading-core

Status: proposed, not built. Product intent:
[`compliance-removal.product-spec.md`](../product/specs/compliance-removal.product-spec.md).

Every line number and count in this record was measured against `e27ad9d` on 2026-09-07. Verify
each one with `grep` before editing, and find a moved line by its content rather than its number.

## 1. The finding that shapes the change

**The replacement does not exist for the callers who would lose the feature.**

`backend_allowlist` is the mechanism the product spec proposes as the replacement for compliance
constraints. It is real, it is enforced on every surface, and it has one source:

```
server/app.py:1254   backend_allowlist=scope       # POST /v1/parse
server/app.py:1545   backend_allowlist=scope       # POST /v1/batch
server/app.py:1599   backend_allowlist=...         # POST /v1/jobs
```

All three read `request.state.api_key_scope`, which comes from `OPENREADING_API_KEY_SCOPES`. A CLI
user and a Python caller cannot set it. `api.run()` does not expose it (`inspect.signature` lists
`source, backend, strategy, config, operation, env_file, mime_type, broker, transport,
keep_candidates, deadline_ms, on_run_armed, request_overrides`), and `openreading parse` has no
flag for it. `openreading.yaml` cannot express it either.

The yaml's inability is deliberate. `config.py:106` and `types/policy.py:36` both carry law P3:

> `fallback` is a request field (chain order), not a constraint, so a policy can never reorder
> someone's chain by naming backends (law P3).

Read carefully, P3 forbids **reordering**, not **subtracting**. An allow-list only ever removes
candidates, which is exactly what the API key scope already does, and the scope does not violate
P3 because subtraction cannot reorder what survives. So P3 does not block a `policy.backends`
allow-list. It does mean the law has to be restated in the same pull request, or the next reader
will find a rule that appears to forbid the key sitting next to the key.

**Consequence:** the allow-list is part of this change, not a prerequisite for it. Deleting the
filter in one change and adding the replacement in the next would leave every non-server caller
unable to bound the backend set at all for the length of a review queue. Shipping both in one
commit removes that window entirely, which is why section 6 keeps the compliance work atomic
rather than staged.

## 2. Inventory

64 source files, 842 test lines. The weight is concentrated:

| file | compliance lines / total | what it is |
|---|---|---|
| `router/compliance.py` | 80 / 267 | the filter itself; delete whole |
| `api.py` | 57 / 1566 | gate calls, `ComplianceRefused` handling, docstring contract |
| `strategies/decider.py` | 50 / 699 | the decider/judge eligibility gate |
| `config.py` | 44 / 398 | `union_compliance`, the five keys, the three attestations |
| `cli/__init__.py` | 38 / 1123 | the manual: a `compliance` chapter, exit codes |
| `strategies/prune.py` | 37 / 448 | tree pruning against the eligible set |
| `schemas/__init__.py` | 34 / 785 | schema docstring, warning codes |
| `strategies/model.py` | 33 / 1076 | grammar: `policy:` block, routable `compliance` fact |
| `strategies/engine.py` | 31 / 2806 | effective compliance threaded through the walk |
| `ledger/inline.py` | 30 / 469 | `ComplianceRefused` recording |
| `router/router.py` | 29 / 271 | stage 1 |
| `__init__.py` | 27 / 603 | package docstring, exports |
| `ledger/retention.py` | 24 / 192 | blob TTL derived from vendor claims |
| `types/policy.py` | 19 / 96 | the nine keys |
| `types/descriptor.py` | 14 / 264 | `ComplianceProfile` |
| `strategies/facts.py` | 14 / 165 | `compliance` as a routable fact |
| 15 adapter modules | 7-12 each | each declares a `ComplianceProfile` |

Dedicated test files: `test_compliance.py`, `test_policy_enforcement.py`,
`test_policy_validation.py`, `test_strategy_policy_limits.py`. 45 test files touch it in total.

## 3. What each piece is, and where it goes

### 3.1 Deleted outright

- `openreading.router.compliance`, all 267 lines: `evaluate`, `baa_tier_confirmation`,
  `_resolves_to_loopback`, `_baa_in_force`, `_region_covered`, `parse_retention_hours`,
  `RouterConfig`'s three attestation fields.
- `ComplianceProfile` (12 fields) and its member on `AdapterDescriptor`. 15 adapters x 12 fields =
  180 vendor claims.
- `Compliance` on `OpenReadingRequest`, and the `compliance` block in `request.v0.2.json`.
- Five policy keys (`require_baa`, `no_train_on_data`, `data_region`, `require_local`,
  `max_retention`) and three attestations (`allow_unverified_compliance`,
  `train_optout_confirmed`, `baa_tier_confirmed`). `policy:` goes from nine keys to two:
  `optimize_for`, and the new `backends`.
- Nine stage-1 drop codes: `not_local`, `no_baa`, `trains_on_data`, `trains_unverified`,
  `region_mismatch`, `region_unverified`, `retention_exceeds`, `retention_unverified`,
  `retention_unparseable`.
- `ComplianceRefused`, the `compliance_refused` 403, `BAA_TIER_CONFIRMED_WARNING`.
- `union_compliance` and law PF1 (request ∩ file, most-restrictive-wins). With one side gone there
  is nothing to intersect.
- The `compliance` routable fact in the strategy grammar
  (`when: { compliance: { require_local: true } }`, `model.py:382`).

### 3.2 Survives untouched, and is the replacement

`ScopeRefused` and the 403 `scope_denied` stay. `errors.py:139` already states the distinction:
compliance refuses because the DOCUMENT may not go to that backend, and scope refuses because
THIS CREDENTIAL may not reach it. After this change only the second question exists, which is
the point. 403 does not disappear from the status ladder.

`backend_allowlist` threading through `prune`, `engine`, `executor`, `router` and `decider` stays
exactly as it is. It gains sources, not behaviour.

### 3.3 Needs a new source, not a deletion

**The ledger's retention ceiling and its `zdr` branch.** `ledger/retention.py:66` derives the
ceiling from `min(max_retention_hours)` over hosted descriptors, and `inline.py:265` skips writing
blobs entirely for a backend carrying `zdr_flag`. Both are vendor claims deciding what happens to
files on the caller's own disk.

Neither gets a new source. Retention leaves core entirely, along with encryption at rest, per
[`ledger-policy-removal.md`](ledger-policy-removal.md). Landing that record first removes two of
this table's consumers before the table goes.

**Stage-3 scoring, which is not a filter and is easy to miss.** `router.py:196` reads
`compliance.runs_fully_local` three times:

```python
local = bool(desc.compliance.runs_fully_local)
if local:
    cost = 0.0                                          # a local library has no per-page price
...
local_bonus = 0.25 if (local and opt in ("cost", "offline")) else 0.0
```

Delete the field with no replacement and three things change silently: every local backend starts
being scored with the `0.05` unpriced penalty instead of zero, the bonus disappears, and
`optimize_for: offline` becomes indistinguishable from `latency` while still being a documented
enum value in `request.v0.2.json:144` and `types/request.py:146`.

`runs_fully_local` is the one field in `ComplianceProfile` that is **not** a vendor claim. Nobody
publishes it and nothing rots: pymupdf is an in-process library, tesseract is a subprocess, and
whether an adapter opens a socket is a structural fact about this repository's own code. It is
sitting in the wrong struct.

**Superseded (Akshay, 2026-09-07):** `optimize_for` is deleted too, along with
`integration_priority` and the whole scorer, per
[`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) section A1. With no scorer there is
no `offline` value to preserve and no cost term to zero, so `runs_fully_local` does not need to
survive anywhere. It leaves with the rest of `ComplianceProfile` and `descriptor.router` keeps
only `normalization_difficulty`, itself a candidate in section B of that record.

What replaces the ordering is `policy.backends`, in the order the caller writes it.

**Per-case compliance in eval datasets.** `evals/dataset.py:94` forwards a case's own
`compliance` key into the request body. Dataset files carrying that key become invalid. Since
labeled datasets live outside this repository (`internal/data/`), this is a migration note for
those files, not a code change beyond deleting the forwarding.

## 4. Schema changes

Three schemas break. None can be done as a minor version, because each removes a property that a
valid document may carry today.

| schema | today | after | change |
|---|---|---|---|
| `request` | v0.2 | v0.3 | remove `compliance` |
| `adapter-descriptor` | v0.7 | v0.8 | remove `compliance` |
| `strategy-config` | v0.3 | v0.4 | `policy` drops eight keys and gains `backends`; `when` drops the `compliance` fact |

`AGENTS.md`'s "Where a change gets documented" table requires for each: a copy to the new file, a
`*_SCHEMA_FILE` constant, a manifest row in `schemas/README.md`, the `openreading.types` default,
and a `CHANGELOG.md` line. `tests/test_schema_evolution.py` pins every released file byte for
byte, so the old files stay on disk and unmodified; only the default moves.

The descriptor is a published schema, so the company repo may read `descriptor.compliance` off
the wire. That is settled and is not a blocker: the field is deleted outright and the company repo
is fixed afterwards (Akshay, 2026-09-07).

## 5. Documentation

`AGENTS.md` states the rule twice and must change in the same pull request:

- Line 61: "Compliance is a hard filter that no fallback relaxes", one of three worked examples
  of a design decision worth writing down.
- Lines 141-142: a golden rule, "Compliance is never relaxed by fallback. Unverified compliance
  fails closed."
- Line 245: "Do not widen the compliance-eligible set from a strategy, route, or fallback."
- Line 164 and 217: the router is described as "3-stage compliance-first".

The replacement rule is worth stating as carefully as the one it replaces, because the reason is
not obvious from the diff: *core holds no fact it cannot verify. A constraint core cannot check is
a constraint core must not appear to enforce.* That sentence is what a reader two years from now
needs, and it belongs in the `openreading.router` docstring where stage 1 used to be.

Eight documents teach the feature and need rewriting, not deleting: `README.md`,
`examples/tutorial.md`, and the READMEs for `openreading`, `adapters`, `router`, `strategies`,
`server` and `evals`. `strategies/presets.py` has 11 cookbook lines built on compliance keys, and
`cli/help.py:91` registers a whole `compliance` manual chapter with `route` and `policy` aliases.

## 6. Plan

Three pull requests, and the compliance one is **atomic**. An earlier draft of this record split
it into a prerequisite plus two follow-ups. That was wrong: a repository where the filter is half
removed, or where the replacement exists but the filter still runs, is a repository that lies in a
new way for the length of the review queue. The reason the prerequisite existed was to avoid
stranding callers, and shipping the replacement inside the same change solves that completely.

**PR A. The MIME resolver.** `design/format-agnostic-intake.md` part 2. Independent of everything
here, fixes a live defect on its own, and makes PR B a smaller change by ending the
everything-is-a-PDF labelling first.

**PR B. Delete the format gate.** `design/format-agnostic-intake.md` part 1, including the
`batch-result` v0.2 bump.

**PR C. Remove compliance, in one commit.** The filter, stage 1, the request block, the descriptor
profile, the eight policy keys, the nine drop codes, `ComplianceRefused`, `compliance_refused`,
`BAA_TIER_CONFIRMED_WARNING`, the routable `compliance` fact and the eleven cookbook entries built
on it, **and** the `policy.backends` allow-list that replaces all of it, **and** the
`runs_fully_local` move that keeps scoring honest. Three schema bumps. `AGENTS.md`'s rules and the
eight documents. Large, and correct to keep whole: at no commit does a caller lack a way to bound
the backend set.

`design/ledger-policy-removal.md` lands before PR C, because it removes two of the compliance
table's consumers (`max_retention_hours`, `zdr_flag`) and is otherwise independent.

There is no deprecation window. `descriptor.compliance` is deleted outright: the company repo is
the only caller that could read it off the wire, and it is fixed after this lands rather than
before (Akshay, 2026-09-07).

## 7. Test strategy

The house rule is to write the failing test first and prove it detects the defect. For a removal
the equivalent is: write the test that proves the behaviour is gone, and watch it fail while the
old code is still present.

- For each of the nine drop codes: a test asserting the code no longer appears in any plan, run
  against the current tree first, where it must fail.
- For the request block: a test asserting `{"compliance": {...}}` is refused as an unknown field.
- For the ledger: a test asserting an armed run's behaviour is identical for a backend that used
  to carry `zdr_flag` and one that did not, since no descriptor may change what is written.
- **For scoring, the test this change would otherwise skip:** pin the current stage-3 chain order
  for `optimize_for` in each of `accuracy`, `cost`, `latency`, `offline` over the full registry,
  BEFORE touching anything. That snapshot is the proof the `runs_fully_local` move preserved
  behaviour, and without it the regression is invisible: routing still returns a plan, just a
  different one.
- **For the allow-list, one test per public execution surface**, not one test of the machinery:
  `run`, `route`, `compare`, `batch`, `resume`, a strategy walk, and the four server endpoints
  that execute. Each asserts the file, caller and API-key sets intersect, that an empty list
  refuses with `scope_denied`, and that an absent list restricts nothing.
- **Delete assertions, not files.** Only `tests/test_compliance.py` goes whole (12 of its 15
  tests are compliance by name, and the rest are the endpoint-resolution helpers that go with it).
  The other three carry real coverage of things this change does not touch, and grepping them by
  filename would throw it away:
  - `test_strategy_policy_limits.py` (10 tests):
    `test_file_level_zero_duration_ceiling_prunes_hedge`
    and `test_file_level_nonzero_duration_ceiling_still_prunes_hedge` are duration-limit coverage,
    plus `test_limits_does_not_touch_direct_named_requests`.
  - `test_policy_validation.py` (28 tests, 1 compliance-named): almost all of it is
    malformed-config behaviour, unknown-key refusal, the misspelling suggestion, and the
    every-subcommand sweep. It needs the key count updated from nine to two, not deletion.
  - `test_policy_enforcement.py` (36 tests, 8 compliance-named): snapshot consistency and resume
    behaviour live here.
- The coverage floor must not drop. Removing this much well-covered code moves the percentage;
  check it after the change lands and raise the floor if the number goes up.

## 8. What this record does not decide

1. ~~The `policy.backends` grammar.~~ Settled: a flat list (Akshay, 2026-09-07). A per-verb
   mapping can only express "this vendor may parse but not extract", which is a belief about
   vendor behaviour and the exact thing this change removes. A flat list can widen to a mapping
   later without a break; the reverse cannot.
2. Whether `openreading backends` keeps showing any compliance-ish column. It should not, but the
   command's output shape is user-visible and worth a separate look.
3. With the scorer gone, what orders a chain when `policy.backends` does not say. It must be a
   documented deterministic order, not an incidental one, and it needs a test either way.
