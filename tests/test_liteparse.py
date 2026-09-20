"""LiteParse adapter: the offline contract, OCR asset gating, and honest normalization.

A fake runner stands in for the isolated worker. It replays the shape LiteParse 2.14.4 returns
(`dataclasses.asdict` of its ParseResult, observed locally on 2026-09-13), so these tests prove
the mapping, not the engine. Real-engine behavior through core's worker lives in
`tests/test_liteparse_worker.py`.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import os

import pytest

from openreading.adapters.liteparse import LiteParseAdapter, assets
from openreading.adapters.liteparse.adapter import LiteParseWorkerError
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType
from openreading.types.errors import TerminalError, UnsupportedFeatureError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

PDF = b"%PDF-1.7\n%synthetic\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _box(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


RAW = {
    "pages": [
        {
            "page_num": 1,
            "width": 612.0,
            "height": 792.0,
            "text": "Invoice\nInvoice total 1,250.00\nName Qty\nWidget 2",
            "markdown": "# Invoice\n\nInvoice total 1,250.00\n\n| Name | Qty |\n|---|---|\n| Widget | 2 |",
            "text_items": [],
            "blocks": [
                {"kind": "heading", "text": "Invoice", "level": 1, "bbox": _box(72, 72, 100, 20)},
                {
                    "kind": "paragraph",
                    "text": "Invoice total 1,250.00",
                    "bbox": _box(72, 100, 200, 14),
                },
                {
                    "kind": "table",
                    "header": [
                        {"text": "Name", "bbox": _box(72, 130, 60, 12)},
                        {"text": "Qty", "bbox": None},
                    ],
                    "rows": [
                        [
                            {"text": "Widget", "bbox": _box(72, 144, 60, 12)},
                            {"text": "2", "bbox": None},
                        ]
                    ],
                    "bbox": _box(72, 130, 160, 26),
                },
                {"kind": "rule", "bbox": None},
            ],
            "form_fields": [
                {
                    "id": "f1",
                    "type": "text",
                    "page": 1,
                    "annotation_index": 0,
                    "widget_index": 0,
                    "field_flags": 0,
                    "name": "customer",
                    "value": "Acme",
                    "checked": None,
                    "rect": _box(72, 600, 200, 18),
                    "options": [],
                    "selected_options": [],
                },
                {
                    "id": "f2",
                    "type": "checkbox",
                    "page": 1,
                    "annotation_index": 1,
                    "widget_index": 0,
                    "field_flags": 0,
                    "name": "paid",
                    "value": None,
                    "checked": True,
                    "rect": None,
                    "options": [],
                    "selected_options": [],
                },
            ],
        }
    ],
    "text": "Invoice\nInvoice total 1,250.00\nName Qty\nWidget 2",
    "total_pages": 1,
    "page_errors": [],
}


class FakeRunner:
    def __init__(self, raw=None, error=None):
        self.raw = raw or RAW
        self.error = error
        self.calls = []

    def parse(self, data, *, suffix, options, ctx):
        self.calls.append({"suffix": suffix, "options": dict(options), "size": len(data)})
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.raw)


def _req(data=PDF, **over):
    body = {
        "document": {"bytes_base64": base64.b64encode(data).decode()},
        "backend": {"id": "liteparse"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req, ctx=None):
    ctx = ctx or RunContext()
    job = adapter.submit(req, ctx)
    return adapter.normalize(job, ctx, req)


@pytest.fixture
def tessdata(tmp_path, monkeypatch):
    content = b"synthetic traineddata"
    directory = tmp_path / "tessdata"
    directory.mkdir()
    (directory / "eng.traineddata").write_bytes(content)
    pinned = {"eng": {"test": (hashlib.sha256(content).hexdigest(), len(content), "fixture")}}
    monkeypatch.setattr(assets, "PINNED_TESSDATA", pinned)
    return directory


def test_liteparse_conforms():
    adapter = LiteParseAdapter(runner=FakeRunner())
    check_adapter_conformance(
        adapter,
        [
            ConformanceCase(request=_req(), label="parse"),
            ConformanceCase(request=_req(outputs={"typed_fields": True}), label="form fields"),
        ],
        adapter_factory=lambda: LiteParseAdapter(runner=FakeRunner()),
    )


def test_blocks_tables_and_geometry_are_projected_without_inference():
    response = _run(LiteParseAdapter(runner=FakeRunner()), _req(outputs={"tables": "cells"}))
    page = response.document.pages[0]
    assert (page.width, page.height, page.text, page.markdown.startswith("# Invoice")) == (
        612.0,
        792.0,
        RAW["pages"][0]["text"],
        True,
    )
    kinds = [block.type for block in page.blocks]
    assert kinds == [BlockType.SECTION_HEADER, BlockType.TEXT, BlockType.TABLE, BlockType.OTHER]
    heading = page.blocks[0]
    assert heading.native_type == "heading"
    assert round(heading.bbox.x, 4) == round(72 / 612, 4)
    assert round(heading.bbox.y, 4) == round(72 / 792, 4)
    table = page.blocks[2].table
    assert (table.n_rows, table.n_cols) == (2, 2)
    header_cells = [cell for cell in table.cells if cell.row == 0]
    assert all(cell.is_header for cell in header_cells)
    assert [cell.text for cell in table.cells if cell.row == 1] == ["Widget", "2"]
    assert next(cell for cell in table.cells if cell.text == "Qty").bbox is None
    assert page.blocks[3].bbox is None


def test_form_fields_become_typed_fields_only_when_requested():
    default = _run(LiteParseAdapter(runner=FakeRunner()), _req())
    assert default.typed_fields is None
    response = _run(LiteParseAdapter(runner=FakeRunner()), _req(outputs={"typed_fields": True}))
    customer = response.typed_fields["customer"]
    assert (customer.value, customer.type, customer.citations[0].page) == ("Acme", "text", 1)
    assert customer.citations[0].bbox is not None
    paid = response.typed_fields["paid"]
    assert (paid.value, paid.type, paid.citations[0].bbox) == (True, "checkbox", None)


def test_form_field_name_and_id_collisions_preserve_every_value_and_citation():
    raw = copy.deepcopy(RAW)
    fields = [
        {"name": "f2", "id": "f1", "value": "first"},
        {"name": "f2#2", "id": "f3", "value": "second"},
        {"name": "f2", "id": "f2", "value": "third"},
        {"name": "f2", "id": "f2", "value": "fourth"},
        {"name": "f2", "id": "f4", "value": "fifth"},
    ]
    raw["pages"] = [
        {**copy.deepcopy(RAW["pages"][0]), "page_num": i, "form_fields": [field]}
        for i, field in enumerate(fields, start=1)
    ]
    adapter = LiteParseAdapter(runner=FakeRunner(raw))
    request = _req(outputs={"typed_fields": True})
    response = _run(adapter, request)
    assert len(response.typed_fields) == len(fields)
    assert list(response.typed_fields) == ["f2", "f2#2", "f2#3", "f2#4", "f4"]
    assert [(f.value, f.citations[0].page) for f in response.typed_fields.values()] == [
        (field["value"], i) for i, field in enumerate(fields, start=1)
    ]
    assert _run(adapter, request).typed_fields == response.typed_fields


def test_ocr_off_never_needs_or_sends_assets():
    runner = FakeRunner()
    _run(LiteParseAdapter(runner=runner), _req(features={"ocr": "off"}))
    options = runner.calls[0]["options"]
    assert options["ocr_enabled"] is False
    assert options.get("tessdata_path") is None


def test_automatic_ocr_without_assets_is_skipped_and_disclosed():
    runner = FakeRunner()
    response = _run(LiteParseAdapter(runner=runner), _req())
    assert runner.calls[0]["options"]["ocr_enabled"] is False
    assert any(w.code == "ocr_skipped" for w in response.warnings)


def test_automatic_ocr_uses_only_verified_assets(tessdata):
    runner = FakeRunner()
    ctx = RunContext(runtime={"tessdata_path": str(tessdata)})
    response = _run(LiteParseAdapter(runner=runner), _req(), ctx)
    options = runner.calls[0]["options"]
    assert options["ocr_enabled"] is True
    assert options["tessdata_path"] == str(tessdata)
    assert options["ocr_language"] == "eng"
    assert options["expected_sha256"] == hashlib.sha256(b"synthetic traineddata").hexdigest()
    assert not any(w.code == "ocr_skipped" for w in response.warnings or [])


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "ocr_assets_missing"),
        ("altered", "ocr_assets_unverified"),
        ("french", "ocr_language_unverified"),
        ("two_languages", "ocr_language_unverified"),
        ("relative", "ocr_assets_missing"),
    ],
)
def test_unverified_ocr_assets_fail_before_the_worker_starts(tessdata, change, code):
    runner = FakeRunner()
    runtime = {"tessdata_path": str(tessdata)}
    request = _req()
    if change == "missing":
        (tessdata / "eng.traineddata").unlink()
    elif change == "altered":
        (tessdata / "eng.traineddata").write_bytes(b"changed traineddata!")
    elif change == "french":
        request = _req(features={"ocr_languages": ["fra"]})
    elif change == "two_languages":
        request = _req(features={"ocr_languages": ["eng", "fra"]})
    else:
        runtime = {"tessdata_path": "relative/tessdata"}
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter(runner=runner).submit(request, RunContext(runtime=runtime))
    assert error.value.backend_code == code
    assert runner.calls == []
    assert str(tessdata) not in str(error.value)


def test_forced_full_page_ocr_is_refused_rather_than_ignored(tessdata):
    runner = FakeRunner()
    ctx = RunContext(runtime={"tessdata_path": str(tessdata)})
    with pytest.raises(UnsupportedFeatureError):
        LiteParseAdapter(runner=runner).submit(_req(features={"ocr": "force"}), ctx)
    assert runner.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"document": {"url": "https://example.invalid/a.pdf"}},
        {"document": {"bytes_base64": base64.b64encode(PDF).decode(), "password": "secret"}},
        {"document": {"bytes_base64": base64.b64encode(b"PK\x03\x04docx").decode()}},
        {"document": {"bytes_base64": base64.b64encode(PDF).decode()}, "pages": {"max_pages": 1}},
    ],
)
def test_unsupported_inputs_are_refused_before_parsing(body):
    runner = FakeRunner()
    request = OpenReadingRequest.model_validate({"backend": {"id": "liteparse"}, **body})
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter(runner=runner).submit(request, RunContext())
    assert error.value.backend_code == "unsupported_input"
    assert "secret" not in str(error.value)
    assert runner.calls == []


@pytest.mark.parametrize("data,suffix", [(PDF, ".pdf"), (PNG, ".png"), (JPEG, ".jpg")])
def test_each_supported_format_keeps_its_own_suffix(data, suffix):
    runner = FakeRunner()
    LiteParseAdapter(runner=runner).submit(_req(data), RunContext())
    assert runner.calls[0]["suffix"] == suffix


def test_document_path_is_read_locally(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(PDF)
    runner = FakeRunner()
    request = OpenReadingRequest.model_validate(
        {"document": {"path": str(source)}, "backend": {"id": "liteparse"}}
    )
    LiteParseAdapter(runner=runner).submit(request, RunContext())
    assert runner.calls[0]["size"] == len(PDF)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("password_required", "password_required"),
        ("unsupported_format", "unsupported_input"),
        ("timeout", "worker_timeout"),
        ("memory_limit", "worker_memory_limit"),
        ("engine_identity_unavailable", "ocr_assets_unverified"),
        ("parse_failed", "local_parse_failed"),
        ("worker_monitor_failed", "local_parse_failed"),
    ],
)
def test_worker_failures_map_to_fixed_terminal_codes(code, expected):
    runner = FakeRunner(error=LiteParseWorkerError(code))
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter(runner=runner).submit(_req(features={"ocr": "off"}), RunContext())
    assert error.value.backend_code == expected


def test_unexpected_runner_exception_is_sanitized():
    runner = FakeRunner(error=RuntimeError("/private/customer/secret.pdf"))
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter(runner=runner).submit(_req(features={"ocr": "off"}), RunContext())
    assert error.value.backend_code == "local_parse_failed"
    assert "secret" not in str(error.value)


def test_page_errors_produce_partial_response_with_warning():
    raw = copy.deepcopy(RAW)
    raw["page_errors"] = [{"page_num": 2, "message": "native failure"}]
    response = _run(LiteParseAdapter(runner=FakeRunner(raw)), _req())
    assert response.status.state.value == "partial"
    assert any(w.code == "partial_conversion" for w in response.warnings)


def test_disabled_outputs_suppress_channels():
    response = _run(
        LiteParseAdapter(runner=FakeRunner()),
        _req(outputs={"text": False, "markdown": False, "blocks": False}),
    )
    page = response.document.pages[0]
    assert (page.text, page.markdown, page.blocks) == (None, None, None)
    assert response.document.text is None


def test_report_cost_counts_local_pages_without_a_price():
    adapter = LiteParseAdapter(runner=FakeRunner())
    job = adapter.submit(_req(), RunContext())
    report = adapter.report_cost(job)
    assert (report.native_unit, report.native_quantity) == ("page", 1)


def test_health_names_the_install_extra_when_missing(monkeypatch):
    from openreading.adapters.liteparse import adapter as module

    def missing(name):
        raise module.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.importlib.metadata, "version", missing)
    health = LiteParseAdapter().health()
    assert not health.ready
    assert health.missing_deps == ["liteparse (pip install 'openreading[liteparse]')"]
    assert LiteParseAdapter(runner=FakeRunner()).health().ready


def test_descriptor_claims_are_documentation_not_verification():
    descriptor = LiteParseAdapter().descriptor
    assert descriptor.type.value == "oss_library"
    assert descriptor.capabilities.ocr == "claimed"
    assert descriptor.runtime.offline_capable is True
    assert descriptor.runtime.version_pin == "liteparse==2.14.4"
    assert descriptor.credentials_spec == []
    assert {field.key for field in descriptor.config_spec} == {
        "tessdata_path",
        "worker_memory_bytes",
    }


@pytest.mark.live
def test_live_local_ocr_with_verified_tessdata(tmp_path):  # pragma: no cover - needs OCR assets
    directory = os.environ.get("LITEPARSE_TESSDATA")
    if not directory:
        pytest.skip("liteparse: set LITEPARSE_TESSDATA to a verified tessdata directory")
    import pymupdf

    with pymupdf.open() as scratch:
        page = scratch.new_page()
        page.insert_text((72, 100), "Invoices are payable within 45 days.", fontsize=16)
        image = page.get_pixmap(dpi=150)
    with pymupdf.open() as document:
        scanned = document.new_page()
        scanned.insert_image(scanned.rect, pixmap=image)
        data = document.tobytes()
    ctx = RunContext(runtime={"tessdata_path": directory}, deadline_ms=120_000)
    response = _run(LiteParseAdapter(), _req(data), ctx)
    assert "45 days" in (response.document.pages[0].text or "")


def test_backend_raw_is_kept_by_default_and_dropped_on_request():
    kept = _run(LiteParseAdapter(runner=FakeRunner()), _req())
    assert kept.backend_raw.payload["pages"][0]["page_num"] == 1
    dropped = _run(
        LiteParseAdapter(runner=FakeRunner()), _req(outputs={"include_backend_raw": False})
    )
    assert dropped.backend_raw is None


def test_requested_cells_without_a_table_are_named_in_a_warning():
    raw = copy.deepcopy(RAW)
    raw["pages"][0]["blocks"] = [
        block for block in raw["pages"][0]["blocks"] if block["kind"] != "table"
    ]
    response = _run(LiteParseAdapter(runner=FakeRunner(raw)), _req(outputs={"tables": "cells"}))
    assert {w.code: w.field for w in response.warnings}["table_cells_unavailable"] == "table_cells"


def test_line_blocks_and_every_form_value_shape_are_kept():
    raw = copy.deepcopy(RAW)
    raw["pages"][0]["blocks"].append(
        {"kind": "code", "text": None, "lines": ["a = 1", "b = 2"], "bbox": None}
    )
    fields = raw["pages"][0]["form_fields"]
    fields.append(
        {**fields[0], "id": "f3", "type": "combobox", "value": None, "selected_options": ["Net 45"]}
    )
    fields.append({**fields[0], "id": "f4", "name": "", "value": None, "rect": None})
    response = _run(LiteParseAdapter(runner=FakeRunner(raw)), _req(outputs={"typed_fields": True}))
    assert response.document.pages[0].blocks[-1].text == "a = 1\nb = 2"
    assert response.typed_fields["f3"].value == ["Net 45"]
    assert response.typed_fields["f4"].value is None


def test_health_reports_missing_psutil_and_the_engine_version(monkeypatch):
    from openreading.adapters.liteparse import adapter as module

    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "2.14.4")
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: None)
    missing = LiteParseAdapter().health()
    assert missing.missing_deps == ["psutil (pip install 'openreading[liteparse]')"]
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    ready = LiteParseAdapter().health()
    assert (ready.ready, ready.version) == (True, "2.14.4")


@pytest.mark.parametrize("raw", ["lots", "0", -5])
def test_invalid_worker_memory_is_refused_before_parsing(raw):
    ctx = RunContext(runtime={"worker_memory_bytes": raw})
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter().submit(_req(features={"ocr": "off"}), ctx)
    assert error.value.backend_code == "worker_config_invalid"


def test_configured_worker_memory_reaches_the_runner():
    runner = LiteParseAdapter()._get_client(RunContext(runtime={"worker_memory_bytes": "1048576"}))
    assert runner.memory_bytes == 1048576


def test_an_unreadable_path_is_refused_as_input(tmp_path):
    request = OpenReadingRequest.model_validate(
        {"document": {"path": str(tmp_path / "absent.pdf")}, "backend": {"id": "liteparse"}}
    )
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter(runner=FakeRunner()).submit(request, RunContext())
    assert error.value.backend_code == "unsupported_input"


def test_retryable_runner_errors_pass_through_unchanged():
    from openreading.types.errors import RetryableError

    runner = FakeRunner(error=RetryableError("busy", backend_code="busy"))
    with pytest.raises(RetryableError):
        LiteParseAdapter(runner=runner).submit(_req(features={"ocr": "off"}), RunContext())


def test_unreadable_language_data_counts_as_missing(tessdata):
    path = tessdata / "eng.traineddata"
    path.chmod(0)
    try:
        with pytest.raises(assets.OcrAssetError) as error:
            assets.verify_tessdata(str(tessdata), "eng")
    finally:
        path.chmod(0o600)
    assert error.value.code == "ocr_assets_missing"
