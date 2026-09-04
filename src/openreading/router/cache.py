"""The idempotency key and the document identity every result cache is keyed by.

Most document backends publish no idempotency-key mechanism, so OpenReading derives one.
`content_key` hashes a document identity, the backend id, the resolved backend version, and
the request options that can change the result. Two callers derive the document part
differently on purpose. The executor identifies a local file by `(realpath, size, mtime_ns)`
through `document_identity`, which never reads the file. The ledger hashes the real bytes
through `document_digest`, because its key has to mean the same thing on another machine.

For a deterministic local backend such as `pymupdf` or `tesseract`, the output is a pure
function of that key, so the same cache doubles as exactly-once. The key is derived from
content and never from secrets, and `credentials_ref` and `webhook_url` are excluded from the
option canonicalization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Protocol

from openreading.types.job import Job
from openreading.types.request import DocumentInput

_DIGEST_CHUNK_BYTES = 1024 * 1024  # 1 MB — matches batch/sources.py's own streaming-hash chunk

# Request fields that change the RESULT (and so belong in the key). Everything else — transport,
# async mode, credentials, webhook target, idempotency_key itself — is excluded.
_OPTION_FIELDS = ("outputs", "extraction_schema", "features", "pages")


def document_identity(d: DocumentInput) -> bytes | None:
    """The document's CONTENT identity, or None when it is only a locator.

    `url` / `file_id` name a remote address that can serve different bytes tomorrow, so they can
    never key a result cache — an uncacheable document is a miss, never a stale hit. A local path
    is identified by `(realpath, size, mtime_ns)` rather than by reading the file, which may be
    huge; the blind spot is an in-place rewrite that preserves both size and mtime (the same
    heuristic make and rsync rely on). Shared by the executor's own result cache and
    `build_run_context`'s default idempotency key (BL-166) — both need "is this content
    identifiable" for the same reason. This path branch is filesystem-metadata identity, not a
    content digest — `internal/design/ledger.md` §5.5 ("Three identities, deliberately distinct")
    already tracks this as machine-local and not safe to key a shared/cross-worker store on; the
    Ledger milestone's own `content_key` derives from a real blob digest instead."""
    if d.bytes_base64:
        return b"b64:" + d.bytes_base64.encode()
    if d.path:
        try:
            st = os.stat(d.path)
        except OSError:
            return None
        return f"file:{os.path.realpath(d.path)}:{st.st_size}:{st.st_mtime_ns}".encode()
    return None


def document_digest(d: DocumentInput) -> bytes | None:
    """The document's real CONTENT digest (sha256 of the actual bytes, streamed for a local path
    rather than loaded whole) — portable across workers, unlike `document_identity`'s machine-local
    `(realpath, size, mtime_ns)` blob for a path. `internal/design/ledger.md` §5.5 ("Three identities,
    deliberately distinct") requires Ledger's own `content_key` to be built from THIS, never from
    `document_identity` — a relocated run computing a different key for byte-identical content is
    exactly the failure §5.5 names. Returns the raw digest BYTES (not hex)
    so a caller can feed it straight into `content_key`, which hashes whatever `document_bytes` it
    receives again internally — hashing a digest is still a valid, unique identifier, and avoids
    loading a large local file whole just to identify it. `None` for a URL/file_id locator (no bytes
    without fetching), matching `document_identity`'s own conservative rule for the same case."""
    if d.bytes_base64:
        return hashlib.sha256(base64.b64decode(d.bytes_base64)).digest()
    if d.path:
        h = hashlib.sha256()
        try:
            with open(d.path, "rb") as fh:
                for chunk in iter(lambda: fh.read(_DIGEST_CHUNK_BYTES), b""):
                    h.update(chunk)
        except OSError:
            return None
        return h.digest()
    return None


def canonical_options(request_dict: dict[str, Any]) -> str:
    """Stable JSON of the result-affecting request options (sorted keys, no whitespace)."""
    backend = request_dict.get("backend", {}) or {}
    opts: dict[str, Any] = {
        "operation": backend.get("operation"),
        # runtime.device/mode can change output (e.g. ocr on/off), keep the result-relevant bits
        "runtime": {
            k: (backend.get("runtime") or {}).get(k)
            for k in ("mode", "device")
            if (backend.get("runtime") or {}).get(k) is not None
        }
        or None,
    }
    for f in _OPTION_FIELDS:
        if request_dict.get(f) is not None:
            opts[f] = request_dict[f]
    return json.dumps(opts, sort_keys=True, separators=(",", ":"), default=str)


def content_key(
    document_bytes: bytes,
    backend_id: str,
    resolved_version: str | None,
    request_dict: dict[str, Any],
) -> str:
    """The idempotency key: `om_` followed by a sha256 over four NUL-separated parts, which
    are sha256(document_bytes), the backend id, the resolved version, and the canonical
    options.

    `document_bytes` may itself already be a digest or an identity string, because the two
    callers disagree about what identifies a document. Hashing it again is harmless and keeps
    the key unique either way.
    """
    h = hashlib.sha256()
    h.update(hashlib.sha256(document_bytes).digest())
    h.update(b"\x00")
    h.update(backend_id.encode())
    h.update(b"\x00")
    h.update((resolved_version or "").encode())
    h.update(b"\x00")
    h.update(canonical_options(request_dict).encode())
    return "om_" + h.hexdigest()


class ResultCache(Protocol):
    def get(self, key: str) -> Job | None: ...
    def put(self, key: str, job: Job) -> None: ...


class InMemoryResultCache:
    """A process-local cache holding every entry for the life of the process. Any other store
    satisfying `ResultCache` can replace it, and the key derivation does not change, so
    neither do the cache semantics. `executor.BoundedResultCache` is what the shipped surfaces
    use, with an LRU bound and a TTL."""

    def __init__(self) -> None:
        self._store: dict[str, Job] = {}

    def get(self, key: str) -> Job | None:
        return self._store.get(key)

    def put(self, key: str, job: Job) -> None:
        self._store[key] = job
