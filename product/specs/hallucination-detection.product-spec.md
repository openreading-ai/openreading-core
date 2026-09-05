---
spec_format_version: "0.1"
title: "Hallucination Detection on Your Own Documents"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-09-05T00:00:00Z"
updated_at: "2026-09-05T00:00:00Z"
applies_to:
  - path: "src/openreading/evals/"
  - path: "src/openreading/cli/"
  - component: "evals-scoring"
---

## Problem

Four of the fifteen backends in the catalog are language models. They write text rather than read
it, so they can return a total nobody printed, a table row nobody typed, or a paragraph that reads
correctly and describes a different document. That failure is invisible to every scorer this
repository shipped before increment one below.

The guide demonstrates it against its own sample. Take a correct response, add an invented total, a
fabricated table row and four hundred words of filler, and score it again. It still scores 1.0.
Four of the five dimensions ask whether the content you expected is PRESENT, and a document with
extra content still contains everything you asked for.

That makes a leaderboard actively misleading in the one comparison where the stakes are highest. A
model backend that hallucinates confidently outranks a deterministic parser that quietly omits a
field, and the number says the model is better. The people most exposed are the ones evaluating an
expensive hosted model against a free local library, which is the decision this project exists to
inform.

The substitutes are each wrong for this need. A public benchmark's scorer does detect invented
content, but only over the publisher's own corpus, and a public corpus cannot tell you what a
backend does to YOUR mail. Writing an absence scorer natively is possible and was priced (see
Alternatives), but it reproduces measurement logic another project maintains and tests, and a
disagreement between our copy and theirs would be invisible. Reading every response by hand does
not scale past the first afternoon.

## Hypothesis

If a labeled case can carry assertions that FAIL when content is added, scored by an engine the
user did not write, then teams will trust a leaderboard enough to act on it for model backends,
because the number can now go down for the specific reason those backends fail.

The falsifiable part: if teams evaluate model backends and never write an absence assertion, then
the friction of authoring one is higher than the fear of hallucination, and the increments below
are the wrong shape.

## Product Summary

A case's `expected` may carry `rules`: assertions written in ParseBench's published vocabulary and
scored by ParseBench's own engine over the document your backend produced. `absent` fails when a
string appears that should not. `present` asserts a string. `table` asserts a cell together with
its right neighbour and its column heading.

Rules are a dimension inside the existing scorer, not a second harness. `leaderboard`, `calibrate`
and a plain dataset run reach them through the same `run_case` they already use, so two graders can
never disagree about one document.

`openreading rules` generates a starting set from the labels a case already carries, so nobody
hand-authors another company's JSON to begin.

## Scope

**Increment 1, shipped on `feat/benchmark-own-documents`.**
- `expected.rules` scored by the publisher's engine, as one named dimension `rule_pass_rate`.
- `openreading rules [--write] [--force]` generating `present` from `text_contains` and `table`
  from `tables`.
- `expected.text_absent`, a plain list of strings that must not appear, generating `absent` rules.

**Increment 2, not built.**
- Generating bag rules (`unexpected_word` and family). These detect content the user did NOT
  predict, which is strictly more valuable than `absent` and strictly harder: the bag must be
  built with the publisher's own tokenizer, which lowercases, strips markdown, drops
  one-character tokens and folds accents.
- The gate for it is a claim the user must make deliberately, that a labeled `text` is the WHOLE
  document rather than as much as they bothered to type. Without that claim, an abridged label
  makes every unlabeled sentence read as invention and defames a correct backend.

**Increment 3, not built.**
- Publisher-comparable numbers on a private corpus, meaning the same report shape `benchmark run`
  produces, over documents you own.

**Out of scope, deliberately.**
- A curated library of rules for a document type. That gets better with private corpora and
  belongs in the company repository (AGENTS.md, "Not here, ever").
- Reimplementing any rule. The publisher owns every verdict, so our number and theirs cannot drift.
- Making rules mandatory, or scoring them when a case does not ask for them.

## User Experience

```bash
# 1. Label a document the way you already do, and add what must NOT appear.
cat > mydata/invoice/case.json <<'JSON'
{ "name": "invoice",
  "input": {"path": "input.pdf"},
  "expected": {
    "text_contains": ["Invoice number", "Total due"],
    "text_absent": ["Total due: 9999999.00"] } }
JSON

# 2. Turn those labels into publisher rules. Prints first; --write applies.
openreading rules mydata --write

# 3. Rank backends. rule_pass_rate is the dimension that can fall for invention.
openreading leaderboard mydata --backends pymupdf,anthropic-claude
```

The user never writes ParseBench JSON to get started, and never installs a corpus. The extra
(`openreading[parsebench]`) is needed only to SCORE rules, and a case that declares them without it
fails loudly naming the install command.

## Acceptance Criteria

1. A response doctored with content the case did not expect scores strictly below 1.0 on
   `rule_pass_rate`, while `text_contains` on the same response stays at 1.0. This is the
   feature's reason to exist and is asserted directly in the test suite.
2. Rules are scored only through `openreading.evals.scorers.score`. No new runner, no second
   command that scores a labeled case.
3. `rule_pass_rate` is a float in [0,1] and appears as its own key in `dimensions`, never folded
   into another dimension.
4. A case declaring `rules` without the publisher package raises, naming the install command. It
   does not score 0 and does not silently omit the dimension.
5. `openreading rules` prints by default, never overwrites an existing `rules` key without
   `--force`, and generates nothing from `text` or `markdown`.
6. `openreading rules` generates `absent` rules from `text_absent`, so the highest-value assertion
   needs no publisher JSON.
7. Scoring writes nothing to stdout and works off the main thread.
8. Every rule type the publisher accepts reaches its engine unchanged.

## Success Metrics

- A team evaluating a model backend writes at least one absence assertion per ten labeled cases.
  Below that, authoring friction beat the fear of hallucination and increment 2 is mis-shaped.
- No reported case of `rule_pass_rate` disagreeing with the same rule under `benchmark run`. A
  single such report means the publisher's engine is not the only grader, and it must be.
- Zero reports of a rule failing for a reason the user cannot see. The known instances are
  documented (cell-relationship rules need HTML tables, bag rules need the publisher's tokenizer).

## Risks

- **A generated rule that cannot fail.** A rule set derived from labels can be vacuously
  satisfiable, so a backend scores well for the wrong reason. Mitigated by generating only
  assertions the case already makes, and by never inferring an assertion from `text` or
  `markdown`. Not fully mitigated: a `present` rule for a string every backend emits is weak, and
  nothing detects that.
- **A rule that cannot pass.** Cell-relationship rules need an HTML table and always fail against a
  pipe-table backend, which reads as a broken backend. Documented in the guide and in
  `openreading.evals.rules`, with the advice to prefer the plain `table` rule.
- **Coupling to another company's schema.** Labels written in ParseBench's vocabulary cannot be
  reshaped unilaterally, and a breaking change upstream invalidates user data. Bounded by keeping
  rules optional, additive, and never the only way to express an expectation.
- **A misleading `overall`.** `overall` is the unweighted mean of every dimension a case names, so
  a case with one rule and four other dimensions moves less than a case with rules alone. It is a
  reading aid; the per-dimension values are the measurement.

## Alternatives considered

- **Write absence scorers natively** (`text_not_contains`, a token-precision budget). Cheaper, no
  coupling, and it was the design record's own question 5. Rejected for increment 1 because it
  reproduces measurement logic another project maintains, and because the same user then gets two
  different absence verdicts depending on which lane they are in. Still the right answer if the
  coupling proves painful; the increment is small either way.
- **Convert a private dataset into a publisher corpus** and run `benchmark run` over it. Reaches
  publisher-comparable numbers, which increment 1 does not. Rejected for now because it needs a
  corpus converter, cache signatures and new CLI surface, and because its own strongest variant
  inferred exhaustiveness from a labeled `text`, which defames a correct backend. Kept as
  increment 3.

## Open Questions

1. Does anyone need publisher-COMPARABLE numbers on their own corpus (increment 3), or is absence
   detection the whole need? The answer decides whether increment 3 is ever built.
2. Should `expected.text` gain an explicit exhaustiveness claim, which is the gate increment 2
   requires? It changes what a long-standing key means.
3. `tables_num_rows` did not pass in testing against either table shape at any row count. Is it
   broken upstream, or misused here?

## Related Artifacts

- `src/openreading/evals/README.md`, path two, "Assert what must NOT be there"
- `src/openreading/evals/rules.py` (the contract), `scorers.py` (the dimension)
- `design/benchmark-rule-vocabulary.md`, the design record, deleted when increment 1 shipped
