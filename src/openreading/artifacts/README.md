# Retained local document evidence

<sub>[Docs home](../README.md) · [← HTTP server](../server/README.md) · [MCP tools →](../mcp_server/README.md)</sub>

## What this gives you

You can reuse one local extraction across questions and cite the exact passages you receive.
An artifact is a retained source document together with its extraction and deterministic evidence records.
For example, a renewal clause on physical page 13 retains that page number after restart.

## Mental model

Import copies one granted file before parsing, then commits its source, response, passages, and manifest together.
A manifest records source and file hashes, engine versions, extraction settings, and the configured input grant.
Search returns literal excerpts, while read returns complete evidence records in the requested order.

## Walkthrough

Start the [MCP profile](../mcp_server/README.md) with separate input and artifact directories.
Import your document using a relative path such as `agreement.pdf` inside the configured input directory.
Search the returned `artifact_id` for `renewal`, then read the matching `evidence_id` values.

A returned passage can identify `p0013-b0002-s0000`, meaning physical page 13 and a deterministic block segment.
Cite its displayed filename, page, and evidence identifier alongside the exact quote you use.
The identifier belongs to that artifact and cannot establish provenance for another extraction.

## Recipes

- Reuse evidence after restarting by keeping the same artifact directory and configured input root.
- Follow `next_cursor` with the same query and limit, or the same ordered evidence identifiers.
- Remove a document's retained directory when you want to delete its source and extracted evidence.

### Local Docling developer setup

Install `openreading[agent,docling-local]` and provide the verified model assets described in `openreading.adapters.docling_local.config`.
Create a setup JSON file with explicit resource limits and absolute local paths:

```json
{
  "pages": 10,
  "deadline_seconds": 60,
  "worker_memory_bytes": 2147483648,
  "worker_idle_seconds": 60,
  "docling": {
    "artifacts_path": "/absolute/models",
    "dependency_lock": "/absolute/uv.lock",
    "ocr": false
  }
}
```

These numbers are developer safeguards, not measured release defaults or host compatibility claims.
Start `openreading mcp --profile local-document-proof-v2 --profile-config /absolute/setup.json --input-root /absolute/documents --artifact-root /absolute/evidence`.
Enable OCR through setup with `ocr: true`, `tesseract_cmd`, and `tessdata_path` pointing to your selected executable and language data.
Include `osd.traineddata` in that directory for orientation detection.
The default language is `eng`; the configured model, executable, language data, and lock hashes enter the extraction identity.
The lock hash identifies a selected file. It does not verify that installed packages match that lock.
Packaging must establish that relationship through a locked build and runtime verification.
A sampled memory limit includes the worker and its descendants; sampling permits transient overshoot.

## How it decides

Each profile selects its local engine explicitly, preventing ambient configuration from choosing a hosted backend.
Source spans preserve Unicode code points without normalization, preventing quotes from drifting away from extracted text.
Geometry comes from the enclosing source block, preventing an approximate box from appearing as a precise character highlight.
Every load checks hashes and regenerates passages from the normalized response, preventing corrupted evidence from being reused.

## Operations

The store permits one import at a time and refuses new imports when its quota is exhausted.
The fixed limits live in `openreading.artifacts.limits`, including the source, extraction, storage, deadline, and payload caps.
Cancellation terminates the parser process before staging cleanup and lock release complete.
Stop all clients using this store before manual cleanup; no MCP tool deletes retained evidence.
Under your configured `--artifact-root`, each document lives at `documents/INPUT_GRANT_SHA256/ARTIFACT_ID/`.
Delete that complete document directory to repair a corrupt artifact, then import the source again.
Delete the artifact-root directory to remove all retained documents, including artifacts from older grants.
Re-import deliberately refuses corruption until cleanup, so it cannot silently replace evidence a caller already cited.
Hashes detect corruption but do not authenticate files against someone who can rewrite the entire store.

Retrieved text can enter the calling agent's cloud context, even though extraction occurs on your machine.
Repeated retrieval can disclose a whole document; bounded individual results do not enforce a cumulative disclosure limit.

## Reference

Read `openreading.artifacts.models` for field definitions and `openreading.artifacts.service` for the import lifecycle.
The [schema guide](../schemas/README.md#retained-local-evidence-contracts) names the three independent wire contracts.
`openreading.artifacts.search` defines lexical ranking, exact excerpt offsets, and request-bound continuation cursors.

## Not built yet

These profiles have no folder import, summaries, vector search, or hosted fallback.
Docling release limits require separate host and base-machine measurements.
Its byte caps do not establish token savings, and its process isolation does not impose a native memory ceiling.
