"""Enumerations shared across the request/response schema and the control plane.

Values are the exact strings used in the vendored JSON Schemas — keep them in sync.
"""

from __future__ import annotations

from enum import StrEnum


class BackendType(StrEnum):
    HOSTED_API = "hosted_api"
    OSS_LIBRARY = "oss_library"
    FRAMEWORK_LOADER = "framework_loader"
    SELF_HOSTED_MODEL = "self_hosted_model"


class OutputParadigm(StrEnum):
    """The six raw output shapes (normalized_schema.md §1). A backend may emit a blend."""

    MARKDOWN = "markdown"
    TYPED_FIELDS = "typed_fields"
    ELEMENT_LIST = "element_list"
    BLOCK_TREE = "block_tree"
    BLOCK_GRAPH = "block_graph"
    TOKEN_STREAM = "token_stream"


class ResponseState(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    PROCESSING = "processing"


class BlockType(StrEnum):
    """Normalized block vocabulary (response schema $defs.Block.type). Unmapped → OTHER."""

    TITLE = "title"
    SECTION_HEADER = "section_header"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    TEXT = "text"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    FIGURE = "figure"
    IMAGE = "image"
    CAPTION = "caption"
    FORMULA = "formula"
    CODE = "code"
    KEY_VALUE = "key_value"
    FORM_FIELD = "form_field"
    SIGNATURE = "signature"
    SELECTION_MARK = "selection_mark"
    BARCODE = "barcode"
    TABLE_OF_CONTENTS = "table_of_contents"
    OTHER = "other"


class TextType(StrEnum):
    PRINTED = "printed"
    HANDWRITING = "handwriting"
    UNKNOWN = "unknown"


class PageUnit(StrEnum):
    """Unit of page.width/height (needed to de-normalize a canonical bbox)."""

    PDF_POINT = "pdf_point"
    PIXEL = "pixel"
    INCH = "inch"


class NativeOrigin(StrEnum):
    """Origin convention of the RAW source geometry preserved in bbox_native."""

    TOP_LEFT = "top_left"
    BOTTOM_LEFT = "bottom_left"


class NativeUnit(StrEnum):
    """Unit of the RAW source geometry preserved in bbox_native."""

    NORMALIZED = "normalized"
    PDF_POINT = "pdf_point"
    PIXEL = "pixel"
    INCH = "inch"


class RawEncoding(StrEnum):
    JSON = "json"
    JSON_SERIALIZED_OBJECT = "json_serialized_object"
    TEXT = "text"
    BASE64 = "base64"
    REFERENCE = "reference"


# --- control-plane enums (adapter_interface.md §1) -------------------------------------


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WaitMode(StrEnum):
    INLINE = "inline"
    POLL = "poll"
    WEBHOOK = "webhook"


class CostBasis(StrEnum):
    BILLED = "billed"
    ESTIMATED = "estimated"
    INFRA_ONLY = "infra_only"
    UNKNOWN = "unknown"


class ChannelGrade(StrEnum):
    """N/D/X grading of an output channel for a backend (adapter_interface.md §6)."""

    NATIVE = "N"  # backend emits it directly
    DERIVABLE = "D"  # router computes it deterministically from what the backend emits
    IMPOSSIBLE = "X"  # no faithful way to produce it; would require fabrication
