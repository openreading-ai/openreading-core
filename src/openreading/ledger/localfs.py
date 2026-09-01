"""`LocalFsBlobStore` + `LocalFsKeyStore` — the core implementations of the `BlobStore`/`KeyStore`
ports (internal/design/ledger.md §9.4). Blobs are encrypted with AES-256-GCM (`cryptography`'s
`AESGCM`, a base dependency now that the ledger is core, not an extra): an AEAD fails decryption
closed on tampering or on-disk corruption instead of silently handing back altered plaintext,
closing M6 (T1's original stdlib SHA-256-counter-mode XOR stream had no authentication at all —
integrity depended on a digest the journal recorded separately, and `BlobStore.get` never actually
checked ciphertext against it; `get` now does check it, on the legacy branch alone — see there).

Every blob `put` writes today is `_FORMAT_AEAD (1 byte) + nonce (12 bytes) + ciphertext‖tag`, under
the run's own 32-byte key, with `run_id` itself as AAD — a blob decrypted against any run id but
its own fails authentication even with the right key file, which is the point (§5.4's per-run
addressing enforced cryptographically, not merely by filesystem layout). `_keystream`/`_xor` are
KEPT, LEGACY-read-only (`put` never calls them): they decode T1's original format, still readable
so a blob written by the pre-upgrade binary — a run already in flight when a deploy swaps it —
survives the upgrade instead of stranding that run. That branch verifies the ref's plaintext
digest before returning, which is the closest a format with no authentication tag can come to
failing closed: a tampered or truncated legacy blob raises `PayloadExpired` rather than decoding
to silently wrong bytes.

Erasure is real deletion, not a flag: `KeyStore.destroy` unlinks the one file holding the run's
key, so `BlobStore.get` after a shred cannot construct a keystream at all — it never reaches the
point of decrypting-with-a-wrong-key, it fails at the key lookup itself (`PayloadExpired`).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openreading.ledger.ports import PayloadExpired
from openreading.ledger.retention import VALID_RUN_ID
from openreading.ledger.step import BlobRef

_KEY_BYTES = 32  # AES-256 key size — unchanged across the T1 XOR cipher and the AES-256-GCM upgrade
_AEAD_NONCE_BYTES = 12  # GCM's standard nonce size; a fresh one per blob, never reused under a key

_FORMAT_AEAD = b"\x02"
# Leading byte of every blob `put` writes today. A LEGACY blob (below) begins with a random 16-byte
# nonce, so roughly 1 legacy blob in 256 happens to start with this same byte by chance. `get`
# resolves the ambiguity by trying AEAD first whenever the leading byte matches, and never falls
# back to the legacy decode on InvalidTag: the legacy XOR cipher has no way to fail (any bytes XOR
# a same-length keystream "succeed"), so a fallback would silently return garbage plaintext for a
# genuinely tampered AEAD blob instead of raising — exactly the failure mode this upgrade exists to
# close. A real 0x02-leading legacy blob is simply unreadable after the upgrade; retention ages it
# out rather than this code trying to recover it.

# LEGACY (pre-M6): T1's original format, `nonce (16 bytes) + XOR-ciphertext`, unauthenticated.
# `_keystream`/`_xor` are read-only from here on: kept solely so `get` can still decode a blob a
# pre-upgrade binary already wrote.
_LEGACY_NONCE_BYTES = 16


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
        if not VALID_RUN_ID.fullmatch(run_id):
            raise ValueError(f"malformed run_id {run_id!r}")
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
        if not VALID_RUN_ID.fullmatch(run_id):
            raise ValueError(f"malformed run_id {run_id!r}")
        safe_digest = digest.split(":", 1)[-1]
        if not re.fullmatch(r"[A-Fa-f0-9]{16,128}", safe_digest):
            raise ValueError(f"malformed digest {digest!r}")
        d = self._root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{safe_digest}.bin"

    def put(self, run_id: str, digest: str, data: bytes, media_type: str) -> BlobRef:
        key = self._keys.get_or_create(run_id)
        nonce = secrets.token_bytes(_AEAD_NONCE_BYTES)
        # run_id as AAD: authenticated but not encrypted, so decrypting this ciphertext under any
        # run_id other than the one it was written for fails the tag check (see module docstring).
        ciphertext = AESGCM(key).encrypt(nonce, data, run_id.encode())
        self._path(run_id, digest).write_bytes(_FORMAT_AEAD + nonce + ciphertext)
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
        if raw[:1] == _FORMAT_AEAD:
            nonce = raw[1 : 1 + _AEAD_NONCE_BYTES]
            ciphertext = raw[1 + _AEAD_NONCE_BYTES :]
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, ref.run_id.encode())
            except InvalidTag as exc:
                # Deliberately not a fallback to the legacy path below — see _FORMAT_AEAD's own
                # comment for why that would defeat the whole point of moving to an AEAD.
                raise PayloadExpired(
                    ref.run_id, reason="ciphertext failed authentication (tampered or corrupted)"
                ) from exc
            except ValueError as exc:
                # A blob truncated short enough that the nonce slice above comes out under 8
                # bytes makes AESGCM.decrypt reject it before it ever reaches the tag check
                # ("Nonce must be between 8 and 128 bytes") -- verified empirically that every
                # other truncation length already raises InvalidTag on its own. Still "this store
                # cannot produce a plaintext for this ref," just caught one step earlier.
                raise PayloadExpired(
                    ref.run_id, reason="ciphertext truncated or corrupted"
                ) from exc
        # LEGACY (pre-M6): see module docstring and the _keystream/_xor definitions above.
        nonce, ciphertext = raw[:_LEGACY_NONCE_BYTES], raw[_LEGACY_NONCE_BYTES:]
        plaintext = _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))
        # The digest is the ONLY integrity signal this format carries, so it is checked here and
        # nowhere else: the XOR stream has no way to fail — any bytes XOR a same-length keystream
        # "succeed" — so tampering or on-disk corruption would otherwise leave `get` handing back
        # silently altered plaintext, exactly the failure the AEAD upgrade closed for every blob
        # written since. Truncation lands here too: a short read decodes to a short plaintext just
        # as cleanly, and only the digest tells the two apart. The AEAD branch above deliberately
        # does NOT repeat this check — its tag already authenticates the ciphertext against both
        # the run key and the run id, and re-hashing every plaintext on read would cost a full
        # SHA-256 pass over payloads up to the document size cap to prove something already proven.
        expected = ref.digest.split(":", 1)[-1].lower()
        if hashlib.sha256(plaintext).hexdigest() != expected:
            raise PayloadExpired(
                ref.run_id, reason="legacy blob failed its plaintext digest (tampered or corrupted)"
            )
        return plaintext
