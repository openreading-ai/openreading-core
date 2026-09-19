"""Execute one internal general request with no MCP stdout or model-controlled acquisition.

The parent passes captured authority through stdin and supplies a private copied source.
Authorization repeats before reading that source or resolving any credentials.
The copied bytes must match the parent's digest and become the shared API's in-memory input.
For example, replacing source after acquisition refuses before adapter execution.
Filename and MIME metadata survive this replacement so adapter format handling stays shared.

The parent owns process-group cancellation and output validation. This module has no public tool.
Its private --parent-fd pipe closes when the supervisor disappears, including abrupt OS termination.
A watcher then terminates this process group. It refuses to arm outside its own group.
Local process termination does not establish remote-provider cancellation.
A resume packet additionally binds the complete retained snapshot before using explicit configuration, ledger and credential arguments.
A complete schema-valid response is written privately, including partial or failed provider statuses.
Only top-level backend_raw is excluded; typed fields, warnings and explicit nulls remain unchanged.
Exceptions produce exit status 1 with no provider diagnostic or credential written to stdout or disk.
Ordinary provider stdout/stderr are discarded by the parent rather than interpreted as control data.

Environment variables this module reads
---------------------------------------
EnvCredentialBroker receives the explicit child environment created by execution_process.
The shared strategy API reads OPENREADING_LEDGER, which the parent sets to the attempt's private journal.
The child receives no ambient host environment unless the operator explicitly supplies those entries.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import sys
import threading
from contextlib import suppress
from pathlib import Path

from openreading.api import resume_run, run_request
from openreading.artifacts.models import json_bytes
from openreading.artifacts.store import safe_read
from openreading.config import router_config
from openreading.credentials import EnvCredentialBroker
from openreading.derive.mime import resolve_mime_type
from openreading.mcp_server.execution import ExecutionConfig
from openreading.schemas import validate_response
from openreading.types.request import OpenReadingRequest


def execute(packet: dict) -> dict:
    """Reauthorize a private control packet and preserve the shared execution response."""
    authority = ExecutionConfig.from_operator(
        config=packet["configuration"],
        allowed_backends=packet["allowed_backends"],
        allowed_strategies=packet["allowed_strategies"],
    )
    plan = authority.authorize(packet["request"])
    if "resume" in packet:
        from openreading.mcp_server.resume_input import verify_snapshot

        snapshot = packet["resume"]
        if snapshot["request"] != json.loads(plan.request_json):
            raise ValueError("Resume request mismatch")
        verify_snapshot(Path.cwd(), authority, snapshot)
        payload = resume_run(
            snapshot["run_id"],
            ledger_root=Path.cwd() / "ledger",
            config=json.loads(authority.configuration),
            broker=EnvCredentialBroker(environ=dict(os.environ)),
            backend_allowlist=authority.allowed_backends,
            keep_candidates=True,
        )
        validate_response(payload)
        return {key: value for key, value in payload.items() if key != "backend_raw"}
    raw = safe_read(Path.cwd() / "source", None)
    if hashlib.sha256(raw).hexdigest() != packet["source_sha256"]:
        raise ValueError("Copied source digest mismatch")
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
    loaded = authority.loaded
    payload = run_request(
        OpenReadingRequest.model_validate(body),
        broker=EnvCredentialBroker(environ=dict(os.environ)),
        config=router_config(loaded.config.policy),
        strategy_config=loaded.config,
        plain_info=loaded.plain_info,
        backend_allowlist=authority.allowed_backends,
        keep_candidates=True,
    )
    validate_response(payload)
    return {key: value for key, value in payload.items() if key != "backend_raw"}


def watch_parent(fd: int) -> None:
    """Kill this owned process group if its supervisor disappears, without retaining diagnostics."""
    if os.getpgrp() != os.getpid():
        raise ValueError("Worker must own process group before arming liveness")
    os.set_inheritable(fd, False)

    def observe():
        # No data is sent. EOF or a broken control channel means ownership was lost.
        with suppress(OSError):
            os.read(fd, 1)
        with suppress(OSError):
            os.close(fd)
        try:
            os.killpg(os.getpid(), signal.SIGKILL)
        finally:
            os._exit(1)

    threading.Thread(target=observe, daemon=True).start()


def main(parent_fd: int | None = None) -> int:
    try:
        if parent_fd is not None:
            watch_parent(parent_fd)
        payload = execute(json.load(sys.stdin))
        fd = os.open("response.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(json_bytes(payload))
            output.flush()
            os.fsync(output.fileno())
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-fd", required=True, type=int)
    raise SystemExit(main(parser.parse_args().parent_fd))
