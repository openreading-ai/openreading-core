# Security Policy

Two audiences read this file. If you found a bug in this project, start at "Reporting a
vulnerability". If you have to approve this project for regulated data, start at "What this
software protects, and what it does not".

## Reporting a vulnerability

Do not open a public issue for a security vulnerability.

Report through GitHub's private vulnerability reporting
([Security → Report a vulnerability](https://github.com/multiversal-ventures/openreading-core/security/advisories/new)),
or by email to <creativeaisle@gmail.com> if you would rather not use GitHub.

Include the affected version or commit, what an attacker gains, and a reproduction if you have
one. The maintainer acknowledges a report within a few days and posts updates while a fix is in
progress. To be credited in the advisory, say so and give the name to use.

## What this software protects, and what it does not

OpenReading runs on a machine you control and sends your documents to the backends you name. A
backend is one document-reading engine, either a library running inside this process or a vendor's
API. Every call is charged to your own vendor account. This section is for the person who has to
approve that arrangement for regulated data. Every fact below belongs to a page or a docstring next
to the code that implements it. This section collects those places rather than restating them.

### Where credentials live, and for how long

You bring the vendor's own key, and the charge lands on your own account. A key is read from the
process environment once per request and held in memory only for that call. This project never
stores, logs, or echoes a key. It forwards a key only to that vendor's own endpoint. Run
`uv run python -m pydoc openreading.credentials` and read "Posture" for the full statement.
"Precedence" lists the five places a key is looked up, highest first.

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

### What leaves your machine, and to whom

OpenReading sends no telemetry and checks for no updates. Every outbound connection listed below
serves a request you made.

`pymupdf` and `tesseract` parse inside this process, so a document they read reaches no socket. The
`runs_fully_local` column in [Backend adapters](src/openreading/adapters/README.md) marks two more
backends true, and those two behave differently. `docling` and `qwen-vl` are services you host
rather than libraries you import. Each sends the document over HTTP to the address in
`DOCLING_SERVE_URL` or `QWEN_VL_ENDPOINT`. Each backend also ships a descriptor, the static record
of what it reads, needs, and guarantees. Both of these descriptors claim local. The router's
compliance stage (stage 1) does not take that claim on trust, so it checks the address itself.
Under `require_local` it reads the same variable the adapter reads, then drops the backend with the
code `not_local` unless the address resolves to loopback. An unset variable passes, because nothing
yet shows it points off this machine. A backend with an unset endpoint has no address to send to,
so it is skipped for missing credentials before any document moves. A real hostname fails the
check, so the backend is dropped before it sees the document. So `require_local` means "nothing
leaves this machine" while those two variables, listed in
[Backend adapters](src/openreading/adapters/README.md), point at a container on this machine. A
hosted backend receives the document itself, which is what naming one means.

Staying on this machine is not the same as being safe. A document you parse locally is read in this
process by third-party native code, `pymupdf` or the `tesseract` engine. OpenReading adds no
sandbox around that code and claims none. A malformed or hostile document therefore reaches it with
the privileges of the process you started. Give a corpus of unknown provenance the isolation you
would give any native parser, such as a container or a dedicated user. Keep those libraries
patched. A crash or a hang inside one of them is that library's own vulnerability, so report it
upstream.

Four paths are easy to miss when reading a single page:

- **A document URL.** `document.url` makes this process fetch that address itself, which is one
  more outbound call than naming a backend. The fetch refuses any host resolving to loopback,
  private, link-local, or reserved space, and `OPENREADING_ALLOW_PRIVATE_URLS=1` lifts that refusal
  for an intranet document store.
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

A plain `parse` writes nothing at all, because the envelope goes to standard output. A batch writes
one file per document only when you pass `--save-dir`. A fan-out comparison does the same, writing
`DIR/<backend>.json` for each subject when you pass `compare --save-dir`. The HTTP server keeps its
job store in memory, and that store dies with the process.

The ledger is the one surface that writes your documents down. With `OPENREADING_LEDGER` set, a
strategy run stores the input document and the response as encrypted blobs. The per-run key lives
in the same directory. A `/v1/parse` strategy request reaches the same code path, so a server
started with `OPENREADING_LEDGER` set writes to that ledger too.
[The run ledger](src/openreading/ledger/README.md) covers what that encryption buys, what the
retention reaper deletes, and what survives deletion. Read it before you set `OPENREADING_LEDGER`
over regulated documents. What survives key destruction is a lasting index of filenames and content
hashes.

### What a compliance policy does and does not guarantee

`require_baa`, `no_train_on_data`, and `require_local` filter backends on what each vendor
advertises about itself in its descriptor. A BAA is the HIPAA business associate agreement a vendor
signs before it may handle regulated data on your behalf. These keys know nothing about which
agreements your organisation has actually signed. Passing that filter is necessary and not
sufficient, and confirming the executed paperwork is yours to do out of band.
[Routing and keys](src/openreading/router/README.md) states this where the policy keys are
introduced, and lists a coded reason for every backend dropped.

The three attestation keys are the operator's own assertion, and they are attestations rather than
feature toggles: you assert that paperwork exists outside the system, the router cannot verify
that, and the default is therefore to refuse. All three live in the `policy:` block of your
`openreading.yaml`, which is the only place any of them can be written. No request body can set
one, on any surface, and the server reads them from the file at `OPENREADING_CONFIG` rather than
from its own environment. All three widen the eligible set, on three different axes.
`allow_unverified_compliance` admits the backends that stayed silent on a fact, and without it
unverified compliance fails closed. `baa_tier_confirmed` admits a named backend whose tier-gated
BAA you signed, and `train_optout_confirmed` a named backend whose training opt-out you applied.
Nothing downstream of the policy widens the set again, and that downstream half is the part you
can promise an auditor.
[Routing and keys](src/openreading/router/README.md#how-it-decides) lists the same three keys
beside the effect each one has.

Three environment variables (`OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE`,
`OPENREADING_TRAIN_OPTOUT_CONFIRMED`, `OPENREADING_BAA_TIER_CONFIRMED`) used to carry these three
assertions on the server alone. They are removed and no longer read anywhere. A deployment that
still sets one gets the file's posture, so the failure mode is a backend dropped that used to be
admitted, never the reverse.

### Non-goals

Each row is absent on purpose, and each is also stated on the page where it bites. The last row
waits on a first tagged release.

| Not provided | What to do instead |
|---|---|
| Rate limiting, spend accounting and TLS termination in `openreading serve` | Put your own reverse proxy in front. `uv run python -m pydoc openreading.server`, "Security": "What this does NOT add: rate limiting, spend accounting, transport encryption". A strategy bounds one run's spend before dispatch with `budget:` and `limits:` (`uv run python -m pydoc openreading.strategies`). Nothing meters spend across runs |
| Webhook *signature* verification for `chunkr` and `open-ocr` | Neither vendor offers one. The server instead appends a per-job token to the callback URL it registers, and refuses an event that cannot present it. Setting `OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1` opts out of that check and accepts forgeable completions |
| Protection of the ledger root beyond file modes | Give that directory the filesystem permissions and disk encryption you give the documents themselves |
| A released version to patch | Track `main`, as "Supported versions" below explains |

## What counts as a vulnerability here

OpenReading forwards documents and *your* credentials to third-party backends, and decides by
policy which backends a document is allowed to reach. Those are the interesting boundaries. Reports
in these areas matter most:

- **Compliance-filter bypass.** Any path by which a request carrying `require_baa`,
  `no_train_on_data`, or `require_local` reaches a backend the policy should have dropped. A
  strategy construct, a fallback chain, a route, and a descriptor field that lies all count.
- **Credential leakage.** A key appearing anywhere other than the outbound request to its own
  backend. Look in logs, error messages, recorded fixtures, `backend_raw`, the ledger journal, the
  HTTP server's responses, and a comparison report.
- **Document exfiltration.** Document bytes leaving the machine on a path the request did not
  ask for. A "local" backend that phones home, a liveness probe that submits content, and a
  batch path that uploads all count.
- **Server-side request forgery.** A `document.url` that reaches an address the guard should have
  refused. That means a non-http(s) scheme, or a host resolving to loopback, private, link-local,
  or reserved space, a cloud metadata endpoint included. The guard pins the connection to the one
  address it vetted and follows no redirect, so a rebinding or redirect path past it counts too.
  `OPENREADING_ALLOW_PRIVATE_URLS=1` turns the check off on purpose and is not a finding.
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

There is no release, so there is no released version to patch. This project carries no git tags and
publishes nothing to PyPI, which [`README.md`](README.md) and [`CHANGELOG.md`](CHANGELOG.md) both
state. Security fixes land on the `main` branch, and the patch channel today is `git pull`.

Pin to a commit if you need a fixed version, and move the pin forward to take a fix.
[`CHANGELOG.md`](CHANGELOG.md) lists tagging a release and publishing to PyPI as planned work, and
this section changes when that ships.
