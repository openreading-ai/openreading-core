# Public benchmark bridge design

**Status:** Proposed

**Product specification:** `product/specs/public-benchmark-bridge.product-spec.md`

**Removal condition:** Delete this file when the bridge ships. Move durable contracts into module
docstrings and the evals guide during that same change.

## Design summary

The bridge extends `openreading.evals` with static benchmark profiles, target execution, artifact
storage, official-scorer adapters, and cross-target analysis. One CLI group exposes these pieces
without adding benchmark packages to OpenReading's base installation.

The first implementation integrates ParseBench and ExtractBench because together they cover the
broadest user-visible parse and extraction outcomes. Other verified corpora appear in the catalog
before they receive execution adapters.

Official metrics remain authoritative for document quality. OpenReading adds operational metrics and
strategy analysis over the same per-case results.

## Rejected approaches

### Convert every corpus into `case.json`

This approach gives one familiar leaderboard but loses official metrics and published comparability.
The current scorer cannot measure geometry, reading order, formulas, charts, citations, or complex
record alignment.

`case.json` remains useful for private traffic and narrow regression cases. It does not become a
universal interchange format for unrelated public benchmarks.

### Document external commands without an OpenReading bridge

This approach preserves official scores but leaves users to maintain provider integrations in several
external repositories. It also cannot compare a strategy with its constituent backends through one
repeatable interface.

### Copy external scorers into OpenReading

Copied scorers drift from their publishers and make an OpenReading result difficult to compare.
The bridge calls versioned official packages and records their identities instead.

## Ownership and files

The implementation stays inside the existing `openreading.evals` package.

- `src/openreading/evals/benchmarks.py` owns profile descriptors and the static catalog.
- `src/openreading/evals/targets.py` parses target references and executes backends or strategies.
- `src/openreading/evals/artifacts.py` owns run identities, atomic writes, resume checks, and manifests.
- `src/openreading/evals/official.py` defines the external scorer boundary and shared result types.
- `src/openreading/evals/parsebench.py` projects OpenReading responses into ParseBench predictions.
- `src/openreading/evals/extractbench.py` projects typed fields and citations into ExtractBench results.
- `src/openreading/evals/analysis.py` computes operational metrics and strategy comparisons.
- `src/openreading/types/benchmark.py` mirrors the benchmark report schema.
- `src/openreading/schemas/benchmark-report.v0.1.json` defines the portable report contract.
- `src/openreading/cli/app.py` adds the `benchmark` command group.
- `src/openreading/evals/README.md` becomes the durable public-corpus guide.

Tests follow the same ownership boundaries. External payload fixtures remain small metadata and
annotation fragments rather than real documents.

## Profile contract

`BenchmarkDescriptor` is static metadata that discovery can read without importing optional packages.
It contains these fields:

```python
@dataclass(frozen=True)
class BenchmarkDescriptor:
    id: str
    title: str
    status: Literal["runnable", "cataloged"]
    license_lane: Literal["commercial", "research_only", "unverified"]
    data_license: str
    data_license_url: str
    code_license: str
    code_license_url: str
    source_url: str
    default_revision: str
    dimensions: tuple[str, ...]
    presets: tuple[str, ...]
    package: str | None
    install_extra: str | None
```

The catalog stores facts only when a publisher states them. An empty or unclear data license uses
`unverified`, even when the scorer code has a permissive software license.

`BenchmarkProfile` is the runnable behavior loaded lazily after selection:

```python
class BenchmarkProfile(Protocol):
    descriptor: BenchmarkDescriptor

    def prepare(self, request: PrepareRequest) -> PreparedBenchmark: ...
    def cases(self, prepared: PreparedBenchmark) -> Iterable[BenchmarkCase]: ...
    def project(self, response: dict[str, Any], case: BenchmarkCase) -> Any: ...
    def score(self, predictions: Path, prepared: PreparedBenchmark) -> OfficialReport: ...
```

Profile modules import external packages inside methods. `openreading benchmark list` therefore
works with OpenReading's base dependencies only.

## Target contract

Targets use an explicit prefix because backend identifiers and strategy names share a string
namespace in user interfaces.

```python
@dataclass(frozen=True)
class BenchmarkTarget:
    kind: Literal["backend", "strategy"]
    name: str
    config: Path | None = None
```

Backend targets call the existing `openreading.api.run` path with `backend=name`. Strategy targets
call that same function with `strategy=name` and the selected configuration path.

The bridge never invokes adapter methods directly. This rule preserves readiness, credentials,
compliance, materialization, strategy traces, cost accounting, and ledger behavior.

Each case supplies its source plus profile-required request options. Extraction profiles also supply
the case JSON Schema and citation request through `extraction_schema`.

## External benchmark integration

ParseBench exposes downstream extension registration through `parse_bench.extensions`. The profile
registers an OpenReading provider in process and delegates evaluation to ParseBench's public runner.

The provider returns projected Markdown and layout data from a normalized response. It also stores
the complete normalized response in OpenReading's artifact directory for operational analysis.

ExtractBench shares the ParseBench runner foundation and accepts custom extraction providers. Its
profile returns schema-valid values plus page and box evidence from `typed_fields[].citations`.

External packages own download behavior, dataset parsing, official normalization, and scoring.
OpenReading supplies configuration, target execution, response projection, and artifact identity.

The adapter checks supported external package versions before running. Unsupported versions produce
an actionable error rather than attempting a best-effort score with changed semantics.

## Artifact layout

The default cache follows the platform user-cache convention. `--cache-dir` and `--output-dir`
override data and result locations independently.

```text
<output>/
  manifest.json
  report.json
  targets/
    <target-slug>/
      cases/
        <case-id>/
          response.json
          prediction.json
          score.json
      official-report.json
  comparisons/
    <left>__<right>/
      <case-id>.json
```

Each case writes into a sibling temporary directory and renames it after validation. An interrupted
write therefore never looks complete during resume.

The manifest stores hashes, versions, timestamps, configuration provenance, and artifact paths. It
stores neither document bytes nor environment values.

Resume accepts an artifact only when its run identity and response schema validate. A changed target,
profile revision, request option, or configuration hash creates a different run identity.

## Report schema

`benchmark-report.v0.1` contains five top-level sections:

- `benchmark` identifies the profile, revision, preset, terms, and official scorer.
- `targets[]` identifies each backend or strategy and its effective configuration hash.
- `official` preserves metric names, values, slices, and per-case scores without renaming them.
- `operations` records states, channels, warnings, durations, pages, costs, and strategy attempts.
- `comparisons[]` records uplift, wins, losses, recovery, regression, oracle distance, and report paths.

Official metric values use a string-keyed numeric object because profiles expose different dimensions.
The profile validates required names before producing the common report.

Missing provider cost stays null and never becomes zero. Missing duration stays null and does not
contribute to latency aggregates.

## Strategy analysis

Analysis joins targets by the external case identifier. It never compares aggregates from different
case sets or scorer revisions.

For strategy `S` and backend `B`, per-case uplift is `official_score(S) - official_score(B)` using
the profile's declared headline metric. Aggregate uplift is the unweighted mean over their common
case set.

A recovered failure occurs when `B` fails and `S` receives a positive official score. A regression
occurs when `S` fails or scores lower than `B` beyond the profile's exact tie tolerance.

The standalone oracle chooses the maximum score among evaluated backend targets for each case.
Oracle distance is the oracle mean minus the strategy mean over the same cases.

Cost uplift uses only cases where both targets report a cost. The report names the contributing
count so missing prices cannot masquerade as free work.

## Pairwise comparisons

Normalized responses allow comparison without another backend call. The bridge passes cached response
pairs into the pure comparison subsystem and stores schema-valid reports.

The default pairs each strategy with every standalone backend that appears in that strategy and in
the benchmark command. `--compare` can request additional explicit pairs.

The first release indexes comparison findings beside official-score gaps. It does not score finding
precision because external benchmarks do not label OpenReading's closed finding vocabulary.

## CLI behavior

`benchmark list` displays one row per descriptor with lane, status, dimensions, and presets.

`benchmark show NAME` displays source links, separate data and code terms, revision, package needs,
dimensions, estimated scale, and available presets.

`benchmark prepare NAME` checks lane policy before calling the external downloader. `--offline`
requires a complete existing cache and prohibits all network access.

Research-only profiles require `--allow-research-only`. Unverified profiles instead require
`--allow-unverified-terms`, so one acknowledgement cannot authorize both categories.

`benchmark estimate NAME` loads only prepared metadata. It prints documents, pages, target calls,
and descriptor-based price ranges when available.

`benchmark run NAME` performs this sequence:

1. Resolve the profile, preset, revision, targets, policy, and output directory.
2. Validate license acknowledgement, optional dependencies, readiness, and strategy configuration.
3. Prepare or validate the external dataset.
4. Print the estimate before any target executes.
5. Run or resume every target and persist each normalized response atomically.
6. Project responses and invoke the official scorer for each target.
7. Compute common operations, strategy uplift, and requested pairwise comparisons.
8. Validate `report.json` against the vendored schema and print a concise summary.

Argument errors and unmet prerequisites exit 2. Execution or scoring failures exit 1 after preserving
all completed artifacts. A complete run exits 0 even when individual cases score zero.

## Security and compliance

Profile case identifiers and paths are untrusted input. Resolved local documents must stay inside the
prepared dataset root, using the same containment rule as `openreading.evals.dataset`.

The bridge never follows arbitrary download URLs from a case record. Dataset acquisition remains in
the pinned publisher package, while OpenReading receives local document paths after preparation.

Hosted targets use the normal compliance path. A profile cannot remove or weaken request, file, or
deployment policy.

Manifests redact secrets through allowlisted fields rather than recursive best-effort filtering.
External benchmark logs are stored separately because OpenReading cannot guarantee their contents.

## Testing

Offline unit tests use fake profile packages and generated in-memory documents. They cover descriptor
discovery, lane enforcement, target parsing, path containment, projection, report validation, atomic
writes, resume identity, and strategy analysis.

ParseBench and ExtractBench contract tests use tiny publisher-shaped annotation fixtures. They never
download documents or invoke external services during `make verify`.

A manually triggered integration lane installs pinned external packages and runs their smallest public
preset. Local targets require no credentials, while hosted targets skip cleanly without keys.

The implementation follows test-driven development. Every bug fix proves its test by breaking and
restoring the fix before completion.

## Documentation migration at shipment

Shipping removes this design and its product specification. Durable content moves as follows:

- Profile and target contracts move into `openreading.evals.benchmarks` and related docstrings.
- CLI commands and exit codes move into the `openreading.cli` module docstring.
- User workflows, corpus selection, terms, and reproduced output move into the evals guide.
- The root README docs index changes only if the workflow answers a new question.
- The schema manifest receives the benchmark report row.
- the changelog records the new CLI and report contract.

No standalone benchmark survey remains after shipment. The live catalog becomes the source of truth
for dataset status, links, revisions, and license lanes.
