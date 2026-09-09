---
spec_format_version: "0.1"
title: "HTTP File Uploads"
artifact_type: "prd"
spec_revision: 1
status: "proposed, not built"
created_at: "2026-09-09"
applies_to:
  - path: "src/openreading/server/"
  - component: "http-document-ingress"
---

# HTTP file uploads

You can submit a file from another computer without writing an encoding script or sharing a filesystem.
This proposal adds file uploads to the HTTP process you start with `openreading serve`.

**Review state:** proposed, not built. This branch contains specifications and documentation markers only.
Implementation requires your approval after reviewing this product spec and its [design record](../../design/http-file-uploads.md).
Every multipart command below describes proposed behavior and does not work with the current server.

## Problem

You currently encode document bytes into JSON before sending a file from a different laptop.
A local path in that JSON instead identifies a file on the server's own machine.
For example, your laptop's `Downloads/report.docx` does not identify any readable file on another computer.

`OPENREADING_SERVER_PATH_ROOT` permits selected server files, but it cannot make a client's filesystem available across the network.
Requiring a base64 script makes an ordinary upload harder than users expect from an HTTP API.

A backend is the document-processing implementation OpenReading calls, such as a separately running Docling Serve process.
An upload changes how OpenReading receives the document before that backend processes its content.
A descriptor is the backend's static declaration of capabilities, configuration requirements, and supported input formats.
Each backend reads the formats its descriptor claims, as listed in the [adapter catalog](../../src/openreading/adapters/README.md).

## Goals and completion measures

| Goal | What you get | Evidence required before release |
|---|---|---|
| G1. Upload directly | One curl command sends a local file without base64 scripting or path-root configuration. | A separate client uploads a synthetic DOCX and receives extracted content through Docling Serve. |
| G2. Keep one processing contract | Uploads retain request options and existing response semantics across single-document HTTP endpoints. | Equivalent multipart and JSON requests produce equivalent processing inputs and semantic results. |
| G3. Bound upload resources | Oversized or malformed uploads fail before document processing starts. | Boundary tests prove byte limits, early refusal, and temporary-file cleanup. |
| G4. Make folder behavior explicit | You can process a client directory through independently tracked file uploads. | A documented client recipe handles nested paths, spaces, duplicate basenames, and an individual failure. |
| G5. Preserve deployment choices | The server keeps its existing authentication, routing, and backend configuration behavior. | Scope, path-access, and JSON compatibility regressions remain green. |

These measures require reproducible test evidence and a manual demonstration, rather than new production telemetry or retention.
The feature is complete only when every acceptance criterion below has evidence attached to its implementation review.

## Proposed experience

Multipart form data sends binary content and named metadata fields together in one HTTP request.
The `file` field carries document bytes, and the `request` field carries existing request options as JSON text.

```bash
curl --fail-with-body -sS \
  http://SERVER_IP:8787/v1/parse \
  -F 'file=@/Users/you/Downloads/report.docx' \
  -F 'request={"backend":{"id":"docling"}}' \
  -o response.json
```

You replace `SERVER_IP` with the OpenReading machine's reachable address, rather than its `0.0.0.0` bind address.
You configure `DOCLING_SERVE_URL` on that machine to identify the separately running Docling Serve endpoint.
When caller authentication is enabled, you send the same bearer authorization header used by JSON requests.
You let curl generate the multipart content type and boundary instead of adding a JSON content-type header.

The upload supplies the filename automatically, including the extension required by the current Docling adapter.
For example, the uploaded `report.docx` reaches Docling with that filename rather than its PDF fallback name.
The response remains the existing normalized response, including its status, extracted content, and applicable warnings.

You keep additional options inside `request`, including output selection, features, extraction instructions, and document metadata.
For example, this request asks for text and omits the backend's raw response payload:

```bash
curl --fail-with-body -sS \
  http://SERVER_IP:8787/v1/parse \
  -F 'file=@/Users/you/Downloads/report.docx' \
  -F 'request={"backend":{"id":"docling"},"outputs":{"text":true,"include_backend_raw":false}}' \
  -o response.json
```

## Release scope

| Surface | Proposed release behavior |
|---|---|
| `POST /v1/parse` | Accept one file plus request options and return the existing parse response. |
| `POST /v1/route` | Accept the same upload and return the existing plan without executing a backend. |
| `POST /v1/jobs` | Accept the same upload and retain existing job submission and retrieval semantics. |
| `POST /v1/batch` | Keep its current JSON document-list contract. Multipart batches are deferred. |
| Other HTTP endpoints | Keep their current request formats. |
| CLI and Python | Keep existing local input handling. This release adds no remote-client command or SDK. |

The first release accepts exactly one file and exactly one JSON metadata field per multipart request.
Backend selection remains explicit through the existing backend object, including its supported null identifier behavior.
Uploading a file never selects Docling automatically or adds candidates to the configured backend chain.

Both supported encodings produce the same internal request and use the same existing processing pipeline.
This proposal adds no document format capability, backend dependency, model download, or container-management behavior to core.

## Folders belong to the client

You can process a folder by enumerating its files locally and uploading each file separately.
The server cannot enumerate another laptop's directory because that directory exists only on the client.
Even a future directory-picker interface must enumerate local files before transmitting their contents over HTTP.

The release includes a client recipe that recursively selects files and submits them one at a time.
The recipe preserves relative paths in its local output mapping, rather than sending directory paths as server destinations.
For example, `team-a/report.docx` and `team-b/report.docx` must produce two distinct local result files.

The recipe starts with serial submission, skips hidden entries and symlinks, and records unreadable files as local failures.
It preserves spaces and Unicode names, continues after one failed upload, and reports totals with a nonzero failure exit.
It uploads regular files selected by the caller without claiming that every selected file is supported.
HTTP failures retain their response bodies, while connection failures remain distinct from completed server responses.

Retries remain explicit because a connection failure can occur after a backend has already started processing.
An idempotency key retains its existing endpoint behavior and does not become a durable exactly-once guarantee.
Successful files need no retry when another file fails, because every upload has its own result.

One multipart request containing several files would require additional mapping, aggregate limits, and batch failure semantics.
That extension remains separate from this release, even though client enumeration would still be required.
Archives remain opaque document bytes for the selected backend, without automatic extraction or expansion by HTTP ingress.

## Acceptance criteria

| ID | Required behavior | Verification |
|---|---|---|
| AC-1 | A remote client uploads one file through `/v1/parse` without path-root configuration or client base64 encoding. | HTTP fixture test plus the remote-client Docling demonstration. |
| AC-2 | Accepted JSON requests retain their processing behavior, response shapes, and endpoint-specific options. | Existing server suite plus JSON/multipart input parity tests. |
| AC-3 | Multipart carries exactly one `file` and one `request`, with either part order accepted. | Missing, duplicate, extra, malformed, and reversed-part tests. |
| AC-4 | Uploaded bytes, filename, and supported document metadata reach the selected backend without a server path. | Capture the normalized request and the mocked Docling HTTP payload. |
| AC-5 | Multipart options use existing request fields without silently ignoring unknown fields or conflicting document sources. | Schema, option-preservation, and source-conflict tests. |
| AC-6 | `/v1/route` and `/v1/jobs` accept the same multipart shape with their existing endpoint semantics. | No-execution routing test plus immediate and pending job tests. |
| AC-7 | Authentication, backend scopes, strategy restrictions, and endpoint configuration restrictions apply equally to both encodings. | Unauthorized and out-of-scope tests assert zero backend dispatch. |
| AC-8 | Oversized bodies, files, and metadata produce the existing JSON error envelope with HTTP 413. | Declared-length, streamed, exact-limit, and one-byte-over tests. |
| AC-9 | Temporary upload resources close after success, rejection, parser failure, cancellation, and client disconnect. | Forced spool rollover and cleanup assertions on every exit path. |
| AC-10 | The folder recipe preserves per-file identity and continues after failure without server directory access. | Temporary directory fixture with nested duplicate names, spaces, Unicode, hidden entries, and symlinks. |
| AC-11 | Server installation includes multipart parsing while ordinary library installation gains no server or Docling dependency. | Installation and dependency metadata checks. |
| AC-12 | HTTP documentation and OpenAPI describe both encodings, exact part names, metadata rules, limits, and errors. | OpenAPI assertions and a fresh-checkout walkthrough. |
| AC-13 | Existing JSON path restrictions and JSON batch semantics remain intact. | Path-root and batch regression tests, including zero dispatch for multipart batches. |
| AC-14 | Uploading synthetic DOCX content through Docling retains paragraph and table content without invented page geometry. | Captured response fixture plus a separately executed live validation. |

Oversized streamed bodies currently have a best-effort refusal, so AC-8 deliberately tightens that failure into HTTP 413.
This change affects shared body-limit handling, including JSON, while accepted request behavior remains unchanged under AC-2.

## Constraints and risks

The default request-body ceiling remains 150 MiB, including multipart boundaries, part headers, metadata, and file content.
Each file is limited to the existing 100 MiB document ceiling, with request metadata limited to 1 MiB.
The effective limit is whichever applicable ceiling the incoming request reaches first during processing.

Multipart simplifies the client, but the current internal request still represents document content as base64 text.
Temporary spooling therefore reduces receive-time memory pressure without making downstream processing zero-copy or constant-memory.
Concurrent requests can still multiply resource use, so deployment capacity remains an operator responsibility.

Upload files receive temporary storage only for ingestion, with no new upload identifier or persistent file store.
Existing backend behavior and explicitly enabled ledger storage retain their own documented retention semantics after ingestion.
A journal is an execution record used for replay, and this feature does not require enabling one.

Filename and MIME metadata describe submitted content, but they do not prove that a backend supports it.
A channel is one named response part, such as text, and unavailable channels retain existing warning behavior.

## Review decisions and deferred work

The recommendation is to approve one-file multipart support across the three single-document endpoints as one release scope.
Folder iteration stays in a client recipe, while server multipart batches require a separately reviewed extension.
This branch requests approval of the contract and boundaries before any implementation work begins afterward.

Deferred work includes resumable uploads, file storage APIs, upload progress endpoints, and server archive expansion.
A remote CLI client, multipart batch manifest, and Docling deployment-management commands also require separate product justification.
Docling authentication and timeout improvements identified earlier remain separate adapter work unless this acceptance demonstration exposes a blocker.

## Delivery and documentation lifecycle

The [design record](../../design/http-file-uploads.md) defines the transport contract, processing boundary, test mapping, and future implementation sequence.
The future implementation follows failing tests first, focused regression proof, and the full offline `make verify` check.
Live Docling validation remains separate from that check and uses only synthetic documents during the demonstration.

When implementation ships, move durable facts into server docstrings, OpenAPI, and the existing server walkthrough.
Delete this product spec, its design record, and their proposal markers in the same finishing pull request.
