---
spec_format_version: "0.1"
title: "Connect assistants to an operator-run Core server"
artifact_type: "prd"
spec_revision: 1
author: "Akshay"
created_at: "2026-09-19T00:00:00Z"
updated_at: "2026-09-19T00:00:00Z"
---

## Problem

The existing MCP launcher uses STDIO, where an assistant starts another OpenReading process and communicates through its input and output streams.
Operators need assistants to use a server they already started with their own configuration and provider credentials.
The existing REST server does not expose an MCP endpoint that native assistant connectors can consume.

## Product intent

You clone Core, configure `.env`, test the CLI, and start one process with the proposed `openreading serve --mcp` command.
You run ngrok to expose that process through HTTPS and register its `/mcp` URL in Claude and ChatGPT.
The server provides its existing REST interface and thirteen general MCP tools without requiring an OpenReading client plugin.
The [design record](../../design/serve-mcp.md) specifies configuration, authentication, processing boundaries, and complete remote result delivery.

## Scope

This proposed mode serves one operator, one authorized input directory, and one retained MCP workspace shared by approved clients.
A backend is the configured adapter responsible for processing a document through a library, model, or hosted provider.
Provider credentials remain server-side, while each approved client receives separate revocable access tokens through the proposed OAuth flow.
An input document must already exist inside the operator's authorized directory before an MCP client can name it.
Ngrok supplies the reference development connection, while another reachable HTTPS reverse proxy can provide the same transport boundary.

Existing STDIO integrations remain supported, and existing REST provider-job semantics remain separate from durable MCP execution-job semantics.
This scope excludes plugins, automatic chat-attachment transfer, local chooser interfaces, separate owners, and shared hosting or billing.
Successful connection tests do not establish document extraction accuracy, hosted-provider readiness, or a supported Windows execution implementation.

## Acceptance criteria

```productspec-acceptance-criteria
- id: AC-1
  criterion: A separately cloned Core instance exposes REST and HTTP MCP from one operator-started serve process using the operator's explicit configuration.
- id: AC-2
  criterion: Claude and ChatGPT native remote connectors authorize through the ngrok HTTPS URL and discover all thirteen general tools without a plugin or STDIO launcher.
- id: AC-3
  criterion: Invalid or unauthorized MCP requests cannot read documents, discover retained content, resolve provider credentials, or create processing work.
- id: AC-4
  criterion: OAuth clients receive resource-bound revocable tokens without receiving the operator's server key or provider credentials.
- id: AC-5
  criterion: Input access remains limited to the configured server folder and refuses traversal, descendant symlinks, and access to private state.
- id: AC-6
  criterion: Both approved clients can discover and inspect the same operator's accepted jobs after disconnecting or reconnecting, without restarting processing.
- id: AC-7
  criterion: Complete large results reconstruct through authenticated MCP continuation with the receipt's exact content hash and byte count.
- id: AC-8
  criterion: HTTP integration preserves existing tool semantics, schema history, retained records, STDIO behavior, and explicit cancellation limitations.
- id: AC-9
  criterion: Configuration parity is tested offline with mock provider credentials, and no sensitive configuration appears in tools, receipts, logs, or retained proof records.
- id: AC-10
  criterion: Offline verification, exact-head CI, and native host acceptance all pass before the final consolidated review and an owner-operated merge.
```

## Status and completion

Every acceptance criterion remains pending, and the commands above must not be presented as implemented setup instructions.
The proposed single-owner OAuth flow requires native compatibility proof before processing acceptance can be considered complete.
When the feature ships, move its durable facts into code documentation and delete this specification with its design record.
