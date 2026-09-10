"""Retain local extraction evidence for bounded, repeatable document retrieval.

A retained artifact contains exact source bytes, a normalized response, and page passages.
The local-document-proof-v1 profile fixes PyMuPDF and ignores ambient routing configuration.
Artifacts are partitioned by the explicitly configured input root. Changing that grant
makes earlier artifacts inaccessible until the original grant is restored.

The service owns import, integrity verification, lexical search, and exact passage reads.
The sibling openreading.mcp_server package exposes these operations over stdio MCP.
No operation summarizes documents or calls a model. Retrieved excerpts can enter the
calling agent's cloud context; local parsing does not prevent that disclosure.

Limits and failure codes live in openreading.artifacts.limits. Exact wire fields live
in openreading.artifacts.models and the three vendored v1 artifact schemas.
"""
