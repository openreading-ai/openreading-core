"""Isolate native extraction in a killable child process with metered output writes.

The parent creates the job file and owns its deadline, process group, lock, and cleanup.
The parent selects PyMuPDF or the explicit local Docling profile. No job names an endpoint.
Docling checks PDFium page limits before conversion and preserves the warm converter.
Missing physical pages reject the entire conversion rather than retaining incomplete evidence.
A partial result is retained only when its page count still matches the source preflight.
Its private control pipe carries bounded stage and completion records, never extracted text.
Parser stdout is discarded by the parent; MCP stdout remains protocol-only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import PageOrigin, json_bytes
from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse

SETTINGS = {
    "outputs": {
        "text": True,
        "blocks": True,
        "markdown": False,
        "include_backend_raw": False,
        "tables": "none",
    },
    "features": {"ocr": "off", "tables": False},
}


__all__ = ["SETTINGS", "main"]


def extract(job: dict, *, client=None, progress=None) -> dict:
    from openreading import run

    if job.get("docling") is not None:
        from openreading.adapters.docling_local.client import preflight_pdf
    else:
        from openreading.adapters.pymupdf.intake import preflight_pdf

    root = Path(job["directory"])
    if progress is not None:
        progress("preflight")
    try:
        page_count, protected = preflight_pdf(root / "source.pdf")
    except Exception:
        raise ArtifactError("unsupported_format") from None
    if protected:
        raise ArtifactError("password_required")
    if page_count > job["pages"]:
        raise ArtifactError("input_too_large")
    if progress is not None:
        progress("conversion")
    origins: dict[str, PageOrigin] = {}
    if job.get("docling") is not None:
        from openreading.adapters.docling_local.client import LocalDoclingClient
        from openreading.adapters.docling_local.config import LocalDoclingConfig
        from openreading.adapters.docling_local.projection import project_document
        from openreading.types.request import Outputs

        configuration = LocalDoclingConfig.from_wire(job["docling"])
        if configuration.validate_assets() != job.get("expected_assets"):
            raise ArtifactError("engine_identity_unavailable")
        if client is None:
            client = LocalDoclingClient(configuration)
        raw = client.convert((root / "source.pdf").read_bytes())
        response, page_origins = project_document(raw, Outputs(**SETTINGS["outputs"]))
        origins = {str(page): origin for page, origin in page_origins.items()}
    else:
        response = NormalizedResponse.model_validate(
            run(
                str(root / "source.pdf"),
                backend="pymupdf",
                config={"version": 1},
                outputs=SETTINGS["outputs"],
                features=SETTINGS["features"],
            )
        )
    if response.document.page_count != page_count:
        raise ArtifactError("parse_failed")
    if response.status.state not in {"succeeded", "partial"}:
        raise ArtifactError("parse_failed")
    passages = list(iter_passages(response, origins))
    if not any(p.text.strip() for p in passages):
        raise ArtifactError("no_readable_text")
    if progress is not None:
        progress("writing")
    remaining = job["available"]
    extraction_size = 0
    for name, records in [
        ("response.json", [response.to_schema_dict()]),
        ("passages.jsonl", [p.wire() for p in passages]),
    ]:
        with (root / name).open("xb") as stream:
            os.chmod(root / name, 0o600)
            for record in records:
                data = json_bytes(record) + b"\n"
                extraction_size += len(data)
                remaining -= len(data)
                if extraction_size > job["extraction_bytes"]:
                    raise ArtifactError("extraction_too_large")
                if remaining < 0:
                    raise ArtifactError("storage_limit")
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    return origins


def serve_worker(control_fd: int) -> int:
    """Process sequential private jobs without exposing native diagnostics to MCP."""
    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig

    os.set_inheritable(control_fd, False)
    client = None
    configuration = None
    while True:
        line = sys.stdin.buffer.readline(8193)
        if not line:
            return 0
        if len(line) > 8192 or not line.endswith(b"\n"):
            return 2
        job = json.loads(line)
        identifier = job["id"]

        def send(value, identifier=identifier):
            os.write(control_fd, json_bytes({"id": identifier, **value}) + b"\n")

        try:
            if configuration is None:
                configuration = job["docling"]
                client = LocalDoclingClient(LocalDoclingConfig.from_wire(configuration))
            elif configuration != job["docling"]:
                raise ArtifactError("parse_failed")
            origins = extract(job, client=client, progress=lambda stage: send({"stage": stage}))
            result = Path(job["directory"]) / "result.json"
            result.write_bytes(json_bytes({"ok": True, "page_origins": origins}))
            os.chmod(result, 0o600)
            send({"ok": True})
        except ArtifactError as error:
            send({"error": error.code})
        except Exception:
            send({"error": "parse_failed"})


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run one parent-created artifact extraction job.")
    parser.add_argument("--job-file")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--control-fd", type=int)
    args = parser.parse_args(argv)
    if args.serve and args.control_fd is not None:
        return serve_worker(args.control_fd)
    if args.job_file is None:
        return 2
    job_file = Path(args.job_file)
    if job_file.stat().st_size > 8192:
        return 2
    job = json.loads(job_file.read_bytes())
    result = job_file.parent / "result.json"
    try:
        extract(job)
        payload = {"ok": True}
    except ArtifactError as error:
        payload = {"error": error.code}
    except Exception:
        payload = {"error": "parse_failed"}
    result.write_bytes(json_bytes(payload))
    os.chmod(result, 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
