# Design record: core counts what the vendor reported, and never converts it to money

Status: proposed, not built. Decision taken by Akshay on 2026-09-07.

Measured against `e27ad9d`. Sixth record in the removal set; the test and the landing order are in
[`README.md`](README.md). This closes
[`unverifiable-claims-sweep.md`](unverifiable-claims-sweep.md) section A2.

## The distinction the whole change rests on

Cost in this repository is three different things wearing one word.

**An observation.** `pages_processed`, `credits`, `input_tokens`, `output_tokens` are counters the
vendor returned for this call. `duration_ms` is measured locally by a clock we own. Core saw all
of these happen.

**An assertion.** `descriptor.cost.usd_per_page_equiv_low` / `_high` on fifteen adapters, plus two
adapters carrying private price tables of their own: `anthropic_claude._MODEL_PRICE` (`$/1M`
tokens, comment dated `accessed 2026-06-24`) and an equivalent in
`azure_document_intelligence`. Someone read a pricing page and typed numbers into Python.

**A derivation that launders the second into the first.** `router/cost.py` turns those tables into
`response.usage.cost_usd` and stamps `cost_basis`. A caller reads `cost_usd: 0.02` in the same
object as `input_tokens: 1523` and has no reason to think one was counted and the other guessed.

The observations stay. The assertion and the derivation go, because converting a count into a
price requires a fact core cannot verify, and pricing moves faster than anything else this sweep
has removed. The `_MODEL_PRICE` comment dates itself to June; it is September.

## Where the guess is presented as a result

Four places, each worse than the last.

**`response.usage.cost_usd`** (`response.v0.3.json:539`). Every parse returns a dollar figure. The
schema's own wording is honest about the mechanism and reads as authoritative anyway: *"the router
computes cost_usd via the pricing model, filling what the backend reports"*.

**`compare`'s `cost_outlier` finding** (`comparison-report.v0.2.json:346`). Comparing two backends
produces a finding that one is a cost outlier, computed by comparing two of our own estimates
against each other. It is in the **closed** finding enum, so removing it is a schema break.

**`leaderboard`'s `cost_per_doc`** (`leaderboard.py:141`, via `calibrate._descriptor_cost`). A
benchmark presents a ranked table over real documents with a cost column that was never measured
during the benchmark. It comes straight from the descriptor.

**`evals/preflight.py`.** The worst one, because a user acts on it. It prices a run from
`usd_per_page_equiv_low`/`_high` and asks the operator to approve spending real money. A stale
number here talks someone into a run they would have declined, or out of one they would have
taken.

## What goes

- `descriptor.cost` entirely: the `Cost` model (`native_unit`, `usd_per_page_equiv_low`,
  `usd_per_page_equiv_high`, `basis`, `lossiness`) and its fifteen instances.
- `anthropic_claude._MODEL_PRICE` and the `azure_document_intelligence` price table.
- `usage.cost_usd` and `usage.cost_basis`, from `response`, from the batch summary
  (`types/batch.py:73`) and from `types/response.py:88`.
- The dollar half of `router/cost.py` and `types/cost.py`. `apply_cost_report` survives as the
  pass-through that fills vendor counters, which is its "fill-only, never invent" rule already.
- `CostReport`'s usd fields and the `billing_target` enum. Its "resale is deliberately never used"
  note becomes true by construction rather than by policy.
- `compare`'s `cost_outlier` finding, its cost column (`comparison/render.py:41`, `:102`) and the
  cost row of the facts scoreboard.
- `leaderboard`'s `cost_per_doc` and `calibrate._descriptor_cost`.
- `evals/preflight.py`'s dollar estimates.
- The cookbook entries in `strategies/presets.py` built on cost, and `optimize_for: cost`, which
  [`explicit-backends.md`](explicit-backends.md) already deletes with the scorer.

## What stays

`usage.pages_processed`, `credits`, `input_tokens`, `output_tokens`, `duration_ms`. A caller who
wants dollars multiplies these by the prices on their own invoice, which is the only price that is
actually true for them: it reflects their tier, their commitments and their negotiated rate, none
of which this repository can see.

That is also the honest answer to "why does the open engine not tell me what this cost". Because
the number it could produce would be wrong for most callers, and confidently formatted.

## The one thing worth replacing rather than deleting

`evals/preflight.py` exists to stop someone accidentally spending money on a benchmark sweep. That
is a real protection and deleting it outright makes the tool more dangerous, not more honest.

Replace the dollars with counts. A preflight that says *"this run will send 240 pages to 3 hosted
backends, billed to your own account"* is entirely verifiable, needs no price table, and protects
the same person just as well. Keep `--yes` and keep requiring it with no terminal attached.

## Schema changes

| schema | from | to | change |
|---|---|---|---|
| `response` | v0.3 | v0.4 | remove `usage.cost_usd`, `usage.cost_basis`; rewrite the `usage` description |
| `adapter-descriptor` | v0.8 | v0.9 | remove `cost` (v0.8 comes from the compliance change) |
| `comparison-report` | v0.2 | v0.3 | remove `cost_outlier` from the closed finding enum; drop the cost facts row |
| `batch-result` | v0.2 | v0.3 | remove `cost_usd` and `cost_bases[]` from the summary (v0.2 comes from the format change) |

Two of these are second bumps on schemas the earlier records already move. If this lands in the
same release, do each bump once and carry both changes, rather than shipping v0.8 and v0.9 of the
descriptor a week apart.

## Test strategy

- A test that no response, batch summary or comparison report contains a dollar figure, run
  against the current tree first, where every one of them does.
- A test that `usage.input_tokens` and `usage.pages_processed` still round-trip from a fake
  adapter's `CostReport`, so the pass-through is proven to survive the deletion.
- A test that preflight's output names a page count and a backend count and contains no `$`.
- `grep -rn "usd" src/openreading/adapters/` returns nothing, which is safe here as a string
  search because no adapter has a legitimate reason to name a currency.

## Order

After [`compliance-removal.md`](compliance-removal.md), for one practical reason: the stage-3
scorer reads `cost.usd_per_page_equiv_*`, and that scorer is deleted there. Doing cost first would
mean teaching the scorer to live without prices and then deleting it days later.

Independent of the ledger and format records.
