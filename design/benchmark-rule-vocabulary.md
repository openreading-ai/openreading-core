# Design record: adopting a publisher's rule vocabulary for your own documents

**Status: proposed, not built. Do not start without the maintainer's go-ahead.**

This is the next piece of benchmark work, queued deliberately. It is written down because it is a
coupling decision rather than a feature, and the cost of getting it wrong is paid slowly.

## The problem this would solve

`openreading.evals.scorers` grades a response on five dimensions: `text` similarity, the
`text_contains` fraction, `markdown` similarity, `typed_fields` precision, recall and F1, and
table-cell accuracy. Three of the five ask only whether the content you expected is present. They
cannot ask whether the backend added anything you did not expect.

The evals guide demonstrates the consequence on the shipped sample. Take a correct response, add a
total nobody wrote, a table row nobody typed, and four hundred words of filler, then score it
again. It still scores 1.0. Four backends in the catalog write text rather than read it
(`anthropic-claude`, `google-gemini`, `nuextract`, `qwen-vl`), so this is the failure mode most
worth catching and the one the scorer is blind to.

ParseBench's rule engine is not blind to it. Its public rule vocabulary carries 78 types, and
these are the ones this repo has no equivalent for:

- **Invented content**: `extra_content`, `unexpected_word`, `unexpected_sentence`,
  `too_many_word_occurence`, `too_many_sentence_occurence`, and the `_percent` variants of each.
- **Table structure**: `table_colspan`, `table_rowspan`, `table_header_chain`, `table_top_header`,
  `table_left_header`, `table_same_row`, `table_same_column`, `table_adjacent_*`. The repo's
  `table_cell_accuracy` compares a flat set of cells, so a merged cell is invisible to it.
- **Rich text**: `is_bold`, `is_italic`, `is_sub`, `is_sup`, `is_strikeout`, `is_latex`,
  `mark_color`, `text_color`, `is_code_block`, and every `is_not_*` negation.
- **Reading order**: `order`, `heading_structure`, `title_hierarchy_percent`, `list_level`.

Today that vocabulary reaches only the publisher's own documents. The rules live in the
publisher's answer key, so there is no `table_colspan` attached to your invoice and their engine
has nothing to say about it. A public score cannot tell you whether a backend invents content on
**your** traffic, which is the question that actually decides a vendor.

## The proposal

`parse_bench.extensions.register_rule_type` is a documented public extension point, and it takes a
pydantic schema plus a scoring class. The proposal is to let a `case.json` carry publisher-format
rules alongside the five dimensions it carries now, and to score those rules with the publisher's
own engine over your own corpus.

Sketch, not a specification:

```json
{ "name": "invoice_2024_03",
  "input": {"path": "input.pdf"},
  "expected": {
    "text_contains": ["Total due"],
    "rules": [
      {"type": "unexpected_word", "word": "9999999"},
      {"type": "table_colspan", "cell": "Region", "span": 2}
    ] } }
```

Everything already in `expected` keeps working and keeps its current meaning. `rules` is additive.

## Why this is a decision and not a task

**It couples your ground-truth format to another company's model.** The rule schemas are
`run-llama`'s, versioned on their release cadence, and shaped by what their corpus needed. Labels
your customers write in that format are labels you cannot reshape unilaterally. The licence is
Apache-2.0 so the legal side is clear; the design commitment is the real cost.

**It puts a second engine on the daily scoring path.** The evals guide states the law: there is one
scoring path, so two harnesses can never disagree about one document. Today the publisher's engine
only ever sees publisher documents and the repo's scorer only ever sees yours, so they never touch
the same document and the law holds. This proposal ends that separation by design. It needs an
explicit answer for which engine owns a document that carries both kinds of expectation.

**It may not belong in this repo at all.** AGENTS.md asks whether a competent engineer could
rebuild a feature in a week from this repo. A rule adapter probably passes. A *curated library of
rules* for a document type would not, because it gets better only with private data, and that is
company-repo work.

## Open questions for the maintainer

1. Does `rules` live in `case.json`, or in a sibling file that keeps the repo's own format clean?
2. Which engine owns a case that carries both `rules` and the five dimensions, and does `overall`
   combine them or report them separately?
3. Is a hard dependency on `parse-bench` acceptable on the private-corpus scoring path, given it is
   currently an optional extra needed only for public runs?
4. Does `calibrate` sweep against rule outcomes, or stay on the five dimensions?
5. Cheaper alternative worth pricing first: implement `unexpected_word` and `extra_content` natively
   in `openreading.evals.scorers`. Two scorers close most of the hallucination gap with no coupling
   at all. That may be the whole answer.

Question 5 deserves an honest look before any of the others. The gap that matters most is invented
content, and closing it does not obviously require anyone else's engine.

## When this ships

Move the durable facts into the `openreading.evals` package docstring and the scorers module
docstring, then delete this file in the same pull request. That is AGENTS.md's rule for design
records, and nothing mechanises it.
