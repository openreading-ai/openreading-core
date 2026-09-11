"""Retain local extraction evidence for bounded, repeatable document retrieval.

A retained artifact contains exact source bytes, a normalized response, and page passages.
The v1 profile selects PyMuPDF; v2 selects local Docling with explicit assets and limits.
Both ignore ambient routing configuration and refuse hosted fallback.
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
in openreading.artifacts.models and the three vendored v0.2 artifact schemas.

Worker supervision
------------------
The v2 worker retains its initialized converter between sequential imports.
Cancellation, deadlines, invalid messages, and sampled memory limits terminate its process group.
An idle timer releases the worker without deleting retained evidence.
Page text origins describe measured native/OCR cells, not confidence or quote accuracy.

Known gaps
----------
Docling release defaults require host timeout and base-machine measurements.
No runtime claims a hard operating-system memory ceiling or sandbox.
Pages with no measured cells currently receive the mixed origin label, even without text.
That label cannot establish native or OCR content and needs a future schema correction.
An import racing idle shutdown can receive retryable busy before any conversion starts.
Older artifact formats require reimport; their retained files still consume the storage budget.
ONNX Runtime 1.30 also tries to persist a telemetry device identifier outside the artifact root.
Its disable_telemetry_events function does not stop that attempt or its session file. The
private worker directory contains only the session file.
"""
