# Security Policy

Two audiences read this file. If you found a bug in this project, start at "Reporting a
vulnerability". If you have to approve this project for regulated data, start at "What this
software protects, and what it does not".

## Reporting a vulnerability

Do not open a public issue for a security vulnerability.

Report through GitHub's private vulnerability reporting
([Security → Report a vulnerability](https://github.com/openreading-ai/openreading-core/security/advisories/new)),
or by email to <creativeaisle@gmail.com> if you would rather not use GitHub.

Include the affected version or commit, what an attacker gains, and a reproduction if you have
one. The maintainer acknowledges a report within a few days and posts updates while a fix is in
progress. To be credited in the advisory, say so and give the name to use.

## What this software protects, and what it does not

OpenReading runs on the machine where you start it and sends documents to the backends you name.
A backend is one document-reading engine, either local code or an HTTP service. You choose the
backend or configure `policy.backends` as the default chain. The router does not verify vendor
contracts, data residency, retention, or training practices. Review your agreements before naming
a hosted backend for regulated data. The [router guide](src/openreading/router/README.md) explains
default and explicit backend selection.

### Where credentials live, and for how long

The credential broker reads backend keys from the process environment for each request. Some
provider SDKs also use ambient credentials, such as AWS profiles or Google ADC. Request bodies
cannot supply credential values, and `backend.runtime.endpoint` cannot redirect a request carrying
them. An allowed `credentials_ref` may select a configured environment alias. Read the
`openreading.credentials` module docstring for the full precedence and alias rules.

The HTTP server's own caller tokens are a separate thing from vendor keys. Caller authentication is
off by default, and with no `OPENREADING_API_KEYS` set anyone who can reach the port spends your
vendor credits. That is why the default bind is 127.0.0.1 and any other `--host` prints a warning.
Setting `OPENREADING_API_KEYS` to a list of bearer tokens turns authentication on for every
endpoint but `GET /healthz` and `POST /v1/webhooks/{backend_id}`. That variable is read once at
process startup, never from a request body or a command-line flag, so rotating a token means
restarting the server. `OPENREADING_API_KEY_SCOPES` narrows one token to a list of backends it may
reach. That list bounds every backend the request reaches. It applies whether the caller names one,
lets the router pick from its fallback chain, or runs a strategy. Startup does not check a scope's
backend ids against the backends that exist. A typo therefore leaves the token reaching nothing
rather than reaching too much. [The HTTP server](src/openreading/server/README.md) states what that
scope bounds and what it does not.

An asynchronous HTTP job belongs to the API-key principal that submitted it. Another key
receives the same 404 response as an unknown identifier. Polling also checks current backend
scope before driving a job. Without authentication, callers share the anonymous principal.

### What leaves your machine, and to whom

Local library backends parse in this process or a supervised child process. LiteParse uses a
worker with a deadline and sampled memory ceiling. That supervision is not a security sandbox.
Native parser code still runs with the privileges of the account you started. Use an isolated
account or container for untrusted documents. Self-hosted HTTP backends, including `docling` and
`qwen-vl`, send documents to their configured endpoints. Those endpoints may be on another
machine. The router does not enforce `require_local` or verify that they point to loopback.

HTTP execution does not impose hard process timeouts or memory ceilings on in-process PyMuPDF
or local Docling parsing. For example, a hostile document can occupy that backend's process
lock until parsing returns. Apply process isolation and request concurrency controls externally.

Several paths are easy to miss when reading a single page:

- **A document URL.** OpenReading checks HTTP or HTTPS URLs and public DNS answers before
  downloading or forwarding them. URL credentials are refused. Its own download pins the
  vetted address, rejects redirects, and limits the streamed response to 100 MiB. A nonempty
  `OPENREADING_ALLOW_PRIVATE_URLS` disables the public-address check and address pinning.
  A backend that accepts URLs directly receives the checked URL instead. That backend resolves
  the hostname again and may follow redirects. Its network policy must prevent private-network
  access because Core cannot pin that later fetch or bound its response size.
- **An HTTP upload.** Multipart `file` uploads use the client's file and need no server path root.
  The server may spool an upload to temporary disk while decoding it. It closes the spool before
  dispatch, including malformed and interrupted requests. The normalized request retains the
  bytes in memory for execution and pending jobs. See `openreading.server.uploads` for limits.
- **A server file path.** `document.path` is refused over HTTP unless
  `OPENREADING_SERVER_PATH_ROOT` names a directory. The server resolves a regular file beneath
  that root and converts it to bytes before dispatch.
- **`OPENREADING_TEXTRACT_S3_BUCKET`.** The multi-page asynchronous Textract flow stages your
  document in an S3 bucket you own before Textract reads it, keyed by the document's own SHA-256.
  Nothing in this project deletes that object afterwards, so set a lifecycle rule on the bucket
  yourself. The sole-page synchronous path sends bytes directly and stages nothing.
- **A vendor liveness probe.** `GET /v1/backends` reports each backend's `liveness_probe` kind from
  its descriptor and makes no outbound call. Firing a probe is the separate
  `POST /v1/backends/{id}/liveness` call, and a kind of `vendor` means that call reaches a third
  party. One backend declares `vendor` today, `anthropic-claude`, whose probe lists models and
  sends no document content.
- **`backend_raw`.** An envelope is the JSON response every backend returns. The vendor's own
  response body rides back inside it untouched. So whatever the vendor echoes travels into every
  saved file, batch result, comparison input, and ledger blob.
  [JSON Schemas](src/openreading/schemas/README.md) says what it holds, where it ends up, and how
  to drop it.

### What each surface writes to disk, and what deletes it

Direct parsing returns an envelope to the caller. CLI save options and batch output directories
write results to disk. The HTTP job store holds work in process memory. Its age limit reaps
terminal records, while active records may remain until they complete or are removed.
`DELETE /v1/jobs/{id}` removes the local record. It does not promise remote cancellation or stop
vendor billing after a hosted submission.

The MCP artifact store retains source copies, normalized responses, passages, manifests, job
records, and exports beneath the configured `--artifact-root`. MCP means Model Context Protocol,
the tool interface used by an assistant. For example, an imported file remains after its host
disconnects. Cancel active imports and wait for terminal status before stopping every client
using the store. Remove that exact artifact-root directory to delete its retained data.
Do not delete the source input directory unless you also intend to remove your original files.
No MCP tool deletes retained data, and plugin removal does not clean a separately configured
store. Launcher-owned selection copies and transfer caches require that launcher's cleanup steps.
The [artifact guide](src/openreading/artifacts/README.md#operations) documents the directory layout.

Detached imports admit at most four nonterminal jobs per input grant and one per source
reference. A fifth queued import returns a retryable busy error instead of spawning a process.
The cap does not rate-limit synchronous HTTP requests or bound native parser memory.

Setting `OPENREADING_LEDGER` records strategy runs for replay. The ledger writes document bytes
and full responses in plaintext beneath the configured directory. It omits source URLs because
they may contain credentials. A URL-only run cannot resume without retained document bytes. It does not
encrypt, expire, sweep, or delete them. Protect the directory and apply your own retention policy.
Direct backend calls do not arm a resumable ledger run. [The run ledger](src/openreading/ledger/README.md)
documents the files and resume rules.

### What backend selection guarantees

`policy.backends` supplies the default backend chain in the order you give. An explicitly named
backend runs directly. The server's API-key scope can narrow either form, including strategy
leaves. The removed `require_baa`, `no_train_on_data`, `require_local`, and attestation keys do
not filter a request. A BAA is a business associate agreement you arrange with a vendor.
Check vendor claims, signed agreements, and endpoint locations outside this software. A
misspelled or removed policy key is refused by configuration validation, not silently enforced.

### Non-goals

These controls are outside the current server and ledger contracts.

| Not provided | What to do instead |
|---|---|
| Rate limiting, spend accounting and TLS termination in `openreading serve` | Put your own reverse proxy and accounting controls in front. Strategy `budget:` and `limits:` bound one run; nothing meters spend across runs |
| Webhook *signature* verification for `chunkr` and `open-ocr` | Neither vendor offers one. The server instead appends a per-job token to the callback URL it registers, and refuses an event that cannot present it. Setting `OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1` opts out of that check and accepts forgeable completions |
| Ledger encryption or automatic retention | Protect and expire the plaintext ledger directory with your own storage controls |
| Remote cancellation on `DELETE /v1/jobs/{id}` | Check the provider's own job status and billing after deleting a local record |
| A supported released patch series | Track `main` and pin the commit you deploy |

## What counts as a vulnerability here

OpenReading forwards documents and credentials to selected backends. Reports about these
boundaries are especially useful:

- **Server scope bypass.** An authenticated caller reaches a backend outside that token's
  `OPENREADING_API_KEY_SCOPES` allow-list, including through a strategy or fallback chain.
- **Credential leakage.** A key appearing anywhere other than the outbound request to its own
  backend. Look in logs, error messages, recorded fixtures, `backend_raw`, the ledger journal, the
  HTTP server's responses, and a comparison report.
- **Document exfiltration.** Document bytes leaving the machine on a path the request did not
  ask for. A "local" backend that phones home, a liveness probe that submits content, and a
  batch path that uploads all count.
- **Server-side request forgery.** A URL with a non-public DNS answer reaches download or
  backend dispatch without `OPENREADING_ALLOW_PRIVATE_URLS` set. A backend's later DNS lookup
  and redirects remain a separate boundary governed by that backend's network policy.
- **Job ownership bypass.** One API key reads, drives, or deletes another key's asynchronous
  job. A job identifier locates a record and does not grant access to it.
- **Server file-read escape.** `document.path` names a file for the server process to open rather
  than the caller. Every HTTP ingress refuses it unless `OPENREADING_SERVER_PATH_ROOT` names a
  directory to serve from. Any path that escapes that root, through a symlink or otherwise, is
  worth a report.
- **Schema-boundary validation gaps.** Server input accepted though it violates the vendored
  request schema, or a response emitted without validating against the response schema.
- **Webhook authentication bypass.** A signed callback accepted without a verified signature, or
  accepted while its secret is unset. `reducto` is the one backend that signs today, through
  `REDUCTO_WEBHOOK_SECRET`. For `chunkr` and `open-ocr`, which cannot sign, a callback accepted
  without the per-job token the server appended to the URL it registered.

## Out of scope

- The quality, safety, or accuracy of any backend's output.
- Attacks that require the operator to have already granted the access being abused. A policy
  that admits a backend, and a document that then reaches it, is working as designed.
- Vulnerabilities in dependencies that do not affect OpenReading. Report those upstream, and open
  an issue here if OpenReading should pin or patch around them.

## Supported versions

This project is pre-release and has no published PyPI package. A `v0.3.0-rc.1` Git tag exists,
but it does not establish a supported patch series. Security fixes land on `main`. Pin the
commit you deploy and update that pin when a fix lands. The [changelog](CHANGELOG.md) records
changes, while the [README](README.md) explains versioning.
