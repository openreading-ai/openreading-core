"""Import once, then search and read verified page evidence across process restarts.

The service copies an opened source into private staging before launching a disposable
native parser process. Cancellation and the deadline terminate the entire child process
group before staging is removed. A successful receipt follows file and directory fsync.
Installed distributions record package/backend versions and a fingerprint of their extraction dependency code.
No Git repository or native parser import participates in parent-side identity discovery.
A core_commit is omitted unless trusted packaging metadata supplies it.

Frozen launchers must verify openreading/engine-identity.json before invoking this service.
That file contains the EngineIdentity fields, including extraction settings and optional commit.
When sys.frozen is true, the executable must dispatch --internal-artifact-worker to
openreading.artifacts.worker.main with the remaining arguments. This is the packager's
versioned integration contract; ordinary Python installs use the module entry point.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from openreading.artifacts.intake import copy_source, source
from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import (
    ArtifactManifest,
    EngineIdentity,
    FileRecord,
    ImportReceipt,
    WarningCode,
    artifact_id,
    json_bytes,
)
from openreading.artifacts.store import Store, safe_read
from openreading.artifacts.worker import SETTINGS

__all__ = ["ArtifactService", "engine_identity"]


class _WithoutDocstrings(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        return node


def _source_tree_hash(package: Path) -> str:
    # Extraction calls API, routing, normalization and type code as well as the adapter.
    # Ignore documentation so editing CLI help cannot consume another retained-artifact slot.
    selected = {p for p in package.glob("*.py")}
    for name in (
        "artifacts",
        "schemas",
        "types",
        "derive",
        "router",
        "ledger",
        "batch",
        "adapters/pymupdf",
    ):
        selected.update(p for p in (package / name).rglob("*") if p.suffix in {".py", ".json"})
    selected.update((package / "adapters").glob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(selected):
        contents = path.read_bytes()
        if path.suffix == ".py":
            tree = _WithoutDocstrings().visit(ast.parse(contents))
            contents = ast.dump(tree, include_attributes=False).encode()
        digest.update(str(path.relative_to(package)).encode())
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()


def engine_identity() -> EngineIdentity:
    package = Path(__file__).resolve().parents[1]
    built = package / "engine-identity.json"
    try:
        if built.exists():
            return EngineIdentity.model_validate_json(built.read_bytes())
        return EngineIdentity(
            core_version=importlib.metadata.version("openreading"),
            backend_version=importlib.metadata.version("pymupdf"),
            extraction_settings={**SETTINGS, "source_tree_sha256": _source_tree_hash(package)},
        )
    except importlib.metadata.PackageNotFoundError:
        raise ImportError("Install the local profile dependencies.") from None
    except (OSError, ValueError, TypeError, SyntaxError):
        raise ArtifactError("engine_identity_unavailable") from None


def _display_name(relative: str) -> str:
    name = "".join(
        c for c in relative.split("/")[-1] if not unicodedata.category(c).startswith("C")
    )
    return name.encode("utf-8")[:255].decode("utf-8", errors="ignore") or "document.pdf"


class ArtifactService:
    def __init__(self, config: ProfileConfig, *, identity: EngineIdentity | None = None):
        self.config = config
        self.store = Store(config)
        self.identity = identity or engine_identity()

    def load_artifact(self, identifier: str) -> ArtifactManifest:
        return self.store.load(identifier)[0]

    def import_document(
        self, path: str, *, cancelled: threading.Event | None = None
    ) -> ImportReceipt:
        started = time.monotonic()
        self._check_time(started, cancelled)
        with source(self.config.input_root, path) as fd, self.store.import_lock():
            staging = Path(tempfile.mkdtemp(dir=self.config.artifact_root / "staging"))
            try:
                available = self.config.limits.store_bytes - self.store.size()
                digest, changed = copy_source(
                    fd,
                    staging / "source.pdf",
                    self.config.limits.source_bytes,
                    available,
                    check=lambda: self._check_time(started, cancelled),
                )
                self._check_time(started, cancelled)
                identifier = artifact_id(digest, self.identity)
                if (self.store.documents / identifier).exists():
                    manifest = self.load_artifact(identifier)
                    self._check_time(started, cancelled)
                    return self._receipt(manifest, reused=True)
                self._check_time(started, cancelled)
                job = {
                    "directory": str(staging),
                    "pages": self.config.limits.pages,
                    "extraction_bytes": self.config.limits.extraction_bytes,
                    "available": self.config.limits.store_bytes - self.store.size() - 65536,
                }
                if job["available"] < 0:
                    raise ArtifactError("storage_limit")
                (staging / "job.json").write_bytes(json_bytes(job))
                os.chmod(staging / "job.json", 0o600)
                self._worker(staging, started, cancelled)
                response = json.loads(
                    safe_read(staging / "response.json", self.config.limits.extraction_bytes)
                )
                passages = safe_read(
                    staging / "passages.jsonl", self.config.limits.extraction_bytes
                ).splitlines()
                files = {}
                for name in ("source.pdf", "response.json", "passages.jsonl"):
                    contents = safe_read(
                        staging / name,
                        max(self.config.limits.source_bytes, self.config.limits.extraction_bytes),
                    )
                    files[name] = FileRecord(
                        length=len(contents), sha256=hashlib.sha256(contents).hexdigest()
                    )
                warnings: list[WarningCode] = []
                if changed:
                    warnings.append("source_changed")
                if response.get("warnings"):
                    warnings.append("parser_warnings_present")
                manifest = ArtifactManifest(
                    artifact_id=identifier,
                    document_sha256=digest,
                    display_name=_display_name(path),
                    source_relative_path=path,
                    input_grant_sha256=self.store.grant,
                    page_count=response["document"]["page_count"],
                    passage_count=len(passages),
                    engine=self.identity,
                    created_at=datetime.now(UTC).isoformat(),
                    files=files,
                    warnings=warnings,
                )
                encoded = json_bytes(manifest.wire())
                if self.store.size() + len(encoded) > self.config.limits.store_bytes:
                    raise ArtifactError("storage_limit")
                with (staging / "manifest.json").open("xb") as stream:
                    os.chmod(staging / "manifest.json", 0o600)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / "job.json").unlink()
                (staging / "result.json").unlink()
                self._check_time(started, cancelled)
                receipt = self._receipt(manifest, reused=False)
                self._fsync(staging)
                os.rename(staging, self.store.documents / identifier)
                self._fsync(self.store.documents)
                return receipt
            except OSError:
                raise ArtifactError("storage_limit") from None
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

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
        if time.monotonic() - started >= self.config.limits.deadline_seconds:
            raise ArtifactError("timeout")

    def _worker(self, staging: Path, started: float, cancelled: threading.Event | None) -> None:
        command = [sys.executable]
        if getattr(sys, "frozen", False):
            command += ["--internal-artifact-worker"]
        else:
            command += ["-m", "openreading.artifacts.worker"]
        command += ["--job-file", str(staging / "job.json")]
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                while process.poll() is None:
                    self._check_time(started, cancelled)
                    time.sleep(0.02)
                self._check_time(started, cancelled)
                if process.returncode:
                    raise ArtifactError("parse_failed")
            finally:
                if process.poll() is None:
                    # The child can exit between poll and kill without changing the failure.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        try:
            result = json.loads(safe_read(staging / "result.json", 1024))
            if "error" in result:
                from openreading.artifacts.models import ToolError

                error = ToolError(code=result["error"], message="", retryable=False)
                raise ArtifactError(error.code)
            if result != {"ok": True}:
                raise ValueError("Invalid worker result")
        except (OSError, ValueError):
            raise ArtifactError("parse_failed") from None

    def _receipt(self, manifest: ArtifactManifest, *, reused: bool) -> ImportReceipt:
        result = ImportReceipt(
            artifact_id=manifest.artifact_id,
            display_name=manifest.display_name,
            document_sha256=manifest.document_sha256,
            page_count=manifest.page_count,
            passage_count=manifest.passage_count,
            reused=reused,
            warnings=manifest.warnings,
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
