# Compare: what differs between two backends' outputs

<sub>[Docs home](../README.md) · [← Strategies](../strategies/README.md) · [Batch runs →](../batch/README.md)</sub>

> **In one sentence.** Hand `compare` two or more saved responses and it names exactly where the
> backends disagree, as a verdict plus findings, without running or billing anything.

## What this gives you

You ran two backends on the same invoice, and the totals in the two outputs differ. A backend is
one parser, such as the local `pymupdf` library or a hosted API. Both runs reported `succeeded`, so
neither output announces itself as the wrong one. You need to see exactly what differs, without
reading two 40 KB files by hand.

`openreading compare pymupdf.json tesseract.json` reads the two envelopes and prints a scoreboard,
a verdict, and findings. An envelope is the one JSON shape every backend returns. The scoreboard is
one row per compared output, with its pages, blocks, characters, fields, cost and time. A verdict
is one word over the whole comparison, and it is `equivalent`, `mixed`, or `divergent`. A finding
is one specific difference, named by a code and sorted by severity. For example,
`table_shape_mismatch` means the backends found different numbers of tables in the document.

Compare picks no winner unless you name one output as the baseline or bring a golden file of
expected values. If your question is which backend is correct rather than where the two differ,
`leaderboard` answers it ([Evals](../evals/README.md)). The recipe "Turn two backends'
disagreements into a labeled evals dataset" below is the cheapest way to reach it. You need two
saved envelopes, such as the `pymupdf.json` and `tesseract.json` step 1 below writes, and no key.

## Mental model

Because every envelope has the same shape, comparing two of them is a pure function of their
contents. Pure means the same inputs always produce a byte-identical report. No backend runs while
a report is computed, so it costs nothing and needs no key. A subject is one response being
compared. Subjects arrive from three sources, and every source ends in the same pure step.

The [response JSON guide](../schemas/README.md#understanding-the-response-json) explains the content paths inside each input envelope.
The comparison output is a separate report, so read its findings instead of looking for `document.text`.

<!-- diagram:src-openreading-comparison-1 -->
<p align="center"><a href="../../../assets/diagrams/src-openreading-comparison-1.svg"><img src="../../../assets/diagrams/src-openreading-comparison-1.svg" alt="Comparison accepts saved envelopes, a document fanned out to named backends, or candidates kept by a strategy. Subjects align by page and block, then compare facts, typed fields, text, and blocks. The report combines a headline verdict and severity-sorted findings in JSON, table, Markdown, diff, or diffs format." /></a></p>

<details>
<summary>Logical flow (Mermaid)</summary>

```mermaid
%%{init: {"theme":"base","fontFamily":"Arial","deterministicIds":true,"deterministicIDSeed":"openreading","htmlLabels":false,"themeVariables":{"fontFamily":"Arial","fontSize":"17px","lineColor":"#8194ad","textColor":"#183451","primaryTextColor":"#183451","primaryColor":"#edf3fc","primaryBorderColor":"#9db4d0","edgeLabelBackground":"#ffffff","clusterBkg":"#f5f8fc","clusterBorder":"#d7e1ee","titleColor":"#183451","actorBkg":"#edf3fc","actorBorder":"#9db4d0","actorTextColor":"#183451","actorLineColor":"#9db4d0","signalColor":"#527095","signalTextColor":"#183451","labelBoxBkgColor":"#fff4de","labelBoxBorderColor":"#c6953a","labelTextColor":"#70501b","loopTextColor":"#527095","noteBkgColor":"#edf3fc","noteBorderColor":"#9db4d0","noteTextColor":"#183451","sequenceNumberColor":"#ffffff","activationBkgColor":"#e7f3ee","activationBorderColor":"#679780"},"flowchart":{"curve":"monotoneY","nodeSpacing":32,"rankSpacing":48,"padding":18,"useMaxWidth":true},"sequence":{"useMaxWidth":true,"actorMargin":65,"messageMargin":38,"mirrorActors":false}}}%%
flowchart TD
  subgraph IN["three sources"]
    direction LR
    A[/"a.json, b.json"/]:::src
    B[/"doc.pdf, fan-out"/]:::src
    C[/"run.json, kept candidates"/]:::src
  end
  A --> S["subjects"]:::work
  B --> S
  C --> S
  S --> AL["align pages, then blocks"]:::work
  AL --> F["four dimensions<br>facts, fields, text, blocks"]:::work
  F --> H["headline verdict"]:::good
  F --> G["findings, severity sorted"]:::out
  H --> R(["report<br>json, table, md, diff, diffs"]):::hero
  G --> R
  classDef src fill:#f5f8fc,stroke:#a7b9d0,stroke-width:1px,color:#29445f;
  classDef work fill:#edf3fc,stroke:#9db4d0,stroke-width:1px,color:#183451;
  classDef gate fill:#fff4de,stroke:#c6953a,stroke-width:1px,color:#70501b;
  classDef good fill:#e7f3ee,stroke:#679780,stroke-width:1px,color:#245740;
  classDef bad fill:#fbeeee,stroke:#c78686,stroke-width:1px,color:#803d3d;
  classDef store fill:#e7f3ee,stroke:#679780,stroke-width:1px,color:#245740;
  classDef out fill:#edf3fc,stroke:#9db4d0,stroke-width:1px,color:#183451;
  classDef hero fill:#164bc5,stroke:#164bc5,stroke-width:1px,color:#ffffff;
  linkStyle default stroke-width:1.4px;
```

</details>

Fan-out, written `compare doc.pdf --backends a,b`, runs each backend once in turn, and it alone
spends money. Each subject carries a `source`: `file` for a saved envelope, `fanout` for a backend
fan-out ran, and `candidate` for a branch a strategy kept. A strategy is a named plan over
backends, and `parse --keep-candidates` keeps its losing branches. `compare --from` reads those
kept branches as subjects.

Compare looks at four dimensions: run facts for the scoreboard, typed fields, text, and blocks with
tables. A typed field is one key and value a backend extracted, such as an invoice total. A backend
that cannot produce one dimension is marked `not_capable`, and it is never blamed for missing it.
The human-readable formats show only differences by default, because agreement is noise in a delta.

## Walkthrough

Start from the root README's sample document and two local parses.

### 1. Parse twice, then compare as a table

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
uv run openreading parse sample.pdf --backend pymupdf > pymupdf.json
uv run openreading parse sample.pdf --backend tesseract > tesseract.json
```

**You should see** two kinds of advice from PyMuPDF, and neither one is an error. The build
command and the tesseract parse print `warning: The fitz API is deprecated`, and the pymupdf
parse prints a line about `pymupdf_layout`. Both parses send that advice to stderr, so the JSON
they write is unaffected. Compare the two files. Two saved files are the simplest source, and the
other two reduce to it.

```bash
uv run openreading compare pymupdf.json tesseract.json --format table
```
```text
COMPARE — 2 subjects (pairwise)

SUBJECT             TYPE             PAGES BLOCKS  CHARS FIELDS    TIME
pymupdf             oss_library          2      9    336      0       -
tesseract           oss_library          2      9    336      0       -

CONTENT: MIXED  (text:agree  table_cells:diverge)

text similarity: 0.93
block alignment: text_first/v1  unaligned=0.06

FINDINGS (4)
  [ warn] table_shape_mismatch  {pymupdf, tesseract}  — table counts differ: {'pymupdf': 1, 'tesseract': 0}
  [ warn] type_conflict p1  {pymupdf, tesseract}  — pymupdf calls it title, tesseract calls it text  "OpenReading Test Document"
  …
WARNINGS
  confidence_unavailable [pymupdf]
```

**You should see** the same text on both sides and the table kept only by pymupdf. The last
line says pymupdf reports no confidence, so it is not faulted for missing confidences. JSON is
the default format, and every report is schema-validated before it is printed.

`text similarity: 0.93` is a `difflib` ratio over the two texts split into words, so it counts
words that line up in the same order. Moving a paragraph lowers it even when every word survives.
It is the one similarity number that also reaches `--format json`, rounded there to four places
under `text.matrix`. The next step prints a second, different number on the same pair.

### 2. Read the JSON, the diff, and the diffs

```bash
uv run openreading compare pymupdf.json tesseract.json --format json > report.json
uv run python -c "import json; r = json.load(open('report.json')); print(list(r), r['headline']['verdict'])"
# ['schema_version', 'mode', 'subjects', 'alignment', 'headline', 'fields', 'text', 'blocks', 'findings', 'warnings'] mixed
```

The `diff` format needs exactly two subjects and prints a git-style text diff. The `diffs` format
prints a four-section view. Read both.

```bash
uv run openreading compare pymupdf.json tesseract.json --format diff
uv run openreading compare pymupdf.json tesseract.json --format diffs
```
```diff
--- pymupdf
+++ tesseract
@@ -1,20 +1,10 @@
 OpenReading Test Document
 …
-Region
-Units
…
+Region Units Revenue
+North 120 4400
```
```text
DIFF — pymupdf vs tesseract   (2 page(s))

① CONTENT — real text/values either side missed
   ✔ EQUIVALENT   content shared by all: 1.00  ·  0 real misses
② TABLES — 1 table(s)
   table counts differ: {'pymupdf': 1, 'tesseract': 0}
…
④ GRANULARITY
   pymupdf 9 blocks  ·  tesseract 9 blocks
```

**You should see** pymupdf emitting one cell per line and tesseract one row per line in the
diff, then `EQUIVALENT` under CONTENT. Neither backend lost a word, but tesseract lost the grid.
`diffs` (plural) separates content from packaging, two axes a raw finding list mixes together.

`content shared by all: 1.00` is a different measurement from step 1's `0.93`, on the same two
files. It is the share of the vocabulary both sides have, counted as sets of words. Order and
repetition are thrown away before the count, which is why it reads 1.00 where the ordered number
reads 0.93. That blindness matters, because reading order is exactly what a backend that flattens
a table gets wrong. A backend that returns every correct word in scrambled order still scores
1.00 here. The renderer computes this number for the `diffs` view only, and no `--format json`
field carries it. A script that wants a similarity therefore reads `text.matrix` and gets the
ordered one.

### 3. Fan out, save, and re-render

Fan-out runs the backends for you and saves each response, so you can replay the comparison later.
Then re-render step 2's `report.json` with `explain`, which recognizes a comparison report by its
shape.

```bash
uv run openreading compare sample.pdf --backends pymupdf,tesseract --save-dir out/ --format table
ls out/
uv run openreading explain report.json
```

**You should see** the same table twice, with `pymupdf.json` and `tesseract.json` listed in
between. `compare out/pymupdf.json out/tesseract.json` now reproduces the report offline.
`--all-ready` fans out over every ready backend. `--format md` renders the same view as markdown.

## Recipes

**Score both subjects against a golden file.**
```bash
uv run python -c "import json; c = json.load(open('src/openreading/evals/sample/loan_page1/case.json')); json.dump(c['expected'], open('golden.json', 'w'))"
uv run openreading compare pymupdf.json tesseract.json --truth golden.json --format json | uv run python -c "import json, sys; print(json.load(sys.stdin)['truth']['by_subject'])"
# {'pymupdf': {'overall': 1.0, 'dimensions': {'text_contains': 1.0, 'table_cell_accuracy': 1.0}}, 'tesseract': {'overall': 0.5, 'dimensions': {'text_contains': 1.0, 'table_cell_accuracy': 0.0}}}
```
The `case.json` path is relative to your clone root, so run this recipe from there. Evals is the
benchmark harness in this repo, and it scores a parse against known-correct values. The golden
file is the `expected` object of an evals case ([Evals](../evals/README.md)), and the evals scorer
scores each subject against it. The `table` renderer prints no truth section, so read JSON.

**Sign every field delta against one backend.**
```bash
uv run openreading compare pymupdf.json tesseract.json --baseline pymupdf --format json | uv run python -c "import json, sys; print(json.load(sys.stdin)['baseline'])"
# {'baseline': 'pymupdf', 'fields': []}
```
Each typed field becomes match, differ, missing, or extra relative to the baseline. The sample
has no typed fields, so the list is empty. If you pass a file path to `--baseline` instead of a
subject label, that file is added as a subject and becomes the baseline.

**Check whether a backend drifts between two runs.**
```bash
uv run openreading parse sample.pdf --backend pymupdf > pymupdf2.json
uv run openreading compare pymupdf.json tesseract.json pymupdf2.json --format table | head -6
# SUBJECT … pymupdf … tesseract … pymupdf#2
```
Subjects may repeat a backend. The second pymupdf becomes `pymupdf#2`. At three or more
subjects the JSON gains a `consensus` section with the majority value per field and the outliers.

**Compare what a strategy discarded.**
Save this YAML as `openreading.yaml` in the working directory, then run both commands.
```yaml
version: 1
strategies:
  duel:
    compare: [pymupdf, tesseract]    # run both, keep the better one
```
```bash
uv run openreading parse sample.pdf --strategy duel --keep-candidates > strat.json
uv run openreading compare --from strat.json --format table | head -5
# SUBJECT … pymupdf … tesseract
```
Without `--keep-candidates` the loser is dropped and `compare --from` exits 5 with the fix in the
message. Both subjects carry `source: candidate`, which `--format json` shows and the table does
not. Branches that ran side by side, as under `compare:`, are kept. A cascade's earlier step is
not kept once a later step supersedes it ([Strategies](../strategies/README.md)).

**Compare two batch runs of the same folder.**
```bash
mkdir -p docs && cp sample.pdf docs/invoice.pdf && cp sample.pdf docs/letter.pdf
uv run openreading parse docs/ --backend pymupdf > runA.json
uv run openreading parse docs/ --backend tesseract > runB.json
uv run openreading compare runA.json runB.json --format table
```
```text
CORPUS COMPARE: runA vs runB
2 document(s): 0 equivalent · 0 divergent · 2 mixed · 0 unpaired

  [     mixed] invoice.pdf
  …
```
Each parse prints one `[n/2] <name> succeeded` line per document on stderr, and the batch result
goes to stdout. When every subject is a batch result, documents pair by `relpath`, then
`filename`, then `sha256`. A `relpath` is the path relative to the folder you passed. A document
in only one run is `unpaired`, not an error. `--format diffs` prints the values one run captured
and the other missed. `--format diff` is refused (exit 2).

**Turn two backends' disagreements into a labeled evals dataset.** Runs on the two batch reports
from the recipe above. This recipe needs `jq`, a command-line JSON filter you install from your
package manager. Without `jq`, replace the second line with
`uv run python -c "import json,sys; [print(d['source']['relpath']) for d in json.load(sys.stdin)['documents'] if d['verdict'] != 'equivalent']"`.
```bash
uv run openreading compare runA.json runB.json --format json \
  | jq -r '.documents[] | select(.verdict != "equivalent") | .source.relpath'
```
```text
invoice.pdf
letter.pdf
```
Each name is a document the two backends read differently, so it is a document where at most one
of them is right. Copy those files into `mydata/<name>/input.pdf`, write a `case.json` beside each
one, and fill `expected` by reading the document yourself ([Evals](../evals/README.md)). Label
these before the documents both backends agreed on, because agreement already tells you the two
would rank the same there.

**Branch on a verdict in Python.**
```python
import openreading

report = openreading.compare(["pymupdf.json", "tesseract.json"])   # dicts or paths
codes = {f["code"] for f in report["findings"]}
print(report["headline"]["verdict"])                                 # mixed
if report["headline"]["verdict"] == "equivalent":
    print("any backend will do")
elif "table_shape_mismatch" in codes:
    print("pick the backend that kept the table")                    # printed
```
There is no fan-out in Python. Call `openreading.run()` per backend yourself, then compare.

> [!WARNING]
> Fan-out over hosted backends bills once per backend. Files and `--from` never bill.

## How it decides

These rules are why a report is safe to run in a loop and safe to trust. Each rule names the
failure it prevents. The full list is the `Laws` section of
`uv run python -m pydoc openreading.comparison`.

- The comparison itself is pure. Over saved files and `--from` it never runs, retries, or bills.
  Fan-out is the one form that spends. `compare doc.pdf --backends a,b` runs each backend you named
  and bills every hosted one. A report costs money exactly when you asked for a fan-out.
- A report never feeds routing or `pick: best`. A feedback loop could widen which backends run.
- The same inputs produce the same report bytes. A report has no timestamps, randomness, or LLM
  calls, so drift detection over reports is sound. This is a property of compare alone, and not of
  the envelopes you feed it. A `parse --backend` response is byte-stable too. A `parse --strategy`
  response and a batch result carry timing that moves between runs. Hashing one of those to detect
  change gives you a false positive every time
  ([JSON Schemas](../schemas/README.md#clocks-and-byte-stability)).
- A backend that cannot produce a dimension is `not_capable`, never `missed`. Blaming pymupdf
  for missing confidences would make every report about it wrong.
- There is one set of metrics. `--truth` calls the evals scorer instead of a second implementation.
- A generative backend, such as `anthropic-claude`, caps content findings at `info`, because its
  run-to-run drift looks like a real difference.
- The exit codes are `0` for a report, `2` for misuse, `3` when fan-out cannot run, and `5` for
  invalid input or no candidates. Anything else exits `1`. The details are in
  `uv run python -m pydoc openreading.cli`, section `compare`.

### Verdict vocabulary

Source: `src/openreading/schemas/comparison-report.v0.2.json` (`headline.verdict`,
`HeadlineChannel.agreement`, `FieldRow.verdict`) and `corpus-report.v0.1.json` (`verdict`). Live
truth: `uv run python -c "import openreading.schemas as s; r = s.comparison_report_schema();
print(r['properties']['headline']['properties']['verdict']['enum'],
r['\$defs']['FieldRow']['properties']['verdict']['enum'])"`. If this table and that output disagree,
the output is right. Fix the table.

A channel is one named kind of content in the envelope. The headline scores three of them: `text`,
`typed_fields`, and `table_cells`. A channel no subject produced is left out of the verdict.

| Word | Where | Meaning |
|---|---|---|
| `equivalent` | `headline.verdict`, corpus document | every compared channel agrees |
| `mixed` | `headline.verdict`, corpus document | anything else: channels split between agree and diverge, or any channel is `partial` |
| `divergent` | `headline.verdict`, corpus document | every compared channel diverges |
| `unpaired` | corpus document only | present in some batch runs, not all |
| `agree` / `partial` / `diverge` | `headline.channels.<name>.agreement` | one word per channel: text, typed fields, table cells |
| `agree` | `fields.rows[].verdict` | every capable subject produced an equivalent value |
| `partial` | `fields.rows[].verdict` | the values present agree, but a capable subject has none |
| `disagree` | `fields.rows[].verdict` | the two values match at no tier of the equivalence ladder (`exact`, `normalized`, `money`, `number`, `date`) |
| `unique` | `fields.rows[].verdict` | only one subject produced the key |
| `not_capable` | `fields.rows[].verdict` | that backend cannot produce typed fields at all |

### Findings taxonomy

Source: `src/openreading/schemas/comparison-report.v0.2.json` (`Finding.code`, `Finding.severity`),
thresholds in `comparison/text.py`, `blocks.py`, `align.py`, `report.py`. Live truth: `uv run python
-c "import openreading.schemas as s;
print(s.comparison_report_schema()['\$defs']['Finding']['properties']['code']['enum'])"`. If this
table and that output disagree, the output is right. Fix the table.

The `Axis` column says where a finding is computed. `tables` and `pages` both sit inside the blocks
dimension, and `facts` covers the scoreboard numbers.

| Code | Axis | What it means |
|---|---|---|
| `field_value_conflict` | fields | the same key, values not equivalent |
| `field_missed` | fields | a capable subject has no value for the key |
| `text_divergence` | text | pairwise text similarity below 0.5 |
| `block_missed` | blocks | some capable subjects have the block, others do not |
| `block_unique` | blocks | one subject alone has the block |
| `structure` | blocks | same content, different packaging (not a loss) |
| `type_conflict` | blocks | one says `title`, another `text` |
| `position_conflict` | blocks | text matched, bbox overlap below 0.3 |
| `table_shape_mismatch` | tables | table counts or grid shapes differ |
| `confidence_gap` | blocks | matched blocks, confidences 0.2 or more apart, on two backends' own scales ([why that is not a ranking](../derive/README.md#a-confidence-is-comparable-inside-one-backend-not-across-two)) |
| `page_count_mismatch` | pages | subjects disagree on page count |
| `empty_output` | facts | a subject produced no text, blocks, or fields |

## Reference

- `uv run python -m pydoc openreading.comparison` has the sections `Acquisition`, `The four
  dimensions`, `Alignment`, `Stances`, `The report`, and `Corpus compare`. The equivalence ladder
  is `uv run python -m pydoc openreading.comparison.equivalence`.
- `uv run openreading compare --help` and `uv run openreading explain --help`.
- `src/openreading/schemas/comparison-report.v0.2.json`, `corpus-report.v0.1.json`, and
  `uv run python -m pydoc openreading.server` for `POST /v1/compare`.

## Not built yet

From the `openreading.comparison` docstring (`Deliberately deferred`):

- Threshold flags (`TAU_TEXT`, `IOU_MIN` stay module constants), chunk-level comparison, and
  page-provenance-aware comparison of `granularity: page` runs.
- A strategy YAML that emits a comparison report. The `compare:` node in the recipe above runs
  backends and keeps one winner, and it never writes a report.
- Streaming or incremental comparison, and cross-document comparison (same backend, different
  documents), which is evals territory.

## See also

- [Docs home](../README.md)
- [Strategies](../strategies/README.md): `--keep-candidates` and the parallel constructs.
- [Batch runs](../batch/README.md): the batch-result envelope corpus compare consumes.
- [Evals](../evals/README.md): the scorer behind `--truth`, and `leaderboard`, the verb that does
  rank backends against labels you wrote.

<sub>[Docs home](../README.md) · [← Strategies](../strategies/README.md) · [Batch runs →](../batch/README.md)</sub>
