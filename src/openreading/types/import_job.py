"""Expose local import progress without transferring input paths or document contents.

An import-job.v0.4 status names persistent work under the current input grant.
Only succeeded jobs carry an artifact receipt. Cancellation requests can race with
publication, so the terminal state decides whether an artifact was committed.
Stages are observations, not percentages or predictions of remaining time.
External execution adds uploading, waiting, receiving, and retaining without server page estimates.
For example, waiting means the local client awaits a response, not that server processing stopped.
Optional page_progress counts successfully assembled physical pages against source preflight.
Assembly precedes document-wide reading order, normalization and publication, so all pages need not mean success.
Absent progress means no observation, including cache reuse and older retained jobs.
Counters may skip intermediate values because updates are coalesced to avoid per-page disk writes.
Listings return bounded summaries without paths, document names or receipts.
An unreadable status retains its job ID with an unavailable state, rather than hiding other jobs.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openreading.artifacts.models import ImportReceipt, ToolError

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobStage = Literal[
    "queued",
    "copying",
    "preflight",
    "conversion",
    "writing",
    "complete",
    "stopped",
    "uploading",
    "waiting",
    "receiving",
    "retaining",
]


class PageProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pages_assembled: int = Field(ge=0)
    total_pages: int = Field(ge=1)

    @model_validator(mode="after")
    def within_source(self):
        if self.pages_assembled > self.total_pages:
            raise ValueError("Assembled pages exceed the physical source count.")
        return self


class ImportJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.2", "0.3", "0.4"] = "0.3"
    job_id: str = Field(pattern=r"^j1_[0-9a-f]{32}$")
    state: JobState
    stage: JobStage
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    cancel_requested: bool = False
    page_progress: PageProgress | None = None
    receipt: ImportReceipt | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def terminal_fields(self):
        if (self.state == "succeeded") != (self.receipt is not None):
            raise ValueError("Only successful jobs carry an artifact receipt.")
        if (self.state in {"failed", "cancelled"}) != (self.error is not None):
            raise ValueError("Only failed or cancelled jobs carry an error.")
        return self

    def wire(self) -> dict:
        value = self.model_dump(mode="json")
        if self.receipt is not None and self.receipt.extraction_state is None:
            # Earlier receipt versions do not declare the external acquisition field.
            value["receipt"].pop("extraction_state", None)
        return value


class ImportJobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str = Field(pattern=r"^j1_[0-9a-f]{32}$")
    state: JobState | Literal["unavailable"]
    elapsed_seconds: float | None = Field(ge=0, allow_inf_nan=False)


class ImportJobList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.1"] = "0.1"
    jobs: list[ImportJobSummary] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=512)

    def wire(self) -> dict:
        return self.model_dump(mode="json")
