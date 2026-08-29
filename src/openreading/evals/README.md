# Evals — score backends on your own documents

<sub>[Docs home](../README.md) · [← The channel contract](../derive/README.md) · [JSON Schemas →](../schemas/README.md)</sub>

> **In one sentence.** Put a `case.json` with the expected text, tables, or fields next to each
> document, and `leaderboard` ranks any set of backends on those documents with measured scores.

## What this gives you

A benchmark harness: three scorers over the response envelope (the response JSON), a runner that
drives any backend over a dataset directory, and a leaderboard that ranks two or more backends on
one dataset. Every score is measured on your documents, never quoted from a vendor. The repo ships
one synthetic case so the harness proves itself offline. Labeled datasets never land here.

## Mental model

A dataset is a directory of case directories. Each holds a `case.json`: the input plus `expected`.
The runner sends each case to one backend through the same compliance gate a normal run uses. It
then scores only the dimensions `expected` names. The leaderboard repeats that for every backend
and ranks by mean score. `calibrate` and `compare --truth` reuse the same scorers. Nothing feeds
back into routing; the number is evidence you read.

## Walkthrough

### 1. Rank two backends on the bundled case

```bash
uv run openreading leaderboard src/openreading/evals/sample --backends pymupdf,tesseract
```
```text
dataset: src/openreading/evals/sample  (1 case(s): loan_page1)

rank  backend                        mean   cost/doc  errors  dimensions
   1  pymupdf                       1.000     0.0000       0  text_contains=1.00 table_cell_accuracy=1.00
   2  tesseract                     0.500     0.0000       0  text_contains=1.00 table_cell_accuracy=0.00

per-case winner:
  loan_page1: winner=pymupdf  (pymupdf=1.00, tesseract=0.50)
```

**You should see** the dataset's identity above the ranking: a verdict on these documents, not a
universal one. Both found the text; OCR lost the table grid. One backend alone is exit 2.
`--format json` prints the schema-valid `leaderboard-report.v0.1`: `dataset {path, case_count,
case_names}`, `backends[] {backend_id, rank, mean_score, n_cases, n_scored, errors, cost_per_doc,
non_deterministic, dimensions}`, `cases[] {name, winner, scores}`.

### 2. Read the bundled case

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
`text_contains`, `markdown`, `typed_fields`, and `tables`; only what it names is scored.

### 3. Write your own dataset

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
mkdir -p mydata/first && cp sample.pdf mydata/first/input.pdf
cat > mydata/first/case.json <<'JSON'
{
  "name": "first",
  "input": {"path": "input.pdf"},
  "expected": {
    "text_contains": ["OpenReading Test Document", "twelve points"],
    "tables": [[["Region", "Units", "Revenue"], ["North", "120", "4400"]]]
  }
}
JSON
uv run openreading leaderboard mydata --backends pymupdf,tesseract
```
```text
dataset: mydata  (1 case(s): first)
…
   1  pymupdf                       1.000     0.0000       0  text_contains=1.00 table_cell_accuracy=1.00
   2  tesseract                     0.500     0.0000       0  text_contains=1.00 table_cell_accuracy=0.00
```

**You should see** the same ranking on your own file. Check: `ls mydata/*/case.json`. A case with
`"expected": {}` is unscored, prints as `—`, and never counts as a zero. A backend with no scored
case still shows `0.000` in the `mean` column; read `n_scored` in `--format json` before trusting
a mean.

## Recipes

**Rank under a compliance policy.**
```bash
echo '{"require_local": true}' > local.json
uv run openreading leaderboard mydata --backends pymupdf,tesseract,reducto --policy local.json
```
```text
   3  reducto                       0.000     0.3750       1
…
  first: winner=pymupdf  (pymupdf=1.00, tesseract=0.50, reducto=—)
```
A backend the policy refuses is an error in its own tally, excluded from its mean, never a silent
skip. The cost column is the backend's declared price. `--all-ready` replaces `--backends` with
every configured backend. Every backend makes a real call per case, so hosted keys bill N × M calls.

**Tune a strategy's gates from the sample (the calibrate bridge).**
```bash
printf 'version: 1\nstrategies:\n  main:\n    try: [pymupdf, tesseract]\n' > openreading.yaml
uv run openreading calibrate mydata --strategy main --target-escalation 0.15
```
```json
{ "strategy": "main", "n_docs": 1, "n_scored": 1, "rung1_backend": "pymupdf", "rung2_backend": "tesseract",
  "target_escalation": 0.15, "max_cost_per_doc": null, "sweeps": [], "recommended": {} }
```
`calibrate` runs the strategy's first rung (backend) over the dataset, scores it with these
scorers, and proposes `escalate_if:` thresholds. It never edits the file. One case is too few to
sweep, so `sweeps` is empty here. See [Strategies](../strategies/README.md).

**Score a compare against a golden.** A golden is a file of expected output. `pymupdf.json` and
`tesseract.json` exist from the root README.
```bash
jq '.expected' mydata/first/case.json > golden.json
uv run openreading compare pymupdf.json tesseract.json --truth golden.json --format json | jq -c .truth
```
```json
{"dimensions":["tables","text_contains"],"by_subject":{"pymupdf":{"overall":1.0,"dimensions":{"text_contains":1.0,"table_cell_accuracy":1.0}},"tesseract":{"overall":0.5,"dimensions":{"text_contains":1.0,"table_cell_accuracy":0.0}}}}
```
The golden is the `expected` shape; scores land in the report's `truth` section
([Compare](../comparison/README.md)).

**Score from Python.**
```python
import json, openreading
from openreading.evals import score, run_dataset
from openreading.adapters.registry import make_adapter
resp = openreading.run("sample.pdf", backend="pymupdf")
print(score(resp, json.load(open("golden.json"))))   # {'overall': 1.0, 'dimensions': {'text_contains': 1.0, 'table_cell_accuracy': 1.0}}
print(run_dataset(make_adapter("tesseract"), "mydata").summary())   # backend=tesseract  mean_overall=0.500  cases=1  errors=0 …
```

## How it decides

- One scoring path. `leaderboard` and `calibrate` call the same `run_case` and `score` a plain
  dataset run uses. Avoids: two harnesses disagreeing about one document.
  `openreading.evals.leaderboard`.
- The compliance gate runs before every case; a refusal is that backend's scored error. Avoids: a
  benchmark that sends PHI to a backend the policy forbids. `openreading.evals.runner.run_case`.
- Unscored is not zero. A case naming no recognized dimension scores `None` and leaves the mean.
  Avoids: a precise-looking number that measured nothing. `scorers.score`,
  `DatasetReport.mean_overall`.
- Ties break on backend id, so a rerun is byte-identical; scores never feed the router. Avoids: rank
  order that depends on the order you typed; a benchmark quietly becoming routing policy.
  `openreading.evals.leaderboard`.

> [!IMPORTANT]
> Labeled datasets, meaning ground truth over real documents, never land in this repository.
> `tests/test_evals_benchmark_only.py` fails `make verify` if a second case or a real document
> appears under `evals/sample/`. Keep your data outside the repo and pass its path.

## Reference

- `uv run python -m pydoc openreading.evals.dataset` — the `case.json` shape and the four input
  forms.
- `uv run python -m pydoc openreading.evals.scorers` — the three scorers and the five dimensions.
- `uv run python -m pydoc openreading.evals.leaderboard` — what the leaderboard never does.
- `uv run openreading leaderboard --help`; `uv run openreading calibrate --help`.
- `src/openreading/schemas/leaderboard-report.v0.1.json` — [JSON Schemas](../schemas/README.md);
  `scripts/leaderboard_smoke.py` — the `make verify` smoke.

## Not built yet

- Corpus-level evals, `--truth` per document of a batch (`openreading.batch` docstring, "Deferred").
- Trend history across invocations: one call, one report, stateless
  (`openreading.evals.leaderboard` docstring).
- A labeled corpus: never here, by rule (`tests/test_evals_benchmark_only.py`); not a gap to file.

## See also

- [Docs home](../README.md)
- [Compare](../comparison/README.md) — `--truth`.
- [Strategies](../strategies/README.md) — `calibrate`.
- [Batch runs](../batch/README.md) · [Backend adapters](../adapters/README.md) · [JSON
  Schemas](../schemas/README.md)

<sub>[Docs home](../README.md) · [← The channel contract](../derive/README.md) · [JSON Schemas →](../schemas/README.md)</sub>
