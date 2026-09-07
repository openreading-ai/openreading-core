# Design record: what else core asserts and cannot know

Status: proposed, not built. A sweep, not a single change.

Measured against `e27ad9d` on 2026-09-07. Sibling records:
[`compliance-removal.md`](compliance-removal.md),
[`format-agnostic-intake.md`](format-agnostic-intake.md),
[`ledger-policy-removal.md`](ledger-policy-removal.md).

## The test

Three sweeps have now removed the same kind of thing for the same reason. Stated once, so the
next reviewer can apply it without rediscovering it:

**A fact core cannot verify must not change what core does.** It may be documentation, clearly
marked and dated. It may not be a routing input, a gate, or a default.

Two corollaries, both learned the hard way this week:

- Being wrong must produce an error, not a quieter success. A gate that drops the right backend
  for the wrong reason looks like normal operation.
- Policy about the caller's own machine is the caller's. Core's opinion about their disk, their
  retention or their vendor agreements is a guess wearing a guarantee's clothing.

`openreading.liveness` is the model for how to keep a fact honestly. It reports
`configured_unverified` with `measured=False, latency_ms=None`, so a reader can tell an assumption
from an observation without reading the source. Every item below either does that, or should be
deleted.

## A. Asserts a vendor fact, and routes on it

### A1. `optimize_for`, `integration_priority`, `_QUALITY_BY_PRIORITY`

Four request values (`accuracy`, `cost`, `latency`, `offline`) feeding seventeen lines at
`router.py:191-206`. Three problems, each independently disqualifying.

**"Quality" is our roadmap.** `_QUALITY_BY_PRIORITY = {"P0": 1.0, "P1": 0.6, "P2": 0.3}` is keyed
on `descriptor.router.integration_priority`, a P0/P1/P2 label about how important the integration
was to build. `adapter-descriptor.v0.7.json:326` defines it as a bare enum with no stated meaning
and a free-text `priority_reason`. Today: 8 adapters P0, 7 P1, none P2. So
`optimize_for: accuracy` means "prefer what we prioritised building".

**`latency` reads no latency.** No descriptor field carries one; `grep -rniE
"latency|p50|p95|throughput|seconds_per_page" types/descriptor.py` is empty, and the code says so:
`# No latency field in the descriptor yet, so w_latency≈0`. Measured, `latency` and `accuracy`
produce byte-identical chains:

```
accuracy -> ['pymupdf', 'docling', 'azure-document-intelligence', 'google-document-ai', 'chunkr']
latency  -> ['pymupdf', 'docling', 'azure-document-intelligence', 'google-document-ai', 'chunkr']
cost     -> ['pymupdf', 'docling', 'tesseract', 'qwen-vl', 'azure-document-intelligence']
offline  -> ['pymupdf', 'docling', 'tesseract', 'qwen-vl', 'azure-document-intelligence']
```

**Four documented values, two behaviours.** And the split between them rests on
`compliance.runs_fully_local` and `cost.usd_per_page_equiv_*`, both of which other records delete.

**Decision (Akshay, 2026-09-07): delete.** `optimize_for` leaves `request.v0.2.json:144` and
`types/request.py:146`, `_WEIGHTS` and `_QUALITY_BY_PRIORITY` go, and `integration_priority` and
`priority_reason` leave `RouterHints`. The replacement is `policy.backends` in the order the
caller wants them tried: a list from someone who measured latency on their own documents beats a
weight table from someone who measured nothing.

**Consequence worth stating:** with `optimize_for` gone there is no `offline` value to preserve,
so `runs_fully_local` no longer needs to survive for scoring. It leaves with the rest of
`ComplianceProfile` and `descriptor.router` is untouched. This supersedes the
`runs_fully_local` move proposed in [`compliance-removal.md`](compliance-removal.md) section 3.

**Open, and it is a real question:** with no scorer, what orders the chain? The answer should be
`policy.backends` order first, then a documented deterministic fallback (registry order), never an
incidental one. Nothing today guarantees registry order is stable across Python versions, so this
needs a test whichever way it goes.

### A2. `Cost`

`usd_per_page_equiv_low`, `usd_per_page_equiv_high`, `basis`, `native_unit`, `lossiness`. Fifteen
adapters each carrying a price range someone read off a pricing page. `basis` already admits the
problem: its values are `billed | estimated | infra_only | unknown`.

Vendor pricing changes more often than vendor compliance, and nothing here detects it. The scorer
reads it (`router.py:197`), so a stale number reorders a real chain, and `apply_cost_report`
puts derived figures on the response where a caller may believe them.

**Recommendation: delete the estimates, keep what is measured.** A cost a vendor *returned* for
*this call* is an observation and belongs on the response. A cost we typed in from a web page is
an assertion and does not belong in core at all. Splitting those two is most of the work.

Not a decision this record takes. It is the next sweep, and it is bigger than it looks because
`compare`, the leaderboard and the strategy cookbook all read cost.

### A3. `Capabilities`, and the grade that does not grade

Nineteen fields, graded `verified | claimed | partial | false`. Across the fleet: **52 verified,
71 claimed, 102 false**.

The stage-2 gate calls `_truthy_cap`, which is `value not in (False, None, "false", "")`.
Measured:

```
verified -> passes    claimed -> passes    partial -> passes    false -> drops
```

So `claimed`, meaning "the vendor's documentation says so and we never checked", gates dispatch
identically to `verified`, meaning "we ran it". The grade is recorded and then ignored at the one
place it would matter. 71 values are deciding routing on vendor marketing.

The `false` side is worse, because it is not a vendor claim at all. 102 of those are our own data
entry, and a backend that can do a thing but whose descriptor says `false` is dropped with
`missing_<cap>` and never tried. The caller sees a refusal caused by our spreadsheet.

**Recommendation:** stop gating on it. Same reasoning as the format gate in
[`format-agnostic-intake.md`](format-agnostic-intake.md): being wrong about capability produces a
vendor error the chain already handles (`executor.py:219-236`), while being wrong in the `false`
direction produces a silent exclusion that nothing recovers from. If a grade is kept for display,
`claimed` and `verified` must render differently to a reader, or the distinction is decoration.

This is the largest single item in the sweep and deserves its own record before anyone edits code.

## B. Asserts something and nothing reads it

Read at zero sites outside their own definition, and shown in no CLI or server output:

| field | also in a README? |
|---|---|
| `adapter_impl` | no |
| `idempotency_supported` | no |
| `cancel_supported` | no |
| `languages` | no |
| `capabilities.max_pages_per_request` | yes, 4 mentions |
| `runtime.cold_start_s` | no |
| `runtime.vram_class` | no |
| `runtime.hardware` | no |
| `runtime.system_deps` | no |
| `runtime.offline_capable` | no |
| `runtime.serving` | no |
| `router.normalization_difficulty` | no |

`max_pages_per_request` is already a known gap in `adapters/README.md`: nothing reads it, so a
document over a vendor ceiling fails at the vendor rather than at the router. That entry has been
true long enough to be documented, which is the argument for deleting the field rather than
finally implementing it.

`idempotency_supported` and `cancel_supported` are the interesting pair: both name a behaviour the
runtime genuinely has (`ctx.idempotency_key` is always set; the job store has a DELETE), so a
reader will reasonably assume the flags gate those paths. They do not. Either wire them or remove
them, but they must not sit in a published schema implying a check that no code performs.

**Recommendation: delete, with two exceptions.** `runtime.license` stays, because a license is a
legal fact about shipped code that a user genuinely needs and that nothing else records.
`descriptor.sources` (`url`, `accessed`, `supports`) stays and is the pattern the rest should have
followed: a dated citation is honest documentation, and its `accessed` date lets a reader judge
staleness themselves.

## C. Already decided elsewhere

- `ComplianceProfile`, 180 vendor claims: [`compliance-removal.md`](compliance-removal.md).
- Ledger retention and encryption at rest: [`ledger-policy-removal.md`](ledger-policy-removal.md).
- `input_formats` as a gate, and the extension-to-MIME tables:
  [`format-agnostic-intake.md`](format-agnostic-intake.md).

## D. Checked and found honest

Recorded so the next sweep does not re-litigate them.

- **`openreading.liveness`.** Separates measured from assumed, and says which in the payload.
- **`descriptor.sources`.** Dated citations with an `accessed` field.
- **Strategy signal thresholds** (`_GARBLE_TRUE_CUT = 0.3`, `chars_per_page_below: 100`). These
  are computed from the caller's own document, not asserted about a vendor, and every one is
  overridable in the strategy file. A default the caller can see and change is not the same thing
  as a claim they cannot check.
- **`signup_url`, `credentials_spec`, `config_spec`.** Facts about how to configure this code,
  verified continuously by `readiness` and the extras-parity gate.

## Order

A1 folds into the compliance change, because it deletes `optimize_for`'s two remaining data
sources and leaving the enum behind would be worse than either state.

A3 and A2 are their own records, in that order. A3 is a routing behaviour change; A2 touches
`compare`, the leaderboard and the cookbook, and should not be started until A3 has settled what
the router reads at all.

B is mechanical and can ride along with any of them, or land alone as a schema cleanup. It is the
only part of this sweep with no behaviour change.
