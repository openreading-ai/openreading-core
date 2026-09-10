"""Retain local extraction evidence for bounded, repeatable document retrieval.

A retained artifact contains exact source bytes, a normalized response, and page passages.
The local-document-proof-v1 profile fixes PyMuPDF and ignores ambient routing configuration.
Artifacts are partitioned by the explicitly configured input root. Changing that grant
makes earlier artifacts inaccessible until the original grant is restored.

The service owns import, integrity verification, lexical search, and exact passage reads.
The sibling openreading.mcp_server package exposes these operations over stdio MCP.
No operation summarizes documents or calls a model. Retrieved excerpts can enter the
calling agent's cloud context; local parsing does not prevent that disclosure.

Integration exports and return shapes
------------------------------------
Import ArtifactService and engine_identity from openreading.artifacts.service. The service
returns ImportReceipt, SearchResult, and ReadResult models with JSON-ready wire() dictionaries.
ProfileConfig and ArtifactError live in openreading.artifacts.limits. The latter exposes envelope().
Packagers may use intake.directory (a held directory descriptor context), store.Store, and
store.safe_read (bounded bytes). worker.SETTINGS and worker.main are supported worker entry points.
These module-qualified symbols form the integration surface, without eager package imports.
For example, importing limits on an unsupported platform still permits a useful startup error.

The ledger records execution history for replay and resume. Artifacts retain retrieval evidence
under an input grant, verify every retained file, and commit the complete document atomically.
Reusing ledger blobs would bypass that grant and integrity boundary during later evidence reads.
Neither store automatically evicts data from the other store or promises compatible directory layouts.

Limits and failure codes live in openreading.artifacts.limits. Exact wire fields live
in openreading.artifacts.models and the three vendored v0.1 artifact schemas.
"""
