"""Expose local document tools over stdio using the optional agent extra.

This profile requires POSIX descriptor and process-group support. Windows startup exits 2.
The launcher resolves explicit root symlinks once, then intake refuses symlinks below that grant.
SIGINT and SIGTERM cancel synchronous imports, reap their workers, and exit 130.
Detached imports continue across transport closure and require explicit job cancellation.

Start openreading mcp with the local-document-proof-v1 profile and explicit input and
artifact roots. The server exposes openreading_import, openreading_search, and
openreading_read, plus openreading_get_document for the complete retained normalized result.
That result excludes backend_raw. Its auto delivery returns intact content or a local export.
The operator sets --document-response-bytes, default 1000000 serialized MCP bytes including escaping.
The optional --document-export-root selects a trusted destination; the private artifact store is default.
For example, delivery="file" exports even a small result without sending its document content.
A local export path establishes no host access and performs no upload. Existing fragment mode
uses lossless pagination defined by openreading.artifacts.document.
Background tools openreading_start_import, openreading_get_import and openreading_cancel_import
return persistent job status without keeping a tool request open during extraction.
Call openreading_backends with {} for the selected backend's descriptor and configured OCR flag.
For example, the Docling profile returns docling_local even when other adapters are installed.
Discovery preserves descriptor claims and their sources. It does not check dependencies,
assets, credentials or live reachability, and never enables general backend selection.
Use openreading_list_imports after reconnecting when the original job ID is unavailable.
Search remains optional. The openreading_select_document tool also accepts a receipt cursor.
Without an explicitly supplied provider, it returns selection_unavailable and opens no UI.
Every successful call returns one JSON TextContent payload without
structuredContent duplication. Domain failures set isError; malformed arguments receive
sanitized protocol errors. The server never loads ambient routing files or credentials.
Call openreading_route for the shared router plan under operator-owned backend scope.
The optional --routing-config snapshots explicit YAML, while repeated --allow-backend
flags grant planning access independently. Without those flags, planning stays local.
For example, a policy listing reducto cannot authorize it outside the allowed set.
Planning reads no document, checks no readiness and does not change local imports.

Call openreading_get_result for complete general results identified by an orr1 result_id.
It shares the operator's delivery budget and export directory with complete document retrieval.
Auto returns complete content or a local export; fragments returns lossless JSON Pointer continuation.
The result-tool.v0.1 contract preserves normalized status, warnings and explicit producer provenance.
For example, a comparison report retains its subject-to-input mapping without creating physical-page citations.
General execution and comparison producers remain unbuilt; trusted library code can retain existing results.

Artifact source, response, and passages persist until you remove their store directory.
Retrieval shares selected document text with the calling agent. Document instructions
remain untrusted data. The server is not an operating-system sandbox.

The sibling openreading.artifacts service owns provenance, byte limits, access checks,
retention, and cancellation. openreading.mcp_server.main.main(argv) is the supported launcher
entry point, returning an integer exit code. Its keyword-only selection_provider and
selection_timeout_seconds arguments pass trusted configuration to serve and create_server.
For example, main(argv, selection_provider=picker) adds a chooser without a model-controlled path.
The provider must follow the transactional cleanup contract in openreading.mcp_server.selection. The profile name remains a compatibility identifier
for existing client configuration, independent of the artifact schema version. Client installers belong in openreading-agent-tools.
"""
