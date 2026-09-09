"""Upload ingress rejects ambiguous or incomplete inputs and releases every temporary file."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile

import pytest
from starlette.requests import ClientDisconnect, Request

from openreading import api, schemas


def _request(body, content_type="multipart/form-data; boundary=sample", failure=None):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": failure is not None}
        if failure:
            raise failure
        return {"type": "http.disconnect"}

    return Request({"type": "http", "headers": [(b"content-type", content_type.encode())]}, receive)


def _part(name, data, filename=None, content_type=None):
    header = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        header += f'; filename="{filename}"'
    if content_type:
        header += f"\r\nContent-Type: {content_type}"
    return b"--sample\r\n" + header.encode() + b"\r\n\r\n" + data + b"\r\n"


def _body(metadata=b'{"backend":{"id":null}}', data=b"hello", filename="report.txt", reverse=False):
    parts = [_part("file", data, filename), _part("request", metadata)]
    return b"".join(reversed(parts) if reverse else parts) + b"--sample--\r\n"


@pytest.mark.parametrize("reverse", [False, True])
async def test_upload_normalizes_bytes_name_and_options(reverse):
    from openreading.server.uploads import decode_request

    body = _body(
        b'{"backend":{},"keep_candidates":true,"document":{"password":"secret"}}',
        b"\x00\xffabc",
        "C:\\fakepath\\résumé 1.txt",
        reverse,
    )
    result = await decode_request(_request(body))
    assert result == {
        "backend": {},
        "keep_candidates": True,
        "document": {
            "password": "secret",
            "filename": "résumé 1.txt",
            "bytes_base64": "AP9hYmM=",
            "mime_type": "text/plain",
        },
    }


@pytest.mark.parametrize(
    "metadata",
    [
        b"null",
        b"[]",
        b"1",
        b'"secret"',
        b'{"password":"SECRET",',
        b'{"x":"\xff"}',
        b'{"document":null}',
        b'{"document":[]}',
    ],
)
async def test_bad_metadata_is_sanitized(metadata, caplog):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(_body(metadata)))
    assert exc.value.status_code == 400
    assert "SECRET" not in str(exc.value) + caplog.text


@pytest.mark.parametrize("field", ["path", "url", "bytes_base64", "file_id", "filename", "unknown"])
async def test_document_source_and_unknown_fields_are_rejected_even_null(field):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(_body(json.dumps({"document": {field: None}}).encode())))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("filename", ["", ".", "..", "/tmp/", "bad\x01.txt", "é" * 128])
async def test_unsafe_filename_rejected(filename):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(_body(filename=filename)))
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [_part("file", b"x", "a")],
        [_part("request", b"{}")],
        [_part("file", b"", "a"), _part("request", b"{}")],
        [_part("file", b"x", "a"), _part("request", b"{}", "options.json")],
        [_part("file", b"x"), _part("request", b"{}")],
        [_part("file", b"x", "a"), _part("request", b"{}"), _part("request", b"{}")],
        [_part("file", b"x", "a"), _part("file", b"y", "b"), _part("request", b"{}")],
        [_part("file", b"x", "a"), _part("request", b"{}"), _part("other", b"x")],
    ],
)
async def test_exact_parts_required(parts):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(b"".join(parts) + b"--sample--\r\n"))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("ending", [b"", b"--sample", b"--sample-", b"--wrong--\r\n"])
async def test_final_boundary_required(ending):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(_body().removesuffix(b"--sample--\r\n") + ending))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("charset", ["iso-8859-1", "utf-16", "bogus"])
async def test_metadata_charset_must_be_utf8(charset):
    from openreading.server.uploads import UploadError, decode_request

    body = (
        _part("file", b"x", "a")
        + _part("request", b"{}", content_type=f"application/json; charset={charset}")
        + b"--sample--\r\n"
    )
    with pytest.raises(UploadError) as exc:
        await decode_request(_request(body))
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "explicit,part_type,filename,data,expected",
    [
        ("custom/type", "image/png", "a.pdf", b"%PDF-1.7", "custom/type"),
        (None, "text/plain; charset=UTF-8", "a.pdf", b"%PDF-1.7", "text/plain"),
        (None, "application/octet-stream", "a.pdf", b"%PDF-1.7", "application/pdf"),
        (None, None, "a.unknownextension", b"\x00", None),
    ],
)
async def test_mime_precedence(explicit, part_type, filename, data, expected):
    from openreading.server.uploads import decode_request

    metadata = {"backend": {}, "document": {"mime_type": explicit}} if explicit else {"backend": {}}
    body = (
        _part("file", data, filename, part_type)
        + _part("request", json.dumps(metadata).encode())
        + b"--sample--\r\n"
    )
    result = await decode_request(_request(body))
    assert result["document"].get("mime_type") == expected
    assert base64.b64decode(result["document"]["bytes_base64"]) == data


async def test_limits_use_actual_bytes_and_accept_exact_limit(monkeypatch):
    from openreading.server import uploads

    monkeypatch.setattr(api, "_MAX_DOWNLOAD_BYTES", 5)
    monkeypatch.setattr(uploads, "_MAX_METADATA_BYTES", 2)
    assert (await uploads.decode_request(_request(_body(b"{}"))))["document"][
        "bytes_base64"
    ] == "aGVsbG8="
    for body in [_body(b"{}", b"123456"), _body(b"{} ")]:
        with pytest.raises(uploads.UploadError) as exc:
            await uploads.decode_request(_request(body))
        assert exc.value.status_code == 413


@pytest.mark.parametrize(
    "raw,expected", [(b'{"x":1,"x":2}', {"x": 2}), (b"null", None), (b"[]", []), (b"1", 1)]
)
async def test_json_semantics_are_preserved(raw, expected):
    from openreading.server.uploads import decode_request

    assert await decode_request(_request(raw, "text/plain")) == expected


async def test_multipart_json_duplicates_use_last_value():
    from openreading.server.uploads import decode_request

    result = await decode_request(
        _request(_body(b'{"backend":{"id":"first"},"backend":{"id":"last"}}'))
    )
    assert result["backend"]["id"] == "last"


@pytest.mark.parametrize(
    "failure", [None, ClientDisconnect(), asyncio.CancelledError(), OSError("SECRET disk location")]
)
async def test_spools_close_after_success_disconnect_cancel_and_storage_error(
    monkeypatch, failure, caplog
):
    import starlette.formparsers

    from openreading.server import uploads

    files = []
    factory = tempfile.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        kwargs["max_size"] = 1
        file = factory(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)
    if failure is None:
        await uploads.decode_request(_request(_body()))
    else:
        with pytest.raises((uploads.UploadError, ClientDisconnect, asyncio.CancelledError)) as exc:
            await uploads.decode_request(_request(_body(), failure=failure))
        if isinstance(failure, OSError):
            assert exc.value.status_code == 500
            assert "SECRET" not in str(exc.value) + caplog.text
    assert files and all(file.closed for file in files)


def test_openapi_schemas_preserve_json_contract_and_constrain_upload_metadata():
    from openreading.server.uploads import request_body_schema

    body = request_body_schema(include_keep_candidates=True)
    content = body["content"]
    assert set(content) == {"application/json", "multipart/form-data"}
    assert (
        content["application/json"]["schema"]["properties"]["document"]
        == schemas.request_schema()["properties"]["document"]
    )
    multipart = content["multipart/form-data"]["schema"]
    assert multipart["required"] == ["file", "request"]
    assert multipart["additionalProperties"] is False
    metadata = multipart["properties"]["request"]
    assert metadata["type"] == "object"
    assert set(metadata["properties"]["document"]["properties"]) == {"mime_type", "password"}
    assert "document" not in metadata["required"]
    assert "keep_candidates" in metadata["properties"]
    assert "keep_candidates" not in schemas.request_schema()["properties"]


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"{}", "multipart/form-data"),
        (_body(), "multipart/form-data; boundary="),
        (_body(), "multipart/form-data; boundary=sample; charset=latin1"),
        (b"nonsense", "multipart/form-data; boundary=sample"),
        (b'{"password":"SECRET",', "application/json"),
    ],
)
async def test_malformed_transport_errors_hide_content(body, content_type, caplog):
    from openreading.server.uploads import UploadError, decode_request

    with pytest.raises(UploadError) as exc:
        await decode_request(_request(body, content_type))
    assert exc.value.status_code == 400
    assert "SECRET" not in str(exc.value) + caplog.text


@pytest.mark.parametrize(
    "mode",
    ["truncated", "bad_metadata", "extra_part", "large_file", "create", "write", "read", "close"],
)
async def test_partial_and_failed_spools_close_with_sanitized_errors(monkeypatch, mode, caplog):
    import starlette.formparsers

    from openreading.server import uploads

    files = []
    factory = tempfile.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        if mode == "create":
            raise OSError("SECRET storage path")
        kwargs["max_size"] = 1
        file = factory(*args, **kwargs)
        files.append(file)
        if mode in {"write", "read", "close"}:
            original = getattr(file, mode)

            def fail(*args, **kwargs):
                if mode == "close":
                    original(*args, **kwargs)
                raise OSError("SECRET storage path")

            setattr(file, mode, fail)
        return file

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)
    body = _body()
    if mode == "truncated":
        body = body.removesuffix(b"--sample--\r\n")
    elif mode == "bad_metadata":
        body = _body(b'{"password":"SECRET",')
    elif mode == "extra_part":
        body = body.removesuffix(b"--sample--\r\n") + _part("other", b"x") + b"--sample--\r\n"
    elif mode == "large_file":
        monkeypatch.setattr(api, "_MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(uploads.UploadError) as exc:
        await uploads.decode_request(_request(body))
    assert exc.value.status_code == (
        500
        if mode in {"create", "write", "read", "close"}
        else 413
        if mode == "large_file"
        else 400
    )
    assert "SECRET" not in str(exc.value) + caplog.text
    assert (files or mode == "create") and all(file.closed for file in files)


async def test_case_insensitive_media_type_and_exact_utf8_filename_length():
    from openreading.server.uploads import decode_request

    result = await decode_request(
        _request(_body(filename="é" * 127 + "a"), "MULTIPART/FORM-DATA; boundary=sample")
    )
    assert result["document"]["filename"] == "é" * 127 + "a"


async def test_unknown_mime_is_omitted_so_valid_unknown_content_reaches_backend():
    from openreading.server.uploads import decode_request

    result = await decode_request(_request(_body(filename="a.unknownextension", data=b"\x00")))
    assert "mime_type" not in result["document"]
    schemas.validate_request(result)


@pytest.mark.parametrize("field", ["password", "mime_type"])
@pytest.mark.parametrize(
    "value",
    [
        ["TOP_SECRET"],
        {"nested": ["TOP_SECRET"]},
        [{"nested": {"value": "TOP_SECRET"}}],
        None,
        42,
        False,
    ],
)
async def test_invalid_document_metadata_is_rejected_without_value_leaks(field, value, caplog):
    from openreading.server.uploads import UploadError, decode_request

    metadata = json.dumps({"backend": {}, "document": {field: value}}).encode()
    with pytest.raises(UploadError) as exc:
        await decode_request(_request(_body(metadata)))
    assert exc.value.status_code == 400
    assert "TOP_SECRET" not in str(exc.value) + caplog.text


async def test_json_metadata_values_remain_for_existing_endpoint_validation():
    from openreading.server.uploads import decode_request

    body = {"backend": {}, "document": {"password": ["TOP_SECRET"], "mime_type": None}}
    assert await decode_request(_request(json.dumps(body).encode(), "application/json")) == body
