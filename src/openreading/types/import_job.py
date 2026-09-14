"""Expose local import progress without transferring input paths or document contents.

An import-job.v0.1 result names persistent work under the current input grant.
Only succeeded jobs carry an artifact receipt. Cancellation requests can race with
publication, so the terminal state decides whether an artifact was committed.
Stages are observations, not percentages or predictions of remaining time.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openreading.artifacts.models import ImportReceipt, ToolError

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobStage = Literal["queued", "copying", "preflight", "conversion", "writing", "complete", "stopped"]


class ImportJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.1"] = "0.1"
    job_id: str = Field(pattern=r"^j1_[0-9a-f]{32}$")
    state: JobState
    stage: JobStage
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    cancel_requested: bool = False
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
        return self.model_dump(mode="json")
