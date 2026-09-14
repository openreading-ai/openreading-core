# Local document MCP tools

<sub>[Docs home](../README.md) · [← Artifacts](../artifacts/README.md) · [Channel contract →](../derive/README.md)</sub>

## What this gives you

Your agent can import a local document once, then read its normalized result with physical page citations.
Full-document access returns the retained JSON in bounded replies; search remains available for focused questions.
MCP is the protocol your client uses to discover these tools and call them over standard input and output.
For example, search for a renewal clause, read its passage, and cite the returned page and evidence identifier.

## Mental model

You start one process with explicit input and artifact directories before the client can call tools.
The input root grants file access, while the separate artifact root retains copies until you remove them.
The profile exposes `openreading_import`, `openreading_get_document`, `openreading_search`, `openreading_read`, and `openreading_select_document`.
Long work uses `openreading_start_import`, `openreading_get_import`, and `openreading_cancel_import`.
Selection returns `selection_unavailable` unless a trusted launcher explicitly supplies a local chooser.

## Walkthrough

Install the optional dependencies in your development environment, then configure your client to launch this command:

```sh
python -m pip install 'openreading[agent,pymupdf]'
openreading mcp --profile local-document-proof-v1 \
  --input-root /absolute/documents --artifact-root /absolute/evidence
```

Source and wheel installs identify their package version, backend version, and installed extraction code.
A core commit appears only when verified packaging metadata supplies it; an enclosing Git repository is never consulted.
Your client supplies the following tool calls through MCP; these JSON objects are their argument payloads.

```json
{"path":"agreement.pdf"}
```

Pass the receipt's `artifact_id` to `openreading_get_document` to read the entire retained normalized result.
Its import receipt retains a legacy search suggestion; you can use either retrieval route.
For example, call `openreading_get_document` with `{"artifact_id":"<returned artifact_id>"}`.
Follow `next_cursor` until null, preserving fragment order when the document requires several replies.
The result includes existing structure, parser warnings, page origins and citation references, while excluding `backend_raw`.
Pass a returned evidence identifier to `openreading_read` for the exact citation quote.
Alternatively, call `openreading_search` with `query` set to `renewal` for a focused question.
Follow `next_cursor` when present, and explain evidence gaps when the returned passages do not support an answer.

## Recipes

For a long import, pass `{"path":"agreement.pdf"}` to `openreading_start_import`.
It returns a job ID promptly. Pass that ID to `openreading_get_import` with `wait_seconds` up to 20.
Stage and elapsed time describe observed work; no percentage or completion estimate is invented.
After `succeeded`, use the returned receipt for normal retrieval and citations.
Call `openreading_cancel_import` to stop a job, then check its terminal state.
A host disconnect or cancelled status request leaves background work running.

- Configure arguments as an array in your MCP client so spaces in directory names remain intact.
- Grant a narrow document directory rather than your home directory, and keep retained evidence outside that grant.
- Reuse the same artifact identifier across questions instead of importing the same source repeatedly.

For an embedded chooser, pass `selection_provider=picker` to `openreading.mcp_server.main.main(argv)`.
The provider follows the transactional context-manager contract in `openreading.mcp_server.selection`.
Shield asynchronous cleanup, including failed acquisition, so cancellation cannot interrupt child reaping or rollback.
For example, use `anyio.CancelScope(shield=True)` around cleanup awaits before propagating cancellation.
Keep blocking work off the event loop and remove only copies created by that selection.
Call `openreading_select_document` with `{}`, then import the returned `path`.
The default selection deadline is 120 seconds, independent of extraction.
Local Cancel remains available when a host Stop button does not deliver protocol cancellation.

## How it decides

The profile names PyMuPDF explicitly and uses an explicit versioned configuration, preventing ambient routing from changing extraction.
Each result contains one JSON text payload, preventing duplicate structured content from spending context twice.
Invalid arguments produce fixed protocol errors, preventing schema validation from echoing private input values.
Document text remains untrusted data, preventing a quoted instruction from gaining tool authority.

## Operations

The process speaks MCP on stdout; configure your client to capture diagnostics separately from that protocol stream.
Domain errors set `isError` and return fixed codes from `openreading.artifacts.limits` or `openreading.types.selection`.
Selection and full-document replies have separate v0.1 families; existing artifact payloads remain at v0.3.
Full-document access sends retained content to your assistant and does not establish token savings.
It preserves the stored result, including extraction limitations, rather than promising perfect OCR recognition.
The import profile decides which channels exist; this tool does not enable disabled table extraction.
Malformed arguments return protocol errors, while normal transport closure exits with status zero.
Configuration failures exit two. SIGINT and SIGTERM cancel synchronous imports and exit 130, including clients that keep stdin open.
Detached imports continue and keep private status under `jobs/INPUT_GRANT_SHA256/JOB_ID/`.
The local profile requires POSIX support. Explicit root symlinks resolve once before file access begins.

The [artifact guide](../artifacts/README.md) explains retention, source hashing, page provenance, and quota behavior.
The Python SDK dependency is optional and pinned through the lockfile's supported v1 release line.

## Reference

Run `openreading help mcp` for the command contract and `openreading mcp --help` for its required arguments.
Read `openreading.mcp_server.tools` for input schemas, tool annotations, and server instructions.
Read `openreading.artifacts.document` for JSON Pointer fragments, exact reconstruction and continuation binding.
Those instructions prohibit guessed evidence identifiers and distinguish literal search gaps from missing source facts.
They describe block offsets and generic warnings as limited evidence, rather than explanations for suspected extraction failures.
Instruction delivery does not prove compliance; each native client needs its own behavior check.
Client packaging and installation checks belong in the separate `openreading-agent-tools` repository.

## Not built yet

The local proof does not expose general parse, compare, strategy, triage, or remote document tools.
The remaining agent surface is proposed in [the agent design](../../../design/agentic.md).
Passing stdio tests does not establish desktop installation compatibility or measured model token savings.
