"""The local-filesystem blob store: `blobs/<run_id>/<digest>.bin`, written as-is.

Blobs used to be encrypted with AES-256-GCM under a per-run key at `keys/<run_id>.key`. That key
sat in the same directory tree as the ciphertext it protected, so anyone who could read one could
read the other, and the package docstring admitted the one scenario it served: a backup that
excludes `keys/`. It cost `cryptography` as a base dependency imported by this file alone.

Encryption at rest is the operator's, and their disk already does it better. On a machine they
control it protected against very little; where the ledger runs on storage they do not control, it
is the hosted product's problem rather than this package's.

What a reader should take from that: `OPENREADING_LEDGER` means "copy every document I process,
and every full response, into this directory, in the clear". The `openreading.ledger` docstring
says so at arming time, because that disclosure is what actually protects somebody, and a key
filed next to the ciphertext never did.

Run-id and digest are still validated before either becomes a path segment, which is the check
that keeps a crafted id from escaping the store.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from openreading.ledger.step import BlobRef

# `<run_id>` and `<digest>` both land in a filesystem path, so both are constrained before use.
VALID_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class LocalFsBlobStore:
    """Content-addressed plaintext blobs under `root/<run_id>/<digest>.bin`.

    `get` verifies the SHA-256 digest before returning bytes. Plaintext storage removes the
    authenticated cipher, but a modified response must still never replay as recorded output.
    """

    def __init__(self, root: Path, keys: object | None = None) -> None:
        # Keep the deprecated argument so external BlobStore setup does not fail during migration.
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str, digest: str) -> Path:
        if not VALID_RUN_ID.fullmatch(run_id):
            raise ValueError(f"malformed run_id {run_id!r}")
        safe_digest = digest.split(":", 1)[-1]
        if not re.fullmatch(r"[A-Fa-f0-9]{16,128}", safe_digest):
            raise ValueError(f"malformed digest {digest!r}")
        d = self._root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{safe_digest}.bin"

    def put(self, run_id: str, digest: str, data: bytes, media_type: str) -> BlobRef:
        self._path(run_id, digest).write_bytes(data)
        return BlobRef(
            run_id=run_id,
            digest=digest,
            size_bytes=len(data),
            media_type=media_type,
            store="localfs",
        )

    def get(self, ref: BlobRef) -> bytes:
        body = self._path(ref.run_id, ref.digest).read_bytes()
        algorithm, separator, expected = ref.digest.partition(":")
        if separator != ":" or algorithm != "sha256":
            raise OSError(f"unsupported blob digest {ref.digest!r}")
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected.lower():
            raise OSError(f"blob digest mismatch for {ref.digest}")
        return body
