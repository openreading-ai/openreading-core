"""Retain local extraction evidence for bounded, repeatable document retrieval.

Full normalized retrieval is defined in openreading.artifacts.document and excludes backend_raw.
It preserves stored channels and warnings without reparsing or selecting relevant blocks.
The profile determines extraction coverage; full retrieval does not recover unavailable channels.

A retained artifact contains exact source bytes, a normalized response, and citation passages.
Physical pages remain authoritative only when the provider establishes them.
Unpaginated blocks use exact JSON locations, such as /document/pages/0/blocks/0/text.
Their synthetic normalized container does not become a physical page citation.
The v1 profile selects PyMuPDF; v2 selects local Docling with explicit assets and optional operator limits.
Both ignore ambient routing configuration and refuse hosted fallback.
Artifacts are partitioned by the explicitly configured input root. Changing that grant
makes earlier artifacts inaccessible until the original grant is restored.

The service owns import, integrity verification, lexical search, and exact passage reads.
The sibling openreading.mcp_server package exposes these operations over stdio MCP.
No operation summarizes documents or calls a model. Requested document content can enter the
calling agent's cloud context; local parsing does not prevent that disclosure.

Integration exports and return shapes
------------------------------------
Import ArtifactService and engine_identity from openreading.artifacts.service. The service
returns ImportReceipt, SearchResult, and ReadResult models with JSON-ready wire() dictionaries.
ArtifactService.get_document returns DocumentResult, defined in openreading.artifacts.document.
Its wire() preserves explicit nulls within normalized content and provides bounded continuation.
ProfileConfig and ArtifactError live in openreading.artifacts.limits. The latter exposes envelope().
Packagers may use intake.directory (a held directory descriptor context), store.Store, and
store.safe_read (bounded bytes). worker.SETTINGS and worker.main are supported worker entry points.
These module-qualified symbols form the integration surface, without eager package imports.
For example, importing limits on an unsupported platform still permits a useful startup error.

Import retain_response from openreading.artifacts.retention to retain an externally acquired
normalized result. Supply the upload's source hash, destination digest, and request digest.
For example, a launcher can retain an HTTP response after sending a selected source snapshot.
The operation returns an ImportReceipt and never uploads, routes, or reparses the document.
It copies the granted source and verifies its hash before committing a v0.5 artifact.
The manifest's engine describes the local retaining runtime, not the remote server version.
Acquisition records the returned extraction state and preserves partial results explicitly.
Structured-only results contain zero passages and direct callers to get_document.
No page origins are fabricated. Local v0.3 and v0.4 artifacts remain readable unchanged.

The ledger records execution history for replay and resume. Artifacts retain retrieval evidence
under an input grant, verify every retained file, and commit the complete document atomically.
Reusing ledger blobs would bypass that grant and integrity boundary during later evidence reads.
Neither store automatically evicts data from the other store or promises compatible directory layouts.

Limits and failure codes live in openreading.artifacts.limits. Exact wire fields live
in openreading.artifacts.models and the vendored v0.4 artifact schemas. Legacy v0.3 artifacts remain readable.

Background imports
------------------
ImportJobs in openreading.artifacts.jobs exposes start(path), get(job_id), and cancel(job_id).
Each returns an ImportJob with wire() data defined by import-job.v0.4.json. Older persisted job records remain readable.
ImportExecution selects a trusted child command and nonsecret snapshot before tools are served.
Its launcher passes a fixed service_factory to jobs.main; ordinary dispatch refuses external jobs.
External imports report uploading before submission, followed by waiting, receiving, and retaining.
For example, a busy store after uploading fails rather than silently submitting the request twice.
The MCP serve library accepts the matching service_factory and execution descriptor.
Network annotations and cancellation guidance change only when that trusted descriptor is present.
Detached supervisors preserve work after client disconnect and publish existing artifact receipts.
Frozen launchers dispatch --internal-artifact-job to jobs.main after verifying their inventory.
The supervisor retains its own profile and closes its parser before a terminal status is written.

Worker supervision
------------------
The v2 worker retains its initialized converter between sequential imports.
Cancellation, invalid messages, and configured time or memory limits terminate its process group.
Null Docling limits impose no file, page, extraction, storage, deadline or sampled RSS ceiling.
Source hashing is streamed and Docling receives a file path; the parser still controls memory use.
An idle timer releases the worker without deleting retained evidence.
Page text origins describe measured native/OCR cells, not confidence or quote accuracy.

Text-less pages have origin none. Text without a measured extraction origin has origin unknown.
Search joins lowercase continuations after line-ending hyphens while retaining original quote offsets.

Known gaps
----------
Docling release defaults require host timeout and base-machine measurements.
No runtime claims a hard operating-system memory ceiling or sandbox.
An import racing idle shutdown can receive retryable busy before any conversion starts.
Docling artifacts imported before integration v4 can contain closed-up compounds such as thirdparty.
Reimport those documents to preserve wrapped hyphens and make each component word searchable.
Artifact formats before v0.3 require reimport; their retained files still consume the storage budget.
Search changes also change import identity, so importing again can repeat extraction and storage.
Separating extraction identity from retrieval remains unbuilt; loading an existing identifier still works.
ONNX Runtime 1.30 also tries to persist a telemetry device identifier outside the artifact root.
Its disable_telemetry_events function does not stop that attempt or its session file. The
private worker directory contains only the session file.
"""
