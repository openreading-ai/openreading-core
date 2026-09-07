"""OpenReading type surface: pydantic mirrors of the vendored JSON Schemas, plus control-plane
dataclasses.

The JSON Schemas in `openreading.schemas` are the contract. Where a model and its schema could
drift, the schema wins (DECISIONS D4). `tests/test_types_roundtrip.py` validates the request,
response, descriptor and leaderboard envelopes back against their files.

Anything that travels on the wire is pydantic: `request`, `response`, `blocks`, `descriptor`,
`geometry`, `batch`, `leaderboard`, and `liveness.LivenessReport`. Anything that stays inside the
process is a dataclass (DECISIONS D5): `job`, `cost`, `runtime`, and `liveness.ProbeResult`.
`enums` holds `StrEnum` values and `errors` holds the exception taxonomy.

Read one module at a time. `python -m pydoc openreading.types.request` is the request envelope and
`python -m pydoc openreading.types.response` is the response envelope. `pydoc openreading.types`
itself prints pydantic's generated internals for every model and is not a useful read.
"""

from __future__ import annotations

from openreading.types.blocks import Block, Chunk, Citation, Table, TableCell, TypedField
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    Cost,
    Output,
    OutputChannels,
    Provisioning,
    RouterHints,
    RuntimeProfile,
)
from openreading.types.enums import (
    BackendType,
    BlockType,
    ChannelGrade,
    CostBasis,
    JobState,
    NativeOrigin,
    NativeUnit,
    OutputParadigm,
    PageUnit,
    ResponseState,
    TextType,
    WaitMode,
)
from openreading.types.errors import (
    AdapterError,
    RetryableError,
    ScopeRefused,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.geometry import (
    BBox,
    NativeGeometry,
    bbox_from_polygon,
    pixels_from_points,
    to_absolute,
    to_canonical,
)
from openreading.types.job import Job
from openreading.types.leaderboard import (
    BenchmarkReport,
    LeaderboardBackend,
    LeaderboardCase,
    LeaderboardDataset,
)
from openreading.types.request import (
    BackendSpec,
    DocumentInput,
    Features,
    OpenReadingRequest,
    Outputs,
    Routing,
)
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    ResponseError,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, ResolvedCredentials, RunContext

__all__ = [
    # geometry
    "BBox",
    "NativeGeometry",
    "to_canonical",
    "to_absolute",
    "pixels_from_points",
    "bbox_from_polygon",
    # blocks
    "Block",
    "Table",
    "TableCell",
    "TypedField",
    "Citation",
    "Chunk",
    # response
    "NormalizedResponse",
    "Status",
    "ResponseError",
    "BackendInfo",
    "Document",
    "Page",
    "Usage",
    "BackendRaw",
    # request
    "OpenReadingRequest",
    "DocumentInput",
    "BackendSpec",
    "Outputs",
    "Features",
    "Routing",
    # descriptor
    "AdapterDescriptor",
    "Provisioning",
    "Capabilities",
    "Output",
    "OutputChannels",
    "Cost",
    "RuntimeProfile",
    "RouterHints",
    # control plane
    "Job",
    "CostReport",
    "infra_only",
    "RunContext",
    "Health",
    "RawResult",
    "ResolvedCredentials",
    # leaderboard (BL-160)
    "BenchmarkReport",
    "LeaderboardDataset",
    "LeaderboardBackend",
    "LeaderboardCase",
    # errors
    "AdapterError",
    "RetryableError",
    "TerminalError",
    "UnsupportedFeatureError",
    "ScopeRefused",
    # enums
    "BackendType",
    "BlockType",
    "TextType",
    "PageUnit",
    "NativeOrigin",
    "NativeUnit",
    "OutputParadigm",
    "ResponseState",
    "ChannelGrade",
    "JobState",
    "WaitMode",
    "CostBasis",
]
