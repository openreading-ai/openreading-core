# Local document MCP tools

<sub>[Docs home](../README.md) · [← Artifacts](../artifacts/README.md) · [Channel contract →](../derive/README.md)</sub>

## What this gives you

Your agent can import a local document once, then retrieve bounded evidence with physical page citations.
MCP is the protocol your client uses to discover these tools and call them over standard input and output.
For example, search for a renewal clause, read its passage, and cite the returned page and evidence identifier.

## Mental model

You start one process with explicit input and artifact directories before the client can call tools.
The input root grants file access, while the separate artifact root retains copies until you remove them.
The fixed local profile exposes exactly `openreading_import`, `openreading_search`, and `openreading_read`.

## Walkthrough

Install the optional dependencies in your development environment, then configure your client to launch this command:

```sh
python -m pip install 'openreading[agent,pymupdf]'
openreading mcp --profile local-document-proof-v1 \
  --input-root /absolute/documents --artifact-root /absolute/evidence
```

Use a source checkout or a packaged runtime carrying its immutable engine identity.
A wheel without that identity cannot establish the required core revision and refuses startup.
Your client supplies the following tool calls through MCP; these JSON objects are their argument payloads.

```json
{"path":"agreement.pdf"}
```

Pass the receipt's `artifact_id` to `openreading_search` with `query` set to `renewal`.
Pass the resulting evidence identifiers to `openreading_read`, then cite the returned filename, physical page, and identifier.
Follow `next_cursor` when present, and explain evidence gaps when the returned passages do not support an answer.

## Recipes

- Configure arguments as an array in your MCP client so spaces in directory names remain intact.
- Grant a narrow document directory rather than your home directory, and keep retained evidence outside that grant.
- Reuse the same artifact identifier across questions instead of importing the same source repeatedly.

## How it decides

The profile names PyMuPDF explicitly and uses an explicit versioned configuration, preventing ambient routing from changing extraction.
Each result contains one JSON text payload, preventing duplicate structured content from spending context twice.
Invalid arguments produce fixed protocol errors, preventing schema validation from echoing private input values.
Document text remains untrusted data, preventing a quoted instruction from gaining tool authority.

## Operations

The process speaks MCP on stdout; configure your client to capture diagnostics separately from that protocol stream.
Domain errors set `isError` and return the fixed codes documented in `openreading.artifacts.limits`.
Malformed arguments return protocol errors, while normal transport closure exits with status zero.
Configuration failures exit two, and keyboard interruption exits 130.

The [artifact guide](../artifacts/README.md) explains retention, source hashing, page provenance, and quota behavior.
The Python SDK dependency is optional and pinned through the lockfile's supported v1 release line.

## Reference

Run `openreading help mcp` for the command contract and `openreading mcp --help` for its required arguments.
Read `openreading.mcp_server.tools` for input schemas, tool annotations, and server instructions.
Client packaging and installation checks belong in the separate `openreading-agent-tools` repository.

## Not built yet

The local proof does not expose general parse, compare, strategy, triage, or remote document tools.
The remaining agent surface is proposed in [the agent design](../../../design/agentic.md).
Passing stdio tests does not establish desktop installation compatibility or measured model token savings.
