"""`LocalFsBlobStore` + `LocalFsKeyStore` — the core implementations of the `BlobStore`/`KeyStore`
ports (internal/design/ledger.md §9.4). One AES-grade dependency (`cryptography`) is not in this run's
"no new dependencies" allowance (RUN.md §0) and the ledger package itself is scoped "zero new
required deps" (§5.2) — so T1 ships a stdlib-only SHA-256-counter-mode stream cipher instead of an
AEAD. This is a real cipher (a fresh 32-byte key from `secrets.token_bytes` per run, a fresh nonce
per blob), not a placeholder flag, but it is unauthenticated: integrity of the plaintext is the
journal's job (the recorded digest), not this cipher's. Flagged to FOUNDER-INBOX.md as a two-line
swap to `cryptography`'s `Fernet` once a new dependency is approved.

Erasure is real deletion, not a flag: `KeyStore.destroy` unlinks the one file holding the run's
key, so `BlobStore.get` after a shred cannot construct a keystream at all — it never reaches the
point of decrypting-with-a-wrong-key, it fails at the key lookup itself (`PayloadExpired`).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from openreading.ledger.ports import PayloadExpired
from openreading.ledger.step import BlobRef

_KEY_BYTES = 32
_NONCE_BYTES = 16


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream, strict=True))


class LocalFsKeyStore:
    """One file per run_id, mode 0600, under its own directory (mode 0700) — separate from
    whatever backs up the journal/blob directories (a documentation obligation for T1, per the
    plan's own §7: there is no backup system yet to integrate with)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # `mode=` on mkdir has no effect when the directory already exists (Path.mkdir's own
        # documented behavior with exist_ok=True) — chmod unconditionally so a keys/ directory
        # created earlier, by a different code path, or under a looser umask doesn't keep wider
        # permissions than this store requires (Phase C round-1, Medium).
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)

    def _path(self, run_id: str) -> Path:
        return self._root / f"{run_id}.key"

    def get_or_create(self, run_id: str) -> bytes:
        p = self._path(run_id)
        if p.exists():
            return p.read_bytes()
        key = secrets.token_bytes(_KEY_BYTES)
        # Create at 0600 atomically — writing then chmod'ing leaves the key briefly readable at
        # the process's default (umask-dependent) mode, e.g. 0644 (Phase C round-1, Medium).
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    def get(self, run_id: str) -> bytes:
        p = self._path(run_id)
        try:
            return p.read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(run_id) from exc

    def destroy(self, run_id: str) -> None:
        self._path(run_id).unlink(missing_ok=True)


class LocalFsBlobStore:
    """One ciphertext file per `(run_id, digest)`, under the run's own subdirectory."""

    def __init__(self, root: Path, keys: LocalFsKeyStore) -> None:
        self._root = root
        self._keys = keys
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str, digest: str) -> Path:
        safe_digest = digest.split(":", 1)[-1]
        d = self._root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{safe_digest}.bin"

    def put(self, run_id: str, digest: str, data: bytes, media_type: str) -> BlobRef:
        key = self._keys.get_or_create(run_id)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = _xor(data, _keystream(key, nonce, len(data)))
        self._path(run_id, digest).write_bytes(nonce + ciphertext)
        return BlobRef(
            run_id=run_id,
            digest=digest,
            size_bytes=len(data),
            media_type=media_type,
            store="localfs",
        )

    def get(self, ref: BlobRef) -> bytes:
        try:
            key = self._keys.get(ref.run_id)
        except KeyError as exc:
            raise PayloadExpired(ref.run_id) from exc
        raw = self._path(ref.run_id, ref.digest).read_bytes()
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))
