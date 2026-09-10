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

## How it decides

The fixed profile selects local PyMuPDF explicitly, preventing ambient configuration from choosing a hosted backend.
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

This profile has no OCR, folder import, summaries, vector search, local model, or hosted fallback.
Its byte caps do not establish token savings, and its process isolation does not impose a native memory ceiling.
