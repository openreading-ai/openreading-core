"""Explicit local proof limits and sanitized domain errors.

The historical v1 profile permits one import per store, 25 MiB input, 100 physical pages, 64 MiB
serialized extraction, 512 MiB retained storage, and 45 seconds per import.
That storage quota includes default exports under artifact_root/exports, which persist without eviction.
Stop clients before manually removing unwanted exports to reclaim space for later imports.
Import, search, and read payloads permit 4096, 8192, and 16384 UTF-8 bytes respectively.
Full-document continuation payloads default to 65536 UTF-8 bytes, with no silent truncation.
These caps do not promise a hard native-parser memory ceiling or an operating-system sandbox.
Docling limits accept None for uncapped documents, storage, time, and sampled RSS.
ExternalLimits permits uncapped retention without configuring a local parser or worker.
For example, an HTTP caller can retain a completed response without a parser deadline.
Explicit positive operator limits remain enforced. Idle shutdown and tool reply caps remain bounded.
The legacy profile rejects worker settings that its disposable parser cannot enforce.
Only busy and storage_limit invite retry after the blocking condition is resolved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.models import ErrorCode, ErrorEnvelope, ToolError

MESSAGES: dict[ErrorCode, str] = {
    "memory_limit": "The worker exceeded its sampled process-memory limit.",
    "worker_monitor_failed": "The worker could not be monitored safely.",
    "os_permission_denied": "Operating-system permissions refused file access or parser process control.",
    "engine_identity_unavailable": "Installed engine identity is missing or invalid. Reinstall the package or verified runtime.",
    "configuration_required": "Configure separate absolute input and artifact directories.",
    "access_denied": "This path is outside the configured grant or is not a regular file.",
    "input_not_found": "The requested input file does not exist.",
    "unsupported_format": "The configured adapter cannot read this document format.",
    "password_required": "This document requires a password. The agent profile cannot unlock it.",
    "input_too_large": "The document exceeds the profile's byte or page limit.",
    "extraction_too_large": "The extraction exceeds the profile's serialized byte limit.",
    "no_readable_text": "No readable text was extracted with the configured OCR setting.",
    "busy": "Another import is using this artifact store. Retry after it finishes.",
    "timeout": "The import exceeded its time limit.",
    "cancelled": "The import was cancelled.",
    "storage_limit": "Local storage could not retain this document. Check free disk space and any configured storage limit.",
    "parse_failed": "Document processing could not complete.",
    "artifact_not_found": "This artifact is unavailable under the current input grant.",
    "artifact_corrupt": "The retained artifact failed integrity validation. Import the source again after removing it.",
    "artifact_version_unsupported": "This artifact format is not supported by this runtime.",
    "evidence_not_found": "At least one requested evidence identifier is absent from this artifact.",
    "invalid_cursor": "The continuation cursor is invalid for this request.",
    "response_too_large": "The result cannot fit within this tool's payload limit.",
}


__all__ = ["ArtifactError", "DoclingLimits", "ExternalLimits", "ProfileConfig", "ProfileLimits"]

# The worker reports these from preflight or its metered writer, after which it waits for the
# next job with its converter intact. Restarting it would repeat model initialization for
# every rejected file in a batch without making the next conversion any safer.
INPUT_REJECTIONS: frozenset[ErrorCode] = frozenset(
    {
        "unsupported_format",
        "password_required",
        "input_too_large",
        "no_readable_text",
        "extraction_too_large",
        "storage_limit",
    }
)


class ArtifactError(Exception):
    def __init__(self, code: ErrorCode):
        self.code: ErrorCode = code
        super().__init__(code)

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ToolError(
                code=self.code,
                message=MESSAGES[self.code],
                retryable=self.code in {"busy", "storage_limit"},
            )
        )


@dataclass(frozen=True)
class ProfileLimits:
    source_bytes: int = 25 * 1024 * 1024
    pages: int = 100
    extraction_bytes: int = 64 * 1024 * 1024
    store_bytes: int = 512 * 1024 * 1024
    deadline_seconds: float = 45
    worker_memory_bytes: int | None = None
    worker_idle_seconds: float = 60
    import_bytes: int = 4096
    search_bytes: int = 8192
    read_bytes: int = 16384
    document_bytes: int = 65536

    def __post_init__(self):
        if self.worker_memory_bytes is not None or self.worker_idle_seconds != 60:
            raise ArtifactError("configuration_required")


@dataclass(frozen=True, kw_only=True)
class DoclingLimits:
    source_bytes: int | None = None
    extraction_bytes: int | None = None
    store_bytes: int | None = None
    import_bytes: int = 4096
    search_bytes: int = 8192
    read_bytes: int = 16384
    document_bytes: int = 65536
    pages: int | None = field()
    deadline_seconds: float | None = field()
    worker_memory_bytes: int | None = field()
    worker_idle_seconds: float = field()

    def __post_init__(self):
        for value in (self.deadline_seconds, self.worker_idle_seconds):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            ):
                raise ValueError("Docling time limits require positive finite numbers or null.")
        if self.worker_idle_seconds is None:
            raise ValueError("Worker idle shutdown must be configured.")
        for value in (
            self.source_bytes,
            self.extraction_bytes,
            self.store_bytes,
            self.pages,
            self.worker_memory_bytes,
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Docling byte and page limits require positive integers or null.")


@dataclass(frozen=True)
class ExternalLimits:
    """Bound external response retention independently from local parser profiles."""

    source_bytes: int | None = None
    pages: int | None = None
    extraction_bytes: int | None = None
    store_bytes: int | None = None
    deadline_seconds: float | None = None
    worker_memory_bytes: None = None
    worker_idle_seconds: float = 60
    import_bytes: int = 4096
    search_bytes: int = 8192
    read_bytes: int = 16384
    document_bytes: int = 65536

    def __post_init__(self):
        if self.worker_memory_bytes is not None or self.worker_idle_seconds != 60:
            raise ValueError("External retention cannot configure a parser worker.")
        for value in (self.source_bytes, self.pages, self.extraction_bytes, self.store_bytes):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Retention limits require positive integers or null.")
        value = self.deadline_seconds
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        ):
            raise ValueError("Retention deadlines require positive finite numbers or null.")


@dataclass(frozen=True)
class ProfileConfig:
    input_root: Path
    artifact_root: Path
    limits: ProfileLimits | DoclingLimits | ExternalLimits = field(default_factory=ProfileLimits)
    docling: LocalDoclingConfig | None = None

    def __post_init__(self):
        if (self.docling is not None) != isinstance(self.limits, DoclingLimits):
            raise ArtifactError("configuration_required")
