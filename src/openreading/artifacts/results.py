"""Retain complete responses and reports separately from local citation artifacts.

RetainedResults uses Store's established input grant and private artifact root.
Records live at results/INPUT_GRANT_SHA256/RECORD_SHA256.json with orr1 identifiers.
The record hash binds format, grant, producer provenance and complete content together.
The receipt separately hashes canonical content bytes for inline delivery, fragments and exports.
Repeated publication of identical records verifies existing bytes rather than replacing files.
Different outputs from identical requests remain distinct results, never automatic execution cache hits.

Publication reuses delivery.save_export's locked temporary file and atomic hard-link protocol.
Reads reuse store.safe_read and reject symlinks, special files and mismatched record hashes.
Existing or1 documents and their source files, manifests and passages are never rewritten.
Comparison producers must supply subject labels mapped to readable, same-grant normalized inputs.
Retention verifies those references but does not rerun comparison or certify report conclusions.

Only the normalized envelope's top-level backend_raw is excluded before storage.
Other values, including nulls, warnings and partial or failed statuses, remain unchanged.
For example, an empty text channel does not prevent retaining a schema-valid failed response.
Responses still obey the existing response schema; retention never manufactures missing fields.
No parser, provider, credential resolver or ambient configuration reader runs here.

This storage primitive adds no document or result-size cap and performs no automatic eviction.
It materializes JSON in memory; bounded replies do not imply bounded process memory.
Fingerprints detect corruption, not malicious rewriting by an actor controlling the entire store.
Source hashes and implementation identities are assertions of the trusted producer.
The comparison MCP producer is implemented in openreading.mcp_server.comparison.
General execution producers remain separate implementation work.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as ModelError

from openreading.artifacts.delivery import save_export
from openreading.artifacts.intake import directory
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import (
    ResultContent,
    ResultError,
    ResultKind,
    ResultProvenance,
    ResultReceipt,
    ResultRecord,
)
from openreading.artifacts.store import Store, safe_read


class RetainedResults:
    """Trusted producer storage and grant-scoped retrieval, with no source execution."""

    def __init__(self, store: Store):
        self.store = store
        self.root = store.config.artifact_root / "results" / store.grant
        with directory(self.root, create=True):
            pass

    def path(self, identifier: str) -> Path:
        if not re.fullmatch(r"orr1_[0-9a-f]{64}", identifier):
            raise ResultError("result_not_found")
        return self.root / (identifier.removeprefix("orr1_") + ".json")

    def publish(
        self, kind: ResultKind, payload: dict, provenance: ResultProvenance
    ) -> ResultReceipt:
        try:
            content = ResultContent(
                kind=kind,
                provenance=provenance,
                payload={k: v for k, v in payload.items() if k != "backend_raw"}
                if kind == "normalized_response"
                else payload,
            )
            # Snapshot caller-owned containers before validation or publication can overlap edits.
            record = ResultRecord.model_validate_json(
                json_bytes(
                    ResultRecord(input_grant_sha256=self.store.grant, content=content).wire()
                )
            )
        except (ValueError, TypeError, ModelError, ValidationError):
            raise ResultError("invalid_result") from None
        for reference in record.content.provenance.subjects.values():
            if reference.startswith("or1_"):
                self.store.load_document(reference)
            elif self.load(reference).kind != "normalized_response":
                raise ResultError("invalid_result")
        data = json_bytes(record.wire())
        identifier = "orr1_" + hashlib.sha256(data).hexdigest()
        try:
            save_export(self.root.parent, self.store.grant, data)
        except ArtifactError as error:
            if error.code == "artifact_corrupt":
                raise ResultError("result_corrupt") from None
            raise
        return self.receipt(identifier, record.content)

    def load(self, identifier: str) -> ResultContent:
        path = self.path(identifier)
        try:
            path.lstat()
            data = safe_read(path, None)
            if hashlib.sha256(data).hexdigest() != identifier.removeprefix("orr1_"):
                raise ValueError("Record digest mismatch")
            record = ResultRecord.model_validate_json(data)
            if record.input_grant_sha256 != self.store.grant or json_bytes(record.wire()) != data:
                raise ValueError("Grant or canonical record mismatch")
            return record.content
        except FileNotFoundError:
            raise ResultError("result_not_found") from None
        except (OSError, ValueError, TypeError, ModelError, ValidationError, ArtifactError):
            raise ResultError("result_corrupt") from None

    @staticmethod
    def receipt(identifier: str, content: ResultContent) -> ResultReceipt:
        data = json_bytes(content.wire())
        return ResultReceipt(
            result_id=identifier,
            kind=content.kind,
            content_bytes=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
        )
