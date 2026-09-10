"""Expose three local document tools over stdio using the optional agent extra.

This profile requires POSIX descriptor and process-group support. Windows startup exits 2.
The launcher resolves explicit root symlinks once, then intake refuses symlinks below that grant.
SIGINT and SIGTERM cancel imports, reap workers, and exit 130 even while stdin remains open.

Start openreading mcp with the local-document-proof-v1 profile and explicit input and
artifact roots. The server exposes openreading_import, openreading_search, and
openreading_read. Every successful call returns one JSON TextContent payload without
structuredContent duplication. Domain failures set isError; malformed arguments receive
sanitized protocol errors. The server never loads ambient routing files or credentials.

Artifact source, response, and passages persist until you remove their store directory.
Retrieval shares selected document text with the calling agent. Document instructions
remain untrusted data. The server is not an operating-system sandbox.

The sibling openreading.artifacts service owns provenance, byte limits, access checks,
retention, and cancellation. openreading.mcp_server.main.main(argv) is the supported launcher
entry point, returning an integer exit code. The profile name remains a compatibility identifier
for existing client configuration, independent of the artifact schema version. Client installers belong in openreading-agent-tools.
"""
