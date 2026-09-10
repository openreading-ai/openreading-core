"""Isolate native extraction in a killable child process with metered output writes.

The parent creates the job file and owns its deadline, process group, lock, and cleanup.
Only this fixed PyMuPDF profile is executable. No job value selects code or an endpoint.
Parser stdout is discarded by the parent; MCP stdout remains protocol-only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
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


def extract(job: dict) -> None:
    from openreading import run
    from openreading.adapters.pymupdf.intake import preflight_pdf

    root = Path(job["directory"])
    try:
        page_count, protected = preflight_pdf(root / "source.pdf")
    except Exception:
        raise ArtifactError("unsupported_format") from None
    if protected:
        raise ArtifactError("password_required")
    if page_count > job["pages"]:
        raise ArtifactError("input_too_large")
    response = NormalizedResponse.model_validate(
        run(
            str(root / "source.pdf"),
            backend="pymupdf",
            config={"version": 1},
            outputs=SETTINGS["outputs"],
            features=SETTINGS["features"],
        )
    )
    if response.status.state not in {"succeeded", "partial"}:
        raise ArtifactError("parse_failed")
    passages = list(iter_passages(response))
    if not any(p.text.strip() for p in passages):
        raise ArtifactError("no_readable_text")
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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run one parent-created artifact extraction job.")
    parser.add_argument("--job-file", required=True)
    args = parser.parse_args(argv)
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
