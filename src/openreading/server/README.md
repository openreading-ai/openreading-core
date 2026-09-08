# The HTTP server: the same engine over HTTP, with auth

<sub>[Docs home](../README.md) · [← The run ledger](../ledger/README.md) · [The channel contract →](../derive/README.md)</sub>

> **In one sentence.** `openreading serve` puts parse, compare, batch, and async jobs behind a JSON
> API on `127.0.0.1:8787`, with bearer auth one variable turns on.

## What this gives you

You have the CLI working, and now a service in another language or on another machine needs the
same results. You do not want to shell out to a binary from a web handler, and you do not want a
hosted service holding your keys. `openreading serve` is one process, run by you, that accepts the
packaged request schema and returns the response envelope. The process runs where you start it, on
your own machine or in your own network.

An envelope is the one JSON shape every surface returns, so a client over HTTP gets the same shape
the CLI prints. A backend is one parser, such as a local library or a hosted API. For example,
`POST /v1/parse` with a body naming `sample.pdf` and `pymupdf` returns an envelope with the same
fields as the CLI prints.

The [response guide](../schemas/README.md#understanding-the-response-json) explains the document body and its optional content with a reusable Python consumer.
Use it after a successful `POST /v1/parse`, not on an HTTP error body or the outer async job handle.

A few endpoints report readiness and routing plans and never run a backend, among them
`GET /healthz`, `GET /v1/backends`, and `POST /v1/route`. Keys come from the server's environment
and never from a request body. You need `sample.pdf` and the install from the root README, which
includes the server's dependencies.

```bash
uv run openreading serve
```

```text
[serve] listening on http://127.0.0.1:8787. Readiness: GET /healthz
INFO:     Started server process [85094]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

## Mental model

An adapter is the code package that drives one backend, and the server builds a fresh adapter per
request. `POST /v1/parse` blocks until the envelope is ready, which is the simplest way to call it.
`POST /v1/jobs` returns a job handle at once, and you poll `GET /v1/jobs/{id}` until `state` is
`succeeded` or `failed`. A hosted backend can instead call back to `POST /v1/webhooks/{backend_id}`
when the submit request names that URL. `DELETE /v1/jobs/{id}` discards a record at once and
answers 204.

<!-- diagram:src-openreading-server-1 -->
<p align="center"><a href="../../../assets/diagrams/src-openreading-server-1.svg"><img src="../../../assets/diagrams/src-openreading-server-1.svg" alt="The client submits POST /v1/jobs to an openreading serve process it operates. The server submits to a hosted backend and returns a running handle. In poll mode, each GET /v1/jobs/{id} advances work. Alternatively, a signed Reducto webhook records a result; a bad signature returns 401 bad_signature. The client reads a succeeded or failed handle." /></a></p>

<details>
<summary>Logical flow (Mermaid)</summary>

```mermaid
%%{init: {"theme":"base","fontFamily":"Arial","deterministicIds":true,"deterministicIDSeed":"openreading","htmlLabels":false,"themeVariables":{"fontFamily":"Arial","fontSize":"17px","lineColor":"#8194ad","textColor":"#183451","primaryTextColor":"#183451","primaryColor":"#edf3fc","primaryBorderColor":"#9db4d0","edgeLabelBackground":"#ffffff","clusterBkg":"#f5f8fc","clusterBorder":"#d7e1ee","titleColor":"#183451","actorBkg":"#edf3fc","actorBorder":"#9db4d0","actorTextColor":"#183451","actorLineColor":"#9db4d0","signalColor":"#527095","signalTextColor":"#183451","labelBoxBkgColor":"#fff4de","labelBoxBorderColor":"#c6953a","labelTextColor":"#70501b","loopTextColor":"#527095","noteBkgColor":"#edf3fc","noteBorderColor":"#9db4d0","noteTextColor":"#183451","sequenceNumberColor":"#ffffff","activationBkgColor":"#e7f3ee","activationBorderColor":"#679780"},"flowchart":{"curve":"monotoneY","nodeSpacing":32,"rankSpacing":48,"padding":18,"useMaxWidth":true},"sequence":{"useMaxWidth":true,"actorMargin":65,"messageMargin":38,"mirrorActors":false}}}%%
sequenceDiagram
  autonumber
  participant C as client
  participant S as openreading serve
  participant B as hosted backend
  C->>S: POST /v1/jobs
  S->>B: submit
  S-->>C: handle: running
  alt poll mode
    C->>S: GET /v1/jobs/ID
    S->>B: one slice of work per GET
    B-->>S: progress, then the result
  else webhook mode
    B->>S: POST /v1/webhooks/reducto
    alt signature valid
      S->>S: record the result
    else signature bad
      S-->>B: 401 bad_signature
    end
  end
  S-->>C: handle: succeeded or failed
```

</details>

## Walkthrough

Stop the server you started above, because the one you need next binds the same port. Start it
again in a second terminal with `OPENREADING_SERVER_PATH_ROOT="$PWD" uv run openreading serve`.
This walkthrough sends `document.path`. The server refuses a path over HTTP unless it lies under
the directory that variable names (`openreading.server` docstring, "Security"). Setting it to
`$PWD` covers every path below. Every command below ran against that server. Generate the root
README's sample first:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
```

That command prints a `fitz` deprecation warning on stderr. It comes from PyMuPDF, not from this
project.

### 1. Health, readiness, parse

Three calls tell you the server is up, which backends it can run, and what an envelope looks like
over HTTP.

```bash
curl -s localhost:8787/healthz
curl -s localhost:8787/v1/backends | jq -c '.[] | select(.slug=="pymupdf" or .slug=="reducto")'
curl -s -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "pymupdf"}}' > server-pymupdf.json
jq -c '{state: .status.state, backend: .backend.id, pages: .document.page_count, warnings: [.warnings[].code]}' server-pymupdf.json
```

```json
{"status":"ok","version":"0.3.0"}
{"slug":"pymupdf","type":"oss_library","extra_installed":true,"creds_found":[],"creds_missing":[],"ready":true,"liveness_probe":"local"}
{"slug":"reducto","type":"hosted_api","extra_installed":true,"creds_found":[],"creds_missing":["REDUCTO_API_KEY","REDUCTO_WEBHOOK_SECRET"],"ready":false,"liveness_probe":"none"}
{"state":"succeeded","backend":"pymupdf","pages":2,"warnings":["confidence_unavailable"]}
```

**You should see** `ready: true` for `pymupdf`, the missing variables named for `reducto`, and the
envelope the CLI prints. `ready` means the backend is configured, not that it was reached. To send
the file inline instead of by `path`, put it in `document.bytes_base64`. Check:
`jq .schema_version server-pymupdf.json` is `"0.3"`.

### 2. Let the cache replay a routed run

The same request sent twice, with no backend named, shows the result cache answering the second
call.

```bash
for i in 1 2; do curl -s -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": null}}' \
  | jq -c '{backend: .backend.id, warnings: [.warnings[].code]}'; done
```

```json
{"backend":"pymupdf","warnings":["confidence_unavailable"]}
{"backend":"pymupdf","warnings":["confidence_unavailable","idempotent_replay"]}
```

**You should see** `idempotent_replay` on the second call. Routed runs are cached for 15 minutes,
keyed on content plus options, and the cache dies with the process. `POST /v1/route` with the same
body returns the routing plan without running a backend. `"id": "strategy:offline_first"` runs a
preset with no config file. A strategy is a named plan over one or more backends, and a preset is
one built into the package. Your own strategies load only from the file `OPENREADING_CONFIG` names.

### 3. Compare, batch, and an async job

One call each shows compare, batch and an async job against the same sample.

```bash
curl -s -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "tesseract"}}' > server-tesseract.json
jq -n --slurpfile a server-pymupdf.json --slurpfile b server-tesseract.json '{responses: [$a[0], $b[0]]}' \
  | curl -s -X POST localhost:8787/v1/compare -H 'content-type: application/json' -d @- \
  | jq -c '{schema_version, subjects: [.subjects[].label], findings: (.findings | length)}'
mkdir -p docs && printf 'not a pdf' > docs/bad.pdf
curl -s -o server-batch.json -w 'HTTP %{http_code}\n' -X POST localhost:8787/v1/batch -H 'content-type: application/json' \
  -d '{"documents": [{"path": "'"$PWD"'/sample.pdf"}, {"path": "'"$PWD"'/docs/bad.pdf"}], "backend": "pymupdf", "jobs": 2}'
jq -c '{state: .status.state, summary: .summary, items: [.items[] | {relpath: .source.relpath, state, code: .error.code}]}' server-batch.json
curl -s -o job.json -X POST localhost:8787/v1/jobs -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "pymupdf"}}'
curl -s localhost:8787/v1/jobs/$(jq -r .job_id job.json) | jq -c '{job_id, state, response_state: .response.status.state, error}'
```

```json
{"schema_version":"0.2","subjects":["pymupdf","tesseract"],"findings":4}
HTTP 200
{"state":"partial","summary":{"total":2,"succeeded":1,"failed":1,"duration_ms":19.0,"pages_processed":2,"backends":{"pymupdf":1}},"items":[{"relpath":"0","state":"succeeded","code":null},{"relpath":"1","state":"failed","code":"FileDataError"}]}
{"job_id":"omjob_86129928b4744727b9f5a2001ddb265c","state":"succeeded","response_state":"succeeded","error":null}
```

**You should see** three results. The first is a compare report built only from saved envelopes.
More than 50 `responses` in one call returns 400 with no override, the same shape as the batch
caps below. The second is HTTP 200 with `state: partial`, because one failed item never fails
the batch, and items are named by index. The third is a job that already `succeeded`, because a
local backend finishes before the first poll. Each GET advances a poll-mode job by one slice, so
keep polling until `state` is not `running`. An empty `documents: []` returns 200 with an
`empty_batch` warning. `jobs` above 32 or more than 200 documents returns 400 with no override.
Your `duration_ms` and `job_id` differ on every run, and everything else matches.

### 4. Read the error ladder

Every error the server raises deliberately has one shape, so one parser reads all of them. The
shape is `{"error": {"category", "message", "backend_code"?, "missing_env"?, "trail"?}}`. A failed
job's `error` is that same body, fetched with 200. Give your client one branch outside that parser
all the same. An error no handler catches falls through to the web framework, which answers `500`
with the plain text `Internal Server Error`. Each row in the table below was triggered against the
running server, except where the row says otherwise.

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "reducto"}}'
```

```json
{"error":{"category":"terminal","message":"missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai","backend_code":"missing_credentials","missing_env":["REDUCTO_API_KEY"]}}
HTTP 424
```

Source: `src/openreading/server/__init__.py` ("HTTP status codes") and
`src/openreading/server/app.py` (`_error_envelope`, module docstring). Live truth: `uv run python -m
pydoc openreading.server` → "HTTP status codes". If this table and that text disagree, the text is
right, so fix the table.

| Status | `category` | When | Trigger (offline unless noted) |
|---|---|---|---|
| `200` | none | success. Also `GET /v1/jobs/{id}` of a failed job, and every liveness probe result | any step above |
| `204` | none | `DELETE /v1/jobs/{id}` removed the record, whatever its state, with an empty body | `curl -X DELETE localhost:8787/v1/jobs/$(jq -r .job_id job.json)` |
| `400` | `bad_request`, `unknown_strategy` | body not JSON, fails the request schema, unknown `strategy:<name>`, bad `jobs` or `timeout_s`, `/v1/jobs` with no backend named | `"backend": {"id": "strategy:nope"}` |
| `401` | `unauthorized`, `bad_signature` | auth on and no valid bearer, on every endpoint but the two named below. Or a webhook signature is invalid or its secret is unset. Or a `chunkr` / `open-ocr` event arrives without its per-job callback token | `POST /v1/webhooks/reducto` with any body and no `REDUCTO_WEBHOOK_SECRET` |
| `403` | `scope_denied` | an unnamed request has an empty default chain, or token scope excludes every backend the request or strategy can reach | `policy: { backends: [] }`, or a token scoped to a backend the request did not name |
| `404` | `unknown_backend`, `unknown_job` | the id names nothing | `"backend": {"id": "nope"}`, or `GET /v1/jobs/j_nope` |
| `413` | `terminal` (`doc_too_large`) | document over the backend's size limit, OR the request body itself over the transport cap `OPENREADING_MAX_BODY_BYTES` (`_BodyLimitMiddleware`) | the doc-size case needs a hosted key, so the shape is shown and not run. The transport cap needs no key, but a 150 MB default body is impractical to demo here |
| `422` | `unsupported_feature` | the named backend cannot produce what you asked for | `"backend": {"id": "pymupdf"}, "extraction_schema": {"instructions": "totals"}` |
| `424` | `terminal` (`missing_credentials`, `auth_rejected`) | named backend has no key (`missing_env[]`), or the provider rejected it | `"backend": {"id": "reducto"}` with no `REDUCTO_API_KEY` |
| `429` | `rate_limited` | the job store already holds `OPENREADING_MAX_ASYNC_JOBS` records (default 1000), or this key holds `OPENREADING_MAX_JOBS_PER_PRINCIPAL` of them (default 100). It bounds the store, and it is not a per-caller request-rate throttle | needs 1000 submits, so the shape is shown and not run |
| `502` | `plan_exhausted`, `terminal` | every backend in the plan failed (`trail` lists them). Two request-shape refusals also land here rather than at 400. `credentials_ref_alias_not_allowed` means the body's `credentials_ref` named an alias you have not allow-listed. `endpoint_not_request_configurable` means the body set `runtime.endpoint`. Both are permanent, so read `backend_code` before retrying a 502 | `"credentials_ref": "env:OPENREADING_REDUCTO"`, or `"runtime": {"endpoint": "https://example.com"}` |
| `504` | `retryable_exhausted` | deadline passed or retries exhausted | needs a hosted key, so the shape is shown and not run |
| `500` | none. The body is the plain text `Internal Server Error`, not JSON | an error no handler caught | no trigger known today. It is the framework's own fallback, so parse defensively anyway |

`/v1/batch` takes `backend` as one bare string where `/v1/parse` takes an object, which is the
easiest mistake to make when moving between the two endpoints. The refusal names the fix:

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8787/v1/batch -H 'content-type: application/json' \
  -d '{"documents": [{"path": "'"$PWD"'/sample.pdf"}], "backend": {"id": "pymupdf"}}'
```

```json
{"error":{"category":"bad_request","message":"\"backend\" on this endpoint is one string shared by every item, not /v1/parse's object — send \"backend\": \"pymupdf\""}}
HTTP 400
```

**You should see** 400 in the enveloped shape rather than a bare 500. Sending
`"backend": "pymupdf"` answers 200 for the same documents.

Two endpoints answer without a bearer even when auth is on: `GET /healthz` and
`POST /v1/webhooks/{backend_id}`. A vendor holds no bearer token of yours, so a callback could
never present one. With auth on and no bearer, `/healthz` returns 200, and
`POST /v1/webhooks/chunkr -d '{"task_id":"forged-1"}'` returns 404 `unknown_job`. That 404 means the
request reached the handler and looked the job up rather than being challenged for a bearer.

A webhook is closed by a signature or by a per-job token instead. Only `reducto` declares a signing
secret today, so the same unauthenticated call to `POST /v1/webhooks/reducto` returns 401
`bad_signature`, which is the row above. `chunkr` and `open-ocr` offer no signature mechanism at
all. For those two the server mints a token when it submits the job and appends it to the
`webhook_url` it registers with the vendor. An event that cannot present that token answers 401.
`OPENREADING_ALLOW_UNSIGNED_WEBHOOKS=1` turns that check off for a vendor that strips query
parameters from a callback URL, and it accepts forgeable completions in doing so. If you set that
variable, decide your network policy for those two paths before you bind beyond loopback.

The lookup is scoped either way. Only jobs for the URL's own `{backend_id}` that are already
waiting in webhook mode are considered. An event can therefore settle no other backend's job and no
polled job. The full rule is under "Endpoints", `POST /v1/webhooks/{backend_id}`, in
`uv run python -m pydoc openreading.server`.

### 5. Turn on caller auth

Stop the server, mint a token, set it in the environment, and restart. The scope variable lists the
backends that token may reach. A token is never a flag, so it never lands in shell history.

```bash
export TOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
OPENREADING_API_KEYS="$TOK" OPENREADING_API_KEY_SCOPES="$TOK=pymupdf" \
  OPENREADING_SERVER_PATH_ROOT="$PWD" uv run openreading serve
```

```bash
curl -s -w '\nHTTP %{http_code}\n' localhost:8787/v1/backends
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOK" -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "pymupdf"}}'
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8787/v1/parse -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOK" -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "tesseract"}}'
```

```json
{"error":{"category":"unauthorized","message":"missing or invalid API key"}}
HTTP 401
HTTP 200
{"error":{"category":"scope_denied","message":"this API key is not scoped to reach backend 'tesseract'","backend_code":"tesseract"}}
HTTP 403
```

**You should see** 401 without a bearer token, 200 with it, and 403 outside the scope. `/healthz`
and `POST /v1/webhooks/{backend_id}` stay open without a bearer, as the note under the ladder
explains. A malformed entry in either variable stops the server before it binds a socket:

```bash
OPENREADING_API_KEYS="$TOK" OPENREADING_API_KEY_SCOPES="$TOK=pymupdf,=tesseract" \
  uv run openreading serve; echo "exit=$?"
```
```text
[serve] OPENREADING_API_KEY_SCOPES entry 2 has an empty key before '='
exit=3
```

The line goes to stderr under the `[serve]` tag every other CLI failure uses, so one log rule
catches it. It names the entry's position and never its value, which keeps a startup log from
becoming the place a token leaks. A second scope for one key and a scope for a key that
`OPENREADING_API_KEYS` never listed are refused the same way.

A token listed in `OPENREADING_API_KEYS` but absent from `OPENREADING_API_KEY_SCOPES` is
unscoped, meaning it reaches every backend. That is deliberate. A second key added for a colleague
would otherwise reach nothing. It is also how you mint an unrestricted token by accident. Give
every token you add its own scope entry unless you mean it to reach everything.

Startup checks the shape of a scope and not the backend ids inside it. A typo such as `pymupfd`
binds the socket with no warning and leaves that token able to reach nothing. Every request under
it then answers 403 naming a backend you believe you allowed. Check each id against the `slug`
values `GET /v1/backends` returns.

## Recipes

**Receive a vendor webhook instead of polling.** (needs a hosted key, so the shape is shown and
not run)
```json
{"document": {"url": "https://example.com/loan.pdf"}, "backend": {"id": "reducto"},
 "async": {"mode": "async", "webhook_url": "https://your-host/v1/webhooks/reducto"}}
```
Reducto signs the callback. The server verifies it with `REDUCTO_WEBHOOK_SECRET` and answers 401
when the secret is unset. You always supply the callback URL.

**Expose the server beyond localhost.** A `--host` outside loopback warns on stderr before the
socket is claimed. The three loopback spellings, `127.0.0.1`, `localhost` and `::1`, bind without
that warning:
```bash
uv run openreading serve --host 0.0.0.0
```
```text
[serve] warning: binding 0.0.0.0 exposes the server. Anyone who can reach it spends your vendor keys. Put it behind your own auth or proxy.
[serve] listening on http://0.0.0.0:8787. Readiness: GET /healthz
…
```
`0.0.0.0` reaches every interface the machine has. Set `OPENREADING_API_KEYS` before you bind
anywhere but the default, and terminate TLS in front of it.

**Probe whether a backend answers.**
`curl -s -X POST localhost:8787/v1/backends/pymupdf/liveness -d '{}'` returns
`"status":"live","measured":true`. The probe always returns 200, because a backend being down is a
successful diagnostic. `measured: false` means the status was inferred from configuration, with no
round trip.

**Smoke-test the whole stack.** `make serve-smoke` boots a server on a free port, posts the
sample through `/v1/parse` and `/v1/batch`, asserts schema-valid responses, and shuts down.

## How it decides

- Auth is off by default and the server binds to loopback only. This avoids a default token nobody
  rotates and an open port that spends your credits. `openreading.server.app` enforces it.
- `document.path` is refused by default, whether or not caller auth is configured at all. This
  avoids a caller turning a JSON field naming a file into a way to read anything the server
  process can open. `OPENREADING_SERVER_PATH_ROOT` is the explicit opt-in. Containment is proved
  on the resolved path, so a symlink pointing outside that directory cannot escape it. The file is
  read at the gate, so no backend re-opens a path that could have been swapped meanwhile.
  `openreading.server.app._gate_document_path` enforces it.
- Tokens and backend credentials come from the environment only, never a body or a flag.
  Nothing lands in `ps` or shell history, and no caller can attest on the operator's behalf.
- A scope only narrows what the deployment's own `policy.backends` already resolves to. A directly
  named backend is checked at the door, before any adapter is built. An out-of-scope name therefore
  never resolves a vendor credential. This is the one boundary a request cannot argue with: naming
  a backend on the command line runs it, naming one through a scoped token does not.
- A request naming no backend runs on the resolved chain pruned to the token's backends. That chain
  is the backend the router picks plus every backend it would fall back to. An out-of-scope member
  is removed before the run rather than reached. A token scoped to `pymupdf` and `tesseract` is
  refused `docling` by name. Its unnamed request on a file neither can parse fails with a trail
  naming those two backends alone.
- A request whose top pick alone is out of scope is rerouted, not refused. The check reads
  the whole fallback chain rather than the first pick alone. A caller is therefore never refused
  over a choice it never made, and no fallback goes unchecked. A token scoped to `tesseract` is
  still refused `pymupdf` by name, and the same token's unnamed request answers 200 on `tesseract`.
- A `strategy:<name>` request is checked too, from inside the walk, because a walk chooses its own
  backends and cannot be judged at the door. Every out-of-scope rung is pruned before it runs. A
  token scoped to `pymupdf` that names
  `strategy:offline_first` therefore runs `pymupdf` alone. The response's `orchestration.dropped`
  lists `docling` and `tesseract` with code `scope_denied` at stage 0.
- Pruning a walk or a chain down to nothing is a refusal, never a 502 and never a silent run on
  nothing. A token scoped to `reducto` that names `strategy:offline_first` answers 403. Its message
  is `this API key is not scoped to reach any backend strategy 'offline_first' can run (denied:
  docling, pymupdf, tesseract)`. The same token sending an unnamed request under a deployment
  whose list starts with `pymupdf` answers 403 with `this API key is not scoped to reach backend
  'pymupdf'`. That message names the backend the router would have used rather than the ones the
  token allows.
- Only a strategy records what the scope removed. A pruned chain leaves no
  `orchestration.dropped` block on the envelope. You see what survived, in `backend.id` on success
  or in a 502 `trail`, but never a list of what was pruned.
- `/v1/parse`, `/v1/batch` and `/v1/compare` responses are schema-validated before they leave the
  process. The server owns the only result cache, so a replayed item never hides a real call.

## Operations

This section is for whoever runs the process and watches it. It covers where the log lines go, what
a restart does to work already in flight, and what there is to measure.

### Read the logs

The server writes to both streams and splits them by kind, which a log-shipping config has to
account for. Start it with the streams apart and drive one request through:

```bash
OPENREADING_SERVER_PATH_ROOT="$PWD" uv run openreading serve --port 8901 > access.log 2> lifecycle.log &
sleep 3
curl -s -o /dev/null -X POST localhost:8901/v1/parse -H 'content-type: application/json' \
  -d '{"document": {"path": "'"$PWD"'/sample.pdf"}, "backend": {"id": "pymupdf"}}'
sleep 1; cat access.log; echo '--- stderr ---'; cat lifecycle.log
```

```text
Consider using the pymupdf_layout package for a greatly improved page layout analysis.
INFO:     127.0.0.1:58618 - "POST /v1/parse HTTP/1.1" 200 OK
--- stderr ---
[serve] listening on http://127.0.0.1:8901. Readiness: GET /healthz
INFO:     Started server process [85067]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

**You should see** one access line per request on stdout and the whole lifecycle on stderr. Three
properties follow. A backend's own chatter lands in the stdout access log between access lines.
That stream is not uniform, and a strict parser fails on the advisory line. There is no request id
and no timestamp on an access line, so a slow request cannot be traced back to a caller. There is
no log-level knob and no structured output, so filtering happens in your shipper rather than here.

The `[serve] listening on …` line prints only once the port is genuinely claimed, and it names the
readiness check for the same reason. A port already in use produces one tagged line and exit 3
rather than a healthy-looking startup followed by a silent death:

```bash
uv run openreading serve --port 8901; echo "exit=$?"
```

```text
[serve] cannot bind 127.0.0.1:8901: Address already in use. Free it or pass a different --port.
exit=3
```

Gate readiness on `GET /healthz` answering 200 all the same. No log line is a readiness contract,
and the uvicorn lines below it are printed by the library rather than by this process.

### Restart, and what it costs a client

The job store lives in the process, so a restart erases it. A job id that answered `succeeded` a
moment ago answers 404 afterwards, with the same category a typo gets:

```bash
curl -s localhost:8901/v1/jobs/omjob_bad0f7e7accf4b528a55c88496545946
```

```json
{"error":{"category":"unknown_job","message":"omjob_bad0f7e7accf4b528a55c88496545946"}}
```

**You should see** `unknown_job` for both a dropped job and an id that never existed. A client
cannot tell "the finished result was lost, resubmit" from "that id never existed, do not retry",
and no field distinguishes them today. Treat 404 on an id your own code minted as a resubmit, and
reserve the do-not-retry reading for an id you did not mint.

That matters more than it looks, because each `GET /v1/jobs/{id}` is what advances a poll-mode job
by one slice. Nothing progresses the job in the background. A rolling restart in the middle of a
poll therefore strands work a vendor has already accepted and will still bill. No record of it
survives the process.

### Measure what you can

There is no metrics or tracing surface here, and the "Not built yet" list says so. Three things are
worth collecting instead. The stdout access log gives request counts and status codes. `uv run
openreading backends --check <slug>` measures whether a backend answers and belongs on a schedule
as a vendor-degradation canary ([Routing and keys](../router/README.md)). Each envelope carries
`usage`, which reports what the backend consumed in the unit it meters in: `pages_processed`,
`credits`, `input_tokens`/`output_tokens`, `duration_ms`. There is no spend figure. A dollar total
needed a per-vendor rate this package could not verify, so `cost_usd` and `cost_basis` are gone.
Join these counters to your provider invoice instead. A counter a backend did not report is absent
rather than zero, so read every field with a default.

### Load and time budgets

One `openreading serve` is one uvicorn process, and it exposes no worker count and no queue depth.
Concurrency inside a request is bounded per backend, which [Batch runs](../batch/README.md#sizing-a-large-run)
measures along with the memory a corpus costs. A request over HTTP is capped at the generic 120
second budget, with no field to raise it. A long hosted document therefore answers 504 here and
needs the CLI or the Python API instead. Nothing rate-limits callers and nothing caps spend, so put
your own proxy in front before more than one client can reach the port.

## Reference

- `uv run python -m pydoc openreading.server` has the sections "Run", "Endpoints", "HTTP status
  codes", and "Security". The Timeouts paragraph sits inside "HTTP status codes". `uv run python -m
  pydoc openreading.server.app` lists every environment variable.
- `uv run openreading serve --help` documents `--host`, `--port`, `--cors-origin`, and `--env-file`.
  Its epilog explains `OPENREADING_API_KEYS` and `OPENREADING_API_KEY_SCOPES`, the two variables
  that turn caller auth on. Step 5 above shows them in a worked session.
- [JSON Schemas](../schemas/README.md), and the OpenAPI page at `/docs`. It answers 200 while auth
  is off. Once step 5 turns auth on, `/docs` and `/openapi.json` both answer 401 without a bearer.
  A browser tab cannot open them, and a client must send the header itself.

## Not built yet

- Rate limiting, spend accounting, and TLS are not provided here by design. Put your own reverse
  proxy in front (`openreading.server` docstring, "Security": "What this does NOT add: rate
  limiting, spend accounting, transport encryption").
- A metrics or tracing surface. There is no `/metrics`, no counters, no request id, and no trace
  hook. With auth on, an unknown path answers 401 rather than 404, so a probe cannot tell "not
  implemented" from "wrong token". [Measure what you can](#measure-what-you-can) names what to
  collect instead.
- A durable job store and server-side resume. The `openreading.server` docstring says under
  "Endpoints", `POST /v1/jobs`: "there is no server-side resume". The `openreading.ledger`
  docstring says under "The substrate contract": "The `/v1/jobs` store stays in-memory,
  per-process". [Restart, and what it costs a client](#restart-and-what-it-costs-a-client) shows
  what a client sees when that store goes away.
- A `deadline_ms` field over HTTP, so a long hosted job hits 504 at 120 s. The `openreading.server`
  docstring covers it under "HTTP status codes", in the Timeouts paragraph: "no `deadline_ms` field
  or query param".
- Webhook *signature* verification for `chunkr` and `open-ocr`, because neither vendor offers a
  signing mechanism. Their callbacks are authenticated by the per-job token described under the
  error ladder above.

## See also

- [Docs home](../README.md)
- [The command line](../cli/README.md) has the same surfaces as verbs and exit codes.
- [Routing and keys](../router/README.md) explains the three selection rules and the 403.
- [The run ledger](../ledger/README.md) covers resume, which the server does not offer.
- [Backend adapters](../adapters/README.md) lists which backend needs which variable.

<sub>[Docs home](../README.md) · [← The run ledger](../ledger/README.md) · [The channel contract →](../derive/README.md)</sub>
