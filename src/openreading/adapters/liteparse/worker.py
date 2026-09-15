"""Run LiteParse parses inside core's supervised worker process.

`openreading.artifacts.supervisor.WarmWorker` starts this module with `--serve --control-fd N`,
writes one JSON job per line to stdin, and reads bounded control records from the pipe. The
parent owns the deadline, the sampled memory ceiling, the process group and cleanup, so this
child only parses. Its stdout and stderr are discarded. The result goes to a file inside the
private work directory the parent created, never over the 8 KiB control pipe.

The child hashes the OCR data again before importing `liteparse`, because the file can change
between the parent's check and this process starting. It never passes an OCR server URL, so
recognition stays on this machine.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
from pathlib import Path

from openreading.adapters.liteparse.assets import file_sha256

# Text-item metadata that no normalized channel reads. Dropping it keeps the result file bounded.
_ITEM_NOISE = ("char_codes", "words")


class WorkerFailure(Exception):
    """A control-protocol error code from `openreading.artifacts.limits.MESSAGES`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def liteparse_options(job: dict) -> dict:
    """The only LiteParse constructor arguments this profile uses."""
    options = {
        "ocr_enabled": bool(job.get("ocr_enabled")),
        "extract_blocks": True,
        "extract_form_fields": True,
        "quiet": True,
        "num_workers": 1,
    }
    if options["ocr_enabled"]:
        options["tessdata_path"] = job["tessdata_path"]
        options["ocr_language"] = job["ocr_language"]
    return options


def _serialize(result) -> dict:
    data = dataclasses.asdict(result)
    data.pop("images", None)
    data.pop("screenshots", None)
    for page in data.get("pages") or []:
        for item in page.get("text_items") or []:
            for key in _ITEM_NOISE:
                item.pop(key, None)
    return data


def parse_document(job: dict) -> dict:
    if job.get("ocr_enabled"):
        path = Path(job["tessdata_path"]) / f"{job['ocr_language']}.traineddata"
        try:
            digest = file_sha256(path)
        except OSError:
            raise WorkerFailure("engine_identity_unavailable") from None
        if digest != job.get("expected_sha256"):
            raise WorkerFailure("engine_identity_unavailable")
    try:
        # Imported by name so a checker resolving this package's own `liteparse` directory
        # cannot mistake it for the engine.
        engine = importlib.import_module("liteparse")
    except ImportError:
        raise WorkerFailure("parse_failed") from None
    try:
        result = engine.LiteParse(**liteparse_options(job)).parse(job["input"])
    except engine.ParseError as error:
        message = str(error).lower()
        if "password" in message:
            raise WorkerFailure("password_required") from None
        if "invalid pdf" in message or "unsupported" in message:
            raise WorkerFailure("unsupported_format") from None
        raise WorkerFailure("parse_failed") from None
    return _serialize(result)


def serve(control_fd: int) -> int:
    os.set_inheritable(control_fd, False)
    while True:
        line = sys.stdin.buffer.readline(8193)
        if not line:
            return 0
        if len(line) > 8192 or not line.endswith(b"\n"):
            return 2
        job = json.loads(line)
        identifier = job["id"]

        def send(value: dict, identifier: str = identifier) -> None:
            os.write(control_fd, (json.dumps({"id": identifier, **value}) + "\n").encode())

        try:
            send({"stage": "conversion"})
            data = parse_document(job)
            send({"stage": "writing"})
            with open(job["output"], "x", encoding="utf-8") as stream:
                json.dump(data, stream)
            send({"ok": True})
        except WorkerFailure as failure:
            send({"error": failure.code})
        except Exception:
            send({"error": "parse_failed"})


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve LiteParse jobs for a supervised parent.")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--control-fd", type=int, required=True)
    args = parser.parse_args(argv)
    return serve(args.control_fd) if args.serve else 2


if __name__ == "__main__":
    sys.exit(main())
