# Design record: the documentation each removal owes

Status: proposed, not built. A checklist, not an argument.

Measured against `e27ad9d` on 2026-09-07. Applies to every row in [`README.md`](README.md).

## Why this is its own record

In this repository the documentation **is** the code: module docstrings, `argparse help=`, the
`openreading.cli` docstring that `openreading help` serves back verbatim. `AGENTS.md` says a
separate document contradicting the code "looks authoritative and is wrong, and nothing forces
anyone to notice".

A removal is the worst case for that rule. Deleting a feature and leaving its chapter behind
produces a manual that documents a flag the binary rejects. Some of that is caught by tests. Much
of it is not, and the uncaught part is prose that a reader will believe.

This record separates the two, so an implementer knows what `make verify` will scream about and
what only a human will catch.

## What the guards catch for free

These fail loudly and need no discipline.

| guard | what it forces |
|---|---|
| `tests/test_cli_help.py` | heading and slug stay a bijection, every subcommand resolves to a topic, every chapter renders verbatim, no chapter names a private path, and **the docstring stays inside 79 columns** |
| `tests/test_docs_truth.py` | every config-shaped YAML block in `examples/tutorial.md` and the seven `strategies` modules passes `strategy validate` |
| `tests/test_docs_policy.py` | the markdown allowlist, one README per directory, every relative link resolves to a git-tracked path |
| `tests/test_schema_evolution.py` | released schema files stay byte-identical, so the manifest in `schemas/README.md` cannot drift silently |
| `scripts/check_extras_parity.py` | registry and install extras agree |
| ruff `D100`/`D104` | every module and package keeps a docstring |

Two consequences worth planning for:

- **The tutorial breaks the build, by design.** `test_docs_truth.py:53` executes its YAML, so the
  `policy: {require_local: true}` block at `tutorial.md:584` fails `strategy validate` the moment
  the key leaves the schema. That is the guard working. It also means the tutorial cannot be
  deferred to a follow-up PR.
- **`openreading help` breaks the build too.** `cli/help.py:91` and `:92` register two chapters,
  `compliance` (aliases `route`, `policy`) and `cost` (aliases `money`, `spend`, `billing`), and
  each must map to a heading in the `openreading.cli` docstring. Delete a chapter without deleting
  its `TOPICS` row and `test_every_heading_has_exactly_one_slug_and_the_reverse` fails.

## What no guard catches

Prose. Every file below carries sentences about features these records delete, and nothing checks
whether a paragraph is still true.

| file | hits | what is in it |
|---|---|---|
| `src/openreading/router/README.md` | 114 | the three-stage router, every drop code, the compliance gate table |
| `examples/tutorial.md` | 63 | step 8 is entirely `policy:` and `route`; the drop-reason walkthrough |
| `src/openreading/adapters/README.md` | 43 | five per-backend tables including compliance and cost columns |
| `README.md` (root) | 23 | "compliance-first router" in the pitch, the docs index |
| `src/openreading/strategies/README.md` | 19 | policy union, compliance-outside-the-tree |
| `src/openreading/ledger/README.md` | 17 | retention, the reaper, encryption at rest |
| `src/openreading/README.md` (docs home) | 16 | the guide map and Prev/Next order |
| `src/openreading/batch/README.md` | 13 | skip reasons, the effective format set |
| `src/openreading/server/README.md` | 10 | the status ladder, 403 `compliance_refused`, the 502 row |
| `AGENTS.md` | 9 | two golden rules, the layout line, "3-stage compliance-first router" |
| `.env.example` | 9 | the two attestation vars and their comments |
| `src/openreading/cli/README.md` | 8 | verbs and exit codes |
| `src/openreading/evals/README.md` | 6 | the cost column, preflight |
| `src/openreading/schemas/README.md` | 6 | the version manifest |
| `CONTRIBUTING.md` | 2 | the compliance rule for contributors |

And the docstrings, which are the primary documentation and where most of this actually lives:

| docstring | hits |
|---|---|
| `cli/__init__.py` (the manual `openreading help` serves) | 59 |
| `openreading/__init__.py` (the package briefing) | 51 |
| `schemas/__init__.py` | 45 |
| `ledger/__init__.py` | 39 |
| `api.py` | 36 |
| `server/__init__.py` | 23 |
| `batch/__init__.py` | 17 |
| `adapters/__init__.py`, `config.py` | 15 each |
| `strategies/__init__.py`, `router/__init__.py`, `comparison/__init__.py`, `credentials.py` | 2 to 10 each |

`CHANGELOG.md` carries 47 hits, and those are **history and must not be edited**. Past entries
describe what was true then. The removals get new `Removed` entries; nothing above the new section
changes.

## Per-row ownership

Each row of [`README.md`](README.md) owns its own documentation. Nothing is deferred.

**Row 0, `page_range_selection`.** The `§2.7` per-page prose in `strategies/__init__.py` and
`strategies/README.md` describes a mechanism that never ran. Whichever way the bug is fixed, that
prose changes.

**Row 1, the MIME resolver.** `derive/README.md`, the `mime_type` wording in the package
docstring's recipes, and the D-v2-9 citation wherever it is quoted, since that decision is
superseded.

**Row 2, the format gate.** `batch/README.md`'s skip reasons, `batch/__init__.py`'s two skip-reason
sentences, `router/README.md`'s stage-2 section, the `input_formats` row in
`adapters/README.md`, and the `openreading backends` description if it shows formats.

**Row 3, the ledger.** `ledger/README.md` and `ledger/__init__.py`'s arming section, which
currently documents `OPENREADING_LEDGER_RETENTION_HOURS` and the reaper in detail. Two `.env`
entries. The `server/__init__.py` sweep-interval line. Plus the disclosure sentence this record's
sibling argues for: `OPENREADING_LEDGER` copies every document and every response into a
directory, and the variable's name does not say so.

**Row 4, compliance and explicit backends.** The largest by far.
- `AGENTS.md`: the golden rule at line 141, the worked example at line 61, the "do not widen"
  rule at line 245, and "3-stage compliance-first router" at lines 164 and 217. The replacement
  rule belongs in the `openreading.router` docstring where stage 1 used to be: *core holds no fact
  it cannot verify; a constraint core cannot check is one core must not appear to enforce.*
- `cli/help.py`: delete the `compliance` topic, and decide what `route` and `policy` alias to
  instead, since both are live aliases a reader may have in muscle memory.
- `examples/tutorial.md`: step 8 is titled "Your first openreading.yaml: a policy" and is built on
  `require_local`. It becomes the `policy.backends` step. Its index row, the Prev/Next chain and
  the drop-reason output block at line 609 all move with it.
- `router/README.md`: 114 hits, effectively a rewrite.
- `server/README.md` and `server/__init__.py`: 403 `compliance_refused` leaves the ladder, 403
  `scope_denied` stays.

**Row 5, cost.** `evals/README.md`'s cost column and preflight description, the `usage` block in
the package docstring's response example (`__init__.py:228` prints `cost_usd`), the batch summary
line at `:302`, the cost-basis sentence at `:333`, `cli/help.py`'s `cost` topic and its three
aliases, and the corresponding chapter in the `openreading.cli` docstring.

## The rule to apply while editing

`AGENTS.md`: a fact is documented where it is read. The flag next to its argparse definition, the
variable next to its `os.environ` read, the field next to its schema. A README indexes those
places and never restates them.

So the order inside each PR is: delete the code, delete or rewrite the docstring that documented
it, then fix the README that pointed at the docstring. A README edited first tends to grow an
explanation that belongs in the module.

## Acceptance

Behavioural, not string searches, for the reason
[`compliance-removal.md`](compliance-removal.md) gives: frozen schemas and `CHANGELOG.md` history
legitimately keep the old words.

1. `make verify` green, which covers the six guards above.
2. `openreading help` lists no chapter for a deleted feature, and every remaining alias resolves.
3. Every command in `examples/tutorial.md` re-run from a fresh clone, with the new output pasted
   in. `AGENTS.md` requires this whenever a verb, flag, output or example document it shows
   changes, and rows 2, 4 and 5 each change several.
4. Every subsystem README re-read end to end by a human, since no test asserts that a paragraph is
   true. This is the step that will be skipped under time pressure, and it is the one the whole
   documentation-lives-in-code rule exists to protect.
