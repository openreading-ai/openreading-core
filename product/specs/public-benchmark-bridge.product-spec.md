# Public benchmark bridge product specification

**Status:** Proposed

**Owner:** `openreading.evals`

**Removal condition:** Delete this file when the benchmark bridge ships. Move durable facts into
the owning module docstrings and `src/openreading/evals/README.md` in the same change.

## Problem

OpenReading can run one labeled dataset through its leaderboard, but public benchmarks use many
different layouts and scoring programs. A prospective user must currently build those integrations
before learning whether a backend or strategy works well.

The existing leaderboard also compares registered backends only. It cannot place an OpenReading
strategy beside its constituent backends on the same public documents. Published backend scores
therefore provide no evidence about the value added by routing, escalation, or parallel comparison.

No single corpus tests every OpenReading responsibility. Parsing quality, structured extraction,
strategy behavior, comparison usefulness, compliance, retries, and batch recovery require different
evidence.

## Outcome

A user runs a small, commercially usable public benchmark through OpenReading with one command.
The same command can compare named backends with named strategies while preserving official metrics.

A full run uses the same profile and produces reproducible artifacts. Every result identifies the
benchmark revision, source terms, target configuration, OpenReading version, official metrics, and
observed resource usage.

## Terminology

A backend is one document processor registered with OpenReading.

A strategy is a named orchestration plan that can run several backends for one document.

A channel is one named response component, such as text, tables, blocks, or typed fields.

A benchmark profile connects one external dataset and scorer to OpenReading without changing either
contract.

A target is one backend or strategy evaluated by a benchmark run.

A license lane states whether the documented dataset terms allow the default commercial evaluation
workflow.

## Users and decisions

An evaluator wants to answer four questions before adopting OpenReading:

1. Does OpenReading preserve a backend's quality on a recognized public benchmark?
2. Does a strategy improve quality or reliability over the backends it invokes?
3. What cost and latency accompany that improvement on the same documents?
4. Which document slices still fail, and do comparison reports expose the relevant disagreement?

An adapter author also wants a repeatable regression run after changing normalization behavior.

## Product surface

The CLI gains one command group with discover, inspect, prepare, estimate, and run operations.

```text
openreading benchmark list
openreading benchmark show parsebench
openreading benchmark prepare parsebench --preset smoke
openreading benchmark estimate parsebench --preset full --target backend:reducto
openreading benchmark run parsebench --preset smoke \
  --target backend:pymupdf --target backend:tesseract
openreading benchmark run extractbench --preset smoke \
  --target backend:nuextract --target strategy:fields --config openreading.yaml
```

`list` and `show` work without network access or optional benchmark packages. They display the data
license separately from the scorer license.

`prepare` downloads data through the benchmark publisher's supported mechanism. It writes only to a
caller-selected cache or OpenReading's documented user cache, never into the source tree.

`estimate` resolves case and page counts without invoking a backend. It reports call counts and uses
descriptor prices only when those estimates exist.

`run` resumes from complete per-target artifacts by default. `--rerun` explicitly replaces an
existing result for the same run identity.

The smoke preset is the default. A user must request the full preset explicitly because hosted runs
can create material charges.

Research-only profiles require `--allow-research-only`. The flag acknowledges terms but never
claims that a particular use is lawful.

Profiles with unclear terms require `--allow-unverified-terms`. This separate flag prevents a
research-use acknowledgement from becoming accidental approval for unknown terms.

## First supported profiles

### Default commercial lane

| Profile | Primary evidence | Initial status |
|---|---|---|
| `parsebench` | tables, charts, content faithfulness, semantic formatting, visual grounding | runnable |
| `extractbench` | schema-guided values, repeated records, citations, degraded captures | runnable |
| `doclaynet` | block classes and geometry | cataloged |
| `pubtables1m` | table detection, structure, spans, and content | cataloged |
| `cord` | receipt OCR and typed-field extraction | cataloged |

`cataloged` means discovery includes a verified source and terms. A later profile can add runnable
projection and scoring without changing the CLI contract.

### Restricted or unverified lane

| Profile | Primary evidence | Reason outside the default lane |
|---|---|---|
| `omnidocbench` | text, tables, formulas, layout, reading order | publisher limits documents to research use |
| `funsd` | noisy English forms and entity links | noncommercial dataset terms |
| `xfund` | multilingual forms and entity links | CC BY-NC-SA 4.0 terms |
| `readoc` | multi-page PDF to structured Markdown | dataset terms need explicit verification |
| `ohrbench` | downstream retrieval and generation after OCR | dataset terms need explicit verification |
| `olmocr-bench` | document OCR unit tests across content types | benchmark-data terms need explicit verification |
| `docile` | invoice fields, locations, and line items | access and dataset terms target research competition |
| `kleister-charity` | long financial-report extraction | source and redistribution terms need explicit verification |
| `fieldbench` | cross-domain field extraction | published license audit reports unresolved source documents |
| `docubench` | hard multilingual extraction across varied inputs | each document retains source-specific terms |
| `govdocs1` | format robustness and throughput | publisher states research redistribution but no standard data license |

Research-only entries use the publisher's stated restriction. Entries with missing or mixed terms
use `unverified` instead and require their own acknowledgement.

An unverified profile never moves into the commercial lane because its code repository has a
permissive license. Dataset terms govern the documents and annotations separately.

## Measurements

Each profile keeps its official metric names, normalization, aggregation, and expected output
projection. OpenReading never relabels a simplified score as an official benchmark result.

Every run also reports these common operational measurements:

- Succeeded, partial, failed, and refused document counts.
- Wall duration and reported backend duration.
- Pages processed and reported cost.
- Produced and missing channels.
- Strategy escalation rate and backend attempt counts.
- warning counts grouped by code.

When a strategy and its constituent backends share per-case official scores, the report adds:

- Mean quality difference from each backend.
- Per-case wins, ties, and losses.
- Recovered backend failures and introduced regressions.
- Quality difference divided by added reported cost.
- Distance from the per-case oracle formed by the evaluated standalone backends.

The oracle is descriptive rather than deployable. It chooses the best observed standalone result
after seeing truth and therefore serves only as an upper bound.

Comparison reports remain diagnostic artifacts in the first release. The benchmark indexes them
beside cases with large official-score gaps, but it does not invent a universal comparison score.

## Result identity and reproducibility

A run identity hashes these inputs:

- Benchmark identifier, profile revision, preset, and selected slices.
- Target kind, backend identifier or strategy name, and effective configuration hash.
- Result-affecting request options and compliance policy.
- OpenReading version and benchmark bridge version.

Artifacts include normalized responses, projected predictions, official per-case scores, aggregate
reports, and pairwise comparison reports. Secrets and document bytes never appear in manifests.

The report records the external scorer package and version. A score produced by another revision
cannot silently join the same leaderboard.

## Error behavior

Missing optional packages produce an actionable install command and exit code 2. Missing credentials
identify variables through the existing readiness path and exit code 2.

Dataset download or checksum failures exit 1 without deleting a previously valid cache. Invalid
profile or target arguments exit 2 before any backend runs.

One document failure becomes a scored failure according to the official benchmark rule. It does not
abort other documents unless the user requests fail-fast behavior.

Restricted or unverified profiles without their matching acknowledgement exit 2 before downloads.

## Documentation and examples

The durable guide belongs in `src/openreading/evals/README.md`. The `openreading.evals` and
`openreading.cli` module docstrings describe their contracts and exit codes.

The repository does not commit downloaded public corpora. Existing policy continues to prohibit a
second labeled sample under `src/openreading/evals/sample/`.

The shipped `examples/` directory may gain small generated documents for instant demonstrations.
Those fixtures exercise scanned text, layout, tables, forms, rotation, and page selection without
claiming benchmark coverage.

## Acceptance criteria

1. `benchmark list` distinguishes runnable, cataloged, research-only, and unverified profiles.
2. `benchmark show` names dataset terms, source revision, dimensions, presets, and install needs.
3. ParseBench smoke runs at least two local backend targets through the official scorer.
4. ExtractBench smoke runs one extraction-capable target through the official scorer when configured.
5. A named strategy runs as a target without bypassing compliance or duplicating strategy execution.
6. Reports retain official metric names and identify the exact external scorer revision.
7. Strategy reports include uplift, wins, losses, recovery, regression, cost, and oracle distance.
8. Pairwise comparison reports are generated from cached normalized responses without new parse calls.
9. Interrupted runs resume completed cases and never count missing cases as successful.
10. Every network and hosted-backend action stays outside `make verify`.
11. Offline tests cover profile discovery, projections, scoring adapters, resume, and error messages.
12. `make verify` remains green, including its 91 percent coverage floor.

## Non-goals

The first release does not replace any benchmark's official leaderboard or scorer.

The first release does not download every cataloged corpus or support model training.

The first release does not claim that public data predicts private production traffic.

The first release does not make benchmark scores influence normal routing automatically.

The first release does not test compliance, idempotency, or ledger invariants through public labels.
Existing offline and live suites remain authoritative for those mechanisms.

## Source register

The source register was checked on 2026-09-04. Every runnable profile pins a revision during
implementation rather than following an unversioned default branch.

- [ParseBench repository](https://github.com/run-llama/ParseBench) and
  [dataset](https://huggingface.co/datasets/llamaindex/ParseBench)
- [ExtractBench repository](https://github.com/run-llama/ExtractBench) and
  [dataset](https://huggingface.co/datasets/llamaindex/ExtractBench)
- [DocLayNet repository](https://github.com/DS4SD/DocLayNet)
- [PubTables-1M repository](https://github.com/microsoft/table-transformer)
- [CORD repository](https://github.com/clovaai/cord)
- [GovDocs1 corpus](https://digitalcorpora.org/corpora/file-corpora/files/)
- [OmniDocBench repository](https://github.com/opendatalab/OmniDocBench)
- [FUNSD repository](https://github.com/crcresearch/FUNSD)
- [XFUND repository](https://github.com/doc-analysis/XFUND)
- [READoc paper and repository link](https://aclanthology.org/2025.findings-acl.1128/)
- [OHR-Bench repository](https://github.com/opendatalab/OHR-Bench)
- [olmOCR repository](https://github.com/allenai/olmocr)
- [DocILE repository](https://github.com/rossumai/docile)
- [Kleister Charity repository](https://github.com/kleister-challenge-2021/kleister-charity)
- [FieldBench corpus](https://github.com/fieldbench/corpus)
- [DocuBench repository](https://github.com/DocuPipe/DocuBench)
