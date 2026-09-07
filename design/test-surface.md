# Design record: the tests each removal owes

Status: proposed, not built. A checklist, and one thing that must happen before any code moves.

Measured against `e27ad9d` on 2026-09-07. Applies to every row in [`README.md`](README.md).
Sibling of [`documentation-surface.md`](documentation-surface.md); same question asked of the
suite instead of the prose.

Every record already carries a test-strategy section. This one covers what none of them say,
because it only becomes visible when you look at all six together.

## Scale

```
total test lines                        48,767
lines naming something being deleted     1,249
test files touched                          69   of 162
fixture files carrying a usage block         15
```

## 1. Write the characterization suite first, before any row lands

This is the only item here with an ordering requirement, and it is the highest-value work in the
whole programme.

Six sweeping removals share one risk: not the behaviour anybody argued about, but the behaviour
nobody thought to mention. Does deleting `descriptor.cost` change a ledger header? Does deleting
the capability gate change what `compare` reports? Nothing in the records answers that, because
nobody thought to ask.

**Built, on `feat/characterization-suite`.** `tests/test_characterization.py` pins seven
surfaces: `parse_single`, `parse_batch`, `route`, `compare`, `strategy`, `leaderboard`, `resume`.
Every row is now judged by which pinned fields it moved.

Two corrections to this record's first draft, left here because both cost time:

- `test_compare_characterize.py` is **not** a worked example of this pattern. It tests a module
  that happens to be named `characterize`. `tests/golden/` is schema-evolution fixtures. The
  suite was built fresh.
- A naive pin is 2.5 MB of block geometry, and trimming it naively destroys the signal:
  collapsing every long list turns the fallback chain into a count, and that chain is the single
  most important thing row 4 moves. The rule that works is **collapse lists of containers, keep
  lists of scalars whole**, so ids, codes and chain order survive while block trees do not.

Nothing pinned may depend on a real OCR run. `tesseract_ocr_works()` exists because a
present-but-broken install answers `which` and then fails at the job (BL-170), so a pin generated
on one machine fails on another for reasons unrelated to anyone's change. `_strategy` therefore
does not escalate and `_leaderboard` drops tesseract's rows.

One golden file already carries a doomed field: `tests/golden/batch-result/` has one with
`cost_usd` or `skip_reason` in it. That is expected churn, but it must be **re-pinned
deliberately with the new expected value**, never regenerated in bulk. A golden file regenerated
without reading the diff asserts whatever the code now does.

## 2. Fixtures are recorded vendor responses. Do not edit them.

Fifteen fixture files under `tests/fixtures/` contain a `usage` block, and an implementer grepping
for "cost" will be tempted to clean them.

**They must not be touched.** A fixture is what a vendor actually sent. Its `usage` block carries
the counters the vendor returned, which is exactly the thing
[`cost-removal.md`](cost-removal.md) keeps. What is being deleted is *our* derivation from those
counters, downstream of the fixture, in `router/cost.py`.

The correct assertion change is in the test, not the fixture: stop asserting on `cost_usd`, keep
asserting that `input_tokens` survived the round trip. If a fixture edit ever looks necessary,
that means the response envelope changed shape, which is a schema question and belongs in the
schema record.

## 3. The live lane breaks the offline build, which is the good news

Seven test files carrying `pytest.mark.live` reference concepts these records delete. None of them
have ever run here: all 18 live tests skip cleanly without keys.

They are still **imported**. Measured:

```
collected in offline lane   3,512
collected with no filter    3,533
```

Deselection happens after collection, so a live test referencing a deleted symbol raises at import
and fails `make verify` even though the test itself would skip. That is a guard, not a hazard, and
it means the live lane cannot silently rot through these removals.

What it does *not* catch is a live test that still imports fine but now asserts something untrue
about a real vendor response. Nobody can catch that offline. Each row that touches an adapter owes
a line in its PR description saying which live tests it changed and that they were not run.

## 4. Delete assertions, not files

[`compliance-removal.md`](compliance-removal.md) makes this point for its four policy test files.
It generalises, and the mechanical check is the same everywhere:

Before deleting any test file, list its test functions and separate those naming a removed concept
from those that do not. The second list is coverage of surviving behaviour, and it is what gets
thrown away when someone deletes by filename. The worked example is
`test_policy_validation.py`: 28 tests, 1 named for compliance, and the other 27 are
malformed-config and unknown-key behaviour that survives untouched.

## 5. The coverage floor moves, and the direction matters

`--cov-fail-under=91`, currently at 95.27%.

Removing well-covered code moves the percentage, and **which way it moves is diagnostic**:

- **Up** is expected. Raise the floor to match, as `AGENTS.md` requires. Never lower it.
- **Down** means tests were deleted that were covering code which survived. That is the
  delete-by-filename mistake in section 4, and the fix is to restore those tests, not to lower the
  floor.

Record the number after each row rather than only at the end, or a drop in row 2 hides inside a
rise in row 4.

## 6. `tests/conftest.py` is the runbook and must be rewritten

Its docstring opens: *"pytest configuration, and the testing runbook for every surface and both
lanes."* It documents what a new backend owes, what each lane covers, and it carries a manual
checklist, with line 188 noting that *"the checklist, not `verify`, is what enforces this row"*.

Rows 4 and 5 delete surfaces the runbook describes. A runbook that still tells a new adapter
author to fill in a compliance profile is the same failure as a stale README, with a worse
audience: the person it misleads is writing the code.

## 7. The conformance kit is a published contract

`testing/adapters.py` carries 4 hits, `testing/conformance.py` none. Small, but it is the kit
third-party adapter authors run against their own backends. Anything removed from it is removed
from their build too, so it belongs in the changelog's `Removed` section alongside the schema
bumps, not treated as an internal test-helper edit.

## 8. Per-row additions the individual records do not state

Beyond what each record already lists:

**Row 2 (format gate).** A test that a visible file with an unknown extension is *dispatched*
rather than skipped. The existing skip tests invert rather than delete.

**Row 3 (ledger).** A test that a ledger written by this build is readable by this build after a
process restart, since removing encryption changes the on-disk format. The records say old
encrypted roots are unreadable and that is accepted; this pins that the new format round-trips.

**Row 4 (compliance, auto).** The chain-order snapshot from
[`explicit-backends.md`](explicit-backends.md) must be taken **before** the scorer is deleted, not
after. Taken after, it pins whatever the new code does and proves nothing.

**Row 5 (cost).** A test that `apply_cost_report` still fills vendor counters, run against a fake
adapter reporting tokens. The deletion is in the same function, so this is the one that catches an
over-broad cut.

## Acceptance

1. The characterization suite exists and is green before row 0.
2. `make verify` green after every row, with the coverage number recorded per row.
3. No fixture file under `tests/fixtures/` modified by any row.
4. Every golden file change reviewed as a diff, with a sentence saying why the new value is right.
5. For each deleted test file, the PR description lists the surviving tests that were moved rather
   than deleted, or states that there were none.
