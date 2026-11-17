---
spec_format_version: "0.1"
title: "Medical Chart Discovery: From Sample Records to an OpenReading Recipe"
artifact_type: "prd"
spec_revision: 1
status: "Draft for product review. Not built."
created_at: "2026-11-16"
updated_at: "2026-11-16"
repository_baseline: "openreading-core main at ea6d127"
proposed_product_owner: "A separate product above openreading-core; repository and name undecided"
applies_to:
  - component: "document-discovery-product"
  - path: "src/openreading/batch/"
  - path: "src/openreading/comparison/"
  - path: "src/openreading/strategies/"
  - path: "src/openreading/evals/"
  - path: "src/openreading/mcp_server/"
---

# Medical Chart Discovery

## 1. Product promise

You bring sample medical charts, your intended API output, and the processing boundaries your organization permits.
OpenReading helps you understand those records, test permitted configurations, and leave with a reproducible extraction recipe.
The recommendation explains its evidence, unresolved failures, operating requirements, and the additional validation needed before production use.

**Working positioning:** Turn unfamiliar documents into an extraction setup you can explain, reproduce, and improve.

Medical charts provide the first use case for a broader discovery product built above OpenReading.
The product hypothesis is that understanding your documents should precede choosing your parser or designing your API.
For example, discovering historical medication lists early prevents an API design that treats every medication mention as current.

This document proposes product behavior for review before implementation planning, including possible Claude or Codex plugin packaging.
It does not select a winning backend, report an experiment, or announce an existing product capability.
No patient records were provided or processed while preparing this specification, so every walkthrough below is illustrative.

## 2. User, problem, and buying hypothesis

The primary user is an AI engineer building an API that exposes information extracted from medical charts.
An electronic medical record or electronic health record is a clinical information system, commonly abbreviated EMR or EHR.
A medical chart is a record of documented care, often exported across multiple encounters and clinical contributors.
An encounter is a documented interaction with a care provider, such as an outpatient visit.

You understand data science and software delivery, but may lack the healthcare knowledge required to recognize extraction mistakes.
For example, a problem list, a suspected diagnosis, and a family history statement require different interpretations downstream.
Readable text alone does not explain which fields your API should expose or which relationships require preservation.

Today, you must investigate sample records, evaluate parsers, design an output contract, and organize domain review yourself.
The proposed product brings those activities into one guided workflow with inspectable intermediate results and explicit decisions.

The engineering lead is the likely buyer, with a privacy owner controlling processing permissions and a domain reviewer resolving clinical ambiguity.
These roles may share personnel, but technical access never substitutes for permission to process the records.
The buying hypothesis is reduced engineering investigation time, with fewer unrecognized semantic mistakes reaching API development.
Pricing, willingness to pay, and the need for a hosted product remain discovery questions for pilot customers.

## 3. Scope and boundaries

The first release helps you discover a workable configuration for a representative sample of at least twelve charts.
Each backend reads the formats its descriptor claims, as documented in [the adapter catalog](../../src/openreading/adapters/README.md).
A backend is a document-processing implementation invoked through OpenReading, such as a local library or a configured model endpoint.
A descriptor is the backend's machine-readable declaration of supported inputs, outputs, and execution capabilities within OpenReading.

The medical content explored can include encounters, conditions, symptoms, clinician notes, medications, and care plans.
Other requested concepts, such as allergies or observations, enter the output contract when your use case requires them.
These examples describe chart content, rather than a fixed list of document formats the product promises to support.

The MVP includes intake, constrained trials, an evidence-backed chart overview, draft extraction requirements, and an exportable recommendation bundle.
A small review worksheet supports the recommendation, while automated evaluation authoring remains a later product increment.
Optional model assistance can deepen interpretation when an approved endpoint exists within the permitted processing boundary.

The MVP excludes autonomous clinical decisions, treatment recommendations, production API hosting, direct EHR integrations, and automatic patient identity resolution.
It also excludes automatic certification, guaranteed clinical accuracy, and a universal healthcare schema inferred from twelve examples.
For example, a successful medication extraction trial does not authorize the resulting API to reconcile prescriptions for treatment.

## 4. What exists and what this product adds

This baseline describes the inspected repository revision, rather than treating earlier design proposals as shipped software.
Source paths identify implementation evidence for reviewers who need to verify the boundary before planning work.

| Area | Existing foundation | Proposed product addition |
|---|---|---|
| Intake | `openreading.run_batch` and `openreading.batch` process collections with per-item results. | Sample inventory, representativeness checks, private project state, and discovery checkpoints. |
| Parsing | Adapters provide normalized responses and backend-specific capabilities. | A bounded experiment plan selected for your records and permitted environment. |
| Comparison | `openreading.comparison` compares responses and corpora without establishing truth from agreement. | Explain disagreements against chart evidence and turn findings into review tasks. |
| Strategies | `openreading.strategies` supports explicit backend leaves and configured orchestration. | Generate and validate a small configuration shortlist with an explanation of each rule. |
| Evaluations | `openreading.evals` supplies datasets, scorers, runners, rules, and leaderboards. | Later: guided labels, clinical error checks, holdout management, and regression setup. |
| Agent tools | `openreading.mcp_server` exposes a bounded local document workflow. | A discovery session and host-specific integration, subject to verified data boundaries. |
| API integration | Python calls and a locally started `openreading serve` process expose core operations. | Export a runnable recipe and an integration example for the engineer's application. |

Important limitations influence the proposed recommendation workflow, even when an adapter can return valid structured output.
The `qwen-vl` adapter already supports schema-driven extraction through a configured endpoint in `src/openreading/adapters/qwen_vl/adapter.py`.
Its current execution processes pages individually, then updates a shared dictionary when combining extracted top-level fields.
Repeated keys from later pages can replace earlier values, so longitudinal medication or encounter extraction requires explicit testing.
The product must disclose this failure or exclude that configuration when the requested output depends on complete repeated records.

The `local-document-proof-v2` agent profile uses the parser in `src/openreading/adapters/docling_local/adapter.py`, which has specific output limitations.
For example, its current table-recognition setting and header handling require review when those regions carry medical context.
The separate `local-document-proof-v1` profile uses PyMuPDF, so the product must identify the exact profile involved in each trial.
General parse, compare, and strategy MCP tools remain unbuilt, as documented in `src/openreading/__init__.py`.
The proposed workflow therefore cannot assume the existing MCP profile already exposes arbitrary backend experimentation or configuration generation.

Strategy leaf `with` overrides and leaf `timeout` appear in accepted grammar but remain unimplemented, as documented in `src/openreading/strategies/model.py`.
Generated YAML validity therefore cannot prove that a requested page selection, extraction schema override, or leaf timeout takes effect.
The pilot must verify executed behavior, using separate supported calls or product-owned orchestration when a required override is unavailable.

The intent layer and a shipped `DeciderPort` wire executor also remain proposals, rather than dependencies the MVP can assume.
A first implementation can orchestrate explicit existing calls without waiting for either proposed subsystem to ship.

## 5. Experience from first sample to recommendation

### Step 1: Describe the API outcome

You describe what your application needs in ordinary language before defining a complete healthcare data model.
For example: "Return encounters, documented conditions, and medication mentions with dates and source references for an engineering review API."
The product asks targeted questions only when an answer changes extraction behavior or a processing boundary.

The initial brief captures requested concepts, required fields, error consequences, expected volume, latency needs, and available review expertise.
It distinguishes mandatory output from useful additions, such as encounter dates versus optional note section labels.
A proposed schema remains a draft until you adopt its definitions, examples, exclusions, and uncertainty behavior.
You can begin with a narrow target, then expand it after discovering what the records actually contain.

### Step 2: Establish the processing boundary

You select a processing environment and explicitly permitted backend deployments before document content enters the workflow.
The product records model endpoints, permitted destinations, retention choices, and the available experiment budget in the same project.
Credentials remain in approved secret mechanisms, while exported configuration contains references and setup instructions rather than secret values.

An installed SDK, an environment variable, or a reachable endpoint does not automatically authorize that deployment.
For example, an available cloud API key never expands a project configured for local processing only.

### Step 3: Provide and inspect sample records

You provide at least twelve representative charts through the approved environment, rather than automatically attaching them to a cloud chat.
The product inventories every file, identifies duplicates, records page counts where available, and reports unsupported or unreadable inputs.
It identifies observable variation such as layout, scan quality, length, repeated sections, and mixed extraction difficulty.
The inventory proposes additional samples when an important pattern appears missing, with the reason visible beside each request.

Twelve charts are an onboarding target for useful discovery, rather than statistical evidence of production reliability.
A smaller collection can produce an explicitly incomplete exploration, with the missing coverage recorded in its result.
For example, twelve exports from one template cannot establish behavior on another institution's export layout or scanning process.

The workflow reserves a confirmation subset before tuning, using trusted patient or source-group identifiers when available.
Exports from the same patient or duplicated source remain together, preventing related records from contaminating both subsets.
Without trustworthy grouping information, the report marks independence as unknown and limits claims about generalization accordingly.

### Step 4: Learn the chart structure

The first permitted baseline run creates an evidence-backed overview of sections, repeated structures, dates, and potential extraction difficulties.
An evidence reference identifies the source document and page or available text span supporting a displayed observation.
The overview links every interpretation to source content, with uncertainty visible when section boundaries or meanings remain ambiguous.

You learn which questions matter for your API through examples drawn from the approved local review surface.
For example, the product can highlight different dates attached to an encounter, an authored note, and the chart export.
An approved language model may propose interpretations, but those suggestions remain separate from observed text and adopted schema definitions.

Without an approved model, the workflow still inventories records, runs parsers, compares outputs, and presents a review worksheet.
It offers deterministic structural observations and user-supplied field definitions, while unresolved clinical interpretation remains assigned to a reviewer.
The reduced path can recommend a parsing setup without claiming to have validated a complete medical extraction API.

### Step 5: Run a bounded experiment

The product proposes a small candidate set based on the requested outputs, observed sample difficulties, and authorized deployments.
A candidate is a complete configuration, including preprocessing, parsing, optional semantic extraction, and output validation behavior.
A model-assisted suggestion can rank permitted candidates, but cannot authorize another destination or increase the experiment budget.

The default pilot compares one baseline with at most one challenger on selected discovery records before expanding execution.
Each proposed run shows the documents, page limits, potential retries, model calls, and applicable stop conditions.
An already approved experiment budget permits execution within those limits without repeatedly interrupting you for confirmation.

The workflow retains failures, partial results, unsupported requests, and unresolved warnings alongside successful candidate outputs for review.
It reuses saved results when compatible, then applies pure comparison without paying for the same extraction again.
Cache reuse requires matching source content, configuration, model identity, and relevant versions within the same authorized project boundary.

### Step 6: Inspect evidence and resolve important questions

You receive a focused review queue containing consequential disagreements and representative agreement cases, with each result beside its source evidence.
Sampling agreement cases matters because two parsers can reproduce the same mistake from an ambiguous chart section.
The product explains the downstream consequence, such as treating a historical medication mention as a current medication record.

You can resolve output requirements yourself, while a qualified domain reviewer resolves clinical meaning when your expertise is insufficient.
The product records who reviewed an item, the source evidence, the decision, and any remaining ambiguity.
When no reviewer is available, clinical claims remain unverified, keeping recipes provisional whenever their declared output scope requires those claims.

### Step 7: Export and replay the recommendation

You receive a primary recipe, any justified alternative, and a list of requirements the available candidates cannot yet meet.
The primary recommendation names its evidence scope, failure behavior, resource requirements, and the reasons it was selected.
It includes executable configuration wherever supported today, with additional product-owned components identified separately from core configuration keys.

You run the exported recipe in the approved environment against the reserved records without requiring the original conversation.
Reproduction means the same declared setup and checks, rather than an unsupported promise of identical stochastic model outputs.
Changes prompted by confirmation failures become a new revision, with the already inspected records no longer described as an untouched holdout.

## 6. Clinical meaning the product must preserve

The product teaches distinctions through evidence and counterexamples, rather than requiring you to master healthcare terminology before starting.
These requirements define reviewable extraction behavior without treating the software as an independent source of clinical truth.

| Dimension | Required behavior | Example review question |
|---|---|---|
| Subject and encounter | Preserve explicit attribution and isolate uncertain associations. | Does this statement concern the patient, a relative, or another encounter? |
| Conditions and symptoms | Retain source wording, assertion context, and documented status. | Is the finding affirmed, negated, suspected, historical, or attributed to family history? |
| Medications | Preserve dose, units, route, timing, source context, and available status separately. | Is this an order, administration record, reported use, or a discontinued medication? |
| Notes | Preserve author role, section context, dates, amendments, and source references where present. | Does a copied passage represent a new observation or repeated historical text? |
| Care plans | Separate proposed or conditional actions from documented completed actions. | Is follow-up recommended, scheduled, or recorded as completed? |
| Time | Keep encounter, authored, signed, effective, and export dates distinct when available. | Which date supports the event your API exposes? |
| Repeated records | Preserve occurrences and conflicting values before any explicit reconciliation rule. | Did a later page replace an earlier medication or encounter record? |
| Missingness | Distinguish unobserved, explicitly absent, unreadable, unsupported, and not requested. | Was an allergy section absent, or did it explicitly document no known allergies? |
| Evidence | Preserve source pointers and raw wording for normalized values. | Can a reviewer find the phrase supporting this structured field? |

Confidence, when available, remains the producing component's reported signal rather than a measured probability of clinical correctness.
Missing confidence stays missing, while unsupported citations or geometry are reported as unavailable rather than reconstructed without evidence.
Ambiguous abbreviations remain verbatim until an adopted interpretation has sufficient support within the declared use case.

Fast Healthcare Interoperability Resources, abbreviated FHIR, is an HL7 standard for representing and exchanging healthcare information.
Its condition model distinguishes clinical status from verification status, while its medication resources distinguish reported use from other medication events.
Those distinctions inform these proposed review requirements, without establishing that a generated JSON schema is FHIR-conformant.
See the versioned [HL7 condition reference](https://hl7.org/fhir/R4/condition.html) and [HL7 medication statement reference](https://www.hl7.org/fhir/R4/medicationstatement.html).

FHIR export, terminology mapping, and vocabulary licensing are separate future decisions requiring named versions and explicit validation requirements.
For example, an extracted condition phrase does not automatically receive a diagnostic code inferred from a model's memory.

## 7. Processing constraints and data boundaries

The product treats processing permission as an operator-owned deployment constraint, rather than a medical fact inferred from document content.
The following environment choices describe proposed product behavior, rather than existing OpenReading policy keys or compliance guarantees.

| Environment | Intended boundary | Required product behavior |
|---|---|---|
| Local only | The approved machine contains document processing and content-aware model execution. | Block external content transmission, including agent context, telemetry, fallback calls, and crash payloads. |
| Private infrastructure | Named organization-controlled services may process content within an approved network boundary. | Restrict destinations and credentials to those deployments, with explicit retention and access controls. |
| Approved external services | Individually authorized third-party deployments may receive specified data. | Record the approval scope and enforce destination restrictions throughout trials and exported execution. |

A self-hosted model on a remote organization-controlled service belongs to private infrastructure, rather than automatically qualifying as local only.
The product records the actual destination and control boundary instead of deciding permission from an adapter name.
Installation and model downloads occur before sensitive execution, or through a separately authorized setup process without chart content.

Every dispatch must satisfy the intersection of project permission, deployment enforcement, and caller authorization where applicable.
That requirement covers explicitly selected backends, strategy leaves, retries, fallback paths, model judges, and later replayed recipes.
An empty eligible set produces a clear blocked result with remediation, rather than an automatic expansion to cloud services.

Current `policy.backends` supplies a default chain, while explicitly named backends and strategy leaves can run outside that chain.
Server API-key scope provides a caller authorization boundary, as described in `src/openreading/config.py` and `src/openreading/router/__init__.py`.
The product therefore needs application enforcement plus operator-controlled process and network restrictions for its stronger local-only promise.
A configuration file or restricted tool description cannot establish that promise when its host can independently read and transmit files.

The same boundary applies to original charts, extracted passages, filenames, screenshots, prompts, caches, reports, logs, backups, and evaluation labels.
Document content remains untrusted input, so embedded instructions cannot change destinations, budgets, credentials, or the extraction objective.
Reports exported for external collaboration require deliberate sanitization and approval under the project's sharing rules before leaving its environment.
Removing names alone does not establish that a report can safely leave the approved processing boundary.

Retention settings identify what persists, its location, access permissions, and the mechanism responsible for eventual deletion.
The product verifies its own cleanup scope and reports external backup or provider retention as operator-managed when applicable.
It must not claim that core already supplies encryption, automatic retention enforcement, or verified vendor compliance metadata.

HIPAA is a United States health-information privacy and security framework whose applicability depends on the parties and their activities.
A business associate agreement, abbreviated BAA, defines permitted handling and obligations for relevant service-provider relationships under that framework.
HHS guidance permits cloud processing under applicable agreements and other HIPAA requirements, rather than banning all cloud processing.
Local execution alone also cannot establish the organization's compliance with those requirements for a particular workflow.
These facts support operator-owned approval, rather than an OpenReading vendor-certification table. See [HHS cloud guidance](https://www.hhs.gov/hipaa/for-professionals/special-topics/health-information-technology/cloud-computing/index.html).

## 8. Recommendation logic and cost discipline

The product prefers the lowest measured operating cost among qualifying tested candidates when their costs are comparable.
Otherwise, it reports measured resource tradeoffs and unknown costs without asserting that one configuration is the cheapest.
When reviewed labels are unavailable, it reports suitability observations and unresolved questions instead of claiming a clinical accuracy ranking.
A valid JSON response establishes structural validity, while cross-backend agreement establishes consistency between outputs rather than correctness.

Selection follows a reviewable order: enforce permissions, verify required capabilities, assess observed failures, then compare resource use among viable candidates.
For example, a cheaper parser cannot win by silently omitting a medication table required by the adopted output contract.
If every candidate fails a mandatory requirement, the correct recommendation is that no tested recipe currently qualifies.

### Separate exploration cost from operating cost

Discovery may run several configurations over a bounded sample, even when the exported recipe uses one extraction path per document.
One path can contain a parser plus a semantic extraction stage, with both stages counted in its operating cost.
It can also use one integrated model backend when that configuration satisfies the adopted requirements and resource limits.
The report counts actual calls and processed pages, including internal per-page model calls, retries, and failed attempts where observable.

For a simple planning estimate, one baseline over twelve charts plus a challenger over four charts means sixteen document-level attempts.
That arithmetic excludes retries and internal model calls, so the estimate must disclose those additional sources of work.
Every run specifies caps for documents, processed pages, concurrency, elapsed time, retries, and model usage where measurable.
The scheduler reserves capacity before dispatch, with adapter-specific limits or conservative estimates covering work hidden inside a call.

Money estimates use operator-supplied, dated rates and measured resource counts when both are available for the chosen deployment.
Unknown monetary cost remains unknown, while local compute and review time remain visible rather than being described as free.
A hard monetary ceiling requires a trustworthy upper bound or enforcement mechanism before dispatch, otherwise that run is blocked.
Token or provider usage that arrives after completion cannot retroactively make a preflight spending guarantee enforceable.

### Selective additional extraction

A second extraction pass is an additional attempt to read or extract content already addressed by the primary path.
Alternative recipes may use selective additional passes when reviewed evidence shows a specific gap worth their additional cost.
For example, difficult medication tables could receive another extraction attempt while ordinary narrative pages retain the primary path.

Every additional pass requires a deterministic trigger, a permitted destination, a maximum scope, and a rule for handling conflicting outputs.
The exported recipe must name whether existing core strategy grammar supports that rule or additional product code is required.
The MVP defaults to one extraction path, while general field-specific multipass orchestration remains outside its initial implementation scope.
Cheap deterministic validation and comparison of saved outputs do not count as another extraction pass in the report.

## 9. The recommendation bundle

The deliverable is a portable project bundle, with a short recommendation report as its human-readable entry point.
Its filenames below are proposed product artifacts, rather than new OpenReading CLI commands or configuration schema fields.
Runnable recipe artifacts are conditional on finding a candidate, while blocked projects still receive an inventory and actionable findings.

| Proposed artifact | Required contents |
|---|---|
| `recommendation.md` | Primary recipe, alternatives, evidence scope, limitations, review state, and reasons for selection. |
| `discovery-manifest.json` | Input hashes, sample groups, discovery/confirmation assignments, versions, run identifiers, and effective constraints. |
| `openreading.yaml` | Configuration validated against the installed core version, with explicit backend choices and supported strategy rules. |
| `extraction.schema.json` | Draft or adopted application requirements, with review state, field definitions, missingness rules, and evidence requirements. Included for semantic extraction scope. |
| `recipe.json` | Product-owned stages, endpoint references, budgets, enforcement prerequisites, and dependencies beyond core configuration. |
| `run_recipe.py` | A runnable integration example with configuration checks, partial-result handling, and clear application ownership boundaries. |
| `setup.md` | Hardware and software prerequisites, endpoint setup, credential variable names, private storage paths, and replay instructions. |
| `review-items.json` | Evidence-linked questions, human decisions, unresolved failures, and representative examples requiring later evaluation. |

Medical field definitions belong to the proposed application contract, while core responses continue to use the vendored OpenReading schemas.
When semantic extraction is in scope, every transformation into the adopted application contract is explicit, versioned, and included in the recipe.
For parsing-only scope, the bundle contains reviewed application requirements and unresolved semantic tasks without promising an implemented clinical output transformation.
Secret values and patient content never enter a shareable bundle by default, including through filenames or embedded examples.
Private evidence can remain in a separate approved workspace referenced by access-controlled identifiers within the local bundle.

Three review states describe recommendations, with the reason and evidence scope visible in every report and export.
`provisional` means a candidate has unresolved checks or coverage gaps, without a known failure of a mandatory requirement in its declared scope.
`qualified_for_pilot` means the declared checks passed on reviewed discovery and confirmation records within the stated scope.
`no_qualifying_recipe` means every tested candidate has a known mandatory failure, so the product cannot export a recommended primary recipe.
None of these states means production certification, and each downstream team owns its separate release acceptance decision.

Execution status is tracked separately as `blocked_before_trial`, `interrupted`, or `completed`, with reasons and next actions attached.
Recommendation status remains unset when no candidate was tested, including projects blocked by missing authorization or unenforceable budgets.
Blocked or unsuccessful projects export their manifest, findings, and remediation steps without fabricating runnable configuration or a primary recipe.

### Illustrative recommendation, without experimental results

> Start with one approved extraction path over the observed export layout, using the locally validated configuration included in this bundle.
> Keep documented medication mentions separate from inferred current medication status until the requested distinction passes domain review.
> Require encounter attribution and source references for the fields your application exposes through its API.
> Send incomplete or conflicting records to review through the included failure handling, without expanding processing destinations.
> The recipe remains provisional until the reserved records pass the adopted checks and unresolved clinical questions receive review.

The actual product must replace those generic statements with tested configuration identifiers, observed counts, limitations, and concrete evidence pointers.
It must never invent measured accuracy, operating cost, or a winning backend to make an incomplete exploration appear finished.

## 10. Evaluations as the next offering

The future evaluation assistant turns sample records, adopted definitions, and reviewer corrections into a maintained evaluation project.
An evaluation harness runs a configuration against labeled examples, then measures specified outcomes with reproducible scoring rules.
A label is a reviewed expected result tied to source evidence, rather than a model-generated answer accepted without review.

The initial discovery product exports review material and compatible starter cases only where existing scorer semantics match the intended assertion.
Automated authoring, adjudication workflows, custom clinical checks, and sustained regression management belong to a later product increment.
For example, core field matching may help test a date, but does not automatically validate longitudinal medication interpretation.

The later assistant should support the following connected workflow:

1. Propose cases covering observed layouts, rare structures, negative assertions, missing sections, partial results, and consequential ambiguities.
2. Draft expected outputs with evidence, then require appropriate human review before those outputs become accepted ground truth.
3. Preserve patient and source-group separation between development, validation, and genuinely untouched final evaluation material where feasible.
4. Measure extraction completeness, unsupported assertions, temporal interpretation, subject attribution, source support, and the cost of human review.
5. Report denominators, unsupported checks, reviewed coverage, and uncertainty beside aggregate scores, preventing a small sample from implying broad reliability.
6. Compare versioned recipes at matched resource budgets, then retain failures as regression cases after their labels receive review.

Model judges can suggest review priorities, but agreement with another model cannot independently establish clinical correctness.
Cases used to tune a schema, prompt, or configuration cannot simultaneously serve as evidence from an untouched final test set.
Real medical records, reviewed labels, and domain tuning remain in customer-controlled storage or the private company repository as appropriate.
The public core continues to carry reusable mechanisms and synthetic fixtures, rather than private clinical evaluation corpora.

## 11. Product packaging and ownership

The recommended packaging hypothesis is a separate discovery application above core, with medical charts as its first guided workflow.
Core remains responsible for reusable contracts, adapters, orchestration mechanisms, evidence access, and generic evaluation execution used by many domains.
The separate product owns guided intake, medical teaching examples, recommendation assembly, domain review workflows, and customer project lifecycle management.
For example, a generic missing-evidence check may belong in core, while a tuned medication-review prompt belongs above it.

A Claude or Codex integration can provide conversational setup, progress reporting, recipe review, and safe project commands.
The Model Context Protocol, abbreviated MCP, is a protocol through which an agent can invoke external tools and receive their results.
MCP transport does not establish where the calling model executes or what information its host stores afterward.
Current artifact documentation already notes that retrieval shares document text with the calling agent in `src/openreading/artifacts/__init__.py`.

The content-processing component must therefore remain inside the approved boundary, regardless of how the conversational interface is packaged.
A cloud-hosted assistant can manage deliberately sanitized setup information only when its file access and tool outputs respect that boundary.
If the host can read raw charts or receive unsanitized results, the deployment cannot claim local-only operation.
An entirely local user interface or approved private assistant is required when host isolation cannot establish the intended boundary.

Plugin selection follows verification of host behavior, permissions, context retention, and output controls for the actual deployment version.
This specification makes no claim that a current Claude or Codex plugin already satisfies those requirements for medical records.
The exported recipe must remain usable without a specific assistant, preserving your ability to operate it from your own application.

## 12. Delivery slices and acceptance criteria

### Slice A: A narrow discovery pilot

Start with one agreed output objective and a representative chart sample inside a single approved processing environment.
Use existing OpenReading operations for bounded trials, with guided human review and a recommendation bundle as the completion artifact.
The baseline scope is a parsing recipe plus reviewed application requirements, with unresolved medical interpretation clearly assigned for further work.
Semantic extraction is an optional pilot extension selected at intake, with qualification limited to the explicitly declared output scope.
An approved model is optional, while the no-model path must clearly identify which semantic questions still require human interpretation.
An engineer-operated pilot is acceptable before a polished plugin, provided the output remains reproducible outside the assisting conversation.

| ID | Acceptance criterion |
|---|---|
| AC-01 | A project inventories at least twelve charts, or explicitly records insufficient coverage, with every input accounted for. |
| AC-02 | Missing authorization or an empty permitted set blocks dispatch without fallback to an unapproved deployment. |
| AC-03 | Local-only claims require recorded host, process, and network controls that enforce the boundary. Synthetic canary tests attempt prohibited transmission through retries, logs, model calls, and host access. Missing enforcement blocks the claim. |
| AC-04 | Without a configured LLM, intake, parsing, comparison, and a parsing recommendation still complete with semantic gaps visible. |
| AC-05 | Every trial records effective configuration, source identity, completion state, available usage, and warnings without inventing missing measurements. |
| AC-06 | Every proposed clinical field includes a definition, source-evidence requirement, and behavior for missing, conflicting, or ambiguous values. |
| AC-07 | Synthetic edge cases preserve negation, family attribution, medication status, repeated records, and date distinctions within the declared output scope, or explicitly fail qualification. |
| AC-08 | Reports distinguish comparison agreement from reviewed correctness, with review coverage and sample limitations visible beside conclusions. |
| AC-09 | Budgets cover retries and internal model work where applicable, with an explicit stop or blocked result when bounds cannot be enforced. |
| AC-10 | Every input has a terminal outcome or a recoverable interrupted state, and resume does not silently repeat completed compatible work. |
| AC-11 | When exporting a provisional or qualified recipe, configuration validates against its pinned version and execution proves requested settings take effect. A second engineer runs it without the original chat. Otherwise, export findings and next actions. |
| AC-12 | Confirmation records follow declared grouping rules, with failures retained and any subsequent tuning disclosed as a new revision. |
| AC-13 | Recommendations use the declared review states, including `no_qualifying_recipe` when all candidates fail mandatory requirements. |
| AC-14 | Multi-page extraction checks detect repeated-field loss, including the inspected Qwen-VL aggregation limitation, before relevant recipes qualify. |

Checks above verify product behavior, rather than establishing numerical clinical release thresholds for every possible customer application.
The pilot defines critical fields and failure consequences with the domain reviewer before measuring an extraction candidate against them.
Unresolved critical semantic errors within the declared output scope prevent pilot qualification, even when aggregate text or field scores appear strong.
Parsing-only qualification requires content-preservation checks and visible interpretation gaps, without claiming that those unresolved clinical meanings have been validated.

### Slice B: An assisted evaluation project

Add reviewed label authoring, clinically relevant checks, held-out evaluation management, version comparisons, and repeatable regression execution over customer-owned material.
Acceptance requires separating generated draft labels from accepted ground truth, with all clinical claims traceable to reviewed cases.
This increment reuses the existing harness where appropriate and proposes generic extensions only when their contracts are clearly defined.

### Slice C: Broader configuration exploration

Consider selective multipass recommendations, document-pattern routing, additional domains, and plugin distribution after pilot evidence identifies repeatable customer value.
Each expansion needs measured benefit over the simplest qualifying recipe, with its additional processing and review costs disclosed.

## 13. Success measures and falsification

The primary product outcome is a reproducible recipe the engineer understands well enough to integrate into the intended application.
Measure time from an authorized sample to that recipe, separating engineering effort, reviewer effort, and machine processing time.
Also measure independent replay success, accepted schema revisions, unresolved critical questions, and the fraction of projects receiving no qualifying recommendation.

Pilot participants should explain important chart distinctions using the report, rather than trusting an unexplained backend ranking.
For example, ask them to explain why a medication mention requires additional context before becoming a current-medication field.
Compare the guided workflow with each participant's normal discovery process on a matched task where practical, recording prior domain familiarity.

The central hypothesis weakens if engineers already know their schema and only need a faster document parser.
It also weakens if review effort exceeds the investigation saved, or recommendations cannot outperform a simple permitted baseline in useful decisions.
Permission violations and unsupported claims of correctness are release failures, rather than tradeoffs averaged into a product success score.
Numeric adoption and time-saving targets remain unset until pilot baselines exist, preventing invented metrics from becoming apparent evidence.

## 14. Decisions for the next review

The following questions refine scope before implementation planning, without blocking this document's use as a first review draft.

1. Choose the first API objective narrowly, such as encounter-linked medication mentions, before promising a complete longitudinal medical record.
2. Identify the domain reviewer and the party authorized to approve processing boundaries for the first pilot sample.
3. Select the initial environment and available hardware, including whether an approved self-hosted model endpoint already exists.
4. Choose any optional semantic extraction target for the first pilot, beyond its baseline parsing recipe and reviewed application requirements.
5. Set pilot budgets and mandatory acceptance checks, including the failure policy for missing evidence and unresolved clinical ambiguity.
6. Choose an initial review interface that preserves the approved boundary, then evaluate Claude or Codex packaging against that requirement.
7. Decide which bundle contracts need generic core support and which belong entirely in the separate discovery product.
8. Establish whether pilot users value evaluation setup enough to prioritize Slice B before broader configuration exploration.

## 15. Review and document lifecycle

Review this draft first for user value, semantic integrity, permission enforcement, actionable outputs, and a narrow first implementation slice.
Preserve the distinction between inspected capabilities, proposed behavior, illustrative examples, and future offerings throughout subsequent revisions.
No existing CLI flag, strategy key, medical quality guarantee, or compliance feature should be invented to make the proposal appear implementable today.

This product-intent record lives here for review under the repository's exception for work that has not shipped.
Implementation belongs in the appropriate repository according to the core and separate-product ownership boundary described above.
When implementation ships, move durable facts into its owning code documentation and remove this proposal in the corresponding lifecycle change.

Repository references were checked at `ea6d127`, while the linked HHS and versioned HL7 references were consulted on 2026-11-16.
The public homepage could not be retrieved through the research tool, so current capability claims rely on inspected repository code.
