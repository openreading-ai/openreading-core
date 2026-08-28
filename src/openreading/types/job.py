"""Job — the one state machine the router driver advances (adapter_interface.md §1.2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from openreading.types.cost import CostReport
from openreading.types.enums import JobState, WaitMode
from openreading.types.errors import AdapterError
from openreading.types.runtime import RawResult


@dataclass
class Job:
    id: str  # OpenReading job id (uuid); stable across polls
    backend_id: str  # descriptor.id, e.g. "aws-textract"
    wait_mode: WaitMode
    state: JobState = JobState.PENDING
    backend_job_id: str | None = None  # Textract JobId, LlamaParse pjb-..., Chunkr task_id
    poll_handle: dict | None = None  # opaque to router: {"get_url":..., "next_token":...}
    webhook_token: str | None = None  # correlates an inbound event back to this Job
    raw: RawResult | None = None  # populated on SUCCEEDED
    error: AdapterError | None = None  # populated on FAILED
    idempotency_key: str | None = None
    submitted_at: str | None = None  # ISO-8601
    finished_at: str | None = None
    attempts: int = 0
    next_poll_at: float | None = None  # monotonic ts; router sleeps until this before poll()
    cost_hint: CostReport | None = None

    def is_terminal(self) -> bool:
        return self.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)

    # ---- serialization (Ledger T4a, R3/AC-6) --------------------------------------------------
    #
    # `Job` must survive a `json.dumps`/`json.loads` round trip in every state, including FAILED —
    # `dataclasses.asdict` alone raises the moment `.error` is set (`AdapterError` is not JSON-
    # native; see types/errors.py). `raw`/`cost_hint` are already plain dataclasses of JSON-native
    # fields, so `asdict` handles them (and their `None` case) unassisted; `.error` and `.state`/
    # `.wait_mode` (enums) are the only fields needing explicit translation.
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["wait_mode"] = self.wait_mode.value
        d["error"] = self.error.to_dict() if self.error is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        kwargs = {k: v for k, v in d.items() if k in {f.name for f in fields(cls)}}
        kwargs["state"] = JobState(d["state"])
        kwargs["wait_mode"] = WaitMode(d["wait_mode"])
        kwargs["raw"] = RawResult(**d["raw"]) if d.get("raw") is not None else None
        kwargs["cost_hint"] = (
            CostReport(**d["cost_hint"]) if d.get("cost_hint") is not None else None
        )
        kwargs["error"] = AdapterError.from_dict(d["error"]) if d.get("error") is not None else None
        return cls(**kwargs)
