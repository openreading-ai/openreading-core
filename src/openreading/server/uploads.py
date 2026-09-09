"""Decode HTTP uploads into the existing request contract before endpoint validation.

Multipart requests contain one binary ``file`` and one UTF-8 JSON ``request`` field.
Document metadata permits only ``mime_type`` and ``password`` because the file supplies
both its source and filename. For example, a null ``document.path`` still conflicts.
Optional metadata uses the vendored property schemas, with value-free errors protecting passwords.
The basename strips both directory separator styles and never names temporary storage.
Explicit MIME metadata precedes the file header, then ``openreading.derive.mime`` inference.

The parser owns every spool until decoding finishes, including partially received files.
Cleanup runs on success, malformed input, disconnect, cancellation, and storage failure.
File reads use ``openreading.api._MAX_DOWNLOAD_BYTES`` before allocating base64 content.
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
from typing import Any

from jsonschema import Draft202012Validator
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from openreading import api, schemas
from openreading.derive.mime import resolve_mime_type

_MAX_METADATA_BYTES = 1024 * 1024


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

    def on_headers_finished(self) -> None:
        part = self._current_part
        disposition, options = parse_options_header(part.content_disposition)
        name = options.get(b"name")
        if disposition != b"form-data" or name not in {b"file", b"request"}:
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
        if (
            self._current_part.file is None
            and len(self._current_part.data) + end - start > _MAX_METADATA_BYTES
        ):
            raise UploadError(413, f"The request field exceeds {_MAX_METADATA_BYTES} bytes.")
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

    def close(self) -> None:
        """Release partial spools even when parsing never produced a complete form."""
        failed = False
        for file in self._files_to_close_on_error:
            try:
                file.close()
            except OSError:
                failed = True
        if failed:
            raise UploadError(500, "Upload temporary storage failed.")


async def decode_request(request: Request) -> Any:
    """Return JSON values or normalize a complete upload into the ordinary request shape."""
    media_type, params = parse_options_header(request.headers.get("content-type", ""))
    if media_type.lower() != b"multipart/form-data":
        try:
            return await request.json()
        except (ValueError, UnicodeError):
            raise UploadError(400, "Invalid JSON request body.") from None

    parser = _UploadParser(request)
    try:
        _utf8_charset(request.headers["content-type"])
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
        if not isinstance(document, dict) or set(document) - {"mime_type", "password"}:
            raise UploadError(400, "Upload document metadata permits only mime_type and password.")
        # Schema error messages can include password values, so reject these without rendering errors.
        properties = schemas.request_schema()["properties"]["document"]["properties"]
        for key, value in document.items():
            if not Draft202012Validator(properties[key]).is_valid(value):
                raise UploadError(400, "Upload document metadata has an invalid field value.")
        limit = api._MAX_DOWNLOAD_BYTES
        if upload.size is not None and upload.size > limit:
            raise UploadError(413, f"The uploaded file exceeds {limit} bytes.")
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise UploadError(413, f"The uploaded file exceeds {limit} bytes.")
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
    except OSError:
        raise UploadError(500, "Upload temporary storage failed.") from None
    finally:
        parser.close()


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
            key: original_document["properties"][key] for key in ("mime_type", "password")
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
