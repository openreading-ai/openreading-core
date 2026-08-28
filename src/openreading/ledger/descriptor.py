"""ExecutorDescriptor — the static, self-describing record every `Executor` exposes
(internal/design/ledger.md §5.4, §16.1). Mirrors `AdapterDescriptor` + 8 methods one layer up: core
reads `executor.descriptor` and never branches on the executor's concrete type.

Every limit is `int | None`, `None` meaning unbounded — substituting a default number for `None`
is forbidden (§5.4): that is a constant re-entering by the back door.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExecutorLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_inline_payload_bytes: int | None = None
    max_payload_bytes: int | None = None
    max_steps_per_run: int | None = None
    max_step_duration_ms: int | None = None


class ExecutorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native_timers: bool = False
    durable_log: bool = False
    at_least_once_dispatch: bool = False
    crash_reentry: bool = False
    child_runs: bool = False


class ExecutorDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    limits: ExecutorLimits
    capabilities: ExecutorCapabilities
