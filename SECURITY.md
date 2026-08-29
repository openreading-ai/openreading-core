# Security Policy

Two audiences read this file. If you found a bug in this project, start at "Reporting a
vulnerability". If you have to approve this project for regulated data, start at "What this
software protects, and what it does not".

## Reporting a vulnerability

Please do not open a public issue for security vulnerabilities.

Report through GitHub's private vulnerability reporting
([Security → Report a vulnerability](https://github.com/multiversal-ventures/openreading-core/security/advisories/new)),
or by email to <creativeaisle@gmail.com> if you would rather not use GitHub.

Include the affected version or commit, what an attacker gains, and a reproduction if you have
one. We will acknowledge within a few days and keep you updated as we work on a fix. If you
would like credit in the advisory, say so and tell us how you would like to be named.

## What this software protects, and what it does not

OpenReading runs on a machine you control, sends your documents to backends you name, and bills
your own vendor accounts. This section is for the person who has to approve that arrangement for
regulated data. Every fact below is owned by a page or a docstring next to the code that
implements it, and this section collects those places rather than restating them.

### Where credentials live, and for how long

You bring the provider's own key, and the charge lands on your own account. A key is read from the
process environment once per request and held in memory only for that call. This project never
stores, logs, echoes, resells, or bills for one, and never forwards one anywhere but that
provider's own endpoint. Run `uv run python -m pydoc openreading.credentials` and read "Posture"
for the full statement, then "Precedence" for the five places a key is looked up, highest first.

The HTTP server's own caller tokens are a separate thing from vendor keys. `OPENREADING_API_KEYS`
is read once at process startup, never from a request body or a command-line flag, so rotating a
token means restarting the server. `OPENREADING_API_KEY_SCOPES` narrows one token to a list of
backends it may reach. That list bounds every backend the request reaches, whether the caller names
one, lets the router pick from its fallback chain, or runs a strategy walk. Startup does not check
the backend ids in a scope against the backends that exist, so a typo leaves the token reaching
nothing rather than reaching too much. [The HTTP server](src/openreading/server/README.md) states
what that scope bounds and what it does not.

### What leaves your machine, and to whom

`pymupdf` and `tesseract` parse inside this process, so a document they read reaches no socket. The
`runs_fully_local` column in [Backend adapters](src/openreading/adapters/README.md) marks two more
backends true, and those two behave differently. `docling` and `qwen-vl` are services you host
rather than libraries you import, and each sends the document over HTTP to whatever address
`DOCLING_SERVE_URL` or `QWEN_VL_ENDPOINT` holds. Stage 1 admits them under `require_local` on the
descriptor's static flag alone. It never reads that address, so an endpoint outside your network
still passes the gate. Read `require_local` as "no third-party vendor" rather than "nothing leaves
this machine", and keep the document where you want it through what you put in those two variables.
A hosted backend receives the document itself, which is what naming one means.

Three paths are easy to miss when reading a single page:

- **`OPENREADING_TEXTRACT_S3_BUCKET`.** The multi-page asynchronous Textract flow stages your
  document in an S3 bucket you own before Textract reads it, keyed by the document's own SHA-256.
  Nothing in this project deletes that object afterwards, so set a lifecycle rule on the bucket
  yourself. The sole-page synchronous path sends bytes directly and stages nothing.
- **A vendor liveness probe.** `GET /v1/backends` reports each backend's `liveness_probe` kind by
  reading its descriptor, which makes no call. Firing a probe is the separate
  `POST /v1/backends/{id}/liveness` call, and a kind of `vendor` means that call reaches a third
  party. One backend declares `vendor` today, `anthropic-claude`, whose probe lists models and
  sends no document content.
- **`backend_raw`.** The vendor's own response body rides back inside the envelope untouched, so
  whatever the vendor echoes travels with it. [JSON Schemas](src/openreading/schemas/README.md)
  says what it holds, where it ends up, and how to drop it.

### What each surface writes to disk, and what deletes it

A plain `parse` writes nothing at all, because the envelope goes to standard output. A batch writes
one file per document only when you pass `--save-dir`. The HTTP server keeps its job store in
memory, and that store dies with the process.

The ledger is the one surface that writes your documents down. With `OPENREADING_LEDGER` set, a
strategy run stores the input document and the response as encrypted blobs, under a per-run key
that lives in the same directory. [The run ledger](src/openreading/ledger/README.md) covers what
that encryption buys, what the retention reaper deletes, and what survives deletion. Read it before
you arm a ledger over regulated documents, because what survives key destruction is a lasting index
of filenames and content hashes.

### What a compliance policy does and does not guarantee

`require_baa`, `no_train_on_data`, and `require_local` filter backends on what each vendor
advertises about itself in its descriptor. They know nothing about which agreements your
organisation has actually signed. Passing that gate is necessary and not sufficient, and confirming
the executed paperwork is yours to do out of band.
[Routing and keys](src/openreading/router/README.md) states this where the policy keys are
introduced, and lists a coded reason for every backend dropped.

The three attestation variables are the operator's own assertion, in the words of `.env.example`:
"TRAIN_OPTOUT / BAA_TIER are ATTESTATIONS, not feature toggles: you assert paperwork exists outside
the system; the router cannot verify that, which is why the default is to refuse." No request body
can set any of them, on any surface. All three widen the eligible set, on three different axes.
`OPENREADING_ALLOW_UNVERIFIED_COMPLIANCE` admits the backends that stayed silent on a fact, and
without it unverified compliance fails closed. `OPENREADING_BAA_TIER_CONFIRMED` admits a named
backend whose tier-gated BAA you signed, and `OPENREADING_TRAIN_OPTOUT_CONFIRMED` a named backend
whose training opt-out you applied. Nothing downstream of the policy widens the set again, and that
downstream half is the part you can promise an auditor.
[Routing and keys](src/openreading/router/README.md#how-it-decides) lists the same three keys in
their policy-file spelling.

### Non-goals

Each row is absent by design or not built yet, and each is also stated on the page where it bites.

| Not provided | What to do instead |
|---|---|
| Rate limiting, spend accounting, TLS termination | Put your own reverse proxy in front. `pydoc openreading.server`, "Security": "What this does NOT add: rate limiting, spend accounting, transport encryption" |
| Webhook signature verification for `chunkr` and `open-ocr` | Those two callback paths are unauthenticated and unverified. Keep them off any interface you do not control |
| Protection of the ledger root beyond file modes | Give that directory the filesystem permissions and disk encryption you give the documents themselves |
| A released version to patch | Track `main`, as "Supported versions" below explains |

## What counts as a vulnerability here

OpenReading forwards documents and *your* credentials to third-party backends, and decides by
policy which backends a document is allowed to reach. Those are the interesting boundaries. We are
especially interested in reports of:

- **Compliance-filter bypass.** Any path by which a request carrying `require_baa`,
  `no_train_on_data`, or a local-only policy reaches a backend the policy should have dropped:
  through a strategy construct, a fallback chain, a route, or a descriptor field that lies.
- **Credential leakage.** A key appearing anywhere other than the outbound request to its own
  backend: logs, error messages, recorded fixtures, `backend_raw`, the ledger journal, the
  HTTP server's responses, a comparison report. `backend_raw` is the vendor's own response body
  carried into the envelope verbatim, so anything a vendor echoes back travels with the envelope
  into every saved file, batch result, comparison input, and ledger blob.
- **Document exfiltration.** Document bytes leaving the machine on a path the request did not
  ask for (a "local" backend that phones home, a liveness probe that submits content, a batch
  path that uploads when it should not).
- **Schema-boundary validation gaps.** Server input that is accepted but violates the vendored
  request schema, or a response that is emitted without validating against the response schema.
- **Webhook signature bypass.** A webhook accepted without a verified signature, or accepted
  when no signing secret is configured.

## Out of scope

- The quality, safety, or accuracy of any backend's output.
- Attacks that require the operator to have already granted the access being abused. A policy
  that admits a backend, and a document that then reaches it, is working as designed.
- Vulnerabilities in dependencies that do not affect OpenReading. Report those upstream, and tell
  us if we should pin or patch around them.

## Supported versions

There is no release, so there is no released version to patch. This project carries no git tags and
publishes nothing to PyPI, which [`README.md`](README.md) and [`CHANGELOG.md`](CHANGELOG.md) both
state. Security fixes land on the `main` branch, and the patch channel today is `git pull`.

Pin to a commit if you need a fixed version, and move the pin forward to take a fix.
[`CHANGELOG.md`](CHANGELOG.md) lists tagging a release and publishing to PyPI as planned work, and
this section changes when that ships.
