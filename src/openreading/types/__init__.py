"""OpenReading type surface: schema-bound pydantic models + control-plane dataclasses."""

from __future__ import annotations

from openreading.types.blocks import Block, Chunk, Citation, Table, TableCell, TypedField
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
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
    ComplianceRefused,
    RetryableError,
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
    Compliance,
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
    "Compliance",
    # descriptor
    "AdapterDescriptor",
    "Provisioning",
    "Capabilities",
    "Output",
    "OutputChannels",
    "Cost",
    "ComplianceProfile",
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
    "ComplianceRefused",
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
