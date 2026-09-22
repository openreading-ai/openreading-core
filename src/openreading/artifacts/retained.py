"""Retain and query normalized documents without importing a parsing engine.

The caller supplies an immutable identity and implements document acquisition.
For example, an HTTP client retains a server response and reuses search and exports.
Local parsing remains in openreading.artifacts.service. No fallback selects that service.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from pathlib import Path

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import ArtifactManifest, EngineIdentity, ImportReceipt, json_bytes
from openreading.artifacts.store import Store


def _display_name(relative: str) -> str:
    name = "".join(
        c
        for c in relative.split("/")[-1]
        if not unicodedata.category(c).startswith("C")
        and unicodedata.category(c) not in {"Zl", "Zp"}
    )
    return name.encode("utf-8")[:255].decode("utf-8", errors="ignore") or "document.pdf"


class RetainedService:
    def __init__(self, config: ProfileConfig, *, identity: EngineIdentity):
        from openreading.artifacts.document import DocumentCache

        self._document_cache = DocumentCache()
        self.store = Store(config)
        self.config = self.store.config
        self.identity = identity

    def close(self):
        self._document_cache.entry = None
        self.store.close()

    def load_artifact(self, identifier: str) -> ArtifactManifest:
        return self.store.load(identifier)[0]

    def import_document(
        self,
        path: str,
        *,
        cancelled: threading.Event | None = None,
        progress=None,
        page_progress=None,
    ) -> ImportReceipt:
        raise NotImplementedError("The client must supply document acquisition.")

    def _available(self, reserve: int = 0) -> int | None:
        cap = self.config.limits.store_bytes
        return None if cap is None else cap - self.store.size() - reserve

    @staticmethod
    def _fsync(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _check_time(self, started: float, cancelled: threading.Event | None) -> None:
        if cancelled is not None and cancelled.is_set():
            raise ArtifactError("cancelled")
        if (
            self.config.limits.deadline_seconds is not None
            and time.monotonic() - started >= self.config.limits.deadline_seconds
        ):
            raise ArtifactError("timeout")

    def _receipt(self, manifest: ArtifactManifest, *, reused: bool) -> ImportReceipt:
        result = ImportReceipt(
            schema_version="0.5" if manifest.acquisition is not None else "0.4",
            artifact_id=manifest.artifact_id,
            display_name=manifest.display_name,
            document_sha256=manifest.document_sha256,
            page_count=manifest.page_count,
            passage_count=manifest.passage_count,
            reused=reused,
            warnings=manifest.warnings,
            extraction_state=manifest.acquisition.extraction_state
            if manifest.acquisition
            else None,
            next_action="search" if manifest.passage_count else "get_document",
        )
        if len(json_bytes(result.wire())) > self.config.limits.import_bytes:
            raise ArtifactError("response_too_large")
        return result

    def search(self, artifact_id: str, query: str, limit: int = 5, cursor: str | None = None):
        from openreading.artifacts.search import search

        manifest, passages = self.store.load(artifact_id)
        return search(manifest, passages, query, limit, cursor, self.config.limits.search_bytes)

    def read(self, artifact_id: str, evidence_ids: list[str], cursor: str | None = None):
        from openreading.artifacts.search import read

        manifest, passages = self.store.load(artifact_id)
        return read(manifest, passages, evidence_ids, cursor, self.config.limits.read_bytes)

    def get_document(self, artifact_id: str, cursor: str | None = None):
        from openreading.artifacts.document import get_document

        manifest, passages, response = self.store.load_document(artifact_id)
        return get_document(
            manifest,
            passages,
            response,
            cursor,
            self.config.limits.document_bytes,
            cache=self._document_cache,
        )
