#!/usr/bin/env python3
"""Upload selected folder contents serially using Python's standard library and installed curl.

You select a folder containing files for your backend, a processing implementation such
as Docling. Each backend reads the formats its descriptor claims in
src/openreading/adapters/README.md. No OpenReading installation is required here.
Traversal follows openreading.batch.sources: relative paths sort bytewise, and hidden
entries and symlinks are skipped. For example, a/report maps to a/report beneath your
separate output directory. Every output contains the HTTP response, regardless of its
extension. Preserving names avoids suffix collisions between a and a.response.json/b,
and supports filenames already at their filesystem length limit. Existing response
files are replaced on reruns. Inaccessible entries receive individual local errors
while readable siblings continue. Each inaccessible directory counts as one failed
entry because its undiscovered contents cannot be counted.

Each stdout line is JSON: one outcome per file followed by counts for each outcome.
HTTP failures retain their response bodies, while transport and local failures have
separate outcomes. Exit 0 means all selected uploads succeeded, 1 reports failures,
and 2 reports invalid arguments. No retries run because disconnected requests may
already have started processing. Curl streams files directly without Python buffering.

Environment variables this module reads
--------------------------------------
OPENREADING_API_KEY supplies the optional bearer token for the server you started.
When unset or empty, uploads omit authentication. PATH locates the installed curl.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def selected_files(root: Path) -> list[tuple[Path, OSError | None]]:
    """Return regular files and local failures in bytewise order while preserving readable siblings."""
    selected: list[tuple[Path, OSError | None]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    path = Path(entry.path)
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except OSError as error:
                        selected.append((path, error))
                        continue
                    if stat.S_ISDIR(mode):
                        pending.append(path)
                    elif stat.S_ISREG(mode):
                        selected.append((path, None))
        except OSError as error:
            selected.append((directory, error))
    return sorted(selected, key=lambda item: os.fsencode(item[0].relative_to(root).as_posix()))


def upload(path: Path, destination: Path, url: str, backend: str, token: str) -> dict:
    """Return an individual outcome while retaining response bytes from completed HTTP requests."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A disconnected rerun must not leave a previous success looking like its response.
        destination.unlink(missing_ok=True)
        # Quote curl's form grammar independently of shell quoting, including commas and quotes.
        quoted = str(path).replace("\\", "\\\\").replace('"', '\\"')
        command = [
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--proto",
            "=http,https",
            "--request",
            "POST",
            "--output",
            str(destination),
            "--write-out",
            "%{http_code}",
            "--form",
            f'file=@"{quoted}"',
            "--form-string",
            "request=" + json.dumps({"backend": {"id": backend}}),
        ]
        if token:
            # Standard input keeps the bearer token out of process-list command arguments.
            command.extend(["--header", "@-"])
        command.extend(["--url", url])
        result = subprocess.run(
            command,
            input="Authorization: Bearer " + token + "\n" if token else "",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return {"outcome": "local_error", "message": error.strerror or "Local operation failed"}
    if result.returncode:
        # Curl distinguishes local file-read/write failures from incomplete network transfers.
        outcome = "local_error" if result.returncode in (23, 26, 37) else "transport_error"
        return {"outcome": outcome, "curl_exit": result.returncode}
    try:
        status = int(result.stdout)
    except ValueError:
        return {"outcome": "transport_error", "message": "curl returned no HTTP status"}
    if status == 0:
        return {"outcome": "transport_error", "message": "curl returned no HTTP status"}
    return {"outcome": "success" if 200 <= status < 300 else "http_error", "http_status": status}


def main() -> int:
    """Validate local destinations before uploading files and printing individual outcomes with totals."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "source", type=Path, help="Folder containing the files you select for upload."
    )
    parser.add_argument(
        "--url", required=True, help="Full parse endpoint URL, including /v1/parse."
    )
    parser.add_argument("--backend", required=True, help="Backend identifier, for example docling.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Response directory outside the input tree, preserving relative filenames.",
    )
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error("source must be a readable directory")
    if output == source or source in output.parents:
        parser.error("output must be outside the input tree")
    url = urlsplit(args.url)
    if url.scheme not in ("http", "https") or not url.netloc:
        parser.error("url must be a full HTTP or HTTPS endpoint URL")
    token = os.environ.get("OPENREADING_API_KEY", "")
    if "\r" in token or "\n" in token:
        parser.error("OPENREADING_API_KEY must not contain line breaks")
    totals = dict(total=0, success=0, http_error=0, transport_error=0, local_error=0)
    for path, traversal_error in selected_files(source):
        relative = path.relative_to(source).as_posix()
        destination = output / relative
        try:
            if traversal_error is not None:
                raise traversal_error
            resolved = destination.resolve()
            if (
                resolved == source
                or source in resolved.parents
                or output not in resolved.parents
                or destination.is_symlink()
            ):
                result = {
                    "outcome": "local_error",
                    "message": "Response path leaves the output tree",
                }
            else:
                result = upload(path, destination, args.url, args.backend, token)
        except OSError as error:
            result = {
                "outcome": "local_error",
                "message": error.strerror or "Local operation failed",
            }
        totals["total"] += 1
        totals[result["outcome"]] += 1
        print(json.dumps({"path": relative, **result}), flush=True)
    print(json.dumps(totals), flush=True)
    return int(bool(totals["http_error"] or totals["transport_error"] or totals["local_error"]))


if __name__ == "__main__":
    raise SystemExit(main())
