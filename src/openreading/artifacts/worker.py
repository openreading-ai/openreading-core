"""Isolate native extraction in a killable child process with metered output writes.

The parent creates the job file and owns its deadline, process group, lock, and cleanup.
The parent selects PyMuPDF or the explicit local Docling profile. No job names an endpoint.
Docling checks PDFium page limits for PDF inputs and preserves the warm converter.
Other formats retain their source suffix and use provider conversion without PDF preflight.
Provider-reported model-free table cells survive without enabling raster table recognition.
Missing physical pages reject the entire conversion rather than retaining incomplete evidence.
A partial result is retained only when its page count still matches the source preflight.
Its private control pipe carries bounded stage, observed page and completion records, never extracted text.
Distinct successful page assembly is counted against PDFium preflight, without inferring successful OCR.
Updates coalesce at one-second intervals, with initial and final counts flushed explicitly.
Document-wide assembly and publication can still fail after all pages have been assembled.
Parser stdout is discarded by the parent; MCP stdout remains protocol-only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import PageOrigin, json_bytes
from openreading.artifacts.passages import iter_passages
from openreading.types.import_job import PageProgress
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


def extract(job: dict, *, client=None, progress=None, page_progress=None) -> dict:
    from openreading import run

    if job.get("docling") is not None:
        from openreading.adapters.docling_local.client import preflight_pdf
    else:
        from openreading.adapters.pymupdf.intake import preflight_pdf

    root = Path(job["directory"])
    source_file = job.get("source_file", "source.pdf")
    if (
        not isinstance(source_file, str)
        or re.fullmatch(r"source\.[a-z0-9]{1,16}", source_file) is None
    ):
        raise ArtifactError("unsupported_format")
    source = root / source_file
    if progress is not None:
        progress("preflight")
    try:
        page_count, protected = (
            preflight_pdf(source)
            if source.suffix == ".pdf" or job.get("docling") is None
            else (None, False)
        )
    except Exception:
        raise ArtifactError("unsupported_format") from None
    if protected:
        raise ArtifactError("password_required")
    if job["pages"] is not None and page_count is not None and page_count > job["pages"]:
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
        if page_progress is None or page_count is None:
            raw = client.convert_path(source)
        else:
            assembled = set()
            last_count, last_time = -1, 0.0

            def publish(force=False):
                nonlocal last_count, last_time
                now = time.monotonic()
                count = len(assembled)
                if count != last_count and (force or now - last_time >= 1):
                    page_progress(PageProgress(pages_assembled=count, total_pages=page_count))
                    last_count, last_time = count, now

            def completed(page):
                if type(page) is not int or not 1 <= page <= page_count:
                    raise ValueError("Page observation is outside the physical source.")
                assembled.add(page)
                publish(force=len(assembled) == page_count)

            publish(force=True)
            try:
                raw = client.convert_path(source, page_completed=completed)
            finally:
                publish(force=True)
        outputs = dict(SETTINGS["outputs"])
        if raw.get("unpaginated"):
            # Model-free formats already provide cells without running a table model.
            outputs["tables"] = "cells"
        response, page_origins = project_document(raw, Outputs(**outputs))
        origins = {str(page): origin for page, origin in page_origins.items()}
    else:
        response = NormalizedResponse.model_validate(
            run(
                str(source),
                backend="pymupdf",
                config={"version": 1},
                outputs=SETTINGS["outputs"],
                features=SETTINGS["features"],
            )
        )
    if page_count is not None and response.document.page_count != page_count:
        raise ArtifactError("parse_failed")
    if (
        job["pages"] is not None
        and response.document.page_count is not None
        and response.document.page_count > job["pages"]
    ):
        raise ArtifactError("input_too_large")
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
                if remaining is not None:
                    remaining -= len(data)
                if (
                    job["extraction_bytes"] is not None
                    and extraction_size > job["extraction_bytes"]
                ):
                    raise ArtifactError("extraction_too_large")
                if remaining is not None and remaining < 0:
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
            origins = extract(
                job,
                client=client,
                progress=lambda stage: send({"stage": stage}),
                page_progress=lambda pages: send(pages.model_dump()),
            )
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
