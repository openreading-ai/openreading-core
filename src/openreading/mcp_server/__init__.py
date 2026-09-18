"""Expose local evidence and scoped general execution through separate stdio profiles.

The general-execution-v1 profile serves eight tools without selecting a mandatory local parser.
Use openreading_parse to start authorized backend or strategy work, then inspect persistent ej1 jobs.
General get_job, list_jobs and cancel_job operate under the original input grant across client reconnects.
Get and list can persist recovery after supervisor exit, so their tool annotations admit writes.
Get_result retrieves complete retained normalized data, while compare consumes retained inputs without provider calls.
Route plans backend order within the same operator execution scope without checking provider readiness.
For example, --execute-backend pymupdf authorizes parsing through that adapter when its dependencies are installed.
The general launcher rejects local planning flags instead of interpreting them as execution authority.
Read openreading.mcp_server.general for the tool lifecycle and openreading.cli for executable setup instructions.

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
Call openreading_compare with result_ids to compare authorized retained normalized responses.
An optional baseline selects an existing input or appends another retained response under the same grant.
The shared comparison engine never executes either backend; its report retains exact subject-to-input mappings.
For example, compare two retained runs of Docling without reprocessing their source documents.
The synchronous call returns a bounded receipt; host cancellation does not undo completed report publication.
Truth scoring, corpus comparison and scoped resume remain unbuilt MCP operations.

Internal execution preflight lives in openreading.mcp_server.execution and exposes no additional tool.
It snapshots explicit configuration with independent backend and strategy entrypoint scopes.
For example, allowing a strategy does not authorize a hosted leaf outside the backend scope.
The shared compiler prunes unauthorized leaves before any document or credential access.
Internal ExecutionAttempt in openreading.mcp_server.execution_process acquires granted inputs and owns a disposable worker.
It returns complete validated content while preserving backend scope at dispatch.
Internal ExecutionJobs in openreading.mcp_server.execution_jobs persists detached work, cancellation and retained-result receipts.
It distinguishes successful publication from partial, failed or still-processing provider outcomes.
General tools still need launcher integration and receipt budgeting before accepting execution requests.

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
