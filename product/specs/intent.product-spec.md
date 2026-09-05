> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. It was written before the monorepo
> split and predates protocol v2, adapter-descriptor v0.7 and the current `.env.example` posture,
> so verify every fact against this repository's code before implementing from it. Delete this
> file in the pull request that finishes the work, moving its durable facts into the module
> docstrings.

---
spec_format_version: "0.1"
title: "Intent Layer"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-08-09T00:00:00Z"
updated_at: "2026-08-09T00:00:00Z"
applies_to:
  - path: "src/openreading/schemas/"
  - path: "src/openreading/evals/scorers.py"
  - path: "src/openreading/strategies/"
  - path: "src/openreading/router/"
  - path: "src/openreading/adapters/"
  - component: "intent-layer"
---

## Problem

Every serious extraction surface treats every field in the schema as equally important, because
the schema is the only intent surface there is and a field is either in it or not. The industry
converged on schema-as-prompt — Google Document AI states that "field name and description ...
form the underlying prompt" (https://docs.cloud.google.com/document-ai/docs/ce-mechanisms,
accessed 2026-08-09), and LlamaExtract's field descriptions "are not only for documentation"
(https://developers.llamaindex.ai/python/cloud/llamaextract/features/schema_design, accessed
2026-08-08) — and stopped there. Across the ten hosted APIs, eight LLM frameworks, and six IDP
incumbents this project surveyed, none lets a caller say "the SSN must be right; the memo line is
best-effort," and none of fourteen public benchmarks scores extraction under field importance
(four confirmed absences, surveyed 2026-08-08). The closest thing shipping is Azure's
transparency note telling users to hand-set per-field confidence thresholds by business
criticality
(https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/document-intelligence/transparency-note,
accessed 2026-08-08) — the assembly language of the capability, with no compiler.

openreading's callers pay for that silence directly. A 1040 parsed to impute qualifying income
is not the same task as a 1040 parsed for archival, even though every byte is the same: the
imputation has six fields that carry the outcome and forty that are furniture, and a system that
spends its precision budget uniformly spends most of it wrong. Review queues sort by raw
confidence or by nothing; verification passes treat the memo line and the SSN as peers; and the
industry's headline metric compounds the waste — 90% straight-through processing says nothing
about the correctness of the 90% that skipped review
(https://www.hyperscience.ai/blog/automation-at-all-costs-the-fallacy-of-business-value-with-straight-through-processing/,
accessed 2026-08-08).

Two buyer shapes anchor the design. The lending buyer imputes income from tax documents under a
formula the counterparty publishes and signs: Fannie Mae Form 1084 is literally a signed sum over
named IRS line items (the official PDF refuses non-browser fetches; content verified via Enact's
reproduction,
https://content.enactmi.com/2024-01/Fannie%20Mae%20Form%201084%20Calculator%20-%20Cash%20Flow%20Analysis.pdf,
accessed 2026-08-08), and the DU validation service prices per-component directional tolerance —
income validated when computed ≥ declared, or within 1% below — in rep-and-warrant relief
(validation-service mechanism per Selling Guide B3-2-02,
https://guide-selling.fanniemae.com/sel/b3-2-02/du-validation-service, accessed 2026-08-08;
tolerance language via search-index snippet of the official FAQ, corroborated by the 2018 FAQ
PDF, https://www.fanniemae.com/media/17821/display, text-extracted 2026-08-08).
Precision on the fields that feed the formula is literally priced; precision elsewhere is not.
The health buyer runs the same pattern in a second regulated industry: clinical quality measures
ship as declared computations over canonical vocabularies
(https://cql.hl7.org/01-introduction.html, accessed 2026-08-08), and claims teams extract what
the payer's published adjudication edits will consume. Both want the horizontal capability —
declare or import what the extraction feeds, and have the system spend accordingly — not another
closed vertical application; that ground is held by program-hard-coded incumbents, and the
horizontal API position is unoccupied.

## Hypothesis

If a caller can say — or adopt from a reviewable draft — which fields carry the outcome and what
computation they feed, and that declaration compiles into machinery the product already has
(routing order, escalation gates, merge votes, verification passes, review queues, scoring),
then outcome error drops at equal or lower cost versus uniform treatment. The bet is falsifiable
and three offline studies are designed to falsify it: if the sensitivity distribution across
fields is flat, if criticality-directed verification never separates from uniform verification at
matched cost, or if weighted gains arrive by materially tanking unweighted accuracy, the layer is
not worth having as designed.

The adoption half of the bet is that intent must be a ladder, not a form. Fewer than 5% of users
change any default settings (Spool/UIE,
https://archive.uie.com/brainsparks/2011/09/14/do-users-change-their-settings/, accessed
2026-08-08), and every vendor that started with "write your schema" has since shipped a
draft-for-me path. So the base rung reads intent out of what the caller already sends, every rung
above it is strictly optional, and every vocabulary word ships with a visible payoff — because
annotations nobody's outcomes depend on rot, the death P3P died
(https://cdt.org/insights/looking-back-at-p3p-lessons-for-the-future/, accessed 2026-08-08).

## Product Summary

Intent is a small, optional, deterministic data object — declared by the caller or inferred with
consent and receipts — that compiles into machinery openreading already has. It adds no engine,
no node type, and no LLM dependency, and the zero-intent path stays byte-identical to today.

Callers express it on a four-level ladder named for the caller's act. L0 Tacit: the call is the
intent — schema field membership, requested outputs, `optimize_for`, `doc_type_hint` — read
engine-side for free. L1 Marked: one criticality code per field (`critical` / `expected` /
`optional` / `ignore`) plus one prose purpose per request. L2 Measured: numbers and policies —
tolerance, target accuracy, failure action, fidelity, aliases. L3 Computed: the declared or
imported downstream computation, from which per-field weights are derived mechanically; the
first importable profiles are `fannie-1084@<tax-year>` and `freddie-91@<tax-year>`.

Two carriers, no new file format: six `x-read-*` keywords in the extraction schema (a set closed at
birth), and an optional `intent` block (purpose / profile / compute / review) on a new request
schema version, marked experimental. What compiles out of them: weighted scoring and a
critical-pass rate defined once in the eval scorers and inherited by compare and calibrate; a
strategy word `critical:` that desugars to existing gates; verification and review spend
allocated as criticality × uncertainty under existing ceilings; criticality-weighted composite
scores and merge fan-out; router stage-3 reordering over the unchanged eligible set; per-backend
push-down of queries, descriptions, and aliases with strip-and-map and deliver-or-warn; and an
honest response echo — the intent hash, the applied codes, and four warning codes.

Inference ships deterministic-first: the call-signature and schema-shape channels emit advisory
defaults, and the compute channel derives weights from a declared formula. Every channel emits a
reviewable intent draft the caller must adopt before anything applies; LLM drafting waits on the
separately-bet decider seat. Reporting follows the honest-metric rule: weighted results appear
beside unweighted results, and wherever an automation rate surfaces it is paired with verified
accuracy on a sampled subset, never reported as raw straight-through processing.

Three offline studies test the causal bet, all fixture-based with zero network. E1 votes only on
price-bearing fields of CORD receipts (CC BY 4.0, https://github.com/clovaai/cord, accessed
2026-08-08) against uniform voting at matched call counts, scored by the fraction of receipts
whose computed total is exact. E2 renders TaxCalcBench's 51 structured inputs
(https://github.com/column-tax/tax-calc-bench, accessed 2026-08-08; exact license UNVERIFIED)
into filled public-domain IRS forms and measures imputed-income dollar error against verification
cost under Form 1084 sensitivity weights. E3 applies deliberately crude binary criticality to
DocILE invoices (code MIT; dataset gated, exact terms UNVERIFIED — re-check at build;
https://github.com/rossumai/docile, accessed 2026-08-08), with unweighted micro-F1 as the
do-no-harm criterion. No AI-eval gate is written into the acceptance criteria below because pass
thresholds are not yet committed — see Open Questions.

The milestone ships in two tranches: tranche A — the schema keywords, weighted scorer and its CI
tests, strategy compile, composite and merge weighting, adapter push-down and descriptor
additions, response echo, deterministic inference, the intent-draft artifact, and E1 — with zero
request-schema churn; tranche B — the request `intent` block, sensitivity derivation, router
stage-3 weighting, the first profiles, and the E2/E3 studies.

## Scope

```productspec-scope
in:
  - Ship the six x-read-* schema keywords as a set closed at birth, with obligation semantics per criticality code.
  - Ship the optional request intent block (purpose, profile, compute, review) on a new request schema version marked experimental, in tranche B.
  - Ship weighted scoring and the critical-pass rate once in the eval scorers, reporting weighted, unweighted, and cost together, inherited by compare's truth stance and calibrate.
  - Ship the strategy word critical, desugaring to existing gates, with the compiled gate set traced and hashed.
  - Weight composite scores and merge fan-out by criticality, entering the run's configuration hash.
  - Weight router stage-3 scoring by intent and consume doc_type_hint there, in tranche B.
  - Extend adapter descriptors with intent-consumption facts, per-request field limits, and declared conflict pairs.
  - Push intent down to capable adapters as queries, descriptions, and aliases, stripping x-read-* keywords first and warning on anything unhonorable.
  - Echo applied intent in the response orchestration block with four warning codes, with no response schema bump.
  - Ship the deterministic inference channels (call signature, schema shape, declared computation), the intent-draft artifact, and a CLI draft-and-adopt flow.
  - Ship the E1 study as in-tree fixtures, design and run E2 and E3 as studies, and ship fannie-1084 and freddie-91 as importable profiles gated on a licensing check.
out:
  - Do not add an intent DSL or a new file format; intent rides the extraction schema and the request block.
  - Do not accept caller-facing float weights; callers write the four codes, and floats stay derived internals with a published versioned mapping.
  - Do not let any LLM into the offline verification gate or any deterministic trust path, and never arm an LLM channel from a request field.
  - Do not infer and apply intent silently; adoption is the caller's act, always.
  - Do not impute or rewrite confidence; a missing signal no-ops its gate with a trace, never a synthetic score.
  - Do not feed comparison output into routing in the community tier.
  - Do not build a lending vertical product; profiles ship as importable artifacts and eval corpora, with no rep-and-warrant claims, ever.
  - Do not build an intent console or GUI; draft-then-edit happens through the CLI and SDK emitting reviewable files.
  - Do not run a standardization campaign for the keyword vocabulary before adoption evidence exists.
  - Do not add a vision-native retrieval index class, and do not build intent-aware triage while the agent milestone stays paused.
cut:
  - Cut cross-document term binding and the batch manifest source form to tranche 2, together with the batch-result family bump it requires.
  - Cut the engine-side compute-check gate; this version executes declared computations in the eval harness only.
  - Cut LLM draft inference until the decider milestone ships its armed seat, and downstream-artifact inference to tranche 2.
  - Cut the usage-telemetry and corrections-loop inference channels; when corrections arrive they update routing and weighting, never a model.
  - Cut the weighted compare report headline and finding-severity modulation, which need a report-family bump and their own honesty design.
  - Cut per-doc-type verified quality priors until the eval harness produces measured numbers.
  - Cut the field-scoped re-extraction primitive; escalation units remain document and page.
  - Cut conformal risk certificates and abstention-cost quoting until calibration corpora are held.
  - Cut live threshold recalibration from a declared target accuracy; the offline calibrate sweep is the stand-in.
  - Cut the on-fail object form, the typed per-field status enum, and reliability-weighted voting, each named for a later version.
```

## User Experience

A caller who does nothing sees no change at all — same routing, same response, same bytes. The
first visible rung costs one keyword: mark two fields `critical` and one `ignore` in the schema
already being sent, and runs get cheaper on the ignored field, gates and review queues
concentrate on the critical ones, and the response says which asks were honored and which drew a
warning. A caller with numbers writes them — a directional tolerance, a target accuracy, a
failure action per field. A caller with a formula declares it, or imports the counterparty's as a
named, versioned profile, and per-field weights are derived rather than guessed — along with the
cheapest cross-backend check there is: do the extracted parts sum to the extracted total? One v1
limit, stated plainly: profiles and compute blocks bind fields within a single document (a
multi-form tax return counts as one document); cross-document role binding — which the flagship
profile's K-1-scales-1065 term needs in its full multi-file shape — arrives with the tranche-2
batch manifest.

Inferred intent arrives as a draft, never as behavior. The CLI emits a reviewable
`*.intent-draft.json` in which every entry carries an evidence pointer; the caller edits and
commits it like any config, and only adopted intent applies, its hash landing in the trace. Eval
and compare reports show the weighted score beside the unweighted one and the critical-pass rate
beside both, so a weighted verdict can never hide an unweighted disagreement.

## Acceptance Criteria

```productspec-acceptance-criteria
- id: AC-1
  criterion: A request expressing no intent runs byte-identically to the previous release, and uniform weights reproduce today's unweighted metrics bit-for-bit, pinned by a degeneracy test in CI.
- id: AC-2
  criterion: The set of eligible backends for any request is identical with and without intent across a request matrix pinned in CI, and no compliance decision reads any intent field.
- id: AC-3
  criterion: Every intent construct at every ladder level is individually optional with a sane default, and no intent feature requires another intent feature to be used.
- id: AC-4
  criterion: Prose slots — the request purpose, schema field descriptions, extraction instructions, and strategy intent strings — never move a gate, budget, threshold, or eligibility set.
- id: AC-5
  criterion: The whole layer works with no LLM configured, no LLM call occurs in the offline verification gate, and every construct degrades along its defined deterministic path.
- id: AC-6
  criterion: Every inference channel emits a reviewable intent draft whose entries carry evidence pointers, and the engine acts only on intent the caller adopted; nothing inferred is applied silently.
- id: AC-7
  criterion: Declared or adopted intent enters the run's configuration hash, and any response or report it shaped says so — carrying the hash, the applied code map, the generated gates, and the applicable warning codes.
- id: AC-8
  criterion: Weighted results are always reported beside unweighted results, a missing signal no-ops its gate with a trace rather than a synthesized score, and an all-critical schema draws a warning naming the problem.
- id: AC-9
  criterion: Callers never write importance-weight floats — criticality is the four-code enum, while measured quantities such as tolerances and target accuracies stay caller-writable numbers — and the code-to-weight mapping is published and versioned so that derived weights change only when that version changes.
- id: AC-10
  criterion: The strategy word critical desugars to the same gates a longhand author writes today, proven by golden files, and the compiled gate set appears traced and hashed in the run record.
- id: AC-11
  criterion: Weighted scoring is implemented once in the eval scorers and inherited by compare's truth stance and by calibrate with no scoring code added in the comparison layer.
- id: AC-12
  criterion: Every x-read-* keyword is stripped before push-down to every backend, and any per-field ask a backend cannot honor produces an intent-not-honored warning rather than silence or fabrication.
- id: AC-13
  criterion: Verification and review spend follows criticality times uncertainty under existing ceilings — high-criticality low-confidence escalates, low-criticality fields never draw gray-band budget, and no budget or limit ceiling is raised.
- id: AC-14
  criterion: Intent reorders only among backends that survive the unchanged eligibility stages, and doc_type_hint is consumed in that reordering against measured quality grades only.
- id: AC-15
  criterion: A compute block or profile whose terms do not name properties of the extraction schema fails at load time with a clean error rather than a silent no-op, and its derived per-field weights are deterministic and reproducible.
- id: AC-16
  criterion: The E1 receipt study runs offline from fixtures in-tree, with no network, reporting the weighted score, the unweighted score, and cost from a pinned static price table.
```

## Success Metrics

```productspec-success-metrics
- id: SM-1
  metric: intent_adoption_share_of_schema_bearing_callers
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: 90 days after tranche B ships
- id: SM-2
  metric: outcome_error_reduction_at_matched_cost
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: the first completed E1 and E2 study runs
- id: SM-3
  metric: unweighted_f1_regression_under_intent
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: the first completed E3 study run
- id: SM-4
  metric: cost_reduction_from_ignore_tier_fields
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: the first reference-corpus measurement after tranche A ships
```

## Risks

The largest risk is not technical, and it is the same one recorded when the agent surface was
paused: this work would be built before the product is widely distributed, so the adoption metric
has no denominator yet. Building it first assumes intent is what makes teams adopt the product
rather than something adopters ask for once they arrive. The mitigations are that tranche A is
deliberately small, and that the three studies produce publishable evidence about the causal bet
whether or not adoption follows — see Open Questions.

The flagship profiles carry a licensing risk. Form 1084 must be hand-transcribed — the official
PDF refuses non-browser fetches and the IRS MeF XML vocabulary is access-gated — and the
redistribution posture of a transcribed GSE worksheet needs a clean-room and licensing check
before it ships vendored. The profile is scope-gated on that check, not on hope.

Annotation rot is the adoption failure mode: vocabularies nobody's outcomes depend on die, as P3P
did. The mitigation is designed in — every keyword ships with a visible caller payoff, the first
being cheaper runs from `ignore`-tier fields — and if that payoff discipline slips, the
vocabulary rots regardless of its technical quality.

Weighting is a fabrication hazard. The named anti-pattern: UiPath's generative validation skips
the human when models agree
(https://www.uipath.com/blog/product-and-updates/introducing-generative-validation, accessed
2026-08-08), and its `AutoValidationConfidenceThreshold` activity parameter then sets confirmed
values' confidence to the threshold (parameter wording UNVERIFIED at the letter level, via docs
search summary) — a confidence score rewritten because models happen to agree. A weighted verdict
that hides an unweighted disagreement would be the same hazard in this product's clothes. The
design answers it structurally: unweighted results are always reported, findings are never
deleted, absent signals get explicit-unknown code points rather than guesses, and every
intent-shaped artifact says which intent shaped it.

## Open Questions

- **(1) Milestone graph honesty.** Does this spec depend on the decider spec (its LLM drafting
  channel does) or merely relate to it, given the decider is designed and unbuilt? This revision
  records `relates_to`, because tranches A and B need no LLM at all — revisit when the drafting
  channel is scheduled.
- **(2) Profile transcription and licensing.** What redistribution posture does a transcribed GSE
  worksheet require before `fannie-1084@2024` ships as a vendored profile? A clean-room and
  licensing check is the gate; decide who runs it and what evidence clears it.
- **(3) Descriptor quality bootstrapping.** Which corpora and how many eval runs earn an adapter
  a per-doc-type verified quality grade, and do unverified claimed grades get any routing weight
  at all? The current answer is no, which starves the signal early.
- **(4) Keywords under strict-schema backends.** The compiler strips `x-read-*` before push-down;
  confirm every adapter's schema-subset handling tolerates the strip-and-map pass before tranche
  A closes.
- **(5) Experimental graduation.** The `intent` block is the request schema's first
  experimental field; write the graduation criteria — what evidence promotes it to stable — into
  the changelog entry at birth.
- **(6) Enum-to-float mapping home.** The code-to-weight mapping is part of the metric's
  identity, since changing it silently re-scores history. It lives as a named versioned constant
  in the scorers and is echoed in every eval report; whether it also belongs inside the intent
  object is unresolved.
- **(7) Purpose prose consumer set.** Which adapters get the request purpose compiled into their
  prompt slot — a per-descriptor opt-in flag, or blanket for LLM-class backends?
- **(8) On-fail object form.** Does real usage demand distinct absent-versus-uncertain failure
  actions? Set the evidence bar before the additive extension ships.
- **(Beyond the spine) Eval pass thresholds.** E1–E3 have real datasets, evaluators, and
  falsification conditions, but no committed pass thresholds — which is why no AI-eval block
  appears in this spec. Decide the thresholds once the first study runs produce baselines, and
  record them by revising this spec.

## Related Artifacts

```productspec-related-artifacts
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Intent design (Aim) — the milestone specification"
- type: engineering_spec
  url: "docs/design/intent-prompt.md"
  title: "Intent prompt companion — prose channels, push-down compilation, and inference prompting"
- type: engineering_spec
  url: "docs/design/intent-build-prompt.md"
  title: "Intent build-loop prompt — the re-entrant Task-layer harness executing the work breakdown"
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 1 — zero intent, zero delta; the degeneracy test"
  section_id: acceptance_criteria
  item_id: AC-1
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 2 and the four-locks widening proof"
  section_id: acceptance_criteria
  item_id: AC-2
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 3 — strictly optional at every level"
  section_id: acceptance_criteria
  item_id: AC-3
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 4 — prose never relaxes hard fields"
  section_id: acceptance_criteria
  item_id: AC-4
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 5 — deterministic core, LLM strictly opt-in"
  section_id: acceptance_criteria
  item_id: AC-5
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 6 and the intent-draft artifact"
  section_id: acceptance_criteria
  item_id: AC-6
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Laws 7 and 8 — hashing and the response echo"
  section_id: acceptance_criteria
  item_id: AC-7
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Laws 9 and 13 — weighting is not fabrication; budget discipline"
  section_id: acceptance_criteria
  item_id: AC-8
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 12 — enum surface, derived numbers"
  section_id: acceptance_criteria
  item_id: AC-9
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 10 and the strategy desugar section"
  section_id: acceptance_criteria
  item_id: AC-10
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design Intent Law 11 — one metric stack"
  section_id: acceptance_criteria
  item_id: AC-11
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design application map A6 — adapter push-down, strip-and-map, deliver-or-warn"
  section_id: acceptance_criteria
  item_id: AC-12
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design application map A3 — spend equals criticality times uncertainty"
  section_id: acceptance_criteria
  item_id: AC-13
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design application map A5 — router stage-3 reorder-only"
  section_id: acceptance_criteria
  item_id: AC-14
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design ladder section L3 — load-time validation and mechanical weight derivation"
  section_id: acceptance_criteria
  item_id: AC-15
- type: engineering_spec
  url: "docs/design/intent.md"
  title: "Design eval section E1 — the offline receipt study"
  section_id: acceptance_criteria
  item_id: AC-16
- type: engineering_spec
  url: "docs/strategies/spec.md"
  title: "Strategy layer specification — the built gate grammar intent compiles into"
- type: engineering_spec
  url: "docs/design/canonical-normalization.md"
  title: "Canon — the built normalization layer the fidelity codes lean on"
- type: engineering_spec
  url: "docs/design/batch-intake.md"
  title: "Manifest — built batch intake, which tranche-2 role binding will extend"
- type: product_spec
  product_spec_path: "./decider.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "In-Run Decider — the armed LLM seat the deferred drafting channel waits on"
- type: product_spec
  product_spec_path: "./agentic.product-spec.md"
  product_spec_revision: 1
  relation: relates_to
  title: "Agent Surface — paused; would consume intent-shaped verdicts when it resumes"
```
