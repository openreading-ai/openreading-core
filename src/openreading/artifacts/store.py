"""Commit private artifacts atomically and verify every retained file before reuse.

One advisory lock serializes imports across processes. Reads only observe committed
artifact directories. Hashes detect corruption, not malicious rewriting by the same OS user.
No eviction occurs automatically. Administrators remove retained directories to reclaim space.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from openreading.artifacts.intake import directory
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import ArtifactManifest, Passage, artifact_id
from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse


def safe_read(path: Path, cap: int) -> bytes:
    with directory(path.parent):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular file")
            data = stream.read(cap + 1)
            if len(data) > cap:
                raise ValueError("File exceeds cap")
            return data


class Store:
    def __init__(self, config: ProfileConfig):
        self.config = config
        a, b = config.input_root, config.artifact_root
        if a == b or a in b.parents or b in a.parents:
            raise ArtifactError("configuration_required")
        with directory(a):
            pass
        for path in (b, b / "staging", b / "documents"):
            with directory(path, create=True) as fd:
                os.fchmod(fd, 0o700)
        self.grant = hashlib.sha256(str(a).encode()).hexdigest()
        self.documents = b / "documents" / self.grant
        with directory(self.documents, create=True):
            pass

    def size(self) -> int:
        total = 0
        for root, dirs, files in os.walk(self.config.artifact_root, followlinks=False):
            for name in dirs + files:
                metadata = (Path(root) / name).lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ArtifactError("artifact_corrupt")
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
        return total

    @contextmanager
    def import_lock(self) -> Iterator[None]:
        with directory(self.config.artifact_root) as root_fd:
            fd = os.open(
                ".import.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd
            )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ArtifactError("busy") from None
            # No active importer can own staging while this lock is held.
            for path in (self.config.artifact_root / "staging").iterdir():
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
            yield
        finally:
            os.close(fd)

    def load(self, identifier: str) -> tuple[ArtifactManifest, list[Passage]]:
        if not re.fullmatch(r"or1_[0-9a-f]{64}", identifier):
            raise ArtifactError("artifact_not_found")
        root = self.documents / identifier
        if not root.exists():
            raise ArtifactError("artifact_not_found")
        try:
            raw = json.loads(safe_read(root / "manifest.json", 65536))
            if not isinstance(raw, dict):
                raise ValueError("Manifest is not an object")
            if raw.get("format") != "local-document.v1":
                raise ArtifactError("artifact_version_unsupported")
            manifest = ArtifactManifest.model_validate(raw)
            if (
                manifest.artifact_id != identifier
                or artifact_id(manifest.document_sha256, manifest.engine) != identifier
                or manifest.input_grant_sha256 != self.grant
            ):
                raise ValueError("Identity mismatch")
            data = {}
            for name, record in manifest.files.items():
                cap = (
                    self.config.limits.source_bytes
                    if name == "source.pdf"
                    else self.config.limits.extraction_bytes
                )
                contents = safe_read(root / name, cap)
                if (
                    len(contents) != record.length
                    or hashlib.sha256(contents).hexdigest() != record.sha256
                ):
                    raise ValueError("Integrity mismatch")
                data[name] = contents
            if hashlib.sha256(data["source.pdf"]).hexdigest() != manifest.document_sha256:
                raise ValueError("Source mismatch")
            response = NormalizedResponse.model_validate_json(data["response.json"])
            passages = [
                Passage.model_validate_json(line) for line in data["passages.jsonl"].splitlines()
            ]
            if (
                passages != list(iter_passages(response))
                or len(passages) != manifest.passage_count
                or response.document.page_count != manifest.page_count
            ):
                raise ValueError("Evidence mismatch")
            return manifest, passages
        except ArtifactError as error:
            if error.code == "artifact_version_unsupported":
                raise
            raise ArtifactError("artifact_corrupt") from None
        except (OSError, ValueError, ValidationError, TypeError, KeyError):
            raise ArtifactError("artifact_corrupt") from None
