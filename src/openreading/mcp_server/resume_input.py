"""Snapshot one retained strategy attempt without acquiring the caller's original source again.

Resume accepts a job identity, never a caller-selected journal directory or run identifier.
The job owner resolves this module's directory beneath execution/<grant> after checking its bound records.
Current authority must authorize the original request and every backend pinned by its strategy header.
For example, revoking a backend refuses before any retained source bytes are read.
A configuration change refuses rather than silently compiling a different continuation.

The snapshot includes the source, one header and journal, and that run's content-addressed blobs.
Every path is opened without following symbolic links. Special files and changed bytes refuse.
The original attempt remains unchanged; the new attempt owns subsequent journal writes.
Hashes detect corruption, not malicious rewriting by the OS user who owns the retained store.
An incomplete journal can redispatch an attempted step whose terminal outcome was never recorded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from openreading.artifacts.intake import directory
from openreading.artifacts.models import json_bytes
from openreading.artifacts.store import safe_read
from openreading.derive.mime import resolve_mime_type
from openreading.ledger.header import RunHeader, slim_request_dict
from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from openreading.types.request import OpenReadingRequest


def inspect_attempt(root: Path, authority: ExecutionConfig, value: dict) -> dict:
    """Bind allowed retained files after reauthorizing the original strategy request."""
    plan = authority.authorize(value)
    if plan.strategy is None:
        raise ValueError("No strategy journal")
    with directory(root / "ledger") as fd:
        headers = [name for name in os.listdir(fd) if name.endswith(".header.json")]
    if len(headers) != 1:
        raise ValueError("Expected one strategy header")
    run_id = headers[0].removesuffix(".header.json")
    if not re.fullmatch(r"[0-9a-f-]{36}", run_id):
        raise ValueError("Invalid retained run identity")
    header = RunHeader.from_dict(json.loads(safe_read(root / "ledger" / headers[0], None)))
    if header.run_id != run_id or header.strategy_name != plan.strategy:
        raise ValueError("Strategy identity mismatch")
    if (
        not header.pinned_eligible
        or not header.pinned_eligible.keys() <= authority.allowed_backends
    ):
        raise ExecutionRefused("scope_denied")
    raw = safe_read(root / "source", None)
    digest = hashlib.sha256(raw).hexdigest()
    if (
        header.document is None
        or header.document_is_url
        or header.document.run_id != run_id
        or header.document.digest != "sha256:" + digest
        or header.document.size_bytes != len(raw)
    ):
        raise ValueError("Retained input identity mismatch")
    req = plan.request
    assert req.document.path is not None
    filename = req.document.filename or Path(req.document.path).name
    body = req.to_schema_dict()
    body["document"] = {
        "bytes_base64": base64.b64encode(raw).decode(),
        "filename": filename,
        "mime_type": resolve_mime_type(
            mime_type=req.document.mime_type, filename=filename, data=raw
        ),
    }
    if header.slim_request != slim_request_dict(OpenReadingRequest.model_validate(body)):
        raise ValueError("Retained request identity mismatch")
    names = ["source", "ledger/" + headers[0]]
    journal = f"ledger/{run_id}.jsonl"
    try:
        (root / journal).lstat()
    except FileNotFoundError:
        pass  # Arming precedes the first attempted step, so a header alone is resumable.
    else:
        names.append(journal)
    blob_dir = root / "ledger" / "blobs" / run_id
    with directory(blob_dir) as fd:
        for name in os.listdir(fd):
            if not re.fullmatch(r"[0-9a-f]{64}\.bin", name):
                raise ValueError("Invalid retained blob name")
            names.append(f"ledger/blobs/{run_id}/{name}")
    files = {
        name: hashlib.sha256(safe_read(root / name, None)).hexdigest() for name in sorted(names)
    }
    if files["source"] != digest:
        raise ValueError("Source changed during inspection")
    return {
        "directory": root.name,
        "request": json.loads(plan.request_json),
        "run_id": run_id,
        "source_sha256": digest,
        "files": files,
    }


def copy_snapshot(root: Path, target: Path, snapshot: dict, check: Callable[[], None]) -> None:
    """Copy only bound regular files into a new attempt while retaining cancellation checks."""
    for name, digest in snapshot["files"].items():
        check()
        # The manifest is internal, but refuse malformed paths again before constructing targets.
        if not re.fullmatch(
            r"source|ledger/[0-9a-f-]{36}\.(header\.json|jsonl)|ledger/blobs/[0-9a-f-]{36}/[0-9a-f]{64}\.bin",
            name,
        ):
            raise ValueError("Invalid snapshot path")
        data = safe_read(root / name, None)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Snapshot changed")
        dest = target / name
        with directory(dest.parent, create=True):
            pass
        with dest.open("xb") as stream:
            os.chmod(dest, 0o600)
            stream.write(data)
    check()


def verify_snapshot(root: Path, authority: ExecutionConfig, snapshot: dict) -> None:
    """Repeat request, source, header and complete snapshot binding inside the isolated child."""
    observed = inspect_attempt(root, authority, snapshot["request"])
    for name in ("run_id", "source_sha256", "files", "request"):
        if json_bytes(observed[name]) != json_bytes(snapshot[name]):
            raise ValueError("Resume snapshot identity mismatch")
