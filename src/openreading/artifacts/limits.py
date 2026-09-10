"""Fixed local proof limits and sanitized domain errors.

The profile permits one import per store, 25 MiB input, 100 physical pages, 64 MiB
serialized extraction, 512 MiB retained storage, and 45 seconds per import.
Import, search, and read payloads permit 4096, 8192, and 16384 UTF-8 bytes respectively.
These caps do not promise a hard native-parser memory ceiling or an operating-system sandbox.
Only busy and storage_limit invite retry after the blocking condition is resolved.
"""

from dataclasses import dataclass, field
from pathlib import Path

from openreading.artifacts.models import ErrorCode, ErrorEnvelope, ToolError

MESSAGES: dict[ErrorCode, str] = {
    "engine_identity_unavailable": "Installed engine identity is missing or invalid. Reinstall the package or verified runtime.",
    "configuration_required": "Configure separate absolute input and artifact directories.",
    "access_denied": "This path is outside the configured grant or is not a regular file.",
    "input_not_found": "The requested input file does not exist.",
    "unsupported_format": "The local proof profile requires a readable PDF document.",
    "password_required": "This document requires a password. The agent profile cannot unlock it.",
    "input_too_large": "The document exceeds the profile's byte or page limit.",
    "extraction_too_large": "The extraction exceeds the profile's serialized byte limit.",
    "no_readable_text": "No readable text was extracted. This profile does not perform OCR.",
    "busy": "Another import is using this artifact store. Retry after it finishes.",
    "timeout": "The import exceeded its time limit.",
    "cancelled": "The import was cancelled.",
    "storage_limit": "The artifact store is full. Stop its clients and manually remove retained directories under --artifact-root, as the artifact guide describes.",
    "parse_failed": "The local parser could not complete this document.",
    "artifact_not_found": "This artifact is unavailable under the current input grant.",
    "artifact_corrupt": "The retained artifact failed integrity validation. Import the source again after removing it.",
    "artifact_version_unsupported": "This artifact format is not supported by this runtime.",
    "evidence_not_found": "At least one requested evidence identifier is absent from this artifact.",
    "invalid_cursor": "The continuation cursor is invalid for this request.",
    "response_too_large": "The result cannot fit within this tool's payload limit.",
}


__all__ = ["ArtifactError", "ProfileConfig", "ProfileLimits"]


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
    import_bytes: int = 4096
    search_bytes: int = 8192
    read_bytes: int = 16384


@dataclass(frozen=True)
class ProfileConfig:
    input_root: Path
    artifact_root: Path
    limits: ProfileLimits = field(default_factory=ProfileLimits)
