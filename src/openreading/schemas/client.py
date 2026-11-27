"""Contracts needed by retained document tools and external response consumers.

This profile includes no routing, adapter descriptor, strategy, benchmark or server contract.
The full schema facade re-exports these constants and functions from this module.
"""

from typing import Any

from openreading.schemas._validation import _load, _validator

# v0.4 (Schema check): the optional top-level `schema_check` object, additive over v0.3.
RESPONSE_SCHEMA_FILE = "response.v0.4.json"
# Every response version a client reads, keyed by the response's own `schema_version`. A client
# validates a retained response against the file its version names, so an artifact retained
# under v0.3 stays readable after an upgrade. Dropping a version here strands those artifacts.
RESPONSE_SCHEMA_FILES = {
    "0.3": "response.v0.3.json",
    "0.4": RESPONSE_SCHEMA_FILE,
}
LOCAL_DOCUMENT_SCHEMA_FILE = "local-document.v0.5.json"
PASSAGE_SCHEMA_FILE = "passage.v0.4.json"
SELECTION_TOOL_SCHEMA_FILE = "selection-tool.v0.2.json"
AGENT_DOCUMENT_TOOL_SCHEMA_FILE = "agent-document-tool.v0.5.json"
DOCUMENT_TOOL_SCHEMA_FILE = "document-tool.v0.4.json"
IMPORT_JOB_SCHEMA_FILE = "import-job.v0.4.json"


def response_schema() -> dict[str, Any]:
    """The vendored ``RESPONSE_SCHEMA_FILE`` file as a dict, loaded once and cached."""
    return _load(RESPONSE_SCHEMA_FILE)


def response_schema_for(version: object) -> dict[str, Any]:
    """The response schema for `version`. Raises ``ValueError`` for a version this client lacks."""
    name = RESPONSE_SCHEMA_FILES.get(version) if isinstance(version, str) else None
    if name is None:
        known = ", ".join(sorted(RESPONSE_SCHEMA_FILES))
        raise ValueError(f"Unknown response schema_version {version!r}; known: {known}")
    return _load(name)


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
    """Raise ``jsonschema.ValidationError`` unless ``instance`` is a valid response.

    The instance's own ``schema_version`` picks the file among ``RESPONSE_SCHEMA_FILES``, so a
    saved v0.3 response still validates, for example as a ``compare`` input. Any other version
    validates against the newest file and fails its ``schema_version`` const."""
    version = instance.get("schema_version") if isinstance(instance, dict) else None
    name = RESPONSE_SCHEMA_FILES.get(version) if isinstance(version, str) else None
    _validator(_load(name) if name else response_schema()).validate(instance)
