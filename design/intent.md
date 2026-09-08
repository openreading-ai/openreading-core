> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. Delete this file in the pull request
> that finishes the work, moving its durable facts into the module docstrings.
>
> **Superseded in part by the removal set, 2026-09-07. Re-scope before implementing.** That set
> deleted the router this record was written against: the three stages are one lookup now (the
> backend the caller named, else `policy.backends` in written order, else `pymupdf`), and with
> them went the compliance filter, the capability gate, stage-3 scoring, `optimize_for`, `auto`,
> and every cost figure. Any passage here that routes on a vendor claim, scores candidates, or
> spells `backend: auto` describes machinery that no longer exists. The governing rule is now
> `openreading.router.router`: core holds no fact it cannot verify. Check `CHANGELOG.md` under
> Unreleased and the module docstrings before taking any line of this as current.

# Intent — Design ("Aim")

**Status:** DESIGN — no code changes in this document's scope. A coding agent executes the §14
work breakdown in tranche order against the §12 eval harness and the two CI tests in §11; the
executing harness is `internal/design/intent-build-prompt.md`.
**Inputs:** the intent synthesis of record (two design proposals adjudicated into one spine,
2026-08-09); fourteen primary-sourced research notes (every external claim below carries its
source URL, access-dated 2026-08-08); three repo maps with file:line evidence. Code anchors in
this document were re-verified against the tree on 2026-08-09; re-verify at build.
**Date:** 2026-08-09.
**Companions:** `internal/product/thesis.md` (why this layer exists),
[`intent.product-spec.md`](../product/specs/intent.product-spec.md) (the
product contract; AC/SM IDs cited here), `internal/design/intent-prompt.md`
(the prose-channel companion), `internal/design/intent-build-prompt.md`
(the build-loop harness), `research/intent/` (evidence pack: landscape, whitespace,
attach-points). These ship in the same documentation tranche; links are forward references
until then.

---

## 1. Thesis in brief

The argument for this layer — openreading not openparser, the 1040-for-imputation example, the
field's schema-as-prompt convergence and binary-schema ceiling, adoption physics, the honest
metric — lives in `internal/product/thesis.md` and is not repeated here; evidence detail is
`research/intent/`. What this document adds is the contracts claim it designs to: **intent is a
small, optional, deterministic data object — declared by the caller or inferred with consent
and receipts — that COMPILES into machinery openreading already has.** It reorders router
stage-3 scoring, desugars into strategy gates, weights merge votes and composite scores,
allocates verification spend as criticality × uncertainty, weights the one metric stack, and is
echoed honestly in the response. It adds no engine, no node type, no LLM dependency, and no
path by which the compliance-eligible set can widen. Product-ladder position: adapters,
strategies, agents (paused) — and intent, the layer that tells the other three what to spend
on. Not a fourth engine; the first layer that makes the others aim — hence the codename.

---

## 2. The thirteen intent laws

Safety invariants (1–9) are non-negotiable and each names its enforcement. Design laws (10–13)
govern shape. Every construct in this document cites these as "Law N".

1. **Zero intent ⇒ zero delta.** A request expressing no intent runs byte-identically to today;
   the inference machinery is not even engaged on the legacy path. "Expressing no intent" is
   defined exactly: no `x-read-*` keyword, no `intent` block, no strategy `critical:` word, and
   no `routing.doc_type_hint`. The last is the deliberate edge case, named rather than
   smuggled: `doc_type_hint` is a pre-existing v0.1 field that nothing consumes today; L0
   declares it tacit intent (§4.1) and tranche B's stage-3 change (B3) starts consuming it — a
   **named routing behavior change** for existing callers already sending it. Their routing
   *order* may change; their eligible set never does (Law 2), and the case sits in the CI
   matrix as a documented carve-out with the expected reorder pinned (§11), not as an exception
   quietly absorbed into "zero delta". Callers sending none of the four intent-bearing signals
   get byte-identical behavior, literally. Uniform weights reproduce today's unweighted metrics
   bit-for-bit (the degeneracy test in CI, §11; the Normalized-Excess-Cost property —
   cost-uniform reduces to plain error rate, https://arxiv.org/html/2510.22016, accessed
   2026-08-08). Mirrors strategies Law 1 (no file ⇒ no change, `docs/strategies/spec.md`).
2. **Intent never widens compliance.** Router stages 1–2 are boolean gates whose signatures take
   no intent input; intent is read only by stage-3 scoring, strategy compilation, merge voting,
   scorers, and the response echo. Four independent locks (§11; locks 1, 3, and 4 hold today,
   lock 2's enforcement is built in tranche B). Pinned by the eligible-set-equality CI test.
3. **Strictly optional at every level.** Every ladder level, every keyword, every block is
   optional with sane defaults; no intent feature is required by any other feature; most callers
   live on L0–L1 forever and that must be fine. Fewer than 5% of users change any default
   (Spool/UIE, https://archive.uie.com/brainsparks/2011/09/14/do-users-change-their-settings/,
   accessed 2026-08-08) — the floor has to be good.
4. **Prose never relaxes hard fields.** `intent.purpose`, schema `description`s,
   `extraction_schema.instructions`, and strategy `intent:` prose are engine-inert by contract:
   they may inform LLM deciders, judges, and inference drafts, and they may be compiled into
   backend prompt slots — they can never move a gate, budget, threshold, compliance decision, or
   eligibility set. Compliance is a hard constraint, never intent. Enforcement: the existing
   strategy posture ("The engine ignores it. Prose can never relax a hard field",
   `docs/strategies/spec.md` node fields) extended verbatim to every intent prose slot; the
   testable contract is in `internal/design/intent-prompt.md`.
5. **Deterministic core; LLM strictly opt-in.** Every construct has a defined deterministic
   degradation path (LLM-verified → rule-verified → threshold-only → inert prose). LLM channels
   are armed exactly like the decider: config + env, never a request field, absent from
   `make verify`'s network-free path (`strategies/decider.py:38` enablement pattern; DECISIONS
   D7 discipline).
6. **Inferred intent is a reviewable draft, never a silent mutation.** Every inference channel
   emits an intent draft (§6); the engine acts only on intent the caller adopted. No-file ⇒
   no-change holds; ambient application is forbidden.
7. **Applied intent is hashed.** Declared or adopted intent enters `config_hash` (as
   `intent_hash`); replay is exact. Strategies Law 8 discipline: any behavior-shaping input is
   hashed or traced (`docs/strategies/execution.md` §6).
8. **Intent echoes itself in the response.** Any response or report shaped by intent says so.
   In v1 the structured echo (`intent_hash`, applied code map, generated gates) rides
   `orchestration`, which the frozen response schema defines as the strategy execution trace —
   so the full echo exists exactly on strategy-engaged runs; a direct-path response shaped by
   intent says so through the warning codes and the hashed trace, and a direct-path echo home
   is a named response.v0.4 candidate (§10 A8, §13). Reports carry
   weighted-alongside-unweighted, always. A weighted verdict that hides an unweighted
   disagreement is a fabrication.
9. **Weighting is not fabrication.** Intent moves selection and spend; it never synthesizes
   confidence, never imputes absent signals (missing-signal law, `docs/strategies/spec.md`
   §"missing signals"; `signals.py:284-299`), never deletes findings (the
   `_cap_nondeterministic` model, `comparison/report.py:41-54`: reshape emphasis, never remove),
   and absent values get explicit-unknown code points, not guesses. The named anti-pattern is
   UiPath-style confidence rewriting on model agreement
   (https://www.uipath.com/blog/product-and-updates/introducing-generative-validation, accessed
   2026-08-08): adopt the routing idea, never the score rewrite.
10. **Intent compiles, never commands.** Every application desugars to constructs that already
    exist (the Plain-dialect precedent, `docs/strategies/spec.md` Plain section). No new engine,
    no new node type; the engine stays a pure function of its hashed inputs.
11. **One metric stack.** Weighted scoring is implemented once in `evals/scorers.py` and
    imported everywhere (compare law L5, `docs/compare/DESIGN.md`); compare's `--truth` stance
    and `calibrate` inherit it with zero code in `comparison/`.
12. **Enum surface, derived numbers.** Callers write a four-code enum; floats are compile
    outputs with a published, versioned mapping. Small teachable vocabularies survive — HL7 v2's
    usage codes are thirty years old
    (https://v2.hl7.org/conformance/HL7v2_Conformance_Methodology_R1_O1_Ballot_Revised_D9_-_September_2019_Constraints.html,
    accessed 2026-08-08); flat numbers invite inflation and are ungradeable in review.
13. **Budget discipline, day-one payoff.** Criticality allocates spend under existing ceilings
    (`budget:`, `limits:`); it never raises them; an all-critical schema draws the
    `intent_all_critical` warning (the DSDM cap on Must-Haves,
    https://www.agilebusiness.org/dsdm-project-framework/moscow-prioritisation.html, accessed
    2026-08-08). And every vocabulary word ships with a visible caller payoff — cheaper runs via
    `ignore`, review queues sorted by what matters, weighted deltas. Annotations nobody's
    outcomes depend on rot; P3P died exactly this death
    (https://cdt.org/insights/looking-back-at-p3p-lessons-for-the-future/, accessed 2026-08-08);
    schema.org survived because a consumer visibly rewarded the markup
    (https://cacm.acm.org/practice/schema-org/, accessed 2026-08-08).

---

## 3. Names, carriers, and the two axes

| Thing | Name |
|---|---|
| Design-layer codename | **Aim** (house pattern: Canon, Manifest, Plain — one blunt word for the layer's act) |
| Request block | `intent` (top-level, `request.v0.2`, `x-stability: experimental`) |
| Schema-annotation namespace | `x-read-*` |
| Expression ladder | **L0 Tacit / L1 Marked / L2 Measured / L3 Computed** |
| Document-level prose slot | `intent.purpose` |
| Computation block | `intent.compute` (XOR `intent.profile`) |
| Intent profiles | `fannie-1084@2024`, `freddie-91@2024` (issuer-form name `@` tax-year) |
| Inference artifact | **intent draft** (`*.intent-draft.json`) — deliberately not "manifest" (that codename is taken by batch intake) |
| Criticality codes | `critical / expected / optional / ignore` |
| Strategy Plain word | `critical: [field, …]` |
| Response warning codes | `intent_not_honored`, `intent_field_unverified`, `intent_field_absent`, `intent_all_critical` (the v1 set; `intent_underspecified` is a named v0.4 candidate, §15.9) |
| Weighted metric names | "intent-weighted score", "critical-pass rate" |

**Two carriers, one derived artifact, one strategy word, one descriptor extension. No new file
format for intent itself.** Ceremony kills intent surfaces: RAIL's XML dialect lost to plain
code, and a bespoke DSL is an adoption tax. Intent rides (1) the extraction schema the caller
already writes (`x-read-*` keywords, §4.2) and (2) the request body (`intent` block, §5); the
derived artifact is the intent draft (§6); the strategy word is `critical:` (§7); the
descriptor extension is v0.5 (§8).

**The two axes are never collapsed.** JSON Schema `required` keeps owning **presence**
("this must be in the output"); `x-read-*` owns **handling** ("this is what you must do about
it"). This is the single most transferable lesson of the vocabulary survey: every mature
criticality vocabulary separates the axes — FHIR cardinality vs `mustSupport`, DICOM Type 1 vs
Type 2, HL7 v2 `R` vs `RE` (https://hl7.org/fhir/profiling.html;
https://dicom.nema.org/dicom/2013/output/chtml/part05/sect_7.4.html; both accessed 2026-08-08).
Collapsing them is the beginner mistake; the interesting codes are the middle ones that mean
"extract it if the document has it, and say so if not."

---

## 4. The expression ladder and the `x-read-*` vocabulary

### 4.1 The ladder

Four levels named for the caller's act: say nothing, mark fields, state numbers, declare the
formula. Every level is optional (Law 3); each subsumes the ones below.

**L0 — Tacit: the call is the intent.** Nothing new declared. Deterministic, engine-side, free —
the floor the <5% statistic demands. Signals read: `extraction_schema.json_schema` field
membership (binary inclusion intent — the NuExtract insight that the template *is* the intent
statement, https://numind.ai/blog/nuextract-a-foundation-model-for-structured-extraction,
accessed 2026-08-08), `required` arrays + field names/types (draft criticality heuristics, C2
in §9), `outputs.*` / `features.*` (channel intent), `routing.optimize_for` (the one coarse
axis stage-3 already consumes, `router/router.py:135-150`), and `routing.doc_type_hint` —
declared in the contract, consumed by nothing in the router today (grep-verified); L0's work
item is to consume it in stage-3 (A5, §10). That work item is the one place tacit intent
changes behavior for unchanged existing requests, and Law 1 names it as a deliberate routing
behavior change with its own CI carve-out rather than pretending it is free (§2 Law 1, §11).
Precedent: Unstructured's `strategy=auto` escalates
to hi_res when tables are requested — the kwarg is the utterance
(https://docs.unstructured.io/api-reference/legacy-api/partition/partitioning, accessed
2026-08-08). `compliance` stays what it is: hard constraint, never intent.

**L1 — Marked: codes per field, purpose per request.** The teachable surface:
`x-read-criticality` on schema properties plus one document-level prose slot, `intent.purpose`
("imputing qualifying income for a self-employed borrower") — consumed by the armed LLM
decider/judge and by inference drafting; engine-inert (Law 4). Two slots — per-field and one
global — is the convergent industry shape across the hosted-API survey.

**L2 — Measured: numbers and policies.** For callers who know their numbers: `x-read-tolerance`,
`x-read-target-accuracy`, `x-read-on-fail`, `x-read-fidelity`, `x-read-aliases`.

**L3 — Computed: the declared downstream computation.** The caller declares (or imports) the
formula the fields feed; the system *derives* per-field weights mechanically — sensitivity of
the target to each term. For linear forms: |coefficient| × typical error magnitude; Sobol
indices for the nonlinear general case
(https://openturns.github.io/openturns/latest/theory/reliability_sensitivity/sensitivity_sobol.html,
accessed 2026-08-08). This is XBRL's calculation linkbase made operational — the taxonomy tells
the consumer which elements feed which totals, weights ±1
(https://www.openriskmanual.org/wiki/XBRL_Calculation_Linkbase, accessed 2026-08-08) — and it
buys the cheapest cross-backend verification there is: do the extracted parts sum to the
extracted total? Profiles are importable and often third-party-authored — the counterparty owns
the formula in lending and claims. First profiles: `fannie-1084@<tax-year>`,
`freddie-91@<tax-year>` (Fannie Mae Form 1084 and Freddie Mac Form 91 are signed formulas over
named IRS line items; the official PDF 403s to non-browser fetches, content verified via
Enact's reproduction,
https://content.enactmi.com/2024-01/Fannie%20Mae%20Form%201084%20Calculator%20-%20Cash%20Flow%20Analysis.pdf,
accessed 2026-08-08). eCQM/CQL proves this artifact class exists machine-readably in a second
regulated industry (https://cql.hl7.org/01-introduction.html, accessed 2026-08-08).

### 4.2 Carrier 1 — `x-read-*` keywords in `extraction_schema.json_schema`

JSON Schema 2020-12 treats unknown keywords as annotations that flow through conformant
validators unharmed (https://json-schema.org/draft/2020-12/json-schema-core, accessed
2026-08-08); the namespace follows the OpenAPI `x-` convention
(https://spec.openapis.org/oas/v3.1.0.html#specification-extensions, accessed 2026-08-08).
`extraction_schema.json_schema` is an open object in `request.v0.1.json`, so the keywords need
**zero request-schema change** — this is what makes tranche A schema-churn-free. The compiler
strips `x-read-*` before push-down to strict-schema backends (§10 A6; open question §15.4). The
set is **closed at birth** — six keywords, no user extension (the XBRL custom-tag lesson:
unconstrained extension degrades comparability and invites strategic obfuscation,
https://www.cpajournal.com/2019/08/21/xbrl-data-comparability/, accessed 2026-08-08):

| Keyword | Values | Default | Consumer |
|---|---|---|---|
| `x-read-criticality` | `critical \| expected \| optional \| ignore` | `expected` | gates, votes, scoring, push-down ordering, review routing |
| `x-read-aliases` | `[string]` | — | `fields_required[].aliases`, Textract `Alias`-class mappings, in-document label variants |
| `x-read-fidelity` | `verbatim \| normalized \| inferred_ok` | `normalized` | grounding promises: `verbatim` fields are string-diffable against source (LMDX-style localization, https://arxiv.org/abs/2309.10952, accessed 2026-08-08); `normalized` is exactly what the Canon layer does to values (groundable, not diffable); `inferred_ok` fields cannot promise citations. The promise degrades honestly across the three |
| `x-read-tolerance` | `{abs \| rel, direction: lower \| higher \| both}` | — | DU-style directional bands (income validated when computed ≥ declared, or within 1% below — https://guide-selling.fanniemae.com/sel/b3-2-02/du-validation-service, accessed 2026-08-08); eval-side recalculation in v1, engine `compute_check` gate tranche 2 |
| `x-read-on-fail` | `escalate \| review \| warn \| accept` | per criticality | the Guardrails OnFailAction insight — failure policy is per-field intent (https://www.guardrailsai.com/docs/concepts/validator_on_fail_actions, accessed 2026-08-08) — collapsed to the four actions the engine already has |
| `x-read-target-accuracy` | number | — | the Hyperscience inversion: caller states the outcome, system derives thresholds (https://help.hyperscience.ai/docs/v38-transcription-accuracy-and-automation, accessed 2026-08-08). Until calibration data exists, a static published policy table maps targets to gate thresholds; `calibrate` is the offline stand-in |

There is deliberately no separate `on_absent`/`on_uncertain` pair: criticality codes imply sane
defaults, and the split is expressible later as an additive object form if evidence demands it
(§15.8). The *status* taxonomy (absent-from-document vs present-but-unreadable vs
not-requested; DICOM Type 2 lineage — you must acknowledge the field even when you cannot fill
it, https://dicom.nema.org/dicom/2013/output/chtml/part05/sect_7.4.html, accessed 2026-08-08)
is response-side truth, expressed in v1 via warnings; a typed per-field status enum is a named
response.v0.4 candidate (§13).

### 4.3 Criticality codes are bundles of testable obligations

The mustSupport lesson: FHIR shipped an undefined "support" bit and spent a decade clarifying
it (https://hl7.org/fhir/profiling.html and https://jira.hl7.org/browse/FHIR-28375, accessed
2026-08-08); the fix was qualifier × function obligation codes with operationally testable
semantics (https://hl7.org/fhir/R5/obligations.html, accessed 2026-08-08). Aim skips the decade:
each code is defined by observable pass/fail obligations from birth.

- **`critical` — extract-or-flag.** The field is returned grounded, or the response carries
  `intent_field_unverified` / `intent_field_absent`; never guessed (Law 9). Verification spend
  is authorized up to the node budget; signal absence escalates (`on_missing: escalate`).
- **`expected` — populate-if-known.** HL7 v2 `RE` / FHIR `populate-if-known`: extract if the
  document has it; absence is recorded, not escalated. The single most useful code for document
  AI — real deployments converge on exactly this floor (AU Core's
  `SHALL:populate-if-known` + `SHALL:no-error`). The default.
- **`optional` — best-effort.** No gate generated; failures never block.
- **`ignore` — excluded.** Excluded from compiled per-field asks and from weighted scoring; the
  immediate cost win that funds the annotation habit (Law 13, the P3P incentive lesson).

`description` remains the prompt channel it already is everywhere — documented as normative
conditioning, not documentation. There is **no** rung for free-floating per-field prose outside
the schema: field prose lives in `description`; node-level decision prose stays in the strategy
`intent:` string. Conflating them would blur the engine-inert guarantee (Law 4; the full prose
contract is `internal/design/intent-prompt.md`).

---

## 5. Carrier 2 — the request `intent` block (`request.v0.2`)

```jsonc
"intent": {
  "purpose": "impute qualifying income for a self-employed borrower",  // prose; engine-inert (Law 4)
  "profile": "fannie-1084@2024",          // XOR compute; versioned, importable, often third-party
  "compute": {
    "target": "qualifying_income",
    "terms": [
      {"ref": "schedule_c_net_profit", "sign": "+"},
      {"ref": "depreciation",          "sign": "+"},
      {"ref": "meals_exclusion",       "sign": "-"},
      {"ref": "k1_ordinary_income",    "sign": "+", "scale_by": "ownership_pct"}
    ],
    "tolerance": {"rel": 0.01, "direction": "lower"}   // DU semantics: ≥ declared, or within 1% below
  },
  "review": {"mode": "budgeted"}          // review-routing posture for gray bands; see below
}
```

Rules:

- All fields optional; `profile` XOR inline `compute` (schema-enforced).
- **`review` is specified, narrow, and narrowing-only.** `review.mode` is a two-value enum,
  `budgeted | none`, default `budgeted` (absent = `budgeted`), consumed only by the intent
  compiler's review emission (A3): `budgeted` emits `review_if` gray bands on `critical` fields
  only, under existing ceilings — the §7 posture, restated as the default; `none` emits no
  `review_if` gates at all (the caller runs review downstream). The mode governs
  compiler-emitted gates only; longhand `review_if` written by an author is untouched. By
  construction it can only narrow compiled review spend relative to the compiler's default —
  it can never arm a decider (Law 5: decision behavior is armed by config + env, never a
  request field), raise any budget or ceiling (Law 13), or touch eligibility (Law 2). Its test
  home is B1's schema tables plus A3's spend tests (AC-13).
- `compute.terms[].ref` must name properties of `extraction_schema.json_schema` — load-time
  validation error otherwise (the D-v3-6 doctrine: unshipped or unbindable constructs error
  cleanly, never no-op silently).
- Conditional terms and cross-document scaling (`scale_by`) are in the shape from day one
  because Form 1084 needs both; cross-*document* term binding waits for the batch manifest
  (§13, tranche 2) — until then L3 binds fields within a single document only.

**Landing mechanics, decided.** A new versioned `request.v0.2.json`: v0.1 stays byte-frozen
(`tests/test_schema_evolution.py` frozen-digest table); pin flip in `schemas/__init__.py`;
pydantic mirror in `types/request.py` + round-trip test; v0.1 goldens validate against v0.2 —
the additivity proof (`tests/test_schema_evolution.py:51`); CHANGELOG entry carrying the
experimental-graduation criteria (§15.5). The block is annotated `x-stability: experimental` —
the sanctioned no-compat-guarantee mechanism. Honesty note on what that buys today: the
`experimental_fields()` registry exists (`schemas/__init__.py:78-105`), but it is generated for
the *response* schema and consumed only by the schema-evolution meta-test
(`tests/test_schema_evolution.py:157`); no compliance code reads it yet, and its docstring's
"compliance code consults it" is aspirational. B1 therefore ships the enforcement, not just
the annotation: extend the registry invocation to the request schema, update the meta-test,
and add a test asserting compliance evaluation is invariant to every experimental-annotated
request path — which is what makes lock 2 of §11 real. Decided against extending the
`keep_candidates` server-pop hack (D-v4-14): a
second pre-validation pop is a smell, and this is the request schema's first experimental
field — a precedent worth setting cleanly.

**Surface wiring.** Python: `run(..., intent={...})` works via the existing `**overrides`
passthrough with zero `api.py` change (`api.py` `build_request` merges raw top-level keys).
Server: needs only the pin flip — validation is schema-driven (`server/app.py` validates raw
bodies against the vendored schema). CLI: grows `--intent` / `--intent-file` beside `--extract`
(`cli/app.py` parser).

---

## 6. The intent draft (derived artifact, not a wire schema)

JSON emitted by every inference channel (§9):

```jsonc
{
  "field": "total_amount",
  "code": "critical",
  "evidence": "sqlglot lineage: total_amount → underwriting.qualifying_income (DIRECT)",
  "derived_at": "2026-08-09T00:00:00Z",
  "expires": "2026-09-08T00:00:00Z",
  "channel": "C4",
  "confidence_class": "mechanical"
}
```

Reviewable, diffable, committed by the caller like any config. File convention:
`*.intent-draft.json`. **Adoption is the caller's act** — committing the file, passing it, or
flipping the flag that references it — never the system's (Law 6). On adoption its hash enters
the trace as `intent_hash` (Law 7). The draft is deliberately not a vendored wire schema: it
never crosses the API boundary un-adopted, and its shape can iterate without a version cut; the
CLI draft/adopt flow (§14, tranche A) is its only producer and consumer in v1.

---

## 7. Strategy surface — the `critical:` word

One new Plain-dialect criterion word, joining `looks_bad` / `low_confidence` / `missing` /
`disagree` in `escalate_when`:

```yaml
invoices:
  try: [reducto, anthropic-claude]
  escalate_when:
    critical: [invoice_number, total_amount]
```

Desugars to longhand exactly as `missing:` / `low_confidence` do — emitted per step on every
non-final rung, per Plain's existing rules:

```yaml
steps:
  - backend: reducto
    escalate_if:
      any_of:
        - fields_required:
            - {name: invoice_number, aliases: []}     # aliases filled from x-read-aliases when present
            - {name: total_amount, aliases: []}
        - field_confidence_below:
            fields: {invoice_number: <τ_critical>, total_amount: <τ_critical>}
            on_missing: escalate                       # critical + signal-absent escalates (Law 9 boundary:
                                                       # absence triggers spend, never a guessed score)
    review_if:                                         # gray band on critical fields ONLY
      field_confidence_below:
        fields: {invoice_number: <τ_review>, total_amount: <τ_review>}
  - backend: anthropic-claude                          # final rung: never gated (Plain rule, unchanged)
```

`τ_critical` and `τ_review` come from the published, versioned policy table (§4.2
`x-read-target-accuracy` row). Precedent: Azure's Document Intelligence transparency note
documents confidence-threshold calibration per scenario, piloted on the customer's own corpus,
with a ≥0.80 straight-through example
(https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/document-intelligence/transparency-note,
accessed 2026-08-08). The stronger tiered reading — thresholds hand-set per field by business
criticality, ≥0.90 critical / ≥0.80 important / ≥0.70 non-critical — reached the research pack
via search excerpt only and is UNVERIFIED against the live page
(`research/intent/whitespace.md` claim 1); the compiles-the-practice claim rests on the
verified per-scenario calibration guidance, which is still hand-set thresholds with no
compiler. Numeric defaults are set at build via `calibrate` sweeps, not invented here.

Notes:

- The compiled constructs are all existing longhand: `fields_required` and per-field
  `field_confidence_below` (`strategies/signals.py:380-428`), `review_if` gray bands. The
  missing-signal law binds unchanged; `strategy validate`'s can-this-bind check applies.
- The emitted `review_if` resolves deterministically to `review_default` (default `escalate`)
  when no decider is armed — Plain files stay deterministic on undecidered deployments; an
  armed decider changes gray-band resolution exactly as it does for advanced files. `spec.md`'s
  Plain section is amended to state this when the word lands.
- The compiled gate set is recorded in the trace and hashed (Law 7); provenance rides the
  existing `GateRecord.source` mechanism from the Plain milestone.
- Longhand authors keep writing the gates directly — the intent compiler emits, it does not
  replace. Node-level `intent:` prose is unchanged, engine-inert. Presets gain nothing: intent
  presets are the request's job, not the strategy file's.
- Grammar mechanics: the word is added by cutting `strategy-config.v0.3.json` (v0.2 byte-frozen;
  the Plain P0 playbook), additive, config `version` const unchanged.

---

## 8. Descriptor additions (`adapter-descriptor.v0.5`, additive)

Intent-relevant capability facts on the existing honesty ladder (`verified | claimed | False`;
`types/descriptor.py` Capabilities):

- **`intent_consumption` flags:** `per_field_queries` (Textract Queries / Azure
  queryFields-shaped asks, https://docs.aws.amazon.com/textract/latest/dg/API_Query.html,
  accessed 2026-08-08), `field_descriptions_consumed` (schema-as-prompt backends),
  `citations`, `field_confidence`, `page_scoping`.
- **`max_fields_per_request`:** the per-backend cap the compiler chunks against (Textract 15/30
  per page, Mindee ~25, Azure ~20 — see `research/intent/landscape.md` for the sourced table).
  Chunking is an importance-ordering problem: critical fields go in the first chunk (§10 A6).
- **Declared conflict pairs** the router must plan around: citations ⊕ structured outputs is a
  400 on the Claude API (https://platform.claude.com/docs/en/build-with-claude/citations,
  accessed 2026-08-08); Reducto citations ⊕ agent-in-the-loop conflict
  (https://llms.reducto.ai/json-schema-extraction-with-citations, accessed 2026-08-08).
  Grounding is repeatedly the feature that conflicts with other machinery; the fidelity keyword
  makes the conflict schedulable instead of surprising.
- **`quality`:** per-doc-type grades, `verified` **only** from the repo's own eval runs — the
  scorers header rule ("quality routing can only use numbers you measured yourself",
  `evals/scorers.py:1-5`); feeds stage-3 (A5). Bootstrapping criteria are open (§15.3);
  until grades exist, stage-3 stays on `integration_priority` + cost.

`Capabilities` is `extra="allow"`, so experimentation predates the v0.5 file; the v0.5 cut
follows the descriptor's own additive-bump playbook (the `batch` block precedent).

---

## 9. Inference channels, ranked, with safety templates

Safety templates, in increasing consequence: **advisory** (computed, shown, never acted on) →
**draft-then-edit** (system emits an intent draft; human adopts; only adopted intent applies) →
**gated** (opt-in flag per channel; off by default) → **revertible** (auto-applied under
verify-and-revert with bounded history — the Azure automatic-tuning discipline,
https://learn.microsoft.com/en-us/azure/azure-sql/database/automatic-tuning-overview, accessed
2026-08-08). Channels ranked by trust, highest first:

| # | Channel | Signal | Mechanism | LLM? | Safety template | Tranche |
|---|---|---|---|---|---|---|
| C1 | Call signature | outputs, optimize_for, doc_type_hint, schema presence, compliance block | deterministic rules (stage-3 generalized) | no | advisory-ambient; never fires gates | A |
| C2 | Schema shape | field names, types, `required`, enum-ness | deterministic heuristics → default codes; a `required` money-typed `*total*` field defaults to `expected`, never `critical` — top-tier importance is never inferred | no | advisory defaults, printed in `strategy plan`/`explain`, overridden by any explicit `x-read-*` | A |
| C3 | Declared computation | the L3 compute block / imported profile | mechanical sensitivity analysis — derivation, not guessing; deterministic, testable | no | deterministic compile step, hashed; declaring is adopting | B |
| C4 | Downstream artifacts | caller-supplied SQL/dbt via sqlglot lineage run locally (https://sqlglot.com/sqlglot/lineage.html, accessed 2026-08-08); OpenLineage column facets with DIRECT/INDIRECT typing (https://openlineage.io/docs/spec/facets/dataset-facets/column_lineage_facet/, accessed 2026-08-08); spreadsheet formula DAGs | value-bearing (DIRECT/AGGREGATION) vs row-routing (INDIRECT/FILTER/JOIN) mechanically distinguished; coverage gaps degrade to "unknown", never "unimportant" (dbt CLL misses joins/filters; Meta's SCARF needed runtime logs to find ~50% more dead code, https://engineering.fb.com/2023/10/24/data-infrastructure/automating-dead-code-cleanup/, accessed 2026-08-08) | no (an LLM pass over code is a separately gated variant) | gated per artifact class + draft-then-edit; parsing local, nothing leaves | 2 |
| C5 | LLM draft | caller schema + ≤5 sample docs + optional steering prompt (LlamaExtract/Google-style caps, https://developers.llamaindex.ai/python/cloud/llamaextract/getting_started/, accessed 2026-08-08); precedent: GuideX auto-infers guidelines for up to +7 F1 on zero-shot NER over previous no-human-label methods — NER benchmarks, not document extraction (https://arxiv.org/abs/2506.00649, accessed 2026-08-08) | drafts profile/codes; every entry cites a quoted span or lineage edge (cite-or-die); LLM-derived entries marked lower-confidence than mechanical ones | yes — decider-armed | gated (decider arm) + draft-then-edit; never in `make verify` | post-decider |
| C6 | Usage telemetry | opt-in SDK field-access counts (Apollo/Hive schema-usage mechanic transplanted to REST, https://www.apollographql.com/docs/graphos/platform/insights/field-usage, accessed 2026-08-08) | proposes downgrades only, blast radius shown | no | gated; windowed; never downgrade on partial coverage | deferred |
| C7 | Corrections | which fields clients bother to validate/fix — revealed preference (Mindee's v1 feedback endpoint took "validated values for all the used fields"; UNVERIFIED-current, the docs page has moved); per-field correction-rate analytics (Rossum, https://knowledge-base.rossum.ai/docs/exploring-usage-reporting-dashboard, accessed 2026-08-08) | correction analytics → proposed weight updates; the RFC 9315 assurance loop — intent needs an outer loop that reports whether the outcome was met (https://www.rfc-editor.org/rfc/rfc9315.html, accessed 2026-08-08) | optional | gated + revertible; human-validated data only; Enterprise E3/E6 adjacency | deferred |

Hard rules across all channels (25 years of precedent — AutoAdmin → Azure auto-tuning →
Apollo/Hive → Meta SCARF):

- Negative inference outranks positive: "nobody consumed this" licenses `ignore` proposals;
  "this is read often" never licenses `critical` on its own.
- Distinguish observed-unused from unobservable; never downgrade a field whose consumption
  channel is not instrumented.
- Inferences are timestamped, windowed (30-day-class windows, never all history), keyed to
  artifact checksums, and expire on change.
- Every channel emits the §6 intent draft; adoption is the caller's act (Law 6).
- LLM channels are armed like the decider — config + env, never a wire field (Law 5).

v1 ships C1–C3 (C1–C2 in tranche A, C3 in tranche B).

---

## 10. The application map

Keyed to verified constructs (anchors re-checked 2026-08-09; re-verify at build). Ordered by
shipping order.

| # | Application | Attach point | What changes |
|---|---|---|---|
| A1 | Weighted scoring | `evals/scorers.py` (`score()`, `:129`; `field_prf`, `:39`); compare `--truth` calls it verbatim (`comparison/stances.py:113`); `calibrate.py` sweeps | optional weights → intent-weighted overall + critical-pass rate; unweighted always reported (§12). Inheritance mechanism, stated: weights ride *inside* the expected/truth dict under a reserved `field_weights` key (ignored when absent) — `truth_section` calls `score(response, truth)` with two arguments and keeps doing so, which is how compare `--truth` and `calibrate` inherit with zero code in `comparison/`; the eval/compare compile step embeds adopted intent weights into that dict (§12). Highest-leverage single change: propagates through compare law L5 for free |
| A2 | Strategy gate compilation | Plain desugar (`docs/strategies/spec.md` Plain section); `signals.py:380-428` | `critical:` word → `fields_required` + per-field `field_confidence_below` + `review_if` gray bands on critical fields only (§7); missing-signal law binds unchanged; `strategy validate`'s can-this-bind check applies |
| A3 | Escalation / review spend | `escalate_if` / `review_if` gates | one rule: **spend = criticality × uncertainty**. High-crit + low-confidence (or signal-absent) escalates; high-crit + high-confidence does not; low-crit never spends gray-band budget. Azure's threshold-calibration practice (provenance note in §7) compiled into existing grammar; ceilings never raised (Law 13); the request `review.mode` posture (§5) narrows the compiler's review emission (`none` emits no `review_if` gates), never widens it. Operator-ceiling caveat, stated because `limits:` binds strategies, not direct-named requests (`docs/strategies/spec.md` limits section): **intent-generated escalation/review spend is emitted only inside the strategy arm, where `limits:` reaches; on the direct-named path intent produces push-down, scoring, and warnings — never additional attempts** |
| A4 | Composite score + merge | `engine.py:820-828` `_branch_quality`; `pick: merge` `_vote_field` (`engine.py:1191`) | criticality-weighted predicate fraction when intent present (enters `config_hash`); merge: critical fields fan out to N branches, optional fields ride `merge_base` — criticality changes *which fields get multi-branch treatment*, not vote arithmetic. Dawid-Skene adapter-reliability weighting (https://aclanthology.org/N13-1132/, accessed 2026-08-08) is named-roadmap, Enterprise-gated |
| A5 | Router stage-3 only | `router/router.py:135-150` (`_score`) | intent-derived weights slot where `optimize_for` sits; reorder-only over stage-1/2 survivors; `doc_type_hint` finally consumed against descriptor `quality` grades (verified-only, §8) — the Law 1 named behavior change for existing callers already sending it (§2). Precedence with `optimize_for`, defined so B3 does not improvise it: `optimize_for` keeps owning the quality-vs-cost-vs-latency tradeoff axis (the `w_quality`/`w_cost` split); intent-derived weights modulate only the *quality term* (per-doc-type grades, per-field-kind fit) and never the cost weight or the axis balance. When both are present the composition is deterministic and the applied composition is recorded in the trace (and the echo where one exists, A8); the both-present case is in B3's test list (§14). Stages 1–2 untouched by construction |
| A6 | Adapter push-down | `submit(req, ctx)` / `normalize(job, req)` see the whole request (`adapters/base.py:33-45`) | criticality-ordered field selection under `max_fields_per_request` (chunking = importance ordering); descriptions/aliases/purpose to the backend's prompt/query slots per descriptor flags; `x-read-*` stripped before push-down; anything unhonorable → `intent_not_honored` warning (Canon C6 deliver-or-warn: never silent, never fabricated); `assert_supports` (`base.py:118`) stays the direct-named guardrail |
| A7 | Decider + judge | `DecisionPoint` (`decider.py:72-84`); `JudgePort.compare(a, b, intent)` (D-v3-17) | enriched intent flows as decision criteria under existing mask/downgrade/replay rules, with no type change — mechanism stated because the slots are typed: `purpose` prose rides the existing `DecisionPoint.intent` slot (`str \| None`, `decider.py:83`) and `JudgePort.compare(a, b, intent)` unchanged; the criticality map is structured data and rides `DecisionPoint.signals` (already `dict[str, Any]`) under a reserved `intent_criticality` key. Never masked values — the `mask_fields` discipline extends to intent payloads. Full flow: `intent-prompt.md` §4 |
| A8 | Response echo | `orchestration` (open object, `response.v0.3.json:627`, `additionalProperties: true`); OPEN warning-code set (`:580`) | `intent_hash`, applied code-map summary, generated gates, verified-vs-single-passed fields. Scope, resolved against the frozen schema: `response.v0.3` defines `orchestration` as "present only for strategy-engaged runs", and released schemas are byte-frozen — so the structured echo ships on strategy-engaged runs only in v1; on the direct/auto path (push-down A6, stage-3 B3) a response shaped by intent says so via the warning codes and the hashed trace, and a direct-path echo home is a named response.v0.4 candidate (Law 8, §13). Warning codes `intent_not_honored`, `intent_field_unverified`, `intent_field_absent`, `intent_all_critical` (top-level `warnings[]`, both paths). No response schema bump in v1; the typed per-field status enum and the direct-path echo are response.v0.4 candidates |
| A9 | Compare | `comparison/`; report family | weighted `--truth` scoring free via L5 (v1). Weighted report headline + finding-severity modulation: deferred, named (§13) — needs a report-family MINOR bump + its own honesty design (findings never deleted; the report echoes the intent that shaped it). Compare law L3 no-influence stands: compare output never feeds routing |
| A10 | Batch role binding | intake source forms (Manifest M1–M5); `RunOne` seam (`batch/runner.py:25`) | a real manifest file as a new intake source form: YAML/JSONL rows of `{source, doc_role, entity, tax_year?, intent_ref?}`; per-item intent binds profile terms to documents by role tag for L3 cross-document terms. Requires a batch-result family bump for the echo — which is why it is tranche 2. Today's batch layer is identity-only by design (`SourceRef` `extra="forbid"`), and this respects that: a new source form, not a widened `SourceRef` |

**What intent never touches:** router stages 1–2, `compliance` blocks, `on_error` classes,
fallback eligibility (`_apply_explicit_fallback` draws only from survivors,
`router.py:186-198`), the legacy no-strategy path, any budget/limit ceiling — and attempt
counts on the direct-named path: intent-generated spend exists only inside the strategy arm,
where the `limits:` operator ceiling reaches (A3).

---

## 11. The widening proof and the two CI tests

Law 2's four locks. Locks 1, 3, and 4 hold in the tree today; lock 2 is an honest
work-in-progress whose enforcement lands in tranche B:

1. **Structural.** Stages 1–2 are boolean functions of `compliance`, `features`, and descriptor
   fields; `intent` is not in their signature. The diff adding intent touches `_score` and the
   strategy compiler only — reviewable as a code property and pinned by the CI test below.
2. **Schema-mechanism.** The block ships `x-stability: experimental`. Today that exclusion is a
   convention with a registry, not enforcement: `experimental_fields()`
   (`schemas/__init__.py:78-105`) walks the *response* schema by default and is consumed only
   by the schema-evolution meta-test (`tests/test_schema_evolution.py:157`); no compliance code
   consults it, whatever its docstring hopes. B1 builds the lock (§5, §14): request-schema
   registry invocation, updated meta-test, and a test asserting compliance evaluation is
   invariant to every experimental-annotated request path. Until B1 lands, this lock carries no
   weight and the proof rests on locks 1, 3, and 4.
3. **Strategy-layer.** Executors consume only pruned trees — "execution is structurally
   incapable of widening eligibility" (`docs/strategies/integration.md`); intent compiles into
   gates *inside* the tree; the decider's action space never contains a dropped backend
   (`DecisionPoint.candidates` is built post-pruning, `decider.py:72-84`).
4. **Policy classification.** Intent fields are narrowing-or-neutral hints in the AG-2
   whitelist sense — the same class as `optimize_for` / `doc_type_hint`; the widening knobs
   (D7/D7a: `allow_unverified_compliance`, `train_optout_confirmed`, `baa_tier_confirmed`)
   remain RouterConfig/deployment-level with no intent-adjacent siblings.

**The two CI tests** (both offline, both in `make verify`):

- **Eligible-set equality.** For a matrix of requests (with/without compliance blocks, with
  direct-named and `auto` backends, with strategies engaged and not), the router's
  survivor set and `dropped` map are equal with and without every intent construct this design
  ships (`x-read-*` annotations, the `intent` block, the `critical:` word). The matrix also
  includes `doc_type_hint`-bearing requests — the Law 1 carve-out: their *eligible set* must be
  equal before and after B3, while the post-B3 stage-3 reorder is pinned as a documented
  expected difference, not silently exempted. Pins locks 1 and 4.
- **Degeneracy.** Uniform weights (and absent weights) reproduce today's unweighted metrics
  bit-for-bit — `score()`'s overall, compare `--truth` by_subject, `DatasetReport.mean_overall`.
  Pins Law 1 for the metric stack.

---

## 12. Metric and the offline eval plan

**The weighted score, defined once (Law 11), in `evals/scorers.py`.** Today's `field_prf`
(`scorers.py:39-53`) is micro-style per document: `precision = correct/len(got)`,
`recall = correct/len(expected)`, F1 their harmonic mean — the `got` denominator is what
penalizes over-prediction. The weighted metric therefore weights the **counts**, not the
per-field scores, and feeds the same P/R/F1 construction:

    weighted_correct  = Σ_{f ∈ expected} w_f · [f correct in got]
    weighted_expected = Σ_{f ∈ expected} w_f
    weighted_got      = Σ_{f ∈ got}      w_f
    precision = weighted_correct / weighted_got
    recall    = weighted_correct / weighted_expected
    F1        = harmonic mean

- The construction is scale-invariant in the weights (no Σ=1 normalization needed), and with
  all `w_f = 1.0` every product and sum is IEEE-exact, so it provably reduces to today's
  `field_prf` bit-for-bit — the degeneracy law is a theorem of the definition, not a hope. A
  naive `Σ_f w_f · F1_f` macro average was considered and rejected: it drops the got-side
  precision term and cannot reproduce today's aggregate, so it would fail AC-1 by
  construction.
- Fields present in `got` but absent from `expected` carry their declared weight when
  annotated, else the default code's weight — preserving the over-prediction penalty. Weights
  never change any per-field correctness judgment, which closes the gaming lane where a system
  farms score by over-predicting high-weight fields: those predictions inflate `weighted_got`
  and dilute weighted precision exactly as they do today.
- The enum→float mapping is published and versioned: `critical` 1.0, `expected` 0.3,
  `optional` 0.1, `ignore` 0.0; compute-block sensitivity weights (L3) override the enum
  mapping when present. The mapping is part of the metric's identity — changing it silently
  re-scores history — so it lives as a named, versioned constant table in `scorers.py`, echoed
  in every eval report (home question: §15.6).
- Weights originate in the declared intent object and are fixed before scoring — never a
  post-hoc knob (the metric-integrity analogue of p-hacking). Mechanically, the eval/compare
  compile step embeds adopted intent weights into the expected/truth dict under a reserved
  `field_weights` key (ignored when absent) — which is how `score(response, expected)` keeps
  its two-argument signature and compare's `--truth` stance and `calibrate` inherit weighting
  with zero code in `comparison/` (Law 11, A1).
- **Degeneracy law, pinned by test** (§11): uniform weights reproduce today's unweighted mean
  bit-for-bit.
- Every intent-eval run reports the triple: unweighted score (unchanged), intent-weighted
  score, and cost from a pinned static price table (offline-deterministic).
- **Critical-pass rate:** the fraction of documents with every `critical` field correct — the
  research-side analogue of straight-through processing that no public benchmark computes
  (confirmed absence, searched 2026-08-08). Wherever an automation/STP-style rate surfaces
  (strategy outcomes, batch summaries, evals), it is reported as the pair (automation rate,
  verified accuracy on a sampled subset) — never raw STP. The most sophisticated incumbent
  argues in public that raw STP is a vanity metric — 90% STP says nothing about the correctness
  of the 90% that skipped review
  (https://www.hyperscience.ai/blog/automation-at-all-costs-the-fallacy-of-business-value-with-straight-through-processing/,
  accessed 2026-08-08). Imported verbatim: confident wrongness is not a product.

**Three offline experiments — the test harness for the whole layer.** All fixture-based, zero
network, `make verify` culture. Shared falsifiable claim: *reallocating precision budget by
declared intent improves the outcome metric at equal or lower cost versus uniform treatment.*
The only public precedent is a notebook: OpenAI's receipt pipeline tolerated an 85%
merchant-name error rate because those errors did not move the audit decision, and spent its
effort on handwriting detection instead
(https://developers.openai.com/cookbook/examples/partners/eval_driven_system_design/receipt_inspection,
accessed 2026-08-08). E1–E3 turn that method into a harness.

- **E1 — CORD receipt total** (CC BY 4.0, https://github.com/clovaai/cord, accessed
  2026-08-08). Declared computation: total = Σ line prices + tax + service − discount (the
  `subtotal.*` / `total.*` subfields encode the terms; already in ground truth). Uniform
  cross-backend voting on all fields vs voting only on price-bearing fields at matched call
  counts. Outcome: fraction of receipts with exact computed total. **Falsified if** the
  cost/accuracy frontier never separates. Ships as fixtures in-tree (tranche A).
- **E2 — TaxCalcBench + Form 1084** (the flagship study, tranche B). Render TaxCalcBench's 51
  structured inputs (https://github.com/column-tax/tax-calc-bench, accessed 2026-08-08; exact
  license UNVERIFIED — re-check at build; scenario coverage also UNVERIFIED — verify at build
  that the 51 scenarios actually contain the self-employment inputs Form 1084 consumes,
  Schedule C net profit / depreciation / K-1 ordinary income / ownership %, else this leg is
  unbuildable as specified) into filled IRS PDFs (public-domain US government
  forms), extract, feed TaxCalcBench's own deterministic evaluator and the Form 1084
  qualifying-income formula (official PDF at
  https://singlefamily.fanniemae.com/media/7746/display 403s to non-browser fetches; content
  verified via Enact's reproduction, §4.1). Sensitivity-derived weights; verify top-k weighted
  fields vs uniform. Outcome: imputed-income dollar error vs verification cost; also produces
  the motivating number "X% of fields carry Y% of income-error variance." **Falsified if** the
  sensitivity distribution is flat or critical-only verification does not dominate the
  frontier.
- **E3 — DocILE invoice-approval** (code MIT; dataset gated, exact terms UNVERIFIED — re-check
  at build; https://github.com/rossumai/docile, accessed 2026-08-08). Deliberately crude binary
  critical/non-critical intent (no formula) over 55 field classes — proves the mechanism works
  with coarse caller-declared intent. Outcomes: weighted F1, simulated critical-pass rate, and
  unweighted micro-F1 as the **do-no-harm criterion** — **falsified if** intent buys weighted
  gains by materially tanking unweighted F1 (moving errors around is not spending budget
  better).

**Success metrics** (ProductSpec discipline — never fabricate; IDs per
[`intent.product-spec.md`](../product/specs/intent.product-spec.md)):

- SM-1 — adoption: share of active schema-bearing callers using ≥1 `x-read-*` keyword or the
  `intent` block. `target: tbd`, `target_status: provisional`, `target_owner: akshay`.
- SM-2 — frontier separation: E1/E2 outcome-error reduction at matched cost. `target: tbd`,
  `target_status: provisional`, `target_owner: akshay`.
- SM-3 — do-no-harm: E3 unweighted micro-F1 regression bound. `target: tbd`,
  `target_status: provisional`, `target_owner: akshay`.
- SM-4 — cost: reduction from `ignore`-tier fields on a reference corpus. `target: tbd`,
  `target_status: provisional`, `target_owner: akshay`.

---

## 13. Versioning

Per the Canon §8 discipline (released files byte-frozen; additive MINOR via file copy; every
family bump carries goldens, forward tolerance, and the non-additive-diff gate):

| Family | This design | Mechanics |
|---|---|---|
| request | **v0.1 → v0.2** (tranche B) | new file, v0.1 frozen; pin flip; pydantic mirror + round-trip; v0.1 goldens validate v0.2; `intent` block `x-stability: experimental`; CHANGELOG entry **carries the graduation criteria at birth** (§15.5) — the request schema's first experimental field sets the precedent |
| adapter-descriptor | **v0.4 → v0.5** (tranche A) | additive intent-consumption facts, limits, conflict pairs, `quality` grades (§8) |
| strategy-config | **v0.2 → v0.3** (tranche A) | the `critical:` criterion word; additive; config `version` const unchanged |
| response | **no bump in v1** | the structured echo rides `orchestration` (`additionalProperties: true`) — and therefore, per the frozen v0.3 description ("present only for strategy-engaged runs"), exists on strategy-engaged runs only; the four warning codes ride the OPEN warning-code set on both paths (§10 A8). **Named response.v0.4 candidates:** a direct-path echo home; the typed per-field status enum (explicit-unknown code points: absent-from-document / present-but-unreadable / not-requested); an `intent_underspecified` warning telling the caller their intent expression is defective (§15.9) |
| comparison-report | **deferred, named** | weighted report headline + finding-severity modulation need a MINOR bump and their own honesty design (Law 8/9: findings never deleted, intent echoed) |
| batch-result | **tranche 2, named** | the manifest source form + per-item intent echo (A10) |

---

## 14. Work breakdown

Ordered; each phase lands failing-tests-first and ends `make verify` green. AC IDs cite the
ProductSpec's acceptance criteria (`intent.product-spec.md`, spec_revision 1 — this numbering
is final): AC-1 degeneracy, AC-2 eligible-set equality, AC-3 strict optionality, AC-4 prose
inertness, AC-5 no-LLM degradation, AC-6 draft adoption, AC-7 hash + echo, AC-8 honest
weighting, AC-9 enum surface, AC-10 desugar goldens, AC-11 one metric stack, AC-12
strip-and-map conformance, AC-13 the spend rule, AC-14 stage-3 reorder-only, AC-15 compute
validation + deterministic derivation, AC-16 E1 offline. AC-3/AC-4/AC-5 are cross-phase law
tests: every phase adds evidence and they flip at tranche reconcile, not at any single phase.
The executing roster is `internal/design/intent-build-prompt.md` (its I0–I11 phases
map onto A1–A8/B1–B5; the correspondence is stated there).

**Tranche A — zero request-schema churn.**

- **A1 — Vocabulary + scorer.** The six `x-read-*` keywords documented and parsed out of
  `extraction_schema.json_schema` (no request-schema change — the object is open); weighted
  scorer + critical-pass rate + enum→float constant table in `evals/scorers.py`, weights
  carried via the reserved `expected.field_weights` key (§12); triple reporting; degeneracy
  test (AC-1); the published, versioned mapping (AC-9); weighted `--truth` and `calibrate`
  inheritance proven by test with zero code in `comparison/` (Law 11, AC-11).
- **A2 — The widening pin.** Eligible-set-equality CI test across the request matrix,
  including the `doc_type_hint` carve-out cases (AC-2). Lands before any consumer so every
  later phase runs against it.
- **A3 — Strategy compile.** Cut `strategy-config.v0.3.json`; the `critical:` word + desugar
  per §7; desugar goldens in the house `PLAIN_EXPANSIONS` pattern (AC-10); spend =
  criticality × uncertainty with ceilings untouched and spend emitted only inside the strategy
  arm (AC-13); trace + hash of compiled gates; `strategy validate` rows (unbindable,
  all-critical → `intent_all_critical`).
- **A4 — Composite + merge weighting.** `_branch_quality` criticality weighting; merge fan-out
  by criticality; both entering `config_hash` (Law 7; hash evidence feeds AC-7).
- **A5 — Descriptor v0.5 + push-down.** Additive descriptor cut (§8); adapter push-down for
  capable adapters (queries / descriptions / aliases), strip-and-map with conformance-kit
  check (AC-12), chunking under `max_fields_per_request`, deliver-or-warn
  (`intent_not_honored`).
- **A6 — Echo + warnings.** `orchestration` echo (`intent_hash`, code map, generated gates) on
  strategy-engaged runs + the four warning codes on both paths (§10 A8; AC-7, AC-8). No
  response schema bump.
- **A7 — Deterministic inference + draft flow.** C1–C2 advisory defaults printed in
  `strategy plan` / `explain`; the intent-draft format (§6) and the CLI draft/adopt flow
  (AC-6).
- **A8 — E1.** CORD receipt-total harness as in-tree fixtures, offline (AC-16).

**Tranche B — the request block.**

- **B1 — `request.v0.2`.** The `intent` block (purpose / profile / compute / review per §5,
  `review` with its narrowing-only mode semantics) with the full landing checklist (§13);
  load-time `terms[].ref` validation (AC-15, validation half); the Law 2 lock-2 enforcement
  (§11): extend the `experimental_fields()` registry invocation to the request schema, update
  its meta-test, and add a test asserting compliance evaluation is invariant to every
  experimental-annotated request path; CLI `--intent` / `--intent-file`; server pin; `run()`
  passthrough test. Closes AC-3's evidence (every construct individually optional, both
  carriers).
- **B2 — C3 sensitivity derivation.** Deterministic compile of compute/profile → weights
  (linear + Sobol paths), hashed; overrides the enum mapping in the scorer (AC-15, derivation
  half).
- **B3 — Router stage-3.** Intent-derived weights in `_score` under the A5 precedence rule
  (`optimize_for` owns the axis; intent modulates the quality term only — the both-present
  case is a named test); `doc_type_hint` consumed against `quality: verified` grades — the
  Law 1 named behavior change, landing with its documented-reorder carve-out test;
  reorder-only, re-run A2's equality test (AC-14, AC-2).
- **B4 — Profiles.** `fannie-1084@2024`, `freddie-91@2024` as importable artifacts + eval
  corpus — gated on the licensing check (§15.2).
- **B5 — E2/E3.** Run as studies; results feed the ProductSpec SM baselines and the
  experimental-graduation evidence.

**Tranche 2 and beyond (designed-for, deferred, named):** the batch manifest source form + L3
cross-document binding + batch-result bump (A10); the engine-side `compute_check` gate
(deterministic recalculation as a strategy signal — v1 executes compute in evals only); C4
artifact inference; C5 LLM drafts (waits for the decider milestone's armed wire adapter); C6/C7
telemetry and corrections (Enterprise adjacency; when C7 comes it updates routing/weighting,
never a model); weighted compare headline; per-doc-type `quality: verified` bootstrapping;
field-scoped re-extraction (escalation units remain document/page); conformal risk
certificates (the math is ready — CRC controls any bounded monotone loss, and a weighted
per-field loss qualifies, https://arxiv.org/abs/2208.02814, accessed 2026-08-08 — calibration
corpora are not yet held); Hyperscience-style live threshold recalibration; the `x-read-on-fail`
object form and the typed per-field status enum; Dawid-Skene vote weighting.

**Never, or not this layer:** a new intent DSL or file format; caller-facing float weights
(Law 12); LLM in `make verify` or request-field arming of any LLM channel; ambient inference;
confidence imputation or rewriting; compare→routing feedback in the community tier (L3
stands); a lending vertical product (`fannie-1084` ships as a profile and eval corpus, no
rep-and-warrant claims, ever); an intent console/GUI; a standardization campaign for `x-read-*`
(proposing an interchange standard before adoption evidence would be P3P cosplay);
vision-native retrieval indexes; intent-aware triage (waits for the paused agentic milestone;
AG-6 and zero-knobs determinism stand).

---

## 15. Open questions

1. **Milestone graph honesty.** Does the intent ProductSpec `depends_on` decider (its C5
   channel does) or `relates_to` it, given decider is designed-unbuilt? Build-before-
   distribution — the agentic pause's own question — applies with equal force; the spec states
   it plainly. Mitigation: tranche A is small, and E1–E3 produce publishable evidence either
   way.
2. **Profile transcription and licensing.** `fannie-1084@2024` must be hand-transcribed (the
   official PDF 403s; IRS MeF XML vocabulary is access-gated); the redistribution posture of a
   transcribed GSE worksheet needs a clean-room/licensing check before it ships as a vendored
   profile.
3. **Descriptor `quality: verified` bootstrapping.** Which corpora and how many eval runs
   before an adapter earns a per-doc-type `verified` grade — and do `claimed` grades get any
   stage-3 weight at all (current answer: no, but that starves the signal early).
4. **`x-read-*` under strict-schema backends.** The compiler strips `x-read-*` before push-down
   (OpenAI-style strict modes reject unknown keywords); confirm every adapter's schema-subset
   handling tolerates the strip-and-map pass (AC-12's conformance check is the mechanization).
5. **Experimental graduation.** `intent` is the request schema's first
   `x-stability: experimental` field; the graduation criteria (what evidence promotes it to
   stable) are written into the CHANGELOG entry at birth.
6. **Enum→float mapping home.** A named, versioned constant table in `scorers.py`, echoed in
   every eval report — whether it also belongs inside the intent object is unresolved.
7. **`purpose` prose consumer set.** Which adapters get `intent.purpose` compiled into their
   system-prompt slot — per-descriptor opt-in (a `field_descriptions_consumed`-style flag) or
   blanket for LLM-class backends. Owned by `internal/design/intent-prompt.md`.
8. **`x-read-on-fail` object form.** Does real usage demand distinct absent-vs-uncertain actions?
   Evidence bar before the additive value-grammar extension ships.
9. **Telling the caller their intent expression is defective.** The best find in the research
   pack that this design does not yet ship: Sensible's `ambiguous_query` is the only surveyed
   instance of a product telling the user *their intent was under-specified* in the API
   response, and Extend's schema-interpretation-issue category is a ready-made enum
   (`research/intent/landscape.md` §5, §8). An `intent_underspecified` warning with
   deterministic trigger conditions (candidates: alias collisions across fields, `required` +
   `x-read-criticality: ignore` contradictions, `x-read-tolerance` on non-numeric fields) is a
   named response.v0.4 / tranche-2 candidate (§13); the v1 warning-code set stays the four of
   §3. Decide the trigger set — deterministic lint-class checks only, never an LLM judgment —
   before it ships.
