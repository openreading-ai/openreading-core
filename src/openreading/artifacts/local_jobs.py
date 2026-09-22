"""Construct a parser-backed service for the local Core job launcher.

Server clients supply their own validated factory and never import this module.
"""

from pathlib import Path

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.limits import DoclingLimits, ProfileConfig, ProfileLimits
from openreading.artifacts.service import ArtifactService


def local_service(request: dict) -> ArtifactService:
    config = ProfileConfig(
        Path(request["input_root"]),
        Path(request["artifact_root"]),
        (DoclingLimits if request["docling"] is not None else ProfileLimits)(**request["limits"]),
        LocalDoclingConfig.from_wire(request["docling"]) if request["docling"] else None,
    )
    return ArtifactService(config)
