"""The reference-free quality probe + gate evaluation: the signal and fact catalog.

Two catalogs, one rule set. **Pre-parse facts** feed route `when:` maps and are computed in
`openreading.strategies.facts` (catalogued here in §2 so this module is the single reference).
**Post-parse quality signals** (§3-4) are computed here by `probe` into one `SignalSnapshot` and
tested by `evaluate_gate` for `escalate_if:` / `review_if:`. Thresholds are tuned per corpus by
`openreading.strategies.calibrate` (§5). The missing-signal law (§6) governs both.

§1 Design rules
---------------
- **Flat, enum-keyed, primitive-valued — always.** Every predicate is one known key mapped to one
  number, string, boolean or small list. No expression language, no user functions, no NOT (facts
  have polar variants: `pages_over` / `pages_under`) — the DSL is deliberately sub-Turing so one
  shape serves three consumers: the deterministic engine evaluates it trivially, a UI renders each
  key as one form widget, and an LLM decider can be handed the catalog as a strict enum it cannot
  extend.
- **Two tiers, and why Tier-1 exists.** Tier-2 signals are confidence values the backend itself
  reported in the envelope. But escalation decisions concentrate exactly where confidence is
  absent: the cheap first rungs (pymupdf, docling's text-layer path) are deterministic parsers
  that emit no confidence at all (the envelope's `confidence_unavailable` warning). So the engine
  computes Tier-1 signals itself, from a reference-free probe over the `NormalizedResponse` of
  every **strategy-engaged** attempt — backend-agnostic and always available. The spec asks for
  the snapshot to be "surfaced as trace telemetry whether or not any gate references it"; **as
  shipped it is not**: `trace.Attempt` carries no snapshot field, only per-predicate
  `GateRecord`s, and `engine._apply_gates` writes one record per predicate present in the step's
  `escalate_if` / `review_if` — a gateless cascade (no cascade-level `escalate_if`; the
  cascade-level gate is never distributed onto the final rung, though an explicit step-level
  gate on the last step is evaluated and, if it fires, walks off the end into the exhausted
  branch) traces no `chars_per_page`, `garble_score` or `text_source` at all. A
  signal reaches the trace only through a gate that names it. Never on the no-config or
  direct-named paths:
  the no-change law binds — a request the strategy layer doesn't touch gets today's response,
  byte-identical. (Unstructured's `strategy="auto"` works the same way: a cheap extractable-text
  probe, not parser confidence, carries the load where cheap backends report nothing.)
- **Polarity.** A gate map (`escalate_if`, `review_if`) **ORs** its keys — any reason to distrust
  ⇒ move on. A route `when:` map **ANDs** its keys — all facts match ⇒ this lane.
- **Thresholds err toward escalation.** A false escalation costs cents (one extra backend call);
  a false acceptance costs correctness. Every cheap stage must be a high-recall filter biased
  toward passing work onward (the Viola-Jones / FrugalGPT cascade invariant). Defaults below
  follow it; `calibrate` tunes them per corpus.
- **Licensing (DECISIONS D-v3-4).** The PDF-layer signals need one introspection of the INPUT
  bytes. That uses `pypdf` (BSD, in core), imported lazily inside `_probe_pdf_layer`; any failure
  (not a PDF, unreadable, absent bytes) leaves those signals absent — graceful degradation, never
  a crash. **pymupdf (AGPL) is never imported here** — it stays a dev/test-only fixture builder,
  keeping core permissive. The garble composite is pure Python and needs no dependency.

§2 Pre-parse facts (route `when:` only — `openreading.strategies.facts`)
-------------------------------------------------------------------------
Available before any backend runs. Keys in one `when:` AND together. A fact that cannot be
computed makes its rule **not match** (traced "fact unavailable" — the spec's `fact_unavailable`;
the literal on the emitted `FactRecord` is `status: "unavailable"`, next to `"match"` /
`"no_match"`, and no `fact_unavailable` token ever appears in a trace), never an error —
`default:` is the guaranteed floor.

- `doc_type` — document class, single value or list (the request's 10-value vocabulary). From
  `routing.doc_type_hint` (the roadmap auto-classifier when it lands; the hint wins). Free; only
  when a hint (or classifier) exists.
- `mime` — MIME type, string or list. From `document.mime_type` (inferred from the extension when
  omitted). Free; near-always.
- `pages_over` / `pages_under` — page count above / below N. One local pypdf page-count probe over
  the materialized bytes. PDF inputs with bytes materialized only; otherwise unavailable.
- `size_over_mb` / `size_under_mb` — byte length above / below N MB. Free once bytes exist.
- `filename_matches` — regex over `document.filename`. Free; only when a filename was supplied.
- `sample_percent` — true for N% of inputs: a sha256(document bytes) bucket, stable per input.
  One hash; always once bytes are materialized; unavailable for a deliberately
  un-materialized URL pass-through. Deterministic per input by design (the AWS A2I `Sampling`
  precedent) so audit routing replays identically and agrees with the idempotency cache.

One rule per fact, for shape:

```yaml
route:
  rules:
    - when: { doc_type: [invoice, bank_statement] }
      use: strategy:tables_heavy
    - when: { mime: image/tiff }
      use: strategy:ocr_first
    - when: { pages_over: 200 }
      use: strategy:big_docs
    - when: { size_under_mb: 1 }
      use: strategy:cheap_first
    - when: { filename_matches: "(?i)payslip" }
      use: strategy:forms
    - when: { sample_percent: 5 }
      use: strategy:audited
  default: strategy:general
```

A `compliance` fact used to sit in this list, matching the request's effective compliance
constraint. It went with the compliance filter: it read a posture core computed from a per-vendor
table it could not verify, so a rule keyed on it branched on a guess. Route on the document
instead, which is what every fact above does.

§3 Tier-1 engine-computed signals
---------------------------------
Computed by `probe` over every normalized result; available on every backend. Per gate
evaluation each predicate's observed value vs threshold lands in the trace (`PredicateResult`).
Cost: one linear pass over the extracted text plus, for the PDF-layer signals, one pypdf
introspection of the input — no network, no model calls. Page text is `page.text`, else
`page.markdown`, else the page's blocks joined; document text is `document.text`, else
`document.markdown`, else the page texts joined.

`chars_per_page_below: N` — mean extracted characters per page < N (with no pages: the length of
the document text). Grounding: ~100 chars/page is the field-tested scan cut (the CCpdf
born-digital classifier, 93% precision; LlamaParse's `page_shorter_than_n_chars`). Not in the
`default` bundle; `100` is the recommended start — low means "probably a scan the text-layer
parser couldn't read", and escalating is nearly free relative to returning an empty parse.

```yaml
steps:
  - backend: pymupdf
    escalate_if: { chars_per_page_below: 100 }
  - reducto
```

`empty_pages_over: F` — fraction of pages with fewer than 25 extracted (stripped) chars > F; with
no pages, 0.0 if any text else 1.0. Page-level metrics, not document means, catch mixed
scanned/digital documents. Default `0.2` (in the `default` bundle): one blank-ish page in five is
tolerable front-matter; more suggests a partly scanned document.

`garbled: true` / `garble_score_over: F` — composite mojibake score in [0,1] over the document
text (`garble_score`): `min(1, 0.4*r_replace + 0.35*r_nonword + 0.25*r_nonascii)` where
`r_replace` = 8× the ratio of `�` / control characters (newline, CR, tab excluded), capped at 1;
`r_nonword` = 1 − the word-like fraction of alphabetic tokens (word-like: ≥2 chars after
stripping punctuation/digits, ≥60% letters, contains an ASCII vowel, ≤50% non-ASCII letters);
`r_nonascii` = the non-ASCII share of letters. `None` (unavailable) when there is no text to
judge. `garbled: true` fires at score > 0.3 (`_GARBLE_TRUE_CUT`; the boolean's value is not
consulted). Tuned so clean prose and legitimately accented text score ~0 and mojibake scores
above the cut. Each component is established mojibake-detection practice (bigram / word-shape
thresholds, out-of-charset ratios, dictionary-word ratio); it catches OCRmyPDF's named failure
class — a damaged ToUnicode map, "selectable but not searchable" — a text layer that exists but is
garbage, invisible to any non-emptiness check. `garbled: true` is in the `default` bundle; 0.3 is
deliberately sensitive because garbled text poisons every downstream consumer. **Known limit:**
the word-like test assumes Latin script — non-Latin text (Cyrillic, CJK, ...) scores above the
cut, so drop or raise the gate for such corpora.

`scanned_pages_detected: true` — any page with no extracted text and image content. Grounding:
the full-page-image scan detector (LlamaParse's `full_page_image_in_page`). v0.3 approximates
"dominant image coverage" as "no pypdf text + at least one image on the page" — it does not
measure image area. In the `default` bundle: a scanned page under a text-layer parser yields
nothing, and OCR-capable rungs exist for exactly this case. PDF inputs only.

`text_source: prior_ocr | none` — text-layer classification of the PDF input. The **gate**
value domain is exactly `prior_ocr` | `none` (the strategy schema's `text_source` enum;
`text_source: digital` is rejected at load time). The **probe** classifies three ways —
`SignalSnapshot.text_source` is `"digital"` (any page has extractable text), `"none"` (no text
layer) or `None` (not a PDF) — and the predicate is a plain equality test, so `digital` is
observable in the trace but not gateable.
**v0.3 scope (DECISIONS D-v3-9):** `prior_ocr` (an invisible OCR layer over a scan) needs
text-render-mode / image-coverage analysis pypdf does not expose; the naive "text + any image ⇒
prior_ocr" heuristic false-positives on a born-digital page with a small decorative image, so it is
deferred — the probe never emits it, and a `text_source: prior_ocr` gate is evaluated but never
matches until that lands. Grounding:
OCRmyPDF's `--redo-ocr` visible/invisible text analysis; Marker treats pre-existing OCR text as a
lower-trust class. No default — pair it with a confidence or garble check rather than escalating
on provenance alone. PDF inputs only (elsewhere inapplicable, §6).

```yaml
escalate_if:
  any_of:
    - all_of:
        - text_source: prior_ocr
        - garble_score_over: 0.15
    - text_source: none
```

`table_sanity_below: F` — composite table-consistency score in [0,1]: `1 − (0.6 × ragged-row
fraction + 0.4 × empty-cell ratio)` over the markdown pipe tables parsed from `table` blocks
(ragged = a row deviating from the modal column count; separator rows skipped). `None` when the
result carries no parseable pipe table — a table block without pipe markup contributes nothing.
Grid-consistency checks are how Marker decides a table reconstruction is low-confidence. No
default — table shapes vary too much by corpus; calibrate (§5).

`zero_blocks: true` — no page emitted any block: the degenerate floor of the char-count family.
Non-emptiness is not a quality signal, but total emptiness is an unambiguous failure to parse. Not
in the bundle (`empty_pages_over` and `scanned_pages_detected` subsume the common cases); an
explicit belt-and-braces gate on permissive parsers.

`matches_regex: "..."` — `re.search` of the user regex over the document text. The escape hatch
for corpus-specific tells (a known watermark, a form code; LlamaParse's `regexp_in_page`). No
default. Example: `escalate_if: { matches_regex: "(?i)this page intentionally left blank" }`.

`sample_percent: N` — the same deterministic content-hash bucket as the §2 fact, used as a gate:
bucket = `int(sha256[:8], 16) % 10000 / 100` in [0, 100); fires when bucket < N regardless of
quality — audit escalation, A2I's second trigger class next to *quality*. Sampled runs of the
expensive backend against the cheap one are the calibration data source (§5). Unavailable when
the input bytes were not supplied. No default — sampling is always an explicit choice.

§4 Tier-2 backend-reported signals
----------------------------------
Read from the response envelope (`openreading.types.response`); present only when the backend
put them there. Deterministic parsers and token-stream models emit **no** confidence — the
envelope carries a `confidence_unavailable` warning instead, and the never-fabricate rule forbids
inventing one. At validate time the adapter descriptor's output-channel grades (`block_confidence`,
`typed_fields`, ... graded **N**ative / **D**erivable / im**X**possible — `ChannelGrade` in
`openreading.types.enums` is literally `"N"` / `"D"` / `"X"`) predict bindability, which powers
the static unbindable-gate error (§6). **As shipped, only three predicates are grade-checked**
(`validate._predicate_binds`): `confidence_below` / `page_confidence_below` bind unless
`block_confidence` is `X`, and `field_confidence_below` binds unless `typed_fields` is `X`. Every
other key — including `doc_type_confidence_below`, deliberately listed in validate's
`_ALWAYS_AVAILABLE` set ("conservative: treat as bindable") even though no descriptor channel
backs it — is assumed bindable, and any predicate wrapped as `{value, on_missing: escalate}` is
bindable by construction (it fires on absence).

- `confidence_below: F` — document-level confidence = **mean of `pages[].confidence` over the
  pages that report one** (aggregated once, here; no page reports ⇒ absent). Emitted by hosted
  OCR/DI APIs (aws-textract, azure, google) and OCR engines (tesseract).
- `page_confidence_below: F` — the lowest `pages[].confidence` < F; absent when no page reports.
- `field_confidence_below: F` or `{fields: {name: F}, on_missing?}` — `typed_fields{}.confidence`,
  **numeric [0,1] only**; a backend's qualitative confidence string is preserved as-is and never
  coerced, so the predicate is inapplicable to it (absent when no field has a numeric value).
  Scalar form: the lowest numeric field confidence < F. The per-field map lives under an explicit
  `fields:` sub-key so the form never collides with the generic `{value, on_missing}` wrapper;
  fields without a numeric confidence are skipped and the observed value is the `{name: conf}`
  map. Emitters: textract forms/queries, azure prebuilt models, reducto extract.
- `fields_required: [name, {name, aliases: []}]` — fires when a named field (or every alias) is
  **missing or empty** (`None` or blank); the observed value is the list of missing names.
  Absence is a first-class trigger evaluable on every backend — no emitter needed (A2I's
  `MissingImportantFormKey` + `ImportantFormKeyAliases`: key names vary across templates).
- `doc_type_confidence_below: F` — `document.doc_type.confidence`; backends emitting
  classification. Never grade-checked at validate time (see above): a gate consisting solely of
  this predicate on a backend that emits no classification passes validation and is traced
  `signal_unavailable` at runtime instead.
- `warning_code: <code>` — a `warnings[]` entry with this code is present; every backend emits the
  warnings channel.

The Azure/Textract straight-through-processing pattern is the model: per-field thresholds set by
business risk, calibrated per doc-type — hence both the scalar and the per-field map forms:

```yaml
steps:
  - backend: aws-textract
    operation: AnalyzeDocument
    escalate_if:
      field_confidence_below: { fields: { pay_date: 0.85, total_amount: 0.9 } }
      fields_required:
        - invoice_number
        - { name: pay_date, aliases: [PayDate, DateOfPay, pay-date] }
      warning_code: unsupported_feature
  - anthropic-claude
```

Cross-branch (`pick: best` only): `disagreement_over: F` — the engine injects
`SignalSnapshot.disagreement_over` = worst pairwise `1 − token-set Jaccard` of `document.text`
among the finished non-shadow branches when gating a parallel step's winner; absent on a
single-response probe and with fewer than two branches.

Per-page gating (`granularity: page`, DECISIONS D-v3-20): the engine builds a one-page snapshot
(`doc_confidence` = that page's confidence, `chars_per_page` = its text length) and reuses the
same `evaluate_gate`, so `confidence_below` / `chars_per_page_below` mean the same thing per page.
The PDF-layer signals are not page-scoped in v0.3 and do not fire per page.

§5 Calibration — `openreading calibrate` (`openreading.strategies.calibrate`)
------------------------------------------------------------------------------
Raw thresholds are meaningless to users (RouteLLM's calibration yields numbers like `0.11593`;
Azure's guidance is "pilot, compare confidence distributions to accuracy, then set thresholds").
The usable knobs are an escalation rate and a budget. **Principle: users pick rates and budgets;
tools derive thresholds.**

- **Inputs:** a sample directory of representative documents; the strategy to tune
  (`--strategy <name>`, required; `--config PATH` only points at the openreading.yaml that
  defines it); a target as `--target-escalation 0.15` (fraction of documents that
  should escalate past rung 1) and/or `--max-cost-per-doc 0.05`.
- **Method:** run the strategy's rung-1 backend over the sample; score each result with the
  existing eval scorers (`openreading.evals.scorers` — no parallel scoring path); compute every
  signal per document with this module's `probe`; sweep each calibratable threshold among the
  **top-level keys of rung 1's `escalate_if:` only**
  (`calibratable_predicates(steps[0].escalate_if)` — `review_if` gates, gates on later rungs, and
  a numeric predicate nested under `any_of` / `all_of` inside rung 1's gate are never swept) over
  its domain and report predicted escalation rate and predicted cost per document (advisory,
  from the descriptor per-page rates × assumed pages, matching the engine's precheck). Shadow
  branches and
  `sample_percent` audit rules generate the paired cheap-vs-premium outputs that ground the sweep
  — the FrugalGPT structure: the scorer is separate from the chain; same chain + different
  thresholds = a different cost/quality point.
- **Per-predicate sweep (DECISIONS D-v3-21):** predicates are swept **independently** on their
  own grid (`[0, 1]` step 0.05 for confidence / table sanity / empty-page / garble; 0..3000 step
  100 for chars-per-page), not as a cartesian threshold vector — a full N-predicate product is
  exponential. Only numeric predicates are calibratable (`confidence_below`,
  `page_confidence_below`, `chars_per_page_below`, `table_sanity_below`, `empty_pages_over`,
  `garble_score_over`); boolean/regex gates are not threshold-tuned. An unavailable signal never
  fires in the sweep (missing-signal law). **Divergence:** calibrate records only the mean
  `doc_confidence` per document and maps `page_confidence_below` onto it
  (`_PREDICATE_SIGNAL["page_confidence_below"] == ("doc_confidence", "below")`), whereas
  `evaluate_gate` tests the per-page **minimum** (§4) — so the predicted escalation rate for
  `page_confidence_below` is computed against a different signal than the one the engine fires
  on, and understates it whenever pages vary.
- **Recommendation:** `--max-cost-per-doc` filters the candidates first — but it is not a hard
  filter: when **no** point fits the budget every point stays in play (`survivors = affordable or
  points`), so the recommendation can exceed the budget, and that fallback does not prefer the
  cheapest point either. Among the survivors the point closest to `--target-escalation` wins
  (ties → higher scorer agreement → lower threshold); with no target, the point maximizing scorer
  agreement (ties → lower cost → lower threshold). `scorer_agreement` = fraction of **scored**
  documents where the gate's fire decision matches the scorer's verdict (overall below the quality
  bar, default 0.8), grounding the threshold in measured quality. A document whose `expected`
  names none of the scorer's dimensions has no verdict (`scorer_overall is None`) and is excluded
  from both numerator and denominator — never read as "agrees with every threshold"; with no
  scored document at all `scorer_agreement` is 0.0 (`CalibrationReport.n_scored` says how many
  were). Unscored documents still count toward escalation rate and cost, which need no label.
- **Output:** a table of candidate operating points — threshold, predicted escalation rate,
  predicted cost/doc, scorer agreement — plus the recommended point as a ready-to-paste
  `escalate_if:` block ("with `confidence_below: 0.72`, 18% of documents escalate, est.
  $0.011/doc"). **The tool proposes; it never rewrites the config** — the file the user commits is
  the authority.

§6 Missing-signal semantics
---------------------------
1. **Inapplicable ⇒ doesn't fire ⇒ traced.** A predicate over an absent signal does not fire; the
   trace records it skipped with reason `signal_unavailable` (`PredicateResult.unavailable`) —
   never silently, never guessed. Inapplicable predicates are also excluded from the
   quality-score denominator.
2. **Per-predicate override:** the object form of a **threshold** gate key —
   `confidence_below: { value: 0.7, on_missing: escalate }` (exactly the keys `value` and
   optional `on_missing`) — makes an absent signal FIRE for that predicate (still traced
   unavailable); `on_missing: skip` is the default. The spec says "any gate key"; **the shipped
   schema accepts the wrapper only where the key is typed `threshold_int` / `threshold_frac` /
   `threshold_bool`** (`chars_per_page_below`, `empty_pages_over`, `garbled`,
   `garble_score_over`, `scanned_pages_detected`, `table_sanity_below`, `zero_blocks`,
   `confidence_below`, `page_confidence_below`, `doc_type_confidence_below`,
   `disagreement_over`) plus `field_confidence_below`'s own `{fields, on_missing}` shape.
   `text_source` (bare enum), `sample_percent` (bare number), `matches_regex` and
   `warning_code` (bare strings) and `fields_required` (array) have **no** wrapper form —
   `text_source: {value: none, on_missing: escalate}` is rejected at load, even though
   `_evaluate_predicate`'s `_unwrap` would honour it if it got through. So the two conditional
   signals where the override would matter most (`text_source`, `sample_percent`, on a non-PDF /
   bytes-less input) cannot be told to fire on absence.
3. **`fields_required` is the exception by definition:** it fires **on absence** — that is its
   job. It never needs `on_missing`.
4. **Static check** (`openreading strategy validate` → `openreading.strategies.validate`): a
   user-written gate that can never fire on its step's backend is reported as a **validate-time
   error** naming the fix ("add an always-available signal such as `chars_per_page_below` or
   `garbled`"). "Can never fire" is computed over the gate's real boolean structure
   (`validate._gate_can_fire`), not over a flat list of its leaves: a gate map and `any_of` are
   ORs, so they die only when every leaf is unbindable, but `all_of` is an AND, so ONE unbindable
   conjunct kills the whole conjunction — `escalate_if: {all_of: [confidence_below: 0.5,
   chars_per_page_below: 50]}` on a pymupdf step is as dead as the lone `confidence_below`, and a
   flatten-then-count check reports it clean. A leaf that cannot bind inside a gate that still
   fires elsewhere is dead weight rather than a dead gate, so it is a **warning** at that leaf's
   own node path; the failure it avoids is a `calibrate`-recommended `escalate_if:` block passing
   validation green while half of it can never fire. The spec calls this a **load-time** error;
   **as shipped it is not**: `validate_config` has exactly two callers — the `strategy validate`
   CLI command and the web UI's strategies panel. `loader.load_config` / `parse_config_raw`, which
   `openreading run --strategy`, the Python `run()` path and every other `cmd_*` use, run
   JSON-Schema + pydantic only, so an unbindable gate loads without complaint, executes, and traces
   each predicate `signal_unavailable`. The spec also says "per descriptor channel grades"; the
   check consults grades for `confidence_below` / `page_confidence_below` (`block_confidence`) and
   `field_confidence_below` (`typed_fields`) only — every other predicate, including
   `doc_type_confidence_below`, is treated as always bindable (§4), so only those three keys ever
   trigger either level. Exemptions: `backend: auto` leaves (no fixed descriptor; checked at
   runtime via the trace instead), and the built-in `default` bundle, which is designed to degrade
   — its Tier-1 members carry the load and its `confidence_below` is a deliberate Tier-2 bonus, so
   it never earns the dead-weight warning (it was never an error: an OR with binding members
   fires).

Gate evaluation (`evaluate_gate`): a gate map ORs its predicates; `any_of` (a list of gate maps,
fires if any fires) and `all_of` (fires if all fire; an empty list never fires) nest to **any**
depth — `evaluate_gate` and validate's `_gate_predicate_keys` recurse without a limit and the
schema's `gate` definition references itself, so nothing enforces the "one level" the schema's
free-text description mentions. Sub-gate predicate results are flattened into the parent's trace.
An unknown key (the schema should have rejected it) is traced unavailable and never fires.

Availability matrix — the runtime availability for a representative backend set, by signal
group. **A** available, **C** conditional, **U** unavailable. The adapter descriptors are the
authority; this matrix illustrates what they encode. The static check (§6.4) reads only the
descriptor grades behind the `page/doc conf` and `field conf` columns, and a grade predicts a
**channel**, not a confidence: a `U` in `doc_type conf` is always a runtime
`signal_unavailable`, never a validate-time error, and so is a `U` in `field conf` wherever the
backend grades `typed_fields` `D` but its derived fields carry no confidence (docling⁸).

| Backend          | text¹ | PDF-layer² | table³ | page/doc conf | field conf | doc_type conf |
|------------------|-------|------------|--------|---------------|------------|---------------|
| pymupdf          | A     | C          | C      | U             | U⁴         | U             |
| tesseract        | A     | C          | U      | A             | U⁴         | U             |
| docling          | A     | C          | C      | C⁵            | U⁸         | U             |
| reducto          | A     | C          | C      | C             | C          | C             |
| aws-textract     | A     | C          | C      | A             | A          | C             |
| anthropic-claude | A     | C          | C      | U⁶            | C⁷         | C             |

1. `chars_per_page_below`, `empty_pages_over`, `garbled`/`garble_score_over`, `zero_blocks`,
   `matches_regex`, `sample_percent` — engine-computed over any normalized result: available on
   every backend, which is why the `default` bundle leans on them.
2. `text_source`, `scanned_pages_detected` — conditional on **PDF input**, independent of backend
   (the probe reads the input, not the parser).
3. Conditional on the result actually containing tables.
4. No `typed_fields` channel at all (pymupdf refuses extraction with `unsupported_feature`;
   tesseract emits plain text); `fields_required` would still fire, on absence.
5. The docling adapter maps docling-serve's ConfidenceScores report onto `pages[].confidence` and
   `document.confidence` when the report is present, so `confidence_below` /
   `page_confidence_below` bind at runtime; absent the report they skip (`signal_unavailable`).
6. Token-stream models emit no page/block confidence.
7. Only when the extraction reports numeric confidence; qualitative strings are never coerced.
8. Docling's descriptor grades `typed_fields` **D** (derived from its key-value / form-item
   graphs), so `validate._predicate_binds` treats `field_confidence_below` as bindable and a
   gate made solely of it on a docling step passes `validate_config` — but the derived
   `TypedField`s carry no `confidence`, so at runtime the predicate is `signal_unavailable`.

`fields_required` and `warning_code` are evaluable on **every** backend (absence fires; the
warnings channel always exists) and so never trigger the unbindable-gate error on their own.
"""

from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass, field
from typing import Any

from openreading.types.response import NormalizedResponse

# garble composite thresholds / weights (module docstring §3). Tuned so clean prose scores ~0 and
# mojibake (replacement chars or runs of non-ASCII non-words) scores > the 0.3 `garbled: true` cut.
_GARBLE_TRUE_CUT = 0.3
_VOWELS = set("aeiouAEIOU")


@dataclass
class SignalSnapshot:
    """Everything the probe could compute for one result. `None` == unavailable (traced as
    `signal_unavailable` when a predicate needs it)."""

    # Tier-1 (text metrics — always computable from a result; 0/empty is a value, not absence)
    chars_per_page: float = 0.0
    empty_pages_fraction: float = 0.0
    garble_score: float | None = None  # None only when there is no text at all to judge
    zero_blocks: bool = True
    doc_text: str = ""
    content_hash: str | None = None  # sha256 of input bytes -> sample_percent bucket

    # Tier-1 PDF-layer (pypdf over the input; None when not a readable PDF)
    scanned_pages_detected: bool | None = None
    text_source: str | None = None  # "none" | "digital" | "prior_ocr"

    # Tier-1 tables (None when the result carries no tables)
    table_sanity: float | None = None

    # Tier-2 (backend-reported; None/empty when absent)
    doc_confidence: float | None = None
    page_confidences: list[float] = field(default_factory=list)
    typed_fields: dict[str, Any] = field(default_factory=dict)  # name -> {value, confidence}
    doc_type_confidence: float | None = None
    warning_codes: set[str] = field(default_factory=set)

    # cross-branch (pick:best only): 1 - content_overlap over the compared branches (§11). None on
    # a single-response probe; the engine injects it when gating a parallel step's winner.
    disagreement_over: float | None = None

    def sample_bucket(self) -> float | None:
        if self.content_hash is None:
            return None
        return (int(self.content_hash[:8], 16) % 10_000) / 100.0  # [0, 100)


@dataclass(frozen=True)
class PredicateResult:
    key: str
    threshold: Any
    observed: Any  # the observed value, or None when unavailable
    fired: bool
    unavailable: bool = False


@dataclass(frozen=True)
class GateResult:
    fired: bool
    predicates: tuple[PredicateResult, ...]


# --------------------------------------------------------------------------- the probe


def probe(
    response: NormalizedResponse, *, doc_bytes: bytes | None = None, mime_type: str | None = None
) -> SignalSnapshot:
    """Compute every available signal for `response`. `doc_bytes` (the input) powers the PDF-layer
    signals and the sample bucket; omit them and those signals are simply absent."""
    snap = SignalSnapshot()
    doc = response.document

    pages = doc.pages or []
    page_texts = [_page_text(p) for p in pages]
    if pages:
        snap.chars_per_page = sum(len(t) for t in page_texts) / len(pages)
        snap.empty_pages_fraction = sum(1 for t in page_texts if len(t.strip()) < 25) / len(pages)
    else:
        full = doc.text or doc.markdown or ""
        snap.chars_per_page = float(len(full))
        snap.empty_pages_fraction = 0.0 if full.strip() else 1.0

    snap.doc_text = doc.text or doc.markdown or "\n".join(page_texts)
    snap.zero_blocks = not any(p.blocks for p in pages)
    snap.garble_score = garble_score(snap.doc_text)
    snap.table_sanity = _table_sanity(response)

    # Tier-2 from the envelope
    confs = [p.confidence for p in pages if p.confidence is not None]
    snap.page_confidences = confs
    snap.doc_confidence = (sum(confs) / len(confs)) if confs else None
    if response.typed_fields:
        snap.typed_fields = {
            k: {"value": v.value, "confidence": v.confidence}
            for k, v in response.typed_fields.items()
        }
    if doc.doc_type and doc.doc_type.confidence is not None:
        snap.doc_type_confidence = doc.doc_type.confidence
    if response.warnings:
        snap.warning_codes = {w.code for w in response.warnings if w.code}

    # Tier-1 PDF-layer + content hash from the input bytes
    if doc_bytes:
        snap.content_hash = hashlib.sha256(doc_bytes).hexdigest()
        if _looks_like_pdf(doc_bytes, mime_type):
            _probe_pdf_layer(doc_bytes, snap)
    return snap


def _page_text(page) -> str:
    if page.text:
        return page.text
    if page.markdown:
        return page.markdown
    if page.blocks:
        return "\n".join(b.text or b.markdown or "" for b in page.blocks)
    return ""


def _looks_like_pdf(doc_bytes: bytes, mime_type: str | None) -> bool:
    if mime_type and "pdf" not in mime_type.lower():
        return False
    return doc_bytes[:5] == b"%PDF-"


def _probe_pdf_layer(doc_bytes: bytes, snap: SignalSnapshot) -> None:
    """Set scanned_pages_detected + text_source from the INPUT via pypdf. Any failure leaves them
    absent (None) — the probe never raises on a malformed input."""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(doc_bytes))
        any_scanned = False
        any_text = False
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if text:
                any_text = True
            elif getattr(page, "images", []):
                any_scanned = True  # no text + an image ≈ a scanned page
        snap.scanned_pages_detected = any_scanned
        # v0.3 classifies none vs digital; `prior_ocr` (invisible-OCR-layer detection) needs
        # render-mode / image-coverage analysis pypdf doesn't expose — deferred (D-v3-9).
        snap.text_source = "digital" if any_text else "none"
    except Exception:  # noqa: BLE001 — a broken input just leaves the PDF-layer signals absent
        snap.scanned_pages_detected = None
        snap.text_source = None


# --------------------------------------------------------------------------- garble composite


def garble_score(text: str) -> float | None:
    """Composite mojibake score in [0,1], or None when there is no text to judge. Combines a
    replacement/control-char ratio, a non-word-token ratio, and a non-ASCII-letter ratio."""
    if not text or not text.strip():
        return None
    n = len(text)
    bad = sum(1 for c in text if c == "�" or (ord(c) < 32 and c not in "\n\r\t"))
    r_replace = min(1.0, (bad / n) * 8)

    tokens = [t for t in re.findall(r"\S+", text) if any(ch.isalpha() for ch in t)]
    r_nonword = 1.0 - sum(1 for t in tokens if _is_wordlike(t)) / len(tokens) if tokens else 0.0

    letters = [c for c in text if c.isalpha()]
    r_nonascii = (sum(1 for c in letters if ord(c) > 127) / len(letters)) if letters else 0.0

    return min(1.0, 0.4 * r_replace + 0.35 * r_nonword + 0.25 * r_nonascii)


def _is_wordlike(token: str) -> bool:
    core = token.strip(string.punctuation + "0123456789")
    if len(core) < 2:
        return False
    letters = sum(1 for c in core if c.isalpha())
    if letters / len(core) < 0.6:
        return False
    if not any(c in _VOWELS for c in core):  # real latin-script words carry an ASCII vowel
        return False
    nonascii = sum(1 for c in core if c.isalpha() and ord(c) > 127)
    return nonascii / max(1, letters) <= 0.5


def _table_sanity(response: NormalizedResponse) -> float | None:
    """1 - weighted(ragged-row fraction, empty-cell ratio) over pipe tables in table blocks;
    None when the result carries no parseable table."""
    grids: list[list[list[str]]] = []
    for page in response.document.pages or []:
        for block in page.blocks or []:
            if str(getattr(block.type, "value", block.type)) == "table":
                grid = _parse_pipe_table(block.markdown or block.text or "")
                if grid:
                    grids.append(grid)
    if not grids:
        return None
    ragged = 0
    total_rows = 0
    empty = 0
    cells = 0
    for grid in grids:
        modal = max({len(r) for r in grid}, key=lambda c: sum(1 for r in grid if len(r) == c))
        for row in grid:
            total_rows += 1
            if len(row) != modal:
                ragged += 1
            for c in row:
                cells += 1
                if not c.strip():
                    empty += 1
    ragged_frac = ragged / total_rows if total_rows else 0.0
    empty_frac = empty / cells if cells else 0.0
    return max(0.0, 1.0 - (0.6 * ragged_frac + 0.4 * empty_frac))


def _parse_pipe_table(md: str) -> list[list[str]]:
    rows = []
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):  # markdown separator row
            continue
        rows.append(cells)
    return rows


# --------------------------------------------------------------------------- gate evaluation

_ALWAYS = "always"  # sentinel: predicate is always evaluable (never signal_unavailable)


def evaluate_gate(gate: dict[str, Any], snap: SignalSnapshot) -> GateResult:
    """Evaluate a normalized gate map against the snapshot. OR semantics: the gate fires if any
    predicate fires. Returns per-predicate trace records."""
    results: list[PredicateResult] = []
    fired = False
    for key, value in gate.items():
        if key == "any_of":
            sub = [evaluate_gate(g, snap) for g in value]
            fired = fired or any(s.fired for s in sub)
            for s in sub:
                results.extend(s.predicates)
        elif key == "all_of":
            sub = [evaluate_gate(g, snap) for g in value]
            fired = fired or (bool(sub) and all(s.fired for s in sub))
            for s in sub:
                results.extend(s.predicates)
        else:
            pr = _evaluate_predicate(key, value, snap)
            results.append(pr)
            fired = fired or pr.fired
    return GateResult(fired=fired, predicates=tuple(results))


def _unwrap(value: Any) -> tuple[Any, str]:
    """Return (threshold, on_missing) for the {value, on_missing} wrapper form, else (value, skip)."""
    if isinstance(value, dict) and "value" in value and set(value) <= {"value", "on_missing"}:
        return value["value"], value.get("on_missing", "skip")
    return value, "skip"


def _evaluate_predicate(key: str, value: Any, snap: SignalSnapshot) -> PredicateResult:
    threshold, on_missing = _unwrap(value)

    def result(observed: Any, fired: bool) -> PredicateResult:
        return PredicateResult(key, threshold, observed, fired)

    def unavailable() -> PredicateResult:
        # on_missing: escalate makes an absent signal FIRE (the caller declared that intent)
        return PredicateResult(key, threshold, None, on_missing == "escalate", unavailable=True)

    # --- Tier-1 text metrics (always available) ---
    if key == "chars_per_page_below":
        return result(snap.chars_per_page, snap.chars_per_page < threshold)
    if key == "empty_pages_over":
        return result(snap.empty_pages_fraction, snap.empty_pages_fraction > threshold)
    if key == "zero_blocks":
        return result(snap.zero_blocks, snap.zero_blocks == bool(threshold))
    if key == "matches_regex":
        hit = re.search(threshold, snap.doc_text) is not None
        return result(hit, hit)
    if key == "sample_percent":
        bucket = snap.sample_bucket()
        if bucket is None:
            return unavailable()
        return result(bucket, bucket < threshold)
    if key in ("garbled", "garble_score_over"):
        if snap.garble_score is None:
            return unavailable()
        cut = _GARBLE_TRUE_CUT if key == "garbled" else threshold
        return result(round(snap.garble_score, 4), snap.garble_score > cut)

    # --- Tier-1 PDF-layer (absent unless a PDF was probed) ---
    if key == "scanned_pages_detected":
        if snap.scanned_pages_detected is None:
            return unavailable()
        return result(snap.scanned_pages_detected, snap.scanned_pages_detected == bool(threshold))
    if key == "text_source":
        if snap.text_source is None:
            return unavailable()
        return result(snap.text_source, snap.text_source == threshold)
    if key == "table_sanity_below":
        if snap.table_sanity is None:
            return unavailable()
        return result(round(snap.table_sanity, 4), snap.table_sanity < threshold)

    # --- Tier-2 (backend-reported) ---
    if key == "confidence_below":
        if snap.doc_confidence is None:
            return unavailable()
        return result(snap.doc_confidence, snap.doc_confidence < threshold)
    if key == "page_confidence_below":
        if not snap.page_confidences:
            return unavailable()
        worst = min(snap.page_confidences)
        return result(worst, worst < threshold)
    if key == "doc_type_confidence_below":
        if snap.doc_type_confidence is None:
            return unavailable()
        return result(snap.doc_type_confidence, snap.doc_type_confidence < threshold)
    if key == "warning_code":
        hit = threshold in snap.warning_codes
        return result(hit, hit)
    if key == "field_confidence_below":
        return _field_confidence(value, snap)
    if key == "fields_required":
        return _fields_required(value, snap)

    # --- cross-branch (pick:best only; §11 phase 2) ---
    if key == "disagreement_over":
        if snap.disagreement_over is None:
            return unavailable()
        return result(round(snap.disagreement_over, 4), snap.disagreement_over > threshold)

    # unknown key (schema should have rejected it) — treat as unavailable, never fire
    return PredicateResult(key, threshold, None, False, unavailable=True)


def _numeric_conf(entry: Any) -> float | None:
    c = entry.get("confidence") if isinstance(entry, dict) else None
    return float(c) if isinstance(c, (int, float)) else None


def _field_present(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    v = entry.get("value")
    return v is not None and str(v).strip() != ""


def _field_confidence(value: Any, snap: SignalSnapshot) -> PredicateResult:
    # per-field map form: {fields: {name: F}, on_missing?}
    if isinstance(value, dict) and "fields" in value:
        on_missing = value.get("on_missing", "skip")
        any_numeric = False
        fired = False
        observed: dict[str, Any] = {}
        for name, floor in value["fields"].items():
            conf = _numeric_conf(snap.typed_fields.get(name))
            if conf is None:
                continue
            any_numeric = True
            observed[name] = conf
            if conf < floor:
                fired = True
        if not any_numeric:
            return PredicateResult(
                "field_confidence_below",
                value["fields"],
                None,
                on_missing == "escalate",
                unavailable=True,
            )
        return PredicateResult("field_confidence_below", value["fields"], observed, fired)
    # scalar form: any field whose numeric confidence < value
    threshold, on_missing = _unwrap(value)
    numeric = [c for v in snap.typed_fields.values() if (c := _numeric_conf(v)) is not None]
    if not numeric:
        return PredicateResult(
            "field_confidence_below", threshold, None, on_missing == "escalate", unavailable=True
        )
    worst = min(numeric)
    return PredicateResult("field_confidence_below", threshold, worst, worst < threshold)


def _fields_required(names: list[Any], snap: SignalSnapshot) -> PredicateResult:
    """Fires on absence by definition (never unavailable). A field is satisfied when it (or any
    alias) is present and non-empty."""
    missing: list[str] = []
    for spec in names:
        if isinstance(spec, str):
            candidates = [spec]
            label = spec
        else:
            label = spec["name"]
            candidates = [label, *spec.get("aliases", [])]
        if not any(_field_present(snap.typed_fields.get(c)) for c in candidates):
            missing.append(label)
    return PredicateResult("fields_required", names, missing, bool(missing))
