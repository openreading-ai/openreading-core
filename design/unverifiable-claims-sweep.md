# Design record: what else core asserts and cannot know

Status: **section B only**. Sections A and C have shipped, and their durable facts moved into the
module docstrings they describe. Section B is the part still proposed and not built: twelve
descriptor fields read at zero sites.

Measured against `e27ad9d` on 2026-09-07. The removal set that carried A and C is in
`CHANGELOG.md` under Unreleased.

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

## A and C: shipped

`optimize_for` with `integration_priority` and `_QUALITY_BY_PRIORITY`, `Cost`, the capability
gate, the compliance profile, ledger retention and encryption, and `input_formats` as a gate are
all removed. What each one avoided is written where the code that replaced it lives, and
`CHANGELOG.md` carries the reader-facing account. This section is a pointer so a reviewer of B
does not go looking for the argument.

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

B is mechanical and lands alone as a schema cleanup. It is the only part of this sweep with no
behaviour change, which is why it was the only part left when the rest shipped.
