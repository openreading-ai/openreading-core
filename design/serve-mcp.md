# MCP on the operator's running HTTP server

Status: proposed, not implemented. This design requires owner review before any runtime implementation begins.
The proposal targets Core branch `feat/local-document-mcp`, following preserved commit `e8e7e13`.
The [product specification](../product/specs/serve-mcp.product-spec.md) defines the operator workflow and acceptance criteria.
Delete both records when implementation ships, after moving durable facts into module documentation and executable help.

## Outcome and boundary

You clone Core, configure its environment, and start one server using the proposed `openreading serve --mcp` command.
That process exposes existing REST operations and general MCP tools using the same operator configuration.
Model Context Protocol, abbreviated MCP, supplies typed tools to compatible assistant clients through a standard connection.
You run ngrok separately to forward a public HTTPS address into that server's loopback port.
Claude and ChatGPT connect to the public `/mcp` address without installing an OpenReading plugin or another processing runtime.
A developer can substitute an existing HTTPS reverse proxy without changing Core's tool implementation.

A backend is an adapter that processes a document through a local library, model, or hosted provider.
Provider credentials remain on the server, including credentials used when an authorized backend contacts an external service.
Document results returned through the tunnel enter the calling assistant's context and pass through the tunnel provider.
Each backend reads the formats its [descriptor claims](../src/openreading/adapters/README.md), subject to installed dependencies and configuration.

This first HTTP mode has one operator, one authorized input directory, and one retained MCP workspace.
Multiple approved clients deliberately share that operator's MCP jobs and retained results across independent connections.
It does not provide separate users, tenant isolation, automatic chat-attachment transfer, or a filesystem chooser.
Existing REST provider jobs and durable MCP execution jobs keep their separate identifiers and documented meanings.
CLI commands remain separate invocations using the same checkout and explicit configuration, rather than clients of the running process.

## Proposed operator experience

All commands and new settings in this section describe proposed behavior, rather than executable instructions for the current branch.
Installation uses the existing server and agent extras, with backend dependencies selected through the usual installation workflow.
You keep one `.env` beside the checkout and point `OPENREADING_CONFIG` at your chosen configuration file.
Existing shell variables continue to take precedence over values loaded from `.env` or the global `--env-file` option.

The following example grants access to `examples/` and stores retained state in a separate private directory.
The server key authenticates access to your OpenReading instance and must differ from every provider API key.

```dotenv
OPENREADING_CONFIG=/absolute/checkout/openreading.yaml
OPENREADING_SERVER_PATH_ROOT=/absolute/checkout/examples
OPENREADING_API_KEYS=<one-random-server-key>
OPENREADING_MCP_PUBLIC_URL=https://your-assigned-domain.ngrok.app/mcp
OPENREADING_MCP_STATE_ROOT=/absolute/private/openreading-mcp-state
OPENREADING_MCP_BACKENDS=pymupdf
OPENREADING_MCP_STRATEGIES=local
```

```sh
# Proposed Core command, after implementation and configuration.
openreading serve --mcp

# Separate terminal, using the assigned ngrok URL configured above.
ngrok http http://127.0.0.1:8787 --url https://your-assigned-domain.ngrok.app
```

You register `https://your-assigned-domain.ngrok.app/mcp` in each client's remote MCP connection settings and complete its authorization flow.
The first authorization opens an OpenReading consent page, where you enter your server key and approve that client.
The page identifies the client and callback destination and explains its access to the operator's input directory and tools.
The model never receives your server key, provider keys, or the contents of your environment file.
Subsequent requests carry client access tokens, and the client refreshes them through the ordinary OAuth protocol.

The public URL must match the actual tunnel address, including its exact `/mcp` resource path.
Changing that URL requires updating the environment, restarting Core, and reconnecting clients against the new authorization resource.
No Core command starts ngrok, installs plugins, edits client settings, or selects a public domain automatically.

## Decisions and alternatives

| Decision | Reason |
|---|---|
| Mount native HTTP MCP inside `serve` | REST and MCP belong to the same operator-started process and configuration. |
| Reuse the existing general handlers | Transport changes must preserve processing semantics and their existing regression coverage. |
| Preserve STDIO and both existing catalogs | Existing local integrations remain usable while HTTP adds another supported connection. |
| Expose the thirteen general tools | The local evidence profile includes installation and chooser assumptions outside this server workflow. |
| Use ngrok in the documented local walkthrough | Both cloud clients can reach the same HTTPS address without a local transport bridge. |
| Add bounded single-owner OAuth using the installed SDK | Native connector authorization should not depend on arbitrary-header controls or another identity-provider account. |
| Keep processing state separate from HTTP sessions | A reconnect must discover accepted work without resubmitting document processing. |

An external STDIO-to-HTTP gateway remains technically possible, but introduces another launcher and process-lifecycle boundary for this workflow.
Turning existing REST routes into new MCP wrappers would duplicate tool semantics without preserving the complete general MCP capability set.
An external OAuth authorization server remains a later deployment option, rather than a dependency of this first owner-operated setup.
The authentication proposal adds real implementation work and must pass native authorization checks before processing acceptance can be claimed.

## Configuration and execution authority

`serve --mcp` constructs one startup context after the CLI loads the operator's environment file.
That context snapshots the existing explicit configuration, provider environment, MCP execution authority, and private storage configuration.
REST and MCP resolve the same configured backend settings and strategies through existing routing and credential mechanisms.
MCP still enforces its narrower request contract, including refusing client-supplied credentials and runtime endpoint overrides.
For example, REST upload support does not make a client attachment available to an MCP path request.

| Setting | Proposed contract |
|---|---|
| `--mcp` | Opts `serve` into HTTP MCP and its authorization routes. Without it, existing behavior remains unchanged. |
| `OPENREADING_CONFIG` | Existing explicit processing configuration. No additional MCP-specific copy of that file is required. |
| `OPENREADING_SERVER_PATH_ROOT` | Existing server path setting, required as an absolute MCP input directory. |
| `OPENREADING_API_KEYS` | Existing REST setting. Initial MCP mode requires exactly one operator key containing at least 32 ASCII characters. |
| `OPENREADING_API_KEY_SCOPES` | Existing backend scope for that key, intersected with the explicit MCP backend grant. |
| `OPENREADING_MCP_PUBLIC_URL` | Required canonical HTTPS resource URL, ending in `/mcp`, without credentials, query, or fragment. |
| `OPENREADING_MCP_STATE_ROOT` | Required private absolute directory, separate from and nonoverlapping with the input directory. |
| `OPENREADING_MCP_BACKENDS` | Required comma-separated explicit backend grant. Missing, empty, or unknown entries refuse startup. |
| `OPENREADING_MCP_STRATEGIES` | Optional comma-separated entrypoint grant. Unset means no strategy execution authority. |
| `OPENREADING_MCP_EXECUTION_ENV` | Optional additional environment variable names for worker dependencies, never secret values in tool arguments. |
| `OPENREADING_MCP_RESPONSE_BYTES` | Positive wire-response budget, initially 1,000,000 bytes with the existing minimum of 4,096. |
| `OPENREADING_MCP_CONCURRENCY` | Positive processing concurrency, initially one, shared across this operator's HTTP clients. |

The public URL and required settings are validated before binding, creating storage, or declaring server readiness.
MCP mode rejects zero or multiple REST keys rather than silently choosing an owner or widening another key's scope.
You generate the key from random bytes, because checking its length cannot establish that it was generated securely.
Plain `serve` continues to support its existing authentication configurations, including its current defaults and multiple scoped keys.
Enabling MCP cannot leave REST processing unauthenticated on the same tunneled port.

Worker configuration uses the provider credential and runtime variable names declared by authorized backend descriptors.
Any additional SDK configuration or executable path must be explicitly named in the operator's worker-environment setting.
Forwarded names are checked for presence, validity, and reserved names before accepting requests or reading input documents.
Incoming server keys, OAuth secrets, and ngrok credentials are never forwarded to processing workers.
The current private `HOME`, temporary directory, cache, configuration, and journal rules remain enforced inside those workers.
Ambient home-directory credential discovery is not silently promised inside isolated workers, even when a direct CLI invocation supports it.
Explicit credential files require supported provider configuration and dedicated tests rather than inheriting the operator's home directory.

Settings remain fixed until restart, and client requests cannot change input directories, grants, providers, or execution environment.
Operators needing separate input directories or independent owners run separately configured server instances in this first version.

## HTTP transport and shared runtime

The installed MCP SDK provides `StreamableHTTPSessionManager`, its ASGI request handler, and HTTP request context for tool calls.
ASGI is the application interface through which the existing FastAPI server receives asynchronous HTTP requests.
The server lifespan owns one manager, general tool server, input grant, result store, and execution-job service.
The lifespan explicitly enters and closes the manager, because mounting an application does not run its lifespan automatically.

```text
Claude or ChatGPT
        |
        | HTTPS MCP and OAuth
        v
ngrok HTTPS endpoint
        |
        | loopback HTTP forwarding
        v
openreading serve --mcp
        +-- /v1/...    existing REST handlers and API-key authentication
        +-- /mcp       HTTP transport, owner authentication, general MCP handlers
        +-- OAuth      discovery, client registration, consent, token and revocation routes
        |
        +-- shared operator configuration and provider settings
        +-- retained MCP workspace and existing isolated processing workers
```

Use stateless HTTP transport initially, with ordinary JSON responses rather than a required persistent server-event stream.
Stateless transport means each HTTP request authenticates independently, while processing jobs and results remain durably stored.
The SDK handles protocol initialization, notification responses, protocol versions, and supported methods after Core's request-boundary checks.
Core sanitizes malformed transport errors before SDK validation details can expose submitted document values or credentials.
This boundary preserves the existing sanitized-error contract in both HTTP replies and logs, including failures before tool dispatch.
MCP request bodies initially have a four-MiB ceiling, independent of the existing larger REST upload limit.
Authorization form bodies have a separate sixteen-KiB ceiling, enforced before decoding submitted credentials or client metadata.
GET or DELETE requests receive the SDK's applicable method response when no stream or transport session exists.
Durable processing never depends on an MCP session identifier, a TCP connection, or the lifetime of a client chat.

The shared handler registration receives an explicit delivery policy instead of embedding STDIO-specific export assumptions.
Blocking operations continue through the existing worker-thread dispatch boundary without blocking the HTTP event loop.
The existing schemas, tool names, authorization checks, annotations, and sanitized errors remain authoritative for tool operations.
MCP authentication runs before parsing tool arguments, reading retained content, resolving credentials, or creating execution jobs.

The tool catalog remains `backends`, `readiness`, `liveness`, `route`, `strategy`, `parse`, `batch`, and `resume` with the `openreading_` prefix.
It also includes `get_job`, `list_jobs`, `cancel_job`, `compare`, and `get_result`, with the same prefix.
No HTTP exposure of the local chooser or additional local evidence tools is included in this proposal.

## Public access and OAuth

OAuth gives each approved client revocable access tokens instead of requiring the operator's permanent server key on every request.
Ngrok supplies reachability and TLS termination, but its agent authtoken authenticates the tunnel rather than MCP callers.
A browser-login policy placed in front of `/mcp` does not establish MCP-compatible OAuth discovery or token handling.
Use Core's proposed OAuth flow for the native acceptance path rather than assuming a client can send custom authentication headers.

The first implementation uses the installed SDK's authorization routes and provider interfaces with a bounded, single-owner provider.
It publishes protected-resource metadata and authorization-server metadata using the configured public HTTPS origin.
It supports authorization-code flow with mandatory S256 PKCE, public-client dynamic registration, refresh, and revocation.
PKCE binds the authorization code to the requesting client so possession of an intercepted code alone cannot exchange it.
Client registration grants no document access and never fetches arbitrary logos, metadata URLs, or client-supplied remote resources.
Registered redirects must match exactly, permit HTTPS or native loopback callbacks, and refuse fragments, credentials, and arbitrary schemes.
Client names and callback destinations are escaped on the consent page, and requests for unsupported scopes are refused.

Only successful owner authentication and explicit consent can issue a code for the bound client, redirect, resource, and scope.
Owner authentication compares the supplied server key with the configured key and returns a fixed failure message on mismatch.
The key is submitted only through the consent page's POST body and never appears in URLs, redirects, receipts, or logs.
Pending consent uses an expiring, single-use identifier and a protected browser cookie with an independently verified CSRF token.
Cookie settings include `Secure`, `HttpOnly`, and `SameSite=Lax`, with exact-origin checks on consent submissions.
The consent page sends `Cache-Control: no-store`, a restrictive content policy, and `Referrer-Policy: no-referrer`.

Access tokens are random opaque values, scoped to the canonical `/mcp` resource and the fixed `openreading:mcp` OAuth scope.
That scope delegates only the operator's configured tool authority, rather than authorizing additional backends or configuration access.
Tokens retain client and owner identity, expiration, consent binding, and a configuration fingerprint without retaining the raw owner key.
Every request checks token validity and resource binding before entering the shared MCP handlers.
The SDK's resource validation is enabled explicitly instead of depending on its current optional default.
OAuth tokens are refused on existing REST routes, which continue to require their existing API-key authentication.
The raw operator key is not accepted as an alternative `/mcp` bearer token in this initial OAuth mode.

Private storage preserves approved client registrations and token digests across ordinary server restarts.
State uses atomic transactions and owner-only permissions, with no raw access or refresh token stored after issuance.
Refresh rotates tokens atomically and refuses reuse, expired grants, revoked clients, changed owner keys, or changed execution authority.
Configuration changes require renewed client authorization but do not delete existing jobs or retained results.
Previously accepted jobs retain their original execution snapshot until completion or explicit cancellation, even after an operator narrows future authority.
The proposed initial lifetimes are five minutes for consent, one minute for codes, one hour for access, and seven days for refresh.
Unapproved registrations expire after ten minutes, and approved registrations remain stable while their client grants are retained.
Bound storage to 256 clients, 128 pending authorizations, and 1,024 token families, with explicit refusal at capacity.
Apply bounded rate limits to registration, authorization, token, and failed owner-login attempts without logging submitted credentials.
Initial per-instance limits allow ten registrations, thirty authorization requests, and five failed owner logins per minute.
Token endpoints allow 120 requests per client per minute, with a 600-request global ceiling and bounded limiter storage.
Limit exhaustion returns 429 with `Retry-After`, while expired unapproved entries are reclaimed before capacity is checked.
No account signup, password recovery, organization administration, external identity federation, or cross-owner access is introduced.

This OAuth provider is new code, even though the SDK supplies its protocol plumbing and validation helpers.
Its first implementation checkpoint must prove authorization and an authenticated catalog in both native clients through ngrok.
Neither a successful curl request nor SDK-only authentication establishes native host acceptance for that checkpoint.
If either host rejects the standards-based flow, record the failure and resolve compatibility before claiming the workflow is ready.

## Tunnel and request boundaries

The reference setup binds Core to `127.0.0.1` and uses ngrok to forward the configured HTTPS endpoint to that port.
Core treats the configured public URL as authoritative for OAuth metadata rather than trusting forwarded headers to construct URLs.
Host validation accepts only the configured public authority and the explicitly configured local listening authority.
MCP requests with an absent Origin remain valid for server clients, while present origins require an exact configured match.
The consent flow requires its canonical public origin, and no wildcard host or origin exception is introduced for ngrok.
Changing the ngrok hostname requires explicit configuration changes rather than accepting arbitrary `*.ngrok.app` hosts.

The tunnel forwards the whole listening port unless an operator configures additional path restrictions.
Consequently, REST authentication remains enabled and discovery exceptions are limited to the exact OAuth metadata and protocol routes.
An authentication exception for discovery must never become an exception for `/v1/parse`, `/mcp`, or retained-state access.
Ngrok browser interstitials, header rewriting, streaming behavior, and plan-specific limits are checked during native acceptance rather than assumed.
Disable or redact tunnel request inspection for authorization traffic, because proxy diagnostics can otherwise capture keys, codes, and tokens.
Core does not depend on ngrok's SDK, account API, traffic-policy language, or commercial plan for its protocol implementation.

## Input, state, and result delivery

An input grant authorizes relative document paths below the operator's configured directory and retains the existing safe-open protections.
For example, `statement.pdf` names a file in that directory, while absolute paths, parent traversal, and descendant symlinks remain refused.
Chat uploads are not automatically synchronized into this directory, and the tool description must say where its files come from.
Startup refuses a grant containing the loaded environment file, processing configuration, or private state directory.
Document paths cannot escape that grant, and tools never expose environment variables or raw server configuration as separate resources.
Other files deliberately placed inside the grant remain authorized document inputs, so this boundary cannot detect copied secrets.
The state directory contains separate processing and authorization subdirectories outside the input grant.
Both native clients share the same operator workspace intentionally, including access to jobs started by the other approved client.

HTTP `get_result` with `delivery=auto` returns complete inline content when the real serialized response fits the configured budget.
Otherwise, it returns the existing lossless fragment shape and a continuation cursor without exporting a server-local file automatically.
Each continuation is authenticated and bound to the retained result, content digest, input grant, and response budget.
The client follows every cursor to completion before claiming complete delivery, and reconstructed content must match the receipt hash.
Measure budgets against actual HTTP JSON serialization, including the request identifier, escaping, and both success and error envelopes.
Preflight acceptance receipts before creating jobs, and cancellation receipts before writing cancellation state.

Explicit `delivery=file` keeps its documented server-side export meaning and does not create a public download URL.
Its response explains that fragments retrieve the content remotely, while the exported path alone establishes no assistant access.
STDIO retains its current automatic inline-or-file behavior, and existing retained record versions remain readable without rewriting them.
The current result contract already represents fragments, inline content, and local files, so no new payload family is required by this design.
Any implementation requiring a wire-shape change must version its schema instead of modifying an existing published schema in place.

## Lifetime and failure behavior

Accepted general jobs retain the existing detached supervisor, source snapshot, cancellation, and recovery semantics.
Closing a chat, disconnecting ngrok, or restarting the HTTP transport does not resubmit accepted processing or imply cancellation.
After reconnecting, clients discover retained jobs with `list_jobs` and inspect them with `get_job` before starting more work.
Repeating parse, batch, or resume can create new processing and remains explicitly non-idempotent.
No HTTP retry policy may replay those calls automatically after a connection failure or an uncertain response.

Server shutdown closes HTTP resources and input descriptors without pretending to cancel detached processing already accepted by its supervisors.
Explicit job cancellation keeps its existing race with publication and never claims cancellation at a hosted provider without evidence.
Failed supervisors remain observable through recovery, and durable records are never converted into fabricated successful processing outcomes.
OAuth failures use bounded protocol errors, while tool failures retain their existing sanitized error contracts.
The initial implementation uses one web-server worker and refuses unsupported multi-worker startup for shared authorization state.
Processing concurrency remains governed by the existing execution slots across all clients of this workspace.
Reply budgets do not imply bounded comparison memory, and existing synchronous comparison limits remain explicitly documented.

## Implementation boundaries

| Existing area | Proposed change |
|---|---|
| `pyproject.toml` and dependency lock | Require the verified SDK 1.30 interfaces for HTTP authorization, while retaining the existing pre-2.0 compatibility boundary. |
| `cli/app.py` and CLI manual | Add `serve --mcp`, startup validation, examples, exit behavior, and explicit ngrok setup. |
| `server/app.py` | Build shared startup context, mount transport and OAuth routes, preserve REST authentication boundaries. |
| `mcp_server/general.py` | Reuse catalog and dispatch while injecting HTTP delivery policy through an explicit parameter. |
| `mcp_server/results.py` | Add inline-or-fragments auto delivery without changing STDIO's existing export behavior. |
| New `mcp_server/http.py` | Own SDK transport lifetime, request boundaries, and HTTP wire-budget integration. |
| New `server/mcp_runtime.py` | Resolve startup configuration, safe provider environment, workspace, and general execution services. |
| New `server/mcp_auth.py` | Integrate SDK OAuth routes with owner consent, token validation, and bounded persistent authorization state. |
| Existing artifacts and execution modules | Preserve source checks, record integrity, cancellation, resume, and shared-engine semantics. |
| Tests and capability inventory | Add transport/authentication coverage and dispositions for new routes without changing processing capability claims. |

Keep provider credentials out of authorization state, processing control records, public metadata, and proof artifacts.
Add environment documentation beside each new read and update `.env.example` when runtime support is implemented.
Update the server, MCP, and CLI guides together, including the HTTP client's server-folder intake and fragment-retrieval instructions.
No Agent Tools package, frozen worker, plugin manifest, schema history, or existing branch needs deletion for this integration.

## Acceptance and evidence

Implementation proceeds through the following checkpoints, with offline tests remaining independent of ngrok and provider credentials.

1. Prove real HTTP initialization, authenticated discovery, and OAuth code/refresh handling using the installed SDK and a temporary server.
2. With owner assistance, prove both native clients authorize through ngrok and discover the correct thirteen-tool catalog.
3. Exercise all general tools over actual HTTP, including reconnect, resume, queued cancellation, comparison, and complete large-result reconstruction.
4. Prove REST/MCP configuration parity with mocked providers and distinct credential sentinels that never appear in responses or retained records.
5. Prove authentication precedes input reads and side effects, including invalid tokens, wrong resource, expired consent, replay, and revoked grants.
6. Preserve STDIO protocol tests, all predecessor schema bytes, stored-result compatibility, source grants, and the existing capability inventory.
7. Run `make verify`, normal push checks, and exact-head CI before consolidated review and the owner's merge decision.

Targeted HTTP tests cover malformed Origin and Host, forged forwarding headers, ngrok-style proxy requests, redirects, body limits, and serialization bounds.
Malformed JSON-RPC tests capture replies and logs, requiring fixed errors without echoed credential or document-text sentinels.
OAuth tests cover PKCE mismatch, redirect substitution, CSRF, duplicate consent, code replay, refresh races, restart, key rotation, and capacity refusal.
Negative tests must observe no file reads, provider calls, publication, or credential forwarding before the relevant authorization check.
Regressions and meaningful authorization-order mutations must fail before their fixes are accepted, following the repository testing runbook.

Native acceptance uses a separate fresh clone at the reviewed commit with its own `.env`, configuration, input directory, and private state.
The owner starts Core and ngrok, then connects ordinary Claude and ChatGPT remote connectors without OpenReading plugins or manual STDIO entries.
Record client versions, connection mode, public-resource identity, redacted authentication outcomes, tool catalog, and actual returned job/result identifiers.
Record source and content hashes, warnings, complete reconstruction, cross-client job discovery, and cancellation without retaining credentials or OAuth codes.
Synthetic local documents establish connection and lifecycle behavior, while hosted-provider accuracy and live credentials remain separate acceptance claims.
Archive review evidence in the private company repository instead of relying only on hash-bound files inside gitignored scratch directories.

## Feasibility evidence and unresolved native proof

The repository already separates `general.create_server` from its STDIO loop and retains execution independently of transport closure.
The installed MCP SDK is version 1.30.0 and exposes the HTTP manager, request context, OAuth route factory, and provider interfaces needed here.
These observations establish compatible integration mechanisms, rather than a completed or native-tested implementation of this proposal.
The current environment needs no SDK upgrade, but the package's declared minimum must cover every interface the implementation actually uses.
The single-owner OAuth provider, shared server integration, and HTTP delivery policy remain unbuilt and require their stated acceptance checks.

The following primary references were checked on 2026-09-19 and support the transport and client assumptions.

- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) defines independently running HTTP servers and transport request protection.
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) defines protected-resource discovery, client registration, PKCE, and resource-bound authorization.
- [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) documents cloud-originating connections and the native OAuth connection flow.
- [ChatGPT connection setup](https://developers.openai.com/plugins/deploy/connect-chatgpt) documents HTTPS MCP endpoints and native connection checks.
- [ChatGPT authentication](https://developers.openai.com/plugins/build/auth) documents OAuth metadata, PKCE, callbacks, and access-token validation expectations.
- [ngrok MCP forwarding](https://ngrok.com/docs/using-ngrok-with/using-mcp) documents forwarding from public endpoints into operator-run MCP processes.
- [ngrok HTTP endpoints](https://ngrok.com/docs/gateway/endpoints/http) documents assigned URLs and forwarding into a loopback HTTP server.

The ngrok gateway example's header-presence rule alone does not validate a credential and is not this design's authentication mechanism.
Native authorization compatibility remains an explicit checkpoint, including dynamic registration and the actual callbacks presented by each installed client.
