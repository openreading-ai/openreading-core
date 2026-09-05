"""Benchmark harness for public corpora and documents you labeled yourself.

A vendor publishes accuracy numbers measured on the vendor's own documents. This package
measures a backend on a dataset you own, so a routing decision rests on a number you can
check. Scoring is uniform across backend types because every backend returns the same response
envelope.

A dataset is a directory of ``<case>/case.json`` files, and ``openreading.evals.dataset``
defines that layout. ``run_dataset`` drives one adapter over the directory. ``score`` measures
the five dimensions a case may declare: text similarity, text-contains fraction, markdown
similarity, typed-field precision, recall and F1, and table-cell accuracy
(``openreading.evals.scorers``). ``run_leaderboard`` ranks several named backends on one
dataset (``openreading.evals.leaderboard``). ``openreading.evals.benchmarks`` provides
offline discovery and terms lanes for public corpora. ``openreading.evals.official``
registers targets inside ParseBench and ExtractBench. Those publishers retain ownership
of case loading, metric code, aggregation, and detailed reports.
``openreading.evals.subset`` cuts a prepared corpus down to a few documents in the publisher's
own on-disk format, and ``openreading.evals.preflight`` prices that selection in pages and asks
before it spends. A public run therefore starts at two documents, not at a corpus.

The public bridge downloads nothing until ``benchmark prepare`` or ``benchmark run``.
Its default cache stays outside the repository. Research-only and unverified terms use
different acknowledgement flags, so one flag cannot authorize the other accidentally.

A case may also declare ``expected.rules``, which are ParseBench's own rule objects scored by
ParseBench's own engine over your document (``openreading.evals.rules``). That is the one
dimension here that sees content a backend INVENTED rather than merely missed, because the other
four ask only whether what you expected is present. It stays a dimension inside ``score`` rather
than a second harness, so ``leaderboard`` and ``calibrate`` reach it through the same
``run_case``. ``expected.text_absent`` is the plain-strings spelling, scored by the same engine.

Known gaps: catching content the user did not PREDICT needs the publisher's bag rules and an
explicit claim that a labeled ``text`` is the whole document, and publisher-comparable numbers
over a private corpus are not built. Both are scoped in
``product/specs/hallucination-detection.product-spec.md``.

One synthetic sample ships at ``src/openreading/evals/sample/loan_page1/case.json``. Labeled
data over real documents never lands in this repository, and
``tests/test_evals_benchmark_only.py`` fails if any does. Pass such a dataset as a path
instead. ``openreading.comparison`` (truth mode) and ``openreading.strategies.calibrate``
import these scorers rather than re-implementing them, so there is one metric stack. The guide
is ``src/openreading/evals/README.md``.
"""

from __future__ import annotations

from openreading.evals.benchmarks import (
    BenchmarkDescriptor,
    BenchmarkTermsError,
    get_benchmark,
    list_benchmarks,
    require_benchmark_terms,
)
from openreading.evals.dataset import EvalCase, load_dataset
from openreading.evals.leaderboard import run_leaderboard
from openreading.evals.runner import CaseResult, DatasetReport, run_case, run_dataset
from openreading.evals.scorers import field_prf, score, table_grid, text_similarity
from openreading.evals.targets import (
    BenchmarkTarget,
    execute_target,
    pipeline_name,
    project_extract_response,
    project_parse_response,
)

__all__ = [
    "score",
    "text_similarity",
    "field_prf",
    "table_grid",
    "EvalCase",
    "load_dataset",
    "run_case",
    "run_dataset",
    "CaseResult",
    "DatasetReport",
    "run_leaderboard",
    "BenchmarkDescriptor",
    "BenchmarkTermsError",
    "list_benchmarks",
    "get_benchmark",
    "require_benchmark_terms",
    "BenchmarkTarget",
    "execute_target",
    "pipeline_name",
    "project_parse_response",
    "project_extract_response",
]
