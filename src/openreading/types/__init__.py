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

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openreading.types.blocks import Block, Chunk, Citation, Table, TableCell, TypedField
if TYPE_CHECKING:
    from openreading.types.cost import CostReport, infra_only
if TYPE_CHECKING:
    from openreading.types.descriptor import (
        AdapterDescriptor,
        Capabilities,
        Output,
        OutputChannels,
        Provisioning,
        RouterHints,
        RuntimeProfile,
    )
if TYPE_CHECKING:
    from openreading.types.enums import (
        BackendType,
        BlockType,
        ChannelGrade,
        JobState,
        NativeOrigin,
        NativeUnit,
        OutputParadigm,
        PageUnit,
        ResponseState,
        TextType,
        WaitMode,
    )
if TYPE_CHECKING:
    from openreading.types.errors import (
        AdapterError,
        RetryableError,
        ScopeRefused,
        TerminalError,
        UnsupportedFeatureError,
    )
if TYPE_CHECKING:
    from openreading.types.geometry import (
        BBox,
        NativeGeometry,
        bbox_from_polygon,
        pixels_from_points,
        to_absolute,
        to_canonical,
    )
if TYPE_CHECKING:
    from openreading.types.job import Job
if TYPE_CHECKING:
    from openreading.types.leaderboard import (
        BenchmarkReport,
        LeaderboardBackend,
        LeaderboardCase,
        LeaderboardDataset,
    )
if TYPE_CHECKING:
    from openreading.types.request import (
        BackendSpec,
        DocumentInput,
        Features,
        OpenReadingRequest,
        Outputs,
        Routing,
    )
if TYPE_CHECKING:
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
if TYPE_CHECKING:
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
]


# A response consumer must not initialize parser registries through a package facade.
_EXPORTS = {
    "Block": ("openreading.types.blocks", "Block"),
    "Chunk": ("openreading.types.blocks", "Chunk"),
    "Citation": ("openreading.types.blocks", "Citation"),
    "Table": ("openreading.types.blocks", "Table"),
    "TableCell": ("openreading.types.blocks", "TableCell"),
    "TypedField": ("openreading.types.blocks", "TypedField"),
    "CostReport": ("openreading.types.cost", "CostReport"),
    "infra_only": ("openreading.types.cost", "infra_only"),
    "AdapterDescriptor": ("openreading.types.descriptor", "AdapterDescriptor"),
    "Capabilities": ("openreading.types.descriptor", "Capabilities"),
    "Output": ("openreading.types.descriptor", "Output"),
    "OutputChannels": ("openreading.types.descriptor", "OutputChannels"),
    "Provisioning": ("openreading.types.descriptor", "Provisioning"),
    "RouterHints": ("openreading.types.descriptor", "RouterHints"),
    "RuntimeProfile": ("openreading.types.descriptor", "RuntimeProfile"),
    "BackendType": ("openreading.types.enums", "BackendType"),
    "BlockType": ("openreading.types.enums", "BlockType"),
    "ChannelGrade": ("openreading.types.enums", "ChannelGrade"),
    "JobState": ("openreading.types.enums", "JobState"),
    "NativeOrigin": ("openreading.types.enums", "NativeOrigin"),
    "NativeUnit": ("openreading.types.enums", "NativeUnit"),
    "OutputParadigm": ("openreading.types.enums", "OutputParadigm"),
    "PageUnit": ("openreading.types.enums", "PageUnit"),
    "ResponseState": ("openreading.types.enums", "ResponseState"),
    "TextType": ("openreading.types.enums", "TextType"),
    "WaitMode": ("openreading.types.enums", "WaitMode"),
    "AdapterError": ("openreading.types.errors", "AdapterError"),
    "RetryableError": ("openreading.types.errors", "RetryableError"),
    "ScopeRefused": ("openreading.types.errors", "ScopeRefused"),
    "TerminalError": ("openreading.types.errors", "TerminalError"),
    "UnsupportedFeatureError": ("openreading.types.errors", "UnsupportedFeatureError"),
    "BBox": ("openreading.types.geometry", "BBox"),
    "NativeGeometry": ("openreading.types.geometry", "NativeGeometry"),
    "bbox_from_polygon": ("openreading.types.geometry", "bbox_from_polygon"),
    "pixels_from_points": ("openreading.types.geometry", "pixels_from_points"),
    "to_absolute": ("openreading.types.geometry", "to_absolute"),
    "to_canonical": ("openreading.types.geometry", "to_canonical"),
    "Job": ("openreading.types.job", "Job"),
    "BenchmarkReport": ("openreading.types.leaderboard", "BenchmarkReport"),
    "LeaderboardBackend": ("openreading.types.leaderboard", "LeaderboardBackend"),
    "LeaderboardCase": ("openreading.types.leaderboard", "LeaderboardCase"),
    "LeaderboardDataset": ("openreading.types.leaderboard", "LeaderboardDataset"),
    "BackendSpec": ("openreading.types.request", "BackendSpec"),
    "DocumentInput": ("openreading.types.request", "DocumentInput"),
    "Features": ("openreading.types.request", "Features"),
    "OpenReadingRequest": ("openreading.types.request", "OpenReadingRequest"),
    "Outputs": ("openreading.types.request", "Outputs"),
    "Routing": ("openreading.types.request", "Routing"),
    "BackendInfo": ("openreading.types.response", "BackendInfo"),
    "BackendRaw": ("openreading.types.response", "BackendRaw"),
    "Document": ("openreading.types.response", "Document"),
    "NormalizedResponse": ("openreading.types.response", "NormalizedResponse"),
    "Page": ("openreading.types.response", "Page"),
    "ResponseError": ("openreading.types.response", "ResponseError"),
    "Status": ("openreading.types.response", "Status"),
    "Usage": ("openreading.types.response", "Usage"),
    "Health": ("openreading.types.runtime", "Health"),
    "RawResult": ("openreading.types.runtime", "RawResult"),
    "ResolvedCredentials": ("openreading.types.runtime", "ResolvedCredentials"),
    "RunContext": ("openreading.types.runtime", "RunContext"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
