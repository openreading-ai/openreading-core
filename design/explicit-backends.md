# Design record: the caller names the backends, and nothing else decides

Status: proposed, not built. Decisions taken by Akshay on 2026-09-07.

Measured against `e27ad9d`. This record closes the two questions
[`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) left open: what orders a chain once
the scorer is gone, and what happens to `Capabilities`. It supersedes that record's section A1
"open" note and answers its section A3.

## 1. `auto` is deleted

**Decision (Akshay, 2026-09-07): delete it. Explicitness beats inference.**

An earlier draft of this proposal kept `auto` and ordered its chain by readiness, on the argument
that "can this backend run here" is a fact core can verify rather than a vendor claim. That was
still core deciding, and it bought nothing measurable. `executor.py:185` already checks readiness
**before** submit and records `Attempt(desc.id, "skipped", "missing_credentials")` with no network
call. An unconfigured backend in a chain costs one line of trail, not a round trip. Ordering to
avoid a free skip is optimisation of nothing.

So there is no filter and no score. Backend selection becomes a lookup:

```
1. The backend the caller named            -> a chain of one
2. else policy.backends, in written order  -> that is the chain
3. else pymupdf                            -> documented default, needs no key and no config
```

Rule 3 is what keeps the first five minutes working. `openreading parse doc.pdf` on a fresh clone
with no `openreading.yaml` and no `.env` reads the document with `pymupdf`, which has zero
credentials and zero config fields, so it cannot fail on configuration. It is a named default in
one line of documentation, not a decision derived from data.

A backend named in `openreading.yaml` whose key is absent from `.env` **fails**, and the trail
names the missing variable. That is the intended behaviour, not a gap to design around.

### What this removes

`"auto"` appears at 67 source sites, 3 schema properties and 132 test lines. It leaves
`request.v0.2.json`, `types/request.py`, the CLI's `--backend` fallback (`cli/app.py:186`), and
`api.run_request`'s `if backend == "auto"` branch.

`strategy:<name>` is untouched. A strategy names its backends explicitly, which is exactly the
posture this change adopts everywhere else.

### What is left of the router

With stage 1 ([`compliance-removal.md`](compliance-removal.md)), the format branch of stage 2
([`format-agnostic-intake.md`](format-agnostic-intake.md)), the capability branch of stage 2
(section 2 below) and stage 3 all removed, `Router` has no stages. `_compliance_drop`,
`_capability_drop`, `_score` and `ScoredBackend` all go, and `route()` reduces to resolving the
three rules above into an ordered list.

`RoutePlan` should survive as the carrier, because eight modules consume it (`api`, `executor`,
`server`, `engine`, `prune`, `errors` and the two `__init__`s) and its `chain`, `eligible_ids` and
`restrict_to` are still exactly right. Its `dropped` map becomes permanently empty and should be
deleted from the type rather than left as an always-`{}` field that a reader will try to use.

**Open, and worth a decision before implementation:** `openreading route` and `POST /v1/route`
exist to show the plan before running it. With nothing dropped and nothing scored, that plan is
the list the caller just wrote, so the verb degenerates into echoing the config back. Two honest
options: delete it, or repoint it at readiness so it answers "here is your chain, and here is
which of these are actually configured on this machine", which is a real question with a
verifiable answer. Recommend the second; it is the diagnostic `route` was informally being used
for anyway.

## 2. The capability gate is deleted

### What a capability is

Nineteen fields per backend, graded `verified` (we ran it), `claimed` (vendor docs say so, we did
not check), `partial`, or `False`. Verbatim from `aws-textract`:

```
ocr 'verified'          handwriting 'verified'      printed_tables 'verified'
reading_order 'claimed'  classification 'claimed'    multi_column False
signatures 'verified'    figures_charts False        vlm_based False
languages ['en','fr','de','it','pt','es']            input_formats ['pdf','png','jpg','tiff']
```

Fleet-wide: 52 `verified`, 71 `claimed`, 102 `False`.

Only five of the nineteen are ever read as a gate (`_FEATURE_CAPABILITY` plus
`custom_schema_extraction`). The other fourteen are documentation that looks like configuration.

### Why it goes

`_truthy_cap` is `value not in (False, None, "false", "")`. So:

```
verified -> passes    claimed -> passes    partial -> passes    False -> drops
```

A vendor's marketing page and a test we actually ran gate dispatch identically. The grade is
recorded and then ignored at the one place it could matter.

The `False` side is worse, because it is not a vendor claim at all, it is our own data entry.
Measured, a request asking for signature detection:

```
eligible: ['aws-textract', 'reducto']
dropped:  13 backends, every one "missing_signatures"
```

Azure Document Intelligence ships signature detection as a documented field type. Claude and
Gemini are vision models that will describe a signature on request. All three are excluded by a
`False` typed into this repository. The caller sees "no eligible backend" and never learns the
cause was a spreadsheet, because a wrong `False` produces a **silent exclusion that nothing
recovers from**, unlike a wrong `claimed`, which at least fails loudly at the vendor.

There is also a product argument, and it is the stronger one. This is general document processing,
not a signature verifier. What a run produced belongs in the response, where the channel contract
already reports it: a channel a backend cannot produce is omitted with a `warnings[]` entry, never
fabricated. Pre-negotiating capability in the request duplicates that contract and does it from
worse data.

### What stays

**`request.features` stays.** Unlike the gate, adapters genuinely use it to configure the vendor
call: `docling` sets `do_ocr` from `features.ocr` (`adapter.py:295`), `open-ocr` forwards
`ocr_languages` (`adapter.py:262`), `tesseract` reads `features.tables` (`adapter.py:416`). That
is pass-through, the caller stating an intent and core forwarding it. Nothing here decides.

**`comparison/capabilities.py` stays, and is the pattern the router should have copied.** Compare
needs to know whether a backend could produce a dimension, or it faults pymupdf for missing
`typed_fields` it never claimed. It already resolves this the honest way: it prefers what the
**response actually contains** (`produced_fields`), falls back to descriptor claims only when the
backend id is known, and degrades an unknown id to "assume capable" so genuine misses still
surface. Its own docstring states the rule this whole sweep converged on: *"we never fabricate a
capability we cannot substantiate."*

That dependency means `Capabilities` cannot be deleted outright. The fields `compare` reads
(`forms_key_value`, `custom_schema_extraction`) survive as descriptor documentation. The gate,
`_FEATURE_CAPABILITY`, `_truthy_cap` and the `missing_<cap>` drop codes go.

**`adapters/base.py:132`** exposes `capabilities()` as one of the eight adapter protocol methods.
It survives, or the protocol drops to seven; either is fine and it is a naming call.

### `page_range_selection` is dead code

`engine.py:2238` and `validate.py:621` both call
`getattr(desc.capabilities, "page_range_selection", False)`. Measured:

```
declared in Capabilities:  False
Capabilities extra policy: allow
adapters where truthy:     NONE
```

The field is not declared. `Capabilities` is `extra="allow"`, so the access never raises, it just
returns `False` for all fifteen backends and always has. `_supports_page_ranges`' docstring
describes a mechanism ("a backend advertises native page-range selection via the (extra-allowed)
capability `page_range_selection`, §2.7") that no backend has ever used, so every cascade rung
silently runs at document granularity and the per-page gate it guards has never fired.

Either declare the field and set it truthfully on the backends that support page ranges, or delete
`_supports_page_ranges`, its two call sites and the §2.7 per-page path. Do not leave a documented
mechanism that is structurally unreachable. This is a bug found by the sweep, not a consequence of
it, and it can be fixed independently of everything else here.

## 3. Test strategy

- **Pin the chain before touching it.** Snapshot the resolved chain for: a named backend, a
  `policy.backends` list, and no config at all. These are the three rules, and the third is the
  first-run promise.
- A test that `backend: "auto"` is refused by the current request schema as an invalid value.
- A test that a `policy.backends` entry with no key in `.env` fails, with the missing variable
  named in the trail. That is the behaviour this design chooses, so it needs a test asserting it
  rather than a comment claiming it.
- A test that `features: {signatures: true}` no longer drops any backend, run against the current
  tree first, where it drops thirteen.
- Registry order is load-bearing under rule 2 only when a caller writes a list, so it needs a test
  that `BUILTIN_ADAPTERS` order is stable, which nothing asserts today.
- `compare`'s capability resolution keeps its existing tests unchanged. If any of them fail, the
  wrong thing was deleted.

## 4. Order

This lands with [`compliance-removal.md`](compliance-removal.md)'s atomic change, because
`policy.backends` is defined there and rules 1 to 3 depend on it. The `page_range_selection` fix
is independent and can go first, alone, as an ordinary bug fix.
