"""Export complete retained content without changing the provider's normalized values.

The export contains response, page origins, citation mappings, and import warnings.
Only the response's top-level backend_raw field is removed. Explicit nulls remain intact.
A warning summary counts provider codes, without inferring causes or affected pages.
Its bounded preview discloses omitted codes; full warning details stay inside the content.

Exports use a trusted operator directory, the input grant, and the content digest.
Model arguments never choose a destination, filename, callback, or executable.
Publication links a completed private temporary file without replacing an existing entry.
Existing entries must match the expected bytes and refuse symlinks or special files.
Completed exports remain until explicitly removed. Removing an artifact retains its exports.
The next export sweeps abandoned .openreading-export-<uuid>.tmp files under the same grant.
A short directory lock coordinates creation and sweeping; each writer locks its temporary file.
Sweeping skips locked files, so an active write never expires or blocks another publisher.
Legacy temporary names lack this lock protocol and require manual removal after writers exit.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
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


def _sweep_exports(fd: int) -> None:
    with os.scandir(fd) as entries:
        for entry in entries:
            if not re.fullmatch(r"\.openreading-export-[0-9a-f]{32}\.tmp", entry.name):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                opened = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(opened, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    current = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
                        os.unlink(entry.name, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                if error.errno != errno.ELOOP:
                    raise


def save_export(root: Path, grant: str, data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    target = root / grant
    name = digest + ".json"
    temporary = ".openreading-export-" + uuid.uuid4().hex + ".tmp"
    with directory(target, create=True) as fd:
        try:
            # Creation and acquiring the writer lock must be atomic relative to sweeping.
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                _sweep_exports(fd)
                opened = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                try:
                    fcntl.flock(opened, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(opened)
                    os.unlink(temporary, dir_fd=fd)
                    raise
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
            with os.fdopen(opened, "wb") as stream:
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
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ArtifactError("artifact_corrupt") from None
            raise ArtifactError("storage_limit") from None
    return target / name
