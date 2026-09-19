"""Run one authorized diagnostic without source documents or ambient host credentials.

The parent selects readiness or liveness through a private control packet, never a model-supplied module.
Backend authorization repeats before adapter construction, health checks or credential resolution.
For example, an unauthorized vendor cannot trigger a credential lookup merely because its name is registered.
Readiness reuses backend_readiness; liveness reuses check_liveness and preserves its measured-versus-inferred status ladder.
The parent isolates stdout and owns process-group cleanup, including its deadline and parent-liveness channel.
Reports exceeding the diagnostic byte ceiling are refused without truncation. Exceptions never persist provider diagnostics.

Environment variables this module reads
---------------------------------------
EnvCredentialBroker receives the explicit child environment supplied by execution_process, including only operator-forwarded credential values.
HOME, TMPDIR and XDG_CACHE_HOME point at private scratch directories, matching general execution isolation.
OPENREADING_CONFIG and OPENREADING_LEDGER are fixed by that runner rather than inherited from the MCP host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from openreading.adapters.registry import make_adapter
from openreading.artifacts.models import json_bytes
from openreading.credentials import EnvCredentialBroker, redact
from openreading.liveness import check_liveness
from openreading.mcp_server.execution import ExecutionRefused
from openreading.mcp_server.execution_worker import watch_parent
from openreading.readiness import backend_readiness
from openreading.types.diagnostic_tool import LivenessRequest, ReadinessReport, ReadinessRequest

MAX_DIAGNOSTIC_BYTES = 65536


def execute(packet: dict, *, environ: dict[str, str] | None = None) -> dict:
    """Repeat scope validation before resolving a backend or its explicit credential environment."""
    operation = packet["operation"]
    model = {"readiness": ReadinessRequest, "liveness": LivenessRequest}[operation]
    arguments = {"backend": packet["backend"]}
    if operation == "liveness":
        arguments["timeout_s"] = packet["timeout_s"]
    request = model.model_validate(arguments)
    if request.backend not in packet["allowed_backends"]:
        raise ExecutionRefused("scope_denied")
    adapter = make_adapter(request.backend)
    environment = dict(os.environ) if environ is None else dict(environ)
    broker = EnvCredentialBroker(environ=environment)
    if isinstance(request, LivenessRequest):
        report = check_liveness(adapter, broker=broker, timeout_s=request.timeout_s)
        # Configuration values can contain signed endpoints even when the descriptor labels them non-secret.
        values = {value for value in environment.values() if value}
        report.detail = redact(report.detail, values)
        report.version = redact(report.version, values) if report.version else report.version
        return report.to_schema_dict()
    return ReadinessReport.model_validate(
        {"readiness": asdict(backend_readiness(adapter, broker=broker))}
    ).wire()


def main(parent_fd: int) -> int:
    try:
        watch_parent(parent_fd)
        payload = execute(json.load(sys.stdin))
        raw = json_bytes(payload)
        if len(raw) > MAX_DIAGNOSTIC_BYTES:
            raw = json_bytes({"error": {"code": "response_too_large"}})
        fd = os.open("diagnostic.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-fd", required=True, type=int)
    raise SystemExit(main(parser.parse_args().parent_fd))
