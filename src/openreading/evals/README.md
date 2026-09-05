# Evals: public benchmarks and your own documents

<sub>[Docs home](../README.md) · [← The channel contract](../derive/README.md) · [JSON Schemas →](../schemas/README.md)</sub>

> **In one sentence.** Rank backends against a public benchmark somebody else wrote, or against
> documents you labeled yourself, and read the ranking in your terminal.

## What this gives you

Two vendors both claim the best accuracy on your kind of document, and both numbers come from the
vendor's own benchmark. This package answers that question twice, from two directions. **The two
commands are not interchangeable, and picking the wrong one is the usual way to get lost here.**

| | `openreading benchmark` | `openreading leaderboard` |
|---|---|---|
| The documents | a publisher's public corpus | yours |
| The expected answers | the publisher's, shipped with the corpus | you write one `case.json` per document |
| Who grades it | the publisher's own scoring code | `openreading.evals.scorers`, plus the publisher's rule engine on any case that declares `rules` |
| Work before the first run | install one extra | label every document by hand |
| What it settles | which backend is better on documents anyone can check | whether that holds on your traffic |

**Start with `benchmark`.** It needs no labeling, and its number came from a scorer you did not
write, so nobody has to take your word for it. Move to `leaderboard` once you need to know whether
a public result transfers to the documents you actually process. That is the one question a public
corpus cannot answer, because it is not your mail.

Three terms the rest of this page uses. A backend is one parser, such as the local `pymupdf`
library or a hosted API. A target is one backend or one strategy that a benchmark evaluates. A
response is the one JSON envelope every backend returns.

Labeled datasets never land in this repo. The repo ships one synthetic case so the labeled path
proves itself offline, and both walkthroughs below run with no key and no bill.

One limit belongs up here rather than in a footnote, along with how to close it. Four of the five
scorers in this repo ask whether the content you expected is present. They do not ask whether the
backend added anything you did not expect, so a response with an invented total scores 1.0 on
them. That matters because four backends in the catalog write text rather than read it.

The fifth closes it on your own documents. Add a `rules` key to a case and the publisher's own
rule engine scores it, including assertions like `absent` that fail on invented content. See
[Assert what must NOT be there](#assert-what-must-not-be-there).

## Path one: a public benchmark (`openreading benchmark`)

**Nothing in this section uses documents of yours.** The corpus, the expected answers and the
scoring code all belong to the publisher. You supply the backends and read the ranking.

A benchmark profile connects a publisher's dataset and scorer to OpenReading. The publisher still
owns case loading, normalization, metrics, and detailed reports. OpenReading supplies the target,
keeps the complete normalized response inside each raw artifact, and reads the publisher's own
numbers back into a table. It never computes a score of its own here, so the terminal and the
publisher's dashboard cannot disagree.

Use Python 3.12 or newer for the two runnable profiles. Install only the profile you need.

### The five verbs, and which ones spend

| Command | What it does | Costs |
|---|---|---|
| `benchmark list` | every profile, its status and its terms lane | nothing, offline |
| `benchmark show NAME` | sources, both licenses, published scale, install extra | nothing, offline |
| `benchmark estimate NAME --preset full --target …` | prices the whole published corpus, in pages | nothing, offline |
| `benchmark prepare NAME` | downloads the corpus to `~/.cache/openreading/benchmarks` | bandwidth and disk |
| `benchmark run NAME --target …` | **the only verb that calls a backend** | two documents unless `--limit` says otherwise |

`run` flags worth knowing before the first one: `--limit N` (default 2, `0` for everything),
`--doc NAME` for documents you name, `--yes` to skip the spending confirmation, `--output-dir`
(default `./benchmark-results`), and `--jobs` for concurrency.

### Try it for nothing, in three commands

`pymupdf` and `tesseract` are local libraries, so this whole walkthrough bills zero. Run it before
you point the same commands at a hosted backend.

```bash
pip install 'openreading[parsebench]'
openreading benchmark prepare parsebench --preset smoke
openreading benchmark run parsebench --target backend:pymupdf --target backend:tesseract \
  --doc text_content/text_simple__results --doc table/222876fb_page22
```
```text
estimate: 2 document(s) of 15 prepared (13 not run), 2 page(s), 2 target(s)
  backend:pymupdf: $0.00 to $0.00
  backend:tesseract: $0.00 to $0.00
  total (priced targets): $0.00 to $0.00
  a range from each backend's declared per-page rates, not a quote
  document: text_content/text_simple__results
  document: table/222876fb_page22
…
completed: backend:pymupdf as openreading_backend_pymupdf_4d8801dee2 in benchmark-results
completed: backend:tesseract as openreading_backend_tesseract_c7f0d4e500 in benchmark-results
comparison: benchmark-results/openreading-leaderboard.html
```

**You should see** the estimate before either backend runs, then the ranked comparison when it
finishes:

```text
target / category                   score   parsed  errors  metric
backend:pymupdf                     0.291      2/2       0
  table                             0.000                   grits_trm_composite
  text_content                      0.874                   rule_pass_rate (80/95 rules)
  text_formatting                   0.000                   rule_pass_rate (0/11 rules)
backend:tesseract                   0.228      2/2       0
  table                             0.000                   grits_trm_composite
  text_content                      0.684                   rule_pass_rate (62/95 rules)
  text_formatting                   0.000                   rule_pass_rate (0/11 rules)
```

**That ordering is the whole point.** Born-digital extraction beats OCR on a born-digital page,
80 rules to 62, and the number saying so came from the publisher's scorer rather than from this
repo. Swap `backend:tesseract` for a hosted backend and the same table is your vendor bake-off.

`openreading benchmark report` prints that table again from a finished run without re-running
anything, and `--format json` hands a script the same numbers.

Two zeros in the same run are worth reading correctly. `text_formatting` scores 0.000 for both,
because those rules assert bold, italic, superscript and strikeout, and neither backend emits rich
text. A backend is not broken for scoring zero on a channel it never claimed.

Drop the two `--doc` flags and `run` picks two documents itself, one per category in id order.
Today that lands on `chart` and `layout`, which are the two categories a plain text extractor is
worst at, so both backends score 0.000 and the run looks broken when it is not. Name documents
until you know a corpus.

**That last command touches two documents, not the corpus.** `run` defaults to two because it
spends your money on someone else's API, and two is enough to watch every target produce output
and a score. Scale up deliberately with `--limit N`, or `--limit 0` for the whole prepared corpus.
Run documents you name with `--doc table/doc1`, repeatable.

The two documents are drawn round-robin across the corpus's categories, so a small run spans a
chart and a table rather than two charts. The choice is stable, so a rerun picks the same
documents and the publisher resumes instead of re-billing.

Before anything runs you get the documents it chose, their **page** count, and a dollar range per
target. Pages, because every hosted backend bills per page while the publishers count documents:
ExtractBench is 370 documents and 4,869 pages, so a document count understates a bill about
thirteen times. Anything unpriced, or over a dollar, asks first. `--yes` answers in advance, and
is required when no terminal is attached.

A `strategy:` target is deliberately never priced. A strategy escalates, so one document is one or
more billed calls across backends at different rates, and nothing here knows how many rungs fire.

The smoke preset is the default and controls which corpus is downloaded. ParseBench selects three
files per category, ExtractBench six documents, and `--limit` then caps what actually runs out of
whichever preset you prepared. Publisher results and the cross-target official leaderboard land in
`./benchmark-results` by default. Data lands under `~/.cache/openreading/benchmarks`, never in
this repository.

`backend:NAME` and `strategy:NAME` are explicit because both identifiers share one namespace in
other commands. A strategy uses the same `openreading.yaml` you run in production.

```bash
openreading benchmark run extractbench --limit 2 \
  --target backend:nuextract --target strategy:fields --config openreading.yaml
```

ParseBench reports its official rule results across tables, charts, content faithfulness, semantic
formatting, and visual grounding. Its current public set has 2,078 unique pages from 1,211
documents and 169,011 rules. See the [publisher repository](https://github.com/run-llama/ParseBench)
and [dataset card](https://huggingface.co/datasets/llamaindex/ParseBench).

ExtractBench reports Unified value F1 plus word and page grounding F1. Its current public set has
370 documents, 4,869 pages, 67 schemas, and eight domains. See the
[publisher repository](https://github.com/run-llama/ExtractBench) and
[dataset card](https://huggingface.co/datasets/llamaindex/ExtractBench).

Every repeated target gets a separate publisher report. Two or more successful targets also get
the publisher's cross-pipeline leaderboard. This is the direct test for whether an OpenReading
strategy improves over the backends it can invoke. The raw result retains `usage`, `warnings`, and
`orchestration`, so you can inspect cost, latency, missing channels, and escalation behavior beside
the official quality result.

### Choose the corpus for the question

The catalog distinguishes runnable profiles from research candidates. It also separates dataset
terms from scorer-code terms. Run `benchmark show NAME` before downloading any corpus.

| Question | Public evidence | Status and terms lane |
|---|---|---|
| broad parse fidelity and grounding | [ParseBench](https://github.com/run-llama/ParseBench) | runnable, commercial |
| schema-guided values and citations | [ExtractBench](https://github.com/run-llama/ExtractBench) | runnable, commercial |
| block classification and geometry | [DocLayNet](https://github.com/DS4SD/DocLayNet) | cataloged, commercial |
| table detection and structure | [PubTables-1M](https://github.com/microsoft/table-transformer) | cataloged, commercial |
| receipt text and semantic fields | [CORD](https://github.com/clovaai/cord) | cataloged, commercial |
| text, tables, formulas, layout, and order | [OmniDocBench](https://github.com/opendatalab/OmniDocBench) | cataloged, research only |
| form entities and links | [FUNSD](https://guillaumejaume.github.io/FUNSD/) and [XFUND](https://github.com/doc-analysis/XFUND) | cataloged, research only |
| structured Markdown continuity | [READoc](https://github.com/DongfuJiang/READoc) | cataloged, unverified |
| OCR impact on retrieval and generation | [OHR-Bench](https://github.com/opendatalab/OHR-Bench) | cataloged, unverified |
| OCR behavior assertions | [olmOCR Bench](https://github.com/allenai/olmocr/tree/main/olmocr/bench) | cataloged, unverified |
| fields, locations, and line items | [DocILE](https://github.com/rossumai/docile) | cataloged, unverified |
| long-report extraction | [Kleister Charity](https://github.com/applicaai/kleister-charity) | cataloged, unverified |
| cross-domain field extraction | [FieldBench](https://github.com/Zipstack/fieldbench) | cataloged, unverified |
| hard multilingual extraction | [DocuBench](https://github.com/Anni-Zou/DocuBench) | cataloged, unverified |
| format robustness and throughput | [GovDocs1](https://digitalcorpora.org/corpora/file-corpora/govdocs1/) | cataloged, unverified |

A commercial lane means the publisher states terms compatible with the default evaluation path.
It is not legal advice. A research-only profile requires `--allow-research-only`. An unverified
profile requires the separate `--allow-unverified-terms` flag after you review every source.
Cataloged profiles do not download or run yet, even after acknowledgement.

No public accuracy corpus proves compliance filtering, retry taxonomy, interruption recovery, or
batch isolation. Those are engine invariants. The offline suite tests them with controlled faults.
Use the private-dataset workflow below to test whether public quality results transfer to your own
documents.

## Path two: documents you labeled (`openreading leaderboard`)

Everything from here to the end of the recipes is the second path. It scores **your** documents
with the scorers in this repo, and it is the only way to learn whether a public result transfers
to your traffic. It costs labeling work, which is why it comes second.

### Mental model

A dataset is a directory of case directories, and each case directory holds a `case.json` with the
input plus `expected`. A policy is a short list of requirements a backend must meet, and the
compliance gate drops every backend that fails one. The runner sends each case to one backend
through the same compliance gate a normal run uses. The runner then scores only the dimensions
`expected` names, so a case with no expected tables is never scored on tables. The leaderboard
repeats that for every backend and ranks them by mean score. `calibrate` and `compare --truth` reuse
the same three scorers rather than carrying their own. Nothing feeds back into routing, and the
number is evidence for you to read.

### Walkthrough

Build the root README's sample document first, because later steps score against it. No key is
needed anywhere on this page, and no step bills anything.

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
```

#### 1. Rank two backends on the bundled case

```bash
uv run openreading leaderboard src/openreading/evals/sample --backends pymupdf,tesseract
```
```text
dataset: src/openreading/evals/sample  (1 case(s): loan_page1)

rank  backend                        mean  scored   cost/doc  errors  dimensions
   1  pymupdf                       1.000     1/1     0.0000       0  text_contains=1.00 table_cell_accuracy=1.00
   2  tesseract                     0.500     1/1     0.0000       0  text_contains=1.00 table_cell_accuracy=0.00

per-case result:
  loan_page1: winner=pymupdf  (pymupdf=1.00, tesseract=0.50)

tally over 1 case(s): 1 win, 0 tie, 0 all-zero, 0 no result
```

**You should see** the dataset's identity above the ranking, because the result is about these
documents and not a universal one. Both backends found the text, and OCR lost the table grid. The
`scored` column is the denominator the mean rests on, written as scored cases over total cases. A
mean over zero scored cases prints as an em dash rather than a number. So a backend that never ran
never looks like a backend that ran and scored zero. Naming one backend alone is exit 2.

**What a 1.000 does and does not tell you.** It says pymupdf produced everything this case asked
for. It does not say pymupdf produced only that. Two of the five expected dimensions count how much
of what you wrote down came back, and nothing else:

| You write in `expected` | Scored as | Sees content the document does not contain? |
|---|---|---|
| `text_contains` | fraction of your strings found in the text | no |
| `tables` | fraction of your expected cells matched | no, unless you write `"tables": []` |
| `text` | `difflib` ratio against your full expected text | yes, extra text lowers it |
| `markdown` | `difflib` ratio against your full expected markdown | yes |
| `typed_fields` | precision, recall and F1, headline F1 | yes, an extra field lowers precision |

Source: `src/openreading/evals/scorers.py` (`score`). Prove the gap on the shipped case. Doctor a
good response by adding a total nobody wrote, a table row nobody typed, and four hundred junk
words, then score it again:

```bash
cat > halluc.py <<'PY'
import json, openreading
from openreading.evals import score

expected = json.load(open("src/openreading/evals/sample/loan_page1/case.json"))["expected"]
resp = openreading.run("sample.pdf", backend="pymupdf")
print("as parsed:", score(resp, expected)["overall"])

resp["document"]["text"] += "\nTotal due: 9999999.00\n" + "lorem ipsum " * 200
for block in resp["document"]["pages"][0]["blocks"]:
    if block["type"] == "table":
        block["table"]["rows"].append(["East", "999", "999999"])
print("with an invented total, a row and 400 junk words:", score(resp, expected)["overall"])
PY
uv run python halluc.py
```
```text
as parsed: 1.0
with an invented total, a row and 400 junk words: 1.0
```

PyMuPDF prints one or two notices on stdout in every Python snippet on this page. They name the
deprecated `fitz` API and the `pymupdf_layout` package. Each notice lands before the scored line
whose work triggered it, so one can fall between two scored lines. The `openreading` CLI sends the
same notices to stderr, which is why the command outputs above are clean. They are library noise, so
read past them to the scored lines.

**You should see** the same 1.0 twice. This is the failure mode of a backend that writes text
rather than reading it. Four backends in the [catalog](../adapters/README.md) do that today:
`anthropic-claude`, `google-gemini`, `nuextract`, and `qwen-vl`. `leaderboard --format json` marks
each of them `non_deterministic: true`, so your own run always shows the current set. Three ways to
cover it, cheapest first. Write `expected.text` for a handful of documents instead of
`text_contains`, because the full-text ratio falls when content is added. Score `typed_fields` where
a backend can produce them, because that dimension carries a real precision term. Run the two
top-ranked backends through `compare --format diffs` on the same documents. That view names the
lines one side has and the other does not ([Compare](../comparison/README.md)).

`--format json` prints the schema-valid `leaderboard-report.v0.1`. It carries a `schema_version` of
`"0.1"` plus three parts: `dataset {path, case_count, case_names}`, `backends[] {backend_id, rank,
mean_score, n_cases, n_scored, errors, cost_per_doc, non_deterministic, dimensions}`, and `cases[]
{name, winner, scores}`. The JSON `winner` field is byte-stable and blunt. It names one backend on a
tie, breaking the tie alphabetically, and it names one on a case every backend scored zero. The
human `per-case result` block distinguishes those, so tally wins from it and never from
`cases[].winner`.

#### 2. Read the bundled case

Source: `src/openreading/evals/sample/loan_page1/case.json`, whitespace compacted.
```json
{ "name": "loan_page1",
  "input": { "builtin_sample": true, "pages": [1] },
  "expected": {
    "text_contains": ["OpenReading Test Document", "twelve points"],
    "tables": [[["Region", "Units", "Revenue"], ["North", "120", "4400"], ["South", "85", "3100"], ["West", "42", "1650"]]] },
  "meta": { "doc_type": "generic", "note": "built-in sample PDF page 1; born-digital text + 3x4 table" } }
```

`builtin_sample` resolves to the generated sample PDF, so the committed case needs no binary. Your
cases use `"input": {"path": "input.pdf"}` next to the file. `expected` may name `text`,
`text_contains`, `markdown`, `typed_fields`, and `tables`, and only what it names is scored.

#### 3. Write your own dataset

Three cases rather than one, because a one-case dataset can show you neither a tie nor a shared
failure. You will meet both on real documents.

```bash
for c in first tie neither; do mkdir -p mydata/$c && cp sample.pdf mydata/$c/input.pdf; done
cat > mydata/first/case.json <<'JSON'
{ "name": "first",
  "input": {"path": "input.pdf"},
  "expected": {
    "text_contains": ["OpenReading Test Document", "twelve points"],
    "tables": [[["Region", "Units", "Revenue"], ["North", "120", "4400"]]] } }
JSON
cat > mydata/tie/case.json <<'JSON'
{ "name": "tie",
  "input": {"path": "input.pdf"},
  "expected": {"text_contains": ["OpenReading Test Document"]} }
JSON
cat > mydata/neither/case.json <<'JSON'
{ "name": "neither",
  "input": {"path": "input.pdf"},
  "expected": {"text_contains": ["Total amount due"]} }
JSON
uv run openreading leaderboard mydata --backends pymupdf,tesseract
```
```text
dataset: mydata  (3 case(s): first, neither, tie)

rank  backend                        mean  scored   cost/doc  errors  dimensions
   1  pymupdf                       0.667     3/3     0.0000       0  text_contains=0.67 table_cell_accuracy=1.00
   2  tesseract                     0.500     3/3     0.0000       0  text_contains=0.67 table_cell_accuracy=0.00

per-case result:
  first: winner=pymupdf  (pymupdf=1.00, tesseract=0.50)
  neither: no winner (every scored backend got 0.00)  (pymupdf=0.00, tesseract=0.00)
  tie: tie=pymupdf,tesseract  (pymupdf=1.00, tesseract=1.00)

tally over 3 case(s): 1 win, 1 tie, 1 all-zero, 0 no result
```

**You should see** one win out of three cases, not three. `tie` asks for a string both backends
find, and `neither` asks for a string no document here contains. Neither case separates the two
backends at all. Check: `ls mydata/*/case.json`. A case with `"expected": {}` is unscored, prints
as `—`, and never counts as a zero.

The `mean` and the `dimensions` on one row do not average to each other, and they are not meant to.
`mean` is the average of that backend's three per-case overalls. Each dimension is averaged over
only the cases that named it, so `table_cell_accuracy=1.00` for pymupdf is one case out of three.

#### 4. Run a vendor bake-off, in order

The steps above each answer one question. This is the order to run them in when the job is picking
a backend for real documents and defending the choice afterwards. Every example on this page runs
at one or three cases, because the repo ships one synthetic document. No number on this page is a
sample size you should copy.

1. **Sample the documents.** Take them from real traffic rather than from the easy pile, and
   include the kinds you expect to be hard. A leaderboard is a statement about the documents in it,
   which is why the dataset path prints above the ranking.
2. **Decide how many.** Nothing in this repo enforces a minimum, warns at a small one, or reports a
   spread, so the guard is yours. Two backends a tenth apart on ten documents is one document
   changing its mind. The tally line tells you how many cases actually separated the backends, and
   that count, not the case count, is the evidence you have.
3. **Label them.** A label here is the `expected` object of a `case.json`, written by a person who
   read the document. Step 2 shows the shape. Write `text` rather than `text_contains` on at least
   a few, so something on the page can see invented content. The cheapest place to start is the
   documents where two backends already disagree ([Compare](../comparison/README.md), the recipe
   "Turn two backends' disagreements into a labeled evals dataset").
4. **Split the labeled set in two before you measure anything.** One slice scores the leaderboard,
   the other tunes a threshold. Keep the split fixed and write down which case went where.
5. **Rank on the scoring slice.** `openreading leaderboard <scoring-slice> --backends a,b`. Read
   the tally and the `scored` column before the mean.
6. **Read the individual failures, not the mean.** A mean tells you there is a problem and never
   which document has it.
   ```bash
   uv run python -c "
   from openreading.evals import run_dataset
   from openreading.adapters.registry import make_adapter
   print(run_dataset(make_adapter('tesseract'), 'mydata').summary())"
   ```
   ```text
   backend=tesseract  mean_overall=0.500  cases=3  errors=0
     first: overall=0.500  text_contains=1.00 table_cell_accuracy=0.00
     neither: overall=0.000  text_contains=0.00
     tie: overall=1.000  text_contains=1.00
   ```
   **You should see** one line per case with its own dimensions, so you can open the worst
   document and look at it. `leaderboard --format json | jq -c '.cases[]'` gives the same per-case
   scores across every backend at once.
7. **Calibrate the strategy gate on the other slice.** `openreading calibrate <tuning-slice>
   --strategy …`. A strategy gate is the threshold that decides whether a strategy escalates from
   one backend to the next. A threshold picked on the same documents you scored is fitted to those
   documents. The score you then report cannot tell you whether the gate learned the corpus or
   learned the noise. Held out, the score is a prediction about documents the threshold never saw.
8. **Ship, and keep the dataset.** Rerun the leaderboard when a backend releases a new version.
   That rerun is the whole reason to have written the labels down.

### Assert what must NOT be there

The four scorers above count what you asked for. None of them can see what a backend ADDED, which
is the failure mode that matters most for the four backends in the catalog that write text rather
than read it. A `rules` key closes that, on your own documents.

A rule is one named assertion, written in ParseBench's own vocabulary and scored by ParseBench's
own engine. You are not adopting their corpus, only their grader, and only for the cases where you
ask for it.

**Start with `text_absent`.** It is a plain list of strings that must not appear, it needs no
publisher JSON, and it is the assertion that catches invented content. Everything else on this
page is refinement.

```json
{ "name": "invoice",
  "input": {"path": "input.pdf"},
  "expected": {
    "text_contains": ["OpenReading Test Document"],
    "text_absent": ["Total due: 9999999.00"],
    "rules": [
      {"type": "present", "id": "has_title", "text": "OpenReading Test Document"},
      {"type": "absent",  "id": "no_invented_total", "text": "Total due: 9999999.00"},
      {"type": "table",   "id": "north_units", "cell": "North", "right": "120", "top_heading": "Region"}
    ] } }
```

```bash
pip install 'openreading[parsebench]'
uv run openreading leaderboard mydata --backends pymupdf,tesseract
```
```text
dataset: mydata  (1 case(s): invoice)

rank  backend                        mean  scored   cost/doc  errors  dimensions
   1  pymupdf                       1.000     1/1     0.0000       0  text_contains=1.00 rule_pass_rate=1.00
   2  tesseract                     0.833     1/1     0.0000       0  text_contains=1.00 rule_pass_rate=0.67

per-case result:
  invoice: winner=pymupdf  (pymupdf=1.00, tesseract=0.83)
```

**You should see** `rule_pass_rate` beside the dimensions you already had, and the two backends
separated by it. Both find the title, so `text_contains` cannot tell them apart. OCR loses the
table's structure, so the `table` rule can. That is the same `leaderboard` command over the same
dataset directory, because rules are a dimension rather than a second harness.

You do not have to write the first ones by hand. `openreading rules` turns the labels a case
already carries into rules, prints them, and edits the files only when you say so:

```bash
uv run openreading rules mydata            # print what it would add
uv run openreading rules mydata --write    # apply it
```
```text
invoice: 4 rule(s) would be added
[
  {"type": "present", "id": "contains_0", "text": "OpenReading Test Document"},
  {"type": "table", "id": "table0_r1_c0", "cell": "North", "right": "120", "top_heading": "Region"}
]

nothing written. Re-run with --write to apply.
```

It generates what your labels already assert and nothing more, so it can never make a backend look
better than the labels justify. It also cannot generate the rule worth having most. Nothing in a
case says what must NOT appear, so every `absent` rule is yours to write, and the command says so
when it finishes.

Three rule types cover most of what a person wants to assert, all verified against ParseBench
1.0.2 on the shipped sample:

| Rule | Asserts |
|---|---|
| `{"type": "present", "text": "..."}` | the string appears |
| `{"type": "absent", "text": "..."}` | the string does NOT appear, which is the whole point |
| `{"type": "table", "cell": "North", "right": "120", "top_heading": "Region"}` | a cell, its neighbour and its column heading |

Matching folds case and collapses whitespace. It does not strip punctuation, so a trailing period
your document lacks fails the rule.

**Those three are what the generator writes, not what the scorer accepts.** Anything you put in
`rules` goes to the publisher's engine untouched, so the whole 78-type vocabulary is available by
hand. Run `uv run python -m pydoc parse_bench.test_cases.parse_rule_schemas` once the extra is
installed. Verified working on ordinary markdown: `order` (this text before that one), `is_bold`,
`is_italic`, `is_title`, `is_not_bold` and `missing_specific_word`, alongside the three above.

Two families need more than markdown, and a rule that cannot see what it needs fails rather than
saying so, which reads as a broken backend:

| Family | Needs | What happens without it |
|---|---|---|
| `table_adjacent_up/down/left/right`, and the other cell-relationship rules | an HTML `<table>` in the markdown | always fails, even when the table is correct |
| `unexpected_word`, `missing_word`, `too_many_word_occurence` and the sentence equivalents | a `bag_of_word` built with the publisher's own tokenizer | the bag is wrong, so the verdict is noise |

The first one bites in practice. `pymupdf` writes GFM pipe tables, so cell-relationship rules can
never pass against it, while the plain `table` rule works on both shapes. Ask a backend for HTML
tables (`outputs.tables = "html"`) before relying on the relationship rules, and prefer `table`
when you do not know what your backend emits.

For the bag rules, build the bag with the publisher's own function rather than splitting on
spaces, because its tokenizer lowercases, strips markdown, drops one-character tokens and folds
accents:

```python
from parse_bench.evaluation.metrics.parse.rules_bag import WordBagRule
bag = dict(WordBagRule._extract_normalized_words_static(expected_text, include_table_cells=True))
```

One known gap: `tables_num_rows` did not pass in testing against either table shape, with any row
count. Treat it as unproven rather than as a measurement.

Two behaviours worth knowing before you rely on this. A case that declares `rules` without the
extra installed raises and names the install command, rather than scoring zero, because a silent
zero reads as a failing backend. And `overall` is the unweighted mean across every dimension a
case names, rules included, so a case asserting both kinds gets one mean over both.

### Recipes

**Rank under a compliance policy.**
```bash
echo '{"require_local": true}' > local.json
uv run openreading leaderboard mydata --backends pymupdf,tesseract,reducto --policy local.json
```
```text
   3  reducto                           —     0/3     0.3750       3  
…
  first: winner=pymupdf  (pymupdf=1.00, tesseract=0.50, reducto=—)
```
A backend the policy refuses is counted as an error in its own tally and excluded from its mean,
never silently skipped. `scored 0/3` with three errors is how you read that reducto never ran.
That is a different row from a backend that ran and scored zero. The `cost/doc` column is a model
rather than a price anyone quoted. It takes the low end of the backend's declared per-page range and
multiplies it by a fixed assumption of 25 pages a document. That is why reducto reads `0.3750` for a
rate of `$0.015` a page. The figure drops the high end of the range, which is four times the low end
on reducto and wider still on others. It is also wrong by the ratio of your real average page count
to 25. Price a corpus from the [cost and limits
table](../adapters/README.md#what-each-backend-charges-and-the-ceilings-on-one-request) and your own
page counts instead. `--all-ready` replaces `--backends` with every configured backend. Every
backend makes a real call per case, so with N backends and M cases a hosted key bills N × M calls.

**Tune a strategy's gates from the sample (the calibrate bridge).** A strategy is a named plan over
one or more backends, and each backend it tries is one rung. A gate is the threshold that decides
whether the strategy escalates from one rung to the next. `calibrate` runs the strategy's first
rung over the dataset, scores it with these scorers, and proposes `escalate_if:` thresholds. It
never edits the file. Write the gate as a plain numeric threshold at the top of `escalate_if`,
because that is the only shape `calibrate` can sweep.
```bash
cat > openreading.yaml <<'YAML'
version: 1
strategies:
  main:
    steps:
      - backend: pymupdf
        escalate_if:
          chars_per_page_below: 200   # flat and numeric, so calibrate can sweep it
      - tesseract
YAML
uv run openreading calibrate mydata --strategy main --target-escalation 0.34 \
  | jq -c '{n_docs, n_scored, points: (.sweeps[0].points[0:3]), recommended}'
```
```json
{"n_docs":3,"n_scored":3,"points":[{"threshold":0.0,"escalation_rate":0.0,"cost_per_doc":0.0,"scorer_agreement":0.6667},{"threshold":100.0,"escalation_rate":0.0,"cost_per_doc":0.0,"scorer_agreement":0.6667},{"threshold":200.0,"escalation_rate":1.0,"cost_per_doc":0.0,"scorer_agreement":0.3333}],"recommended":{"escalate_if":{"chars_per_page_below":0.0}}}
```
Sample size is not what fills `sweeps`. The gate's shape is. The same strategy written in Plain, as
`try: [pymupdf, tesseract]` with `escalate_when: looks_bad`, returns `"sweeps": []` on three
documents and on three thousand. That word compiles to an `any_of` block, and `calibrate` sweeps
only the flat numeric predicates. [Strategies](../strategies/README.md) step 8 lists which
predicates qualify and how to read the points and the recommendation.

**Score a compare against a golden.** A golden is a file of expected output. The golden below is the
`first` case's `expected` block, which describes `sample.pdf`. Parse that document into the two
envelopes rather than reusing envelopes another page saved from a different document.
```bash
uv run openreading parse sample.pdf --backend pymupdf > pymupdf.json
uv run openreading parse sample.pdf --backend tesseract > tesseract.json
jq '.expected' mydata/first/case.json > golden.json
uv run openreading compare pymupdf.json tesseract.json --truth golden.json --format json | jq -c .truth
```
```json
{"dimensions":["tables","text_contains"],"by_subject":{"pymupdf":{"overall":1.0,"dimensions":{"text_contains":1.0,"table_cell_accuracy":1.0}},"tesseract":{"overall":0.5,"dimensions":{"text_contains":1.0,"table_cell_accuracy":0.0}}}}
```
The golden has the `expected` shape, and scores land in the report's `truth` section
([Compare](../comparison/README.md)).

**Score from Python.**
```python
import json, openreading
from openreading.evals import score, run_dataset
from openreading.adapters.registry import make_adapter
resp = openreading.run("sample.pdf", backend="pymupdf")
print(score(resp, json.load(open("golden.json"))))   # {'overall': 1.0, 'dimensions': {'text_contains': 1.0, 'table_cell_accuracy': 1.0}}
print(run_dataset(make_adapter("tesseract"), "mydata").summary())   # backend=tesseract  mean_overall=0.500  cases=3  errors=0 …
```

## How it decides

- There is one scoring path. `leaderboard` and `calibrate` call the same `run_case` and `score` a
  plain dataset run uses, so two harnesses can never disagree about one document. See
  `openreading.evals.leaderboard`.
- The compliance gate runs before every case, and a refusal is that backend's scored error. Without
  this rule a benchmark could send protected health information to a backend the policy forbids.
  See `openreading.evals.runner.run_case`.
- Unscored is not zero. A case naming no recognized dimension scores `None` and leaves the mean, so
  a precise-looking number never reports a measurement that did not happen. See `scorers.score`
  and `DatasetReport.mean_overall`.
- A mean is reported, and a sample size is not judged. The harness accepts one case as readily as
  five hundred, warns at neither, and reports no spread or interval. Deciding that a gap is real
  rather than one document's opinion is your job, and step 4 is the order to do it in.
- Absence is opt in, and its verdict is the publisher's. `text_contains` and the table scorer
  count what you asked for and cannot see what the backend added, so a case that declares no
  `rules` still cannot catch invented content. A case that declares them hands them to
  ParseBench's own engine, unchanged, because a rule this repo reimplemented could disagree with
  the same rule under `benchmark run`. See `openreading.evals.rules`.
- Rules are a dimension, not a second path. They are scored inside the same `score` call as every
  other dimension, gated on one key, so `leaderboard` and `calibrate` reach them through the one
  `run_case` they already use. Without that, two harnesses could disagree about one document. A
  case with no ParseBench installed raises rather than scoring zero, because a silent zero reads
  as a failing backend and a silent omission reads as passing rules.
- A public benchmark result is read back, never recomputed. `benchmark run` and `benchmark report`
  print the publisher's own numbers out of its own report files, so the terminal table and the
  publisher's dashboard cannot disagree about a document. The one thing added is the join from a
  pipeline name to the target that produced it, which the publisher does not record. See
  `openreading.evals.report`.
- A public benchmark run is small until you say otherwise. `benchmark run` touches two documents
  by default and prices the rest in pages before it spends, because the failure it avoids is a
  command typed once that bills a full corpus across several hosted backends. See
  `openreading.evals.subset` and `openreading.evals.preflight`.
- Ties break on backend id, so a rerun is byte-identical and rank order never depends on the order
  you typed. Scores never feed the router, so a benchmark never quietly becomes routing policy. See
  `openreading.evals.leaderboard`.

> [!IMPORTANT]
> Labeled datasets, meaning ground truth over real documents, never land in this repository.
> `tests/test_evals_benchmark_only.py` fails `make verify` if a second case or a real document
> appears under `evals/sample/`. Keep your data outside the repo and pass its path.

## Reference

- `uv run python -m pydoc openreading.evals.dataset` documents the `case.json` shape and the four
  input forms.
- `uv run python -m pydoc openreading.evals.scorers` describes the three scorers and the five
  dimensions.
- `uv run python -m pydoc openreading.evals.leaderboard` states what the leaderboard never does.
- `uv run openreading benchmark run --help` for every flag that changes what a public run
  touches or costs, and `uv run openreading leaderboard --help` and
  `uv run openreading calibrate --help` for the labeled-dataset path.
- `uv run python -m pydoc openreading.evals.subset` for how a corpus is cut down,
  `uv run python -m pydoc openreading.evals.preflight` for how the run is priced, and
  `uv run python -m pydoc openreading.evals.report` for how a finished run is read back.
- `src/openreading/schemas/leaderboard-report.v0.1.json` is described in
  [JSON Schemas](../schemas/README.md), and `scripts/leaderboard_smoke.py` is the `make verify`
  smoke.

## Not built yet

- Corpus-level evals, `--truth` per document of a batch (`openreading.batch` docstring, "Deferred").
- Trend history across invocations. Each call produces one report and keeps no state
  (`openreading.evals.leaderboard` docstring).
- A labeled corpus, which never lands here by rule (`tests/test_evals_benchmark_only.py`) and is
  not a gap to file.
- Detecting content you did NOT predict. `text_absent` and an `absent` rule catch a string you
  named; nothing yet catches arbitrary invention, which needs the publisher's bag rules and an
  explicit claim that a labeled `text` is the whole document. Increment 2 of
  [the product spec](../../../product/specs/hallucination-detection.product-spec.md).
- Publisher-comparable numbers over your own corpus, which is increment 3 of the same spec and
  may never be worth building. Its open question is whether anyone needs comparability rather
  than absence detection.

## See also

- [Docs home](../README.md)
- [Compare](../comparison/README.md) for `--truth`, and for turning two backends' disagreements
  into the cases worth labeling first.
- [Strategies](../strategies/README.md) step 8 for reading a `calibrate` sweep and its
  recommendation.
- [Batch runs](../batch/README.md) · [Backend adapters](../adapters/README.md) · [JSON
  Schemas](../schemas/README.md)

<sub>[Docs home](../README.md) · [← The channel contract](../derive/README.md) · [JSON Schemas →](../schemas/README.md)</sub>
