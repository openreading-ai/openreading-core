"""Benchmark harness: score any backend on documents you labeled yourself.

A vendor publishes accuracy numbers measured on the vendor's own documents. This package
measures a backend on a dataset you own, so a routing decision rests on a number you can
check. Scoring is uniform across backend types because every backend returns the same response
envelope.

A dataset is a directory of ``<case>/case.json`` files, and ``openreading.evals.dataset``
defines that layout. ``run_dataset`` drives one adapter over the directory. ``score`` measures
the five dimensions a case may declare: text similarity, text-contains fraction, markdown
similarity, typed-field precision, recall and F1, and table-cell accuracy
(``openreading.evals.scorers``). ``run_leaderboard`` ranks several named backends on one
dataset (``openreading.evals.leaderboard``).

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
    "project_parse_response",
    "project_extract_response",
]
