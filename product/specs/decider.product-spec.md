> **Migrated from the private company repo on 2026-09-05, verbatim.** This record describes work
> `openreading-core` has not shipped. AGENTS.md keeps an unbuilt design record here, next to the
> code it proposes to change, so it is reviewed in the open. It was written before the monorepo
> split and predates protocol v2, adapter-descriptor v0.7 and the current `.env.example` posture,
> so verify every fact against this repository's code before implementing from it. Delete this
> file in the pull request that finishes the work, moving its durable facts into the module
> docstrings.

---
spec_format_version: "0.1"
title: "In-Run Decider"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-08-03T00:00:00Z"
updated_at: "2026-08-03T00:00:00Z"
applies_to:
  - path: "src/openreading/strategies/executor.py"
---

## Problem

The orchestration layer already has three places where a judgment call decides what happens
next — whether a borderline result is good enough, which branch to take, which of two outputs is
better. All the safety around those calls is built and tested: the eligible choices are
enumerated by the engine, compliance is invisible and cannot be overridden, every outcome is
traced, and any failure falls back to a deterministic default.

Nobody is sitting in the seat. The judgment is a protocol with no implementation, so on every
shipped surface every one of those decision points falls back to the engine default. The
fallback is recorded honestly — a finished run's trace says the seat was empty and why — but
only after the fact, in a per-decision field of a run that has already happened. An operator who
configured a decider and set the gate cannot ask, before spending anything, whether in-run
judgment is actually live.

## Hypothesis

If a model makes those in-run calls inside the existing rails, escalate-or-accept judgments get
measurably better than a threshold comparison can manage — because the borderline cases are
exactly the ones a fixed rule handles worst. If that holds, the spend is worth it; if the model
merely agrees with the default, the seat should stay empty.

## Product Summary

A shipped implementation behind the existing decision ports, calling a small fast model, turned
on only by an explicit operator switch that upgrading never flips. Before anything is armed, an
operator can see whether it would be armed and why not. Every call it makes reports what it cost,
labels that number as an estimate rather than a bill, and records the tokens and prompt identity
behind it. Every way it can fail — unavailable, timed out, refused, unusable answer — lands the
run on the deterministic default with the reason recorded.

## Scope

```productspec-scope
in:
  - Ship a working implementation behind the existing decision ports in this version.
  - Require an explicit operator switch before anything is constructed, on top of the two gates that exist today.
  - Let an operator see the arming posture, and the reason for a dormant one, without any provider call.
  - Record cost, token counts, and prompt identity on every decision the model makes.
  - Make a judgment that times out recoverable and honestly labelled at every decision point, including the one where a timeout currently fails the whole run.
out:
  - Do not wire the flag that would send document content to the model in this version.
  - Do not add a second model vendor in this version; the port stays the extension point.
  - Do not fold the explicit switch back into the existing gate in this version.
cut:
  - Cut spending ceilings enforced by the engine; the product reports what a run cost and never caps it.
```

## User Experience

An operator who wants in-run judgment sets one additional environment switch, and the readiness
view changes from dormant to armed, naming the backend and model it would use. A run that hits a
decision point then shows, in its trace, that a model chose rather than the engine — with the
cost, the token counts, and the prompt identity attached. An operator who does nothing sees no
change at all, and no charge.

## Acceptance Criteria

```productspec-acceptance-criteria
- id: AC-1
  criterion: Upgrading to this version spends nothing; a deployment that already has a decider configured, the existing gate set, and a provider key present keeps taking engine defaults until an operator sets the new switch.
- id: AC-2
  criterion: An operator can see whether in-run judgment is armed or dormant, and the reason it is dormant, without any request being sent to a provider.
- id: AC-3
  criterion: Every decision the model makes records what it cost, labels that cost as an estimate rather than a bill, and carries the token counts and an identifier for the prompt that produced it.
- id: AC-4
  criterion: When the model is unavailable, times out, refuses, or answers unusably, the run completes on the deterministic default and the trace names which of those happened.
- id: AC-5
  criterion: A backend that compliance rules exclude is never consulted for a judgment, and the run records the exclusion as the reason the model was not used.
```

## Success Metrics

```productspec-success-metrics
- id: SM-1
  metric: decisions_diverging_from_engine_default
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: first 30 days after any deployment arms the switch
- id: SM-2
  metric: escalation_accuracy_delta_versus_engine_default
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: first labelled corpus evaluation after arming, once such a corpus exists; see Open Questions
- id: SM-3
  metric: decider_cost_per_document
  target: tbd
  target_status: provisional
  target_owner: Akshay
  window: first 30 days after any deployment arms the switch
```

## Risks

This is the only part of the product that can spend money on a model, and the failure mode that
matters most is an upgrade that starts spending without anyone choosing it — which is why the
switch is explicit and separate from the gates that already exist, and why arming posture is
visible before any call. A model outage must never turn into a failed document, so every failure
path degrades to the deterministic engine rather than raising.

The strategic risk is that the bet is unfalsifiable as currently framed: without a labelled
corpus there is no way to show the model beats the default, and a decider that merely agrees with
the engine costs money to change nothing.

## Open Questions

- **What evaluation would actually test the hypothesis?** No AI-eval gate is written into the
  acceptance criteria above, because real cases, an evaluator, and a threshold do not exist
  yet — and a placeholder gate with an invented threshold would be worse than none. Decide what
  the eval is: which borderline documents, judged by what, at what passing bar.
- **Is there a corpus to measure against?** SM-2 assumes a labelled set where the right
  escalate-or-accept answer is known. Decide whether to build one, borrow one, or accept that the
  bet ships on judgment rather than measurement.
- **Should this ship at all before the surfaces that would use it are adopted?** In-run judgment
  is worth paying for only when strategies are running at volume. Decide whether this waits on
  evidence of that volume.

## Related Artifacts

```productspec-related-artifacts
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Decider executor design — the milestone specification"
- type: engineering_spec
  url: "docs/design/decider-executor-prompt.md"
  title: "Decider build-loop prompt — phase-by-phase execution"
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Design invariant DE-1 — no upgrade-triggered spend"
  section_id: acceptance_criteria
  item_id: AC-1
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Design invariant DE-2 — observability before spend"
  section_id: acceptance_criteria
  item_id: AC-2
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Design invariant DE-3 — honest accounting"
  section_id: acceptance_criteria
  item_id: AC-3
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Design section 5 — soft degradation on every failure path"
  section_id: acceptance_criteria
  item_id: AC-4
- type: engineering_spec
  url: "docs/design/decider-executor.md"
  title: "Design section 0 and test layer DT3 — compliance-excluded backends are never consulted"
  section_id: acceptance_criteria
  item_id: AC-5
- type: product_spec
  product_spec_path: "./agentic.product-spec.md"
  product_spec_revision: 1
  relation: depends_on
  title: "Agent Surface — ships first"
```
