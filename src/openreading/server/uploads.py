"""Decode HTTP uploads into the existing request contract before endpoint validation.

Multipart requests contain one binary ``file`` and one UTF-8 JSON ``request`` field.
Document metadata permits only ``mime_type`` and ``password`` because the file supplies
both its source and filename. For example, a null ``document.path`` still conflicts.
Optional metadata uses the vendored property schemas, with value-free errors protecting passwords.
The basename strips both directory separator styles and never names temporary storage.
Explicit MIME metadata precedes the file header, then ``openreading.derive.mime`` inference.

The parser owns every spool until decoding finishes, including partially received files.
Cleanup runs on success, malformed input, disconnect, cancellation, and storage failure.
File bytes count against ``openreading.api._MAX_DOWNLOAD_BYTES`` as they arrive, so an
oversized file stops at the limit instead of after it has been spooled to disk.
Metadata is bounded separately at 1 MiB, while the application middleware bounds raw bodies.
Storage failures produce sanitized 500 errors without recording request content or passwords.
JSON bodies retain their existing values for the endpoint's schema validation to inspect.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from copy import deepcopy
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect, Request

from openreading import api, schemas
from openreading.derive.mime import resolve_mime_type

_MAX_METADATA_BYTES = 1024 * 1024
# The metadata an upload may carry. The file part supplies every other document field.
_DOCUMENT_METADATA_KEYS = ("mime_type", "password")
_STORAGE_FAILED = "Upload temporary storage failed."
_DISCONNECTED = "The client disconnected before the request body was complete."


class UploadError(Exception):
    """A sanitized transport error for the endpoint's existing response envelope."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _utf8_charset(content_type: str | bytes) -> None:
    _, options = parse_options_header(content_type)
    if options.get(b"charset", b"utf-8").lower() not in {b"utf-8", b"utf8"}:
        raise UploadError(400, "Multipart metadata must use UTF-8.")


def _filename(raw: bytes) -> str:
    name = raw.decode("utf-8").replace("\\", "/").rsplit("/", 1)[-1]
    if (
        not name
        or name in {".", ".."}
        or len(name.encode("utf-8")) > 255
        or any(unicodedata.category(char) == "Cc" for char in name)
    ):
        raise UploadError(400, "The file part requires a safe basename within 255 UTF-8 bytes.")
    return name


class _UploadParser(MultiPartParser):
    """Retain Starlette spooling while enforcing structure and strict metadata decoding."""

    def __init__(self, request: Request) -> None:
        super().__init__(
            request.headers,
            request.stream(),
            max_files=1,
            max_fields=1,
            max_part_size=_MAX_METADATA_BYTES,
        )
        self.complete = False
        self.names: set[bytes] = set()
        self.file_bytes = 0

    def on_headers_finished(self) -> None:
        part = self._current_part
        disposition, options = parse_options_header(part.content_disposition)
        name = options.get(b"name")
        if disposition.lower() != b"form-data" or name not in {b"file", b"request"}:
            raise UploadError(400, "Multipart requires exactly the file and request parts.")
        if name in self.names:
            raise UploadError(400, "Multipart part names must not repeat.")
        self.names.add(name)
        has_filename = b"filename" in options
        if name == b"request" and has_filename:
            raise UploadError(
                400, "The request part must be a field. Use request=<options.json with curl."
            )
        if name == b"file" and not has_filename:
            raise UploadError(400, "The file part requires a filename.")
        filename = _filename(options[b"filename"]) if has_filename else None
        if not has_filename:
            _utf8_charset(Headers(raw=part.item_headers).get("content-type", ""))
        super().on_headers_finished()
        if part.file is not None:
            part.file.filename = filename

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._current_part
        if part.file is None:
            if len(part.data) + end - start > _MAX_METADATA_BYTES:
                raise UploadError(413, f"The request field exceeds {_MAX_METADATA_BYTES} bytes.")
        else:
            # Starlette defers the spool write until the whole chunk is parsed, so UploadFile.size
            # lags this callback. Counting here stops an oversized file at the limit instead of
            # after the whole part has been spooled, which could cost up to the raw body ceiling.
            limit = api._MAX_DOWNLOAD_BYTES
            self.file_bytes += end - start
            if self.file_bytes > limit:
                raise UploadError(413, f"The uploaded file exceeds {limit} bytes.")
        super().on_part_data(data, start, end)

    def on_part_end(self) -> None:
        if self._current_part.file is None:
            # Starlette's fallback decoding would silently accept non-UTF-8 metadata.
            self.items.append(
                (self._current_part.field_name, self._current_part.data.decode("utf-8"))
            )
        else:
            super().on_part_end()

    def on_end(self) -> None:
        # Finalize alone accepts truncated bodies, so require the closing boundary callback.
        self.complete = True

    def close(self) -> bool:
        """Release every spool, complete or partial, and report whether all of them closed."""
        closed = True
        for file in self._files_to_close_on_error:
            try:
                file.close()
            except OSError:
                closed = False
        return closed


@cache
def _metadata_validator(key: str) -> Draft202012Validator:
    # Built once per key. The vendored schema is process-stable, and constructing a validator
    # re-checks its schema on every call (BL-167), so a per-request build is pure waste.
    return Draft202012Validator(
        schemas.request_schema()["properties"]["document"]["properties"][key]
    )


async def decode_request(request: Request) -> Any:
    """Return JSON values or normalize a complete upload into the ordinary request shape."""
    media_type, params = parse_options_header(request.headers.get("content-type", ""))
    if media_type.lower() != b"multipart/form-data":
        try:
            return await request.json()
        except (ValueError, UnicodeError) as e:
            # The decoder's own text names a position, never the body, so it stays useful
            # and safe to echo, the same wording the JSON handlers used before uploads.
            raise UploadError(400, f"invalid JSON body: {e}") from None
        except ClientDisconnect:
            raise UploadError(400, _DISCONNECTED) from None

    parser = _UploadParser(request)
    try:
        options = await _upload_options(parser, request.headers["content-type"], params)
    except BaseException:
        # The failure in flight is the diagnosis the client needs. A spool that also fails to
        # close must not replace a 400 or 413 with a 500 that invites a retry of the same body.
        parser.close()
        raise
    if not parser.close():
        raise UploadError(500, _STORAGE_FAILED)
    return options


async def _upload_options(parser: _UploadParser, content_type: str, params: dict) -> dict[str, Any]:
    try:
        _utf8_charset(content_type)
        boundary = params.get(b"boundary", b"")
        if not re.fullmatch(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}", boundary) or boundary.endswith(
            b" "
        ):
            raise UploadError(400, "Multipart requires a valid boundary.")
        form = await parser.parse()
        if not parser.complete or parser.names != {b"file", b"request"}:
            raise UploadError(400, "Multipart requires complete file and request parts.")
        upload, metadata = form["file"], form["request"]
        assert isinstance(upload, UploadFile) and isinstance(metadata, str)
        options = json.loads(metadata)
        if not isinstance(options, dict):
            raise UploadError(400, "The request field must contain a JSON object.")
        document = options.get("document", {})
        if not isinstance(document, dict) or set(document) - set(_DOCUMENT_METADATA_KEYS):
            raise UploadError(
                400,
                f"Upload document metadata permits only {' and '.join(_DOCUMENT_METADATA_KEYS)}.",
            )
        # Schema error messages can include password values, so reject these without rendering errors.
        for key, value in document.items():
            if not _metadata_validator(key).is_valid(value):
                raise UploadError(400, "Upload document metadata has an invalid field value.")
        # The parser already refused anything past api._MAX_DOWNLOAD_BYTES while it streamed.
        data = await upload.read()
        if not data:
            raise UploadError(400, "The uploaded file must not be empty.")
        # A supplied MIME override takes precedence over file headers and byte inference.
        if "mime_type" not in document:
            part_type, _ = parse_options_header(upload.content_type or "")
            hint = None
            if part_type.lower() not in {b"", b"application/octet-stream"}:
                hint = part_type.decode("latin-1")
            resolved = resolve_mime_type(mime_type=hint, filename=upload.filename, data=data)
            if resolved is not None:
                document["mime_type"] = resolved
        document.update(
            bytes_base64=base64.b64encode(data).decode("ascii"), filename=upload.filename
        )
        options["document"] = document
        return options
    except (MultiPartException, MultipartParseError, ValueError, UnicodeError):
        raise UploadError(400, "Invalid multipart request or UTF-8 JSON metadata.") from None
    except ClientDisconnect:
        raise UploadError(400, _DISCONNECTED) from None
    except OSError:
        raise UploadError(500, _STORAGE_FAILED) from None


def request_body_schema(*, include_keep_candidates: bool = False) -> dict[str, Any]:
    """Describe both encodings using independent copies of the vendored request schema."""
    normal = deepcopy(schemas.request_schema())
    normal.pop("$id", None)
    if include_keep_candidates:
        normal["properties"]["keep_candidates"] = {"type": "boolean", "default": False}
    metadata = deepcopy(normal)
    metadata["title"] = "UploadRequestOptions"
    metadata["required"].remove("document")
    original_document = metadata["properties"]["document"]
    metadata["properties"]["document"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: original_document["properties"][key] for key in _DOCUMENT_METADATA_KEYS
        },
        "description": "Optional MIME override and password. The file part supplies the source and filename.",
    }
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": normal,
                "example": {
                    "backend": {"id": "pymupdf"},
                    "document": {"bytes_base64": "aGVsbG8=", "filename": "report.txt"},
                },
            },
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file", "request"],
                    "additionalProperties": False,
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": f"One nonempty document, at most {api._MAX_DOWNLOAD_BYTES} bytes. A safe basename within 255 UTF-8 bytes is required.",
                        },
                        "request": {
                            **metadata,
                            "example": {"backend": {"id": "pymupdf"}, "outputs": {"text": True}},
                        },
                    },
                },
                "encoding": {"request": {"contentType": "application/json"}},
                "description": f"Exactly one file and one request field in either order. The request field contains UTF-8 JSON within {_MAX_METADATA_BYTES} bytes, without a filename. Duplicate or unknown parts return 400. Byte limits return 413.",
            },
        },
    }
