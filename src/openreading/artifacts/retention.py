"""Retain an externally acquired normalized response without invoking any parser.

The caller supplies the hash of the bytes it uploaded, its nonsecret destination identity,
and the request digest. Retention copies the current granted source and verifies that hash.
For example, replacing a selected file after upload fails instead of binding unrelated bytes.
The acquisition records correlation with that upload, not independent server correctness.
The manifest's engine identifies the local retaining runtime, never the remote server build.

Only succeeded and partial responses are retained. Structured-only responses have zero
passages; retrieval returns their values without inventing quotes, pages, or measured origins.
Decoded values are preserved, including explicit nulls and additive envelope fields.
The caller owns bounded JSON decoding, duplicate-key rejection, transport, and credentials.
This module validates values against the vendored response contract before publication.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
from pydantic import ValidationError

from openreading.artifacts.intake import copy_source
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import (
    AcquisitionProvenance,
    ArtifactManifest,
    ImportReceipt,
    WarningCode,
    artifact_id,
    json_bytes,
)
from openreading.artifacts.passages import iter_passages
from openreading.artifacts.service import ArtifactService, _display_name
from openreading.artifacts.store import file_record
from openreading.schemas import validate_response
from openreading.types.response import NormalizedResponse


def validate_external_response(response: dict) -> NormalizedResponse:
    """Validate known channels while preserving additive envelope fields for delivery."""
    # The producer schema is closed. Consumers tolerate additive envelope channels,
    # as NormalizedResponse does, without dropping those values from retained JSON.
    validate_response(
        {key: value for key, value in response.items() if key in NormalizedResponse.model_fields}
    )
    return NormalizedResponse.model_validate(response)


def retain_response(
    service: ArtifactService,
    path: str,
    response: dict,
    *,
    source_sha256: str,
    destination_sha256: str,
    request_sha256: str,
    cancelled: threading.Event | None = None,
) -> ImportReceipt:
    """Publish one validated response bound to the exact previously uploaded source bytes."""
    started = time.monotonic()
    service._check_time(started, cancelled)
    try:
        encoded = json_bytes(response)
        parsed = validate_external_response(response)
        state = parsed.status.state.value
        if state != "succeeded" and state != "partial":
            raise ValueError("Response has no usable extraction")
        acquisition = AcquisitionProvenance(
            destination_sha256=destination_sha256,
            request_sha256=request_sha256,
            response_sha256=hashlib.sha256(encoded).hexdigest(),
            extraction_state=state,
        )
    except (ValueError, TypeError, RecursionError, jsonschema.ValidationError, ValidationError):
        raise ArtifactError("parse_failed") from None
    cap = service.config.limits.extraction_bytes
    if cap is not None and len(encoded) > cap:
        raise ArtifactError("extraction_too_large")
    suffix = Path(path).suffix.lower()
    source_file = "source" + (suffix if re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) else ".bin")
    with service.store.source(path) as fd, service.store.import_lock():
        staging = Path(tempfile.mkdtemp(dir=service.config.artifact_root / "staging"))
        try:
            digest, changed = copy_source(
                fd,
                staging / source_file,
                service.config.limits.source_bytes,
                service._available(),
                check=lambda: service._check_time(started, cancelled),
            )
            if digest != source_sha256:
                raise ArtifactError("artifact_corrupt")
            identifier = artifact_id(
                digest,
                service.identity,
                version="0.5",
                source_file=source_file,
                acquisition=acquisition,
            )
            if (service.store.documents / identifier).exists():
                manifest = service.load_artifact(identifier)
                service._check_time(started, cancelled)
                return service._receipt(manifest, reused=True)

            def write(name: str, chunks):
                length = 0
                available = service._available(reserve=65536)
                with (staging / name).open("xb") as output:
                    os.chmod(staging / name, 0o600)
                    for chunk in chunks:
                        service._check_time(started, cancelled)
                        length += len(chunk)
                        if cap is not None and length > cap:
                            raise ArtifactError("extraction_too_large")
                        if available is not None and length > available:
                            raise ArtifactError("storage_limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())

            write("response.json", [encoded])
            count = 0

            def evidence():
                nonlocal count
                for passage in iter_passages(parsed):
                    count += 1
                    yield json_bytes(passage.wire()) + b"\n"

            write("passages.jsonl", evidence())
            warnings: list[WarningCode] = []
            if changed:
                warnings.append("source_changed")
            if response.get("warnings"):
                warnings.append("parser_warnings_present")
            manifest = ArtifactManifest(
                format="local-document.v0.5",
                artifact_id=identifier,
                document_sha256=digest,
                display_name=_display_name(path),
                source_relative_path=path,
                source_file=source_file,
                input_grant_sha256=service.store.grant,
                page_count=parsed.document.page_count,
                passage_count=count,
                engine=service.identity,
                acquisition=acquisition,
                created_at=datetime.now(UTC).isoformat(),
                warnings=warnings,
                files={
                    name: file_record(staging / name, None)
                    for name in (source_file, "response.json", "passages.jsonl")
                },
            )
            write("manifest.json", [json_bytes(manifest.wire())])
            receipt = service._receipt(manifest, reused=False)
            service._check_time(started, cancelled)
            service._fsync(staging)
            os.rename(staging, service.store.documents / identifier)
            service._fsync(service.store.documents)
            return receipt
        except OSError:
            raise ArtifactError("storage_limit") from None
        finally:
            if staging.exists():
                shutil.rmtree(staging)
