"""Declare formats handled by the pinned local Docling pipelines.

Model-free backends use Docling's SimplePipeline. Raster inputs reuse the verified local
layout and OCR pipeline. Audio and video require additional models outside this configuration.
The converter verifies this declaration against the installed provider before parsing.
"""

SIMPLE_FORMATS = (
    "docx",
    "doc",
    "pptx",
    "ppt",
    "html",
    "asciidoc",
    "md",
    "csv",
    "xlsx",
    "xls",
    "odt",
    "ods",
    "odp",
    "xml_uspto",
    "xml_jats",
    "xml_xbrl",
    "xml_doclang",
    "dclx",
    "json_docling",
    "vtt",
    "latex",
    "email",
    "epub",
    "boxnote",
    "iwork_pages",
    "ebcdic",
)
INPUT_FORMATS = ("pdf", "image", *SIMPLE_FORMATS)


def selection_extensions() -> tuple[str, ...]:
    """Return chooser hints from the provider table, without declaring bytes valid."""
    from docling.datamodel.base_models import FormatToExtensions, InputFormat

    return tuple(
        sorted(
            {
                extension.lower()
                for name in INPUT_FORMATS
                for extension in FormatToExtensions[InputFormat(name)]
            }
        )
    )


def extension_for_mime(mime_type: str) -> str | None:
    """Use the provider's format table when operating-system MIME mappings differ."""
    from docling.datamodel.base_models import FormatToExtensions, FormatToMimeType, InputFormat

    for name in INPUT_FORMATS:
        kind = InputFormat(name)
        if mime_type in FormatToMimeType.get(kind, []):
            return "." + FormatToExtensions[kind][0]
    return None
