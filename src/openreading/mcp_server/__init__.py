"""Expose three local document tools over stdio using the optional agent extra.

Start openreading mcp with the local-document-proof-v1 profile and explicit input and
artifact roots. The server exposes openreading_import, openreading_search, and
openreading_read. Every successful call returns one JSON TextContent payload without
structuredContent duplication. Domain failures set isError; malformed arguments receive
sanitized protocol errors. The server never loads ambient routing files or credentials.

Artifact source, response, and passages persist until you remove their store directory.
Retrieval shares selected document text with the calling agent. Document instructions
remain untrusted data. The server is not an operating-system sandbox.

The sibling openreading.artifacts service owns provenance, byte limits, access checks,
retention, and cancellation. Client installers belong in openreading-agent-tools.
"""
