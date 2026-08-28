"""OpenReading eval harness — score any backend on your own documents (recommendations.md:
own eval harness before trusting any vendor number). Uniform scoring across all backend types
because they all return the same NormalizedResponse."""

from __future__ import annotations

from openreading.evals.dataset import EvalCase, load_dataset
from openreading.evals.leaderboard import run_leaderboard
from openreading.evals.runner import CaseResult, DatasetReport, run_case, run_dataset
from openreading.evals.scorers import field_prf, score, table_grid, text_similarity

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
]
