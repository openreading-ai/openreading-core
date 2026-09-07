"""The pydantic mirrors must serialize into schema-valid instances (DECISIONS D4): build a
rich response/request/descriptor with the models, dump, and validate against the vendored
JSON Schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openreading import schemas
from openreading.types import (
    AdapterDescriptor,
    BackendInfo,
    BackendType,
    BBox,
    BenchmarkReport,
    Block,
    BlockType,
    Capabilities,
    ChannelGrade,
    Document,
    LeaderboardBackend,
    LeaderboardCase,
    LeaderboardDataset,
    NativeOrigin,
    NativeUnit,
    NormalizedResponse,
    Output,
    OutputChannels,
    OutputParadigm,
    Page,
    Provisioning,
    ResponseState,
    RuntimeProfile,
    Status,
    Table,
    TableCell,
    TypedField,
    Usage,
    WaitMode,
    to_canonical,
)
from openreading.types.request import PageRange


def _title_bbox() -> BBox:
    return to_canonical(
        [72.0, 64.14, 200.0, 84.14],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=612.0,
        page_height=792.0,
        page=1,
    )


def test_full_response_roundtrips_through_json_schema():
    resp = NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(
            id="aws-textract",
            type=BackendType.HOSTED_API,
            operation="AnalyzeDocument",
            output_paradigm=[OutputParadigm.BLOCK_GRAPH, OutputParadigm.TYPED_FIELDS],
        ),
        document=Document(
            markdown="# Title\n\nbody",
            text="Title\nbody",
            page_count=1,
            pages=[
                Page(
                    page_number=1,
                    width=612.0,
                    height=792.0,
                    unit="pdf_point",
                    blocks=[
                        Block(
                            type=BlockType.TITLE,
                            text="Title",
                            bbox=_title_bbox(),
                            confidence=0.99,
                            native_type="LAYOUT_TITLE",
                            reading_order=0,
                        ),
                        Block(
                            type=BlockType.TABLE,
                            native_type="TABLE",
                            table=Table(
                                n_rows=1,
                                n_cols=2,
                                cells=[
                                    TableCell(row=0, col=0, text="a", is_header=True),
                                    TableCell(row=0, col=1, text="b"),
                                ],
                                rows=[["a", "b"]],
                            ),
                        ),
                    ],
                )
            ],
        ),
        typed_fields={"total": TypedField(value=42.0, type="currency", confidence=0.97)},
        usage=Usage(pages_processed=1),
    )
    resp.add_warning("confidence_partial", "some blocks lack confidence", field="blocks")
    schemas.validate_response(resp.to_schema_dict())


def test_typed_fields_only_response_is_valid():
    """A pure extractor (Sensible-like) with no document text still validates via typed_fields."""
    resp = NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(id="sensible", type=BackendType.HOSTED_API),
        document=Document(page_count=1),
        typed_fields={"invoice_number": TypedField(value="INV-1", confidence="High")},
    )
    schemas.validate_response(resp.to_schema_dict())


def test_page_range_end_before_start_rejected():
    # M3: cross-field numeric comparison is inexpressible in the vendored JSON Schema draft, so
    # this is enforced only in pydantic — see PageRange._end_not_before_start.
    with pytest.raises(ValidationError):
        PageRange(start=5, end=2)


def test_descriptor_roundtrips_through_json_schema():
    desc = AdapterDescriptor(
        id="pymupdf",
        type=BackendType.OSS_LIBRARY,
        protocol_version=1,
        adapter_impl="in_process",
        provisioning=Provisioning(byo_mode=["pip"], auth="none"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr=False,
            layout="verified",
            printed_tables="verified",
            input_formats=["pdf", "xps", "epub"],
        ),
        output=Output(
            paradigms=[OutputParadigm.BLOCK_TREE],
            channels=OutputChannels(
                markdown=ChannelGrade.DERIVABLE,
                text=ChannelGrade.NATIVE,
                blocks=ChannelGrade.NATIVE,
                block_bbox=ChannelGrade.NATIVE,
                block_confidence=ChannelGrade.IMPOSSIBLE,
                typed_fields=ChannelGrade.IMPOSSIBLE,
                table_cells=ChannelGrade.NATIVE,
            ),
        ),
        runtime=RuntimeProfile(offline_capable=True, license="AGPL-3.0", sandbox="in_process"),
    )
    dumped = desc.to_schema_dict()
    import json
    from pathlib import Path

    from jsonschema.validators import validator_for

    # The CURRENT schema, not v0.1. A v0.8 descriptor carries no `compliance` block, which every
    # frozen schema from v0.1 to v0.7 required, so descriptor validation is deliberately no longer
    # backward-compatible across that boundary.
    schema = json.loads(
        (Path(schemas.__file__).parent / schemas.DESCRIPTOR_SCHEMA_FILE).read_text()
    )
    validator_for(schema)(schema).validate(dumped)
    # channel grades serialize to N/D/X
    assert dumped["output"]["channels"]["text"] == "N"
    assert dumped["output"]["channels"]["block_confidence"] == "X"


def test_leaderboard_report_roundtrips_through_json_schema():
    """BL-160 — leaderboard-report.v0.1's pydantic mirror, round-tripped the same way every other
    cross-surface envelope in this file already is. Deliberately includes a case with `winner=None`
    (nobody produced a real score on it) — the one field the schema leaves out of `required` so
    `to_schema_dict()`'s blanket `exclude_none=True` can drop it without violating the schema."""
    report = BenchmarkReport(
        dataset=LeaderboardDataset(
            path="src/openreading/evals/sample", case_count=2, case_names=["a", "b"]
        ),
        backends=[
            LeaderboardBackend(
                backend_id="pymupdf",
                rank=1,
                mean_score=0.91,
                n_cases=2,
                n_scored=2,
                errors=0,
                non_deterministic=False,
                dimensions={"text_contains": 0.95, "table_cell_accuracy": 0.87},
            ),
            LeaderboardBackend(
                backend_id="qwen-vl",
                rank=2,
                mean_score=0.40,
                n_cases=2,
                n_scored=1,
                errors=1,
                non_deterministic=True,
                dimensions={"text_contains": 0.40},
            ),
        ],
        cases=[
            LeaderboardCase(name="a", winner="pymupdf", scores={"pymupdf": 0.95, "qwen-vl": 0.40}),
            LeaderboardCase(name="b", winner=None, scores={"pymupdf": None, "qwen-vl": None}),
        ],
    )
    dumped = report.to_schema_dict()
    schemas.validate_leaderboard_report(dumped)
    assert "winner" not in dumped["cases"][1]  # dropped, not emitted as a bare `null`
    assert dumped["cases"][1]["scores"] == {"pymupdf": None, "qwen-vl": None}  # kept, inside a dict
