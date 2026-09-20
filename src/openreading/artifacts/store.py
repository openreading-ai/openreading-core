"""Commit private artifacts atomically and verify every retained file before reuse.

One advisory lock serializes imports across processes. Reads only observe committed
artifact directories. Hashes detect corruption, not malicious rewriting by the same OS user.
No eviction occurs automatically. Administrators remove retained directories to reclaim space.
Storage accounting includes regular files throughout the root, including completed exports.
Links within the default exports directory are ignored by accounting, never followed.
Managed artifact storage still refuses links, and export access validates its own destination.
The private worker directory is the working directory of parser processes, which keeps
their relative writes inside the artifact root instead of wherever a client started the server.
Passages are decoded and checked against the normalized response one line at a time.
The response is decoded once, and comparison generates one expected passage at a time.
Retrieval still materializes the normalized document and returned passage list; memory grows
with retained content. Bounded reply sizes do not establish bounded server-process memory.
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

from openreading.artifacts.intake import InputGrant, ancestor_identities, directory
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import ArtifactManifest, FileRecord, Passage, artifact_id
from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse

__all__ = ["Store", "safe_read"]


def safe_read(path: Path, cap: int | None) -> bytes:
    with directory(path.parent) as parent_fd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular file")
            data = stream.read() if cap is None else stream.read(cap + 1)
            if cap is not None and len(data) > cap:
                raise ValueError("File exceeds cap")
            return data


def file_record(path: Path, cap: int | None) -> FileRecord:
    """Fingerprint source bytes incrementally without retaining a document-sized buffer."""
    with directory(path.parent) as parent_fd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular file")
            digest, length = hashlib.sha256(), 0
            while data := stream.read(65536):
                length += len(data)
                if cap is not None and length > cap:
                    raise ValueError("File exceeds cap")
                digest.update(data)
            return FileRecord(length=length, sha256=digest.hexdigest())


def _verified_lines(path: Path, record: FileRecord, cap: int | None) -> Iterator[bytes]:
    """Hash one descriptor's lines; callers must exhaust this iterator before publishing results."""
    with directory(path.parent) as parent_fd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Not a regular file")
            digest, length = hashlib.sha256(), 0
            for line in stream:
                length += len(line)
                if cap is not None and length > cap:
                    raise ValueError("File exceeds cap")
                digest.update(line)
                yield line
            if FileRecord(length=length, sha256=digest.hexdigest()) != record:
                raise ValueError("Integrity mismatch")


class Store:
    def __init__(self, config: ProfileConfig):
        self.config = config
        if not config.input_root.is_absolute() or not config.artifact_root.is_absolute():
            raise ArtifactError("configuration_required")
        self.input_grant = InputGrant(config.input_root)
        a, b = self.input_grant.path, config.artifact_root.resolve()
        from dataclasses import replace

        self.config = replace(config, input_root=a, artifact_root=b)
        if a == b or a in b.parents or b in a.parents:
            raise ArtifactError("configuration_required")
        with directory(a):
            pass
        for path in (b, b / "staging", b / "documents", b / "worker"):
            with directory(path, create=True) as fd:
                os.fchmod(fd, 0o700)
        with directory(b) as fd:
            metadata = os.fstat(fd)
            if (
                self.input_grant.identity in ancestor_identities(fd)
                or (metadata.st_dev, metadata.st_ino) in self.input_grant.ancestors
            ):
                raise ArtifactError("configuration_required")
        self.grant = hashlib.sha256(
            json.dumps([str(a), *self.input_grant.identity]).encode()
        ).hexdigest()
        self.documents = b / "documents" / self.grant
        with directory(self.documents, create=True):
            pass

    def source(self, relative: str):
        return self.input_grant.source(relative)

    def close(self):
        self.input_grant.close()

    def size(self) -> int:
        total = 0
        exports = self.config.artifact_root / "exports"
        for root, dirs, files in os.walk(self.config.artifact_root, followlinks=False):
            for name in dirs + files:
                path = Path(root) / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    # Export links must not poison unrelated imports or charge their targets.
                    if path == exports or path.is_relative_to(exports):
                        continue
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
        manifest, passages, _ = self._load_document(identifier, include_response=False)
        return manifest, passages

    def load_document(self, identifier: str) -> tuple[ArtifactManifest, list[Passage], dict]:
        """Return retained normalized values and passages after verifying their bytes."""
        return self._load_document(identifier, include_response=True)

    def _load_document(
        self, identifier: str, *, include_response: bool
    ) -> tuple[ArtifactManifest, list[Passage], dict]:
        if not re.fullmatch(r"or1_[0-9a-f]{64}", identifier):
            raise ArtifactError("artifact_not_found")
        root = self.documents / identifier
        if not root.exists():
            raise ArtifactError("artifact_not_found")
        try:
            raw = json.loads(safe_read(root / "manifest.json", self.config.limits.extraction_bytes))
            if not isinstance(raw, dict):
                raise ValueError("Manifest is not an object")
            if raw.get("format") not in {"local-document.v0.3", "local-document.v0.4"}:
                raise ArtifactError("artifact_version_unsupported")
            manifest = ArtifactManifest.model_validate(raw)
            if (
                manifest.artifact_id != identifier
                or artifact_id(
                    manifest.document_sha256,
                    manifest.engine,
                    version=manifest.format.rsplit("v", 1)[1],
                    source_file=manifest.source_file,
                )
                != identifier
                or manifest.input_grant_sha256 != self.grant
            ):
                raise ValueError("Identity mismatch")
            measured = file_record(root / manifest.source_file, self.config.limits.source_bytes)
            if (
                measured != manifest.files[manifest.source_file]
                or measured.sha256 != manifest.document_sha256
            ):
                raise ValueError("Source mismatch")
            contents = safe_read(root / "response.json", self.config.limits.extraction_bytes)
            record = manifest.files["response.json"]
            if (
                len(contents) != record.length
                or hashlib.sha256(contents).hexdigest() != record.sha256
            ):
                raise ValueError("Integrity mismatch")
            data = json.loads(contents)
            del contents
            response = NormalizedResponse.model_validate(data)
            if not include_response:
                data = {}
            expected = iter_passages(response, manifest.page_origins)
            passages = []
            for line in _verified_lines(
                root / "passages.jsonl",
                manifest.files["passages.jsonl"],
                self.config.limits.extraction_bytes,
            ):
                passage = Passage.model_validate_json(line)
                if passage != next(expected, None):
                    raise ValueError("Evidence mismatch")
                passages.append(passage)
            if (
                next(expected, None) is not None
                or len(passages) != manifest.passage_count
                or response.document.page_count != manifest.page_count
            ):
                raise ValueError("Evidence mismatch")
            return manifest, passages, data
        except ArtifactError as error:
            if error.code == "artifact_version_unsupported":
                raise
            raise ArtifactError("artifact_corrupt") from None
        except (OSError, ValueError, ValidationError, TypeError, KeyError):
            raise ArtifactError("artifact_corrupt") from None
