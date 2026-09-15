"""Export complete retained content without changing the provider's normalized values.

The export contains response, page origins, citation mappings, and import warnings.
Only the response's top-level backend_raw field is removed. Explicit nulls remain intact.
A warning summary counts provider codes, without inferring causes or affected pages.
Its bounded preview discloses omitted codes; full warning details stay inside the content.

Exports use a trusted operator directory, the input grant, and the content digest.
Model arguments never choose a destination, filename, callback, or executable.
Publication links a completed private temporary file without replacing an existing entry.
Existing entries must match the expected bytes and refuse symlinks or special files.
Exports remain until explicitly removed. Removing an artifact does not remove its exports.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from openreading.artifacts.document import DocumentRequest, DocumentResult
from openreading.artifacts.intake import directory
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import (
    ArtifactId,
    ArtifactManifest,
    Digest,
    ErrorEnvelope,
    Passage,
    WireModel,
)


class DeliveryRequest(DocumentRequest):
    delivery: Literal["fragments", "auto", "file"] = "fragments"


class WarningCount(WireModel):
    code: str = Field(max_length=128)
    count: int = Field(ge=1)


class WarningSummary(WireModel):
    total: int = Field(ge=0)
    codes: list[WarningCount] = Field(max_length=16)
    omitted: int = Field(ge=0)


class DeliveryReceipt(WireModel):
    schema_version: Literal["0.2"] = "0.2"
    scope: Literal["retained_normalized_response"] = "retained_normalized_response"
    artifact_id: ArtifactId
    display_name: str
    content_bytes: int = Field(ge=1)
    content_sha256: Digest
    page_count: int = Field(ge=0)
    passage_count: int = Field(ge=0)
    parser_warnings: WarningSummary


class CompleteResult(DeliveryReceipt):
    delivery: Literal["tool_result"] = "tool_result"
    content: dict[str, JsonValue]

    def wire(self) -> dict:
        # Nulls within normalized values are content, not absent receipt fields.
        return self.model_dump(mode="json")


class FileResult(DeliveryReceipt):
    delivery: Literal["local_file"] = "local_file"
    local_path: str
    next_action: str


DeliveryPayload = Annotated[
    DeliveryRequest | DocumentResult | CompleteResult | FileResult | ErrorEnvelope,
    Field(title="OpenReading Document Tool v0.2"),
]


def document_content(manifest: ArtifactManifest, passages: list[Passage], response: dict) -> dict:
    return {
        "response": {key: value for key, value in response.items() if key != "backend_raw"},
        "page_origins": manifest.page_origins,
        "evidence": [
            {key: value for key, value in passage.wire().items() if key not in {"text", "bbox"}}
            for passage in passages
        ],
        "warnings": manifest.warnings,
    }


def warning_summary(response: dict) -> WarningSummary:
    warnings = response.get("warnings") or []
    counts = Counter(warning["code"] for warning in warnings)
    codes = [
        WarningCount(code=code, count=count)
        for code, count in sorted(counts.items())
        if len(code) <= 128
    ][:16]
    return WarningSummary(
        total=len(warnings), codes=codes, omitted=len(warnings) - sum(c.count for c in codes)
    )


def save_export(root: Path, grant: str, data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    target = root / grant
    name = digest + ".json"
    temporary = "." + uuid.uuid4().hex + ".tmp"
    with directory(target, create=True) as fd:
        try:
            with os.fdopen(
                os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                ),
                "wb",
            ) as stream:
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                    try:
                        os.link(
                            temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False
                        )
                    except FileExistsError:
                        existing = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                        )
                        with os.fdopen(existing, "rb") as saved:
                            if not stat.S_ISREG(os.fstat(saved.fileno()).st_mode):
                                raise ArtifactError("artifact_corrupt") from None
                            hasher, length = hashlib.sha256(), 0
                            while chunk := saved.read(65536):
                                hasher.update(chunk)
                                length += len(chunk)
                            if length != len(data) or hasher.hexdigest() != digest:
                                raise ArtifactError("artifact_corrupt") from None
                    os.fsync(fd)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=fd)
        except PermissionError:
            raise ArtifactError("os_permission_denied") from None
        except OSError:
            raise ArtifactError("storage_limit") from None
    return target / name
