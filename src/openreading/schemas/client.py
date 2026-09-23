"""Contracts needed by retained document tools and external response consumers.

This profile includes no routing, adapter descriptor, strategy, benchmark or server contract.
The full schema facade re-exports these constants and functions from this module.
"""

from typing import Any

from openreading.schemas._validation import _load, _validator

# v0.3 (Canon): named channel invariants (C1-C11 in $defs descriptions), confidence bounds [0,1]
# on TableCell/Page/doc_type/Citation, + document.confidence / channel_provenance / schema_url,
# and the const-fix for the v0.1/0.2 version-identity drift. Additive+Changed over v0.2 (§6/§8).
RESPONSE_SCHEMA_FILE = "response.v0.3.json"
LOCAL_DOCUMENT_SCHEMA_FILE = "local-document.v0.5.json"
PASSAGE_SCHEMA_FILE = "passage.v0.4.json"
SELECTION_TOOL_SCHEMA_FILE = "selection-tool.v0.2.json"
AGENT_DOCUMENT_TOOL_SCHEMA_FILE = "agent-document-tool.v0.5.json"
DOCUMENT_TOOL_SCHEMA_FILE = "document-tool.v0.4.json"
IMPORT_JOB_SCHEMA_FILE = "import-job.v0.4.json"


def response_schema() -> dict[str, Any]:
    """The vendored ``RESPONSE_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(RESPONSE_SCHEMA_FILE)


def local_document_schema() -> dict[str, Any]:
    """The retained source and extraction identity contract."""
    return _load(LOCAL_DOCUMENT_SCHEMA_FILE)


def passage_schema() -> dict[str, Any]:
    """Exact source spans and physical page provenance."""
    return _load(PASSAGE_SCHEMA_FILE)


def selection_tool_schema() -> dict[str, Any]:
    """Closed local selection inputs, receipts, and sanitized errors."""
    return _load(SELECTION_TOOL_SCHEMA_FILE)


def agent_document_tool_schema() -> dict[str, Any]:
    """Bounded import, search, read, and error payloads."""
    return _load(AGENT_DOCUMENT_TOOL_SCHEMA_FILE)


def document_tool_schema() -> dict[str, Any]:
    """Whole retained normalized results with lossless continuation and existing artifact errors."""
    return _load(DOCUMENT_TOOL_SCHEMA_FILE)


def import_job_schema() -> dict[str, Any]:
    """Persistent local import status and closed background tool requests."""
    return _load(IMPORT_JOB_SCHEMA_FILE)


def validate_response(instance: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid response."""
    _validator(response_schema()).validate(instance)
