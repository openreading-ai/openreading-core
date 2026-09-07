"""Pydantic mirror of leaderboard-report.v0.1.json (BL-160) — the ranked, cross-backend benchmark
envelope `openreading.evals.leaderboard.run_leaderboard` produces. Modeled directly on
`types.batch.CorpusReport`: extra="ignore" on the envelope (forward-tolerant of a newer producer's
additive top-level fields), extra="forbid" on nested payload models (construction typos still
raise), `to_schema_dict()` matches NormalizedResponse (enums -> values, None dropped).

`LeaderboardCase.winner` is the one field that is schema-optional rather than schema-required-and-
nullable: pydantic's `exclude_none=True` (used by every `to_schema_dict()` in this repo) drops a
None field entirely rather than emitting a JSON `null`, so a REQUIRED-but-nullable `winner` would
violate the schema's own required list the moment no backend produced a real score for a case. The
schema leaves `winner` out of `required` for exactly this reason (the same reason
corpus-report.v0.1.json's CorpusDocumentSource fields aren't required either).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LeaderboardDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    case_count: int = Field(ge=0)
    case_names: list[str] = Field(default_factory=list)


class LeaderboardBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_id: str
    rank: int = Field(ge=1)
    mean_score: float = Field(ge=0.0, le=1.0)
    n_cases: int = Field(ge=0)
    n_scored: int = Field(ge=0)
    errors: int = Field(ge=0)
    non_deterministic: bool = False
    dimensions: dict[str, float] = Field(default_factory=dict)


class LeaderboardCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    winner: str | None = None
    scores: dict[str, float | None] = Field(default_factory=dict)


class BenchmarkReport(BaseModel):
    """Envelope for one leaderboard run (extra="ignore" — forward-tolerant, matching every other
    cross-surface report envelope this repo ships)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "0.1"
    dataset: LeaderboardDataset
    backends: list[LeaderboardBackend] = Field(default_factory=list)
    cases: list[LeaderboardCase] = Field(default_factory=list)

    def to_schema_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
