# Backend adapters: what each backend reads, needs, and promises

<sub>[Docs home](../README.md) · [← JSON Schemas](../schemas/README.md)</sub>

> **In one sentence.** These tables are what every backend declares about itself: its formats, its
> variables, its price and its compliance posture.

You want to know which backend can read your file, what key it needs, and whether your policy
allows it. This page answers all three from the descriptor each backend ships. A backend is one
parser, such as the local `pymupdf` library or a hosted API. An adapter is the package that wraps
one backend. Its descriptor is the static record in which the backend declares the formats it
reads, the environment variables it needs and its compliance posture.

The five tables under [Catalog](#catalog) cover what each backend reads, needs, promises, charges,
and can put in a response. You need the package installed with `uv sync --all-extras --dev`, and a
key for any hosted backend you want to call. Local backends such as `pymupdf` and `tesseract` need
no key.

## What this is

One command tells you which backends are ready on this machine. Each backend lives in its own
package, and `BUILTIN_ADAPTERS` in `registry.py` is the only registration point. Run
`uv run openreading backends` to see readiness. "Configured" means the backend's install extra
imports and its declared environment variables resolve. An install extra is a named optional
dependency group in `pyproject.toml`, so a backend's client library installs only when you ask for
it. "Configured" never means the vendor accepted the key.
`uv run openreading backends --check <id>` measures that by calling the vendor, and a vendor with
no free liveness call reports `configured_unverified` instead of spending your money. After
`uv sync --all-extras --dev`, with no keys set, you should see this output.

```
BACKEND                        TYPE               CONFIGURED  MISSING
anthropic-claude               hosted_api         no          ANTHROPIC_API_KEY, ANTHROPIC_MODEL
aws-textract                   hosted_api         no          AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_REGION, OPENREADING_TEXTRACT_S3_BUCKET
azure-document-intelligence    hosted_api         no          AZURE_DOCUMENT_INTELLIGENCE_KEY, AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
chunkr                         hosted_api         no          CHUNKR_API_KEY
docling                        oss_library        no          DOCLING_SERVE_URL
google-document-ai             hosted_api         no          GCP_PROJECT_ID, GCP_PROCESSOR_ID
google-gemini                  hosted_api         no          GEMINI_API_KEY
mistral-ocr                    hosted_api         no          MISTRAL_API_KEY
nuextract                      hosted_api         no          NUEXTRACT_API_KEY
open-ocr                       hosted_api         no          OPENOCR_API_KEY
pulse                          hosted_api         no          PULSE_API_KEY
pymupdf                        oss_library        yes         -
qwen-vl                        self_hosted_model  no          QWEN_VL_ENDPOINT
reducto                        hosted_api         no          REDUCTO_API_KEY
tesseract                      oss_library        yes         -
```

The two commands below run against `sample.pdf`, the generated document every guide uses. Build it
first.

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_sample_pdf; open("sample.pdf","wb").write(build_sample_pdf())'
```

Using a backend without its key gives exit code 3, not a crash. `uv run openreading parse sample.pdf
--backend reducto` prints this message.

```
[reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
```

Every adapter is tested offline against recorded vendor responses and injected faults, and that
offline suite is what the CI badge covers. Real vendor calls run only in a manual, key-gated lane
(`make verify-live`), so nothing here proves a hosted backend answered today. Measure that yourself
with `uv run openreading backends --check <id>`, which probes the vendor.

Every adapter passes a conformance kit before it ships, which checks bbox geometry, channel
honesty, cost shape and determinism. Channel honesty means a channel graded `N`, `D` or `X` in
[the fifth table](#what-each-backend-can-put-in-a-response) behaves that way.

## Catalog

A table cell reading none means the descriptor sets no value for that field, which is different
from a value of zero. A required env var has `required: true` in the backend's descriptor (its
`AdapterDescriptor`). A backend with no required var uses its SDK's own credential chain when
nothing is set.

Source: `src/openreading/adapters/registry.py` (the `BUILTIN_ADAPTERS` mapping) and each
`make_adapter(id).descriptor` (`credentials_spec`, `config_spec`, `compliance`, `runtime.license`,
`signup_url`). The install extra is the `pyproject.toml` extra of the same name. The one exception
is `aws-textract`, whose extra is named `textract`. `scripts/check_extras_parity.py` holds it in
`EXTRA_NAME_EXCEPTIONS`. Live truth: `uv run
openreading backends` (readiness) and `uv run python -c "from openreading.adapters.registry import
make_adapter; print(make_adapter('reducto').descriptor.to_schema_dict())"` (every field). If a
table and that output disagree, the output is right and the table needs fixing.

### What each backend reads

The first table gives the formats each backend reads. Every backend declares them in
`capabilities.input_formats` of its descriptor. When a request names the file's MIME type, the
router drops a backend whose list does not contain that format, with the drop code
`unsupported_format`. A batch run skips such a file with the same reason. A format marked
`(rasterized)` means the backend turns each page into an image before it reads it.

Source: `Capabilities.input_formats` in `src/openreading/types/descriptor.py`, read
through `make_adapter(id).descriptor.capabilities.input_formats`. Live truth: `uv run python -c
"from openreading.adapters.registry import make_adapter, BUILTIN_ADAPTERS; [print(i,
make_adapter(i).descriptor.capabilities.input_formats) for i in BUILTIN_ADAPTERS]"`. If the table
and that output disagree, the output is right and the table needs fixing.

| id | input formats |
|---|---|
| `anthropic-claude` | pdf, png, jpg |
| `aws-textract` | pdf, png, jpg, tiff |
| `azure-document-intelligence` | pdf, png, jpg, tiff, bmp, docx, xlsx, pptx, html |
| `chunkr` | pdf, docx, pptx, xlsx, png, jpg, tiff, webp, html |
| `docling` | pdf, docx, pptx, xlsx, html, png, jpg |
| `google-document-ai` | pdf, tiff, gif, png, jpg, bmp, webp |
| `google-gemini` | pdf |
| `mistral-ocr` | pdf, docx, pptx, png, jpg, jpeg, avif |
| `nuextract` | pdf, png, jpg, pptx, odt, txt |
| `open-ocr` | pdf, png, jpg, gif, webp, tiff, bmp |
| `pulse` | pdf, docx, pptx, xlsx, png, jpg |
| `pymupdf` | pdf, xps, epub, mobi, cbz, svg |
| `qwen-vl` | png, jpg, pdf (rasterized) |
| `reducto` | pdf, png, jpg, docx, xlsx, pptx |
| `tesseract` | png, jpg, tiff, bmp, pdf (rasterized) |

### What each backend needs to run

The second table gives each backend's install extra and environment variables. In the env columns,
none means the backend declares no variable of that kind.

| id | type | install extra | required env | optional env |
|---|---|---|---|---|
| `anthropic-claude` | hosted_api | `anthropic-claude` | none | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| `aws-textract` | hosted_api | `textract` | none | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` or `AWS_DEFAULT_REGION`, `OPENREADING_TEXTRACT_S3_BUCKET` |
| `azure-document-intelligence` | hosted_api | `azure-document-intelligence` | `AZURE_DOCUMENT_INTELLIGENCE_KEY`, `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | none |
| `chunkr` | hosted_api | `chunkr` | `CHUNKR_API_KEY` | `CHUNKR_BASE_URL` |
| `docling` | oss_library | `docling` | `DOCLING_SERVE_URL` | none |
| `google-document-ai` | hosted_api | `google-document-ai` | `GCP_PROJECT_ID` or `GOOGLE_CLOUD_PROJECT`, `GCP_PROCESSOR_ID` | `GOOGLE_APPLICATION_CREDENTIALS`, `GCP_LOCATION` |
| `google-gemini` | hosted_api | `google-gemini` | `GEMINI_API_KEY` | `GEMINI_MODEL` |
| `mistral-ocr` | hosted_api | `mistral-ocr` | `MISTRAL_API_KEY` | `MISTRAL_OCR_MODEL` |
| `nuextract` | hosted_api | `nuextract` | `NUEXTRACT_API_KEY` or `NUMIND_API_KEY` | `NUEXTRACT_BASE_URL` |
| `open-ocr` | hosted_api | `open-ocr` | `OPENOCR_API_KEY` | `OPENOCR_ENGINE` |
| `pulse` | hosted_api | `pulse` | `PULSE_API_KEY` | none |
| `pymupdf` | oss_library | `pymupdf` | none | none |
| `qwen-vl` | self_hosted_model | `qwen-vl` | `QWEN_VL_ENDPOINT` | `QWEN_VL_API_KEY`, `QWEN_VL_MODEL` |
| `reducto` | hosted_api | `reducto` | `REDUCTO_API_KEY` | `REDUCTO_WEBHOOK_SECRET` |
| `tesseract` | oss_library | `tesseract` | none (needs the system `tesseract` binary) | none |

Two rows read `none` under required env and still fail without a key. `anthropic-claude` and
`aws-textract` resolve credentials through their vendor SDK's own chain, so the descriptor marks
those fields optional. Run either with nothing set and the command exits 3 with a message naming
the variable.

### What each backend promises about your data

The third table gives each backend's compliance posture, license and signup page. A backend's
compliance posture is the set of claims its descriptor makes about data handling. A BAA (Business
Associate Agreement) is the contract a vendor signs under HIPAA before it may handle protected
health information. The `hipaa_baa` column records whether the vendor offers one, never whether
you signed one. A `require_baa` policy therefore admits a backend on the vendor's published offer,
which is necessary and not sufficient. Confirm your own executed agreement out of band before you
send regulated data to a row reading `yes` or `tier_gated`. The license column quotes each
descriptor's `runtime.license` string as written. In the signup column, none means the backend
runs locally and has nothing to sign up for.

| id | `hipaa_baa` | `trains_on_customer_data` | `runs_fully_local` | license | signup |
|---|---|---|---|---|---|
| `anthropic-claude` | yes | no | false | proprietary | https://console.anthropic.com |
| `aws-textract` | yes | opt_out | false | proprietary | https://aws.amazon.com/textract/ |
| `azure-document-intelligence` | yes | no | false | proprietary | https://azure.microsoft.com/products/ai-services/ai-document-intelligence |
| `chunkr` | tier_gated | opt_out | false | proprietary (AGPL-3.0 self-host available) | https://chunkr.ai |
| `docling` | na_local | na_local | true | MIT | none |
| `google-document-ai` | yes | no | false | proprietary | https://cloud.google.com/document-ai |
| `google-gemini` | no | unverified | false | proprietary | https://aistudio.google.com/apikey |
| `mistral-ocr` | no | unverified | false | proprietary | https://console.mistral.ai/api-keys |
| `nuextract` | no | unverified | false | proprietary (open-weight NuExtract 2.0 [MIT 2B/8B] is self-hostable via vLLM, but its wire protocol differs and would need a separate adapter) | https://nuextract.ai |
| `open-ocr` | no | unverified | false | proprietary | https://open-ocr.com |
| `pulse` | tier_gated | unverified | false | proprietary | https://www.runpulse.com |
| `pymupdf` | na_local | na_local | true | AGPL-3.0 | none |
| `qwen-vl` | na_local | na_local | true | Apache-2.0 (Qwen3-VL; Qwen2.5-VL per-size) | none |
| `reducto` | tier_gated | no | false | proprietary | https://platform.reducto.ai |
| `tesseract` | na_local | na_local | true | Apache-2.0 | none |

### Where those compliance claims come from

The compliance cells in the third table are vendor claims, and each descriptor records the pages a
maintainer read. Each descriptor carries a `sources` list whose entries are `{url, accessed,
supports}`. `accessed` is the day a maintainer read the page, and `supports` names what that page
established. Sources are recorded per descriptor, not per compliance field, so a cell can have
no source that speaks to it. Six `hipaa_baa` cells cite no source for that claim today. Two of them
read `tier_gated` (`chunkr` and `pulse`) and four read `no` (`google-gemini`, `mistral-ocr`,
`nuextract` and `open-ocr`). Filter a backend's sources to see what they say about a BAA and when
they were read.

```bash
uv run python -c "
from openreading.adapters.registry import make_adapter
for s in make_adapter('azure-document-intelligence').descriptor.to_schema_dict()['sources']:
    print(s['accessed'], s['url'], s['supports'])
" | grep -iE "baa|hipaa"
```

```text
2026-07-21 https://learn.microsoft.com/azure/ai-services/document-intelligence/ AnalyzeResult shape, LRO, pricing, HIPAA BAA
```

Change the id for any other backend, and keep the filter so the output stays on the BAA claim.
Some descriptors carry further citations that this filter hides, and every source URL among them is
public. A `supports` line may open with a review id such as `BL-166`, which
[`AGENTS.md`](../../../AGENTS.md) explains. Vendor terms move after the date in that column, and
nothing here re-reads them on a schedule. The table is where your own verification starts rather
than where it ends. A cell that no longer matches its source is a reportable defect under
[`SECURITY.md`](../../../SECURITY.md), which names a lying descriptor field as a compliance-filter
bypass.

Descriptors record four more compliance facts that no policy key gates: `soc2`, `gdpr`, `pci` and
`phi_path_constraints`. A fifth field, `data_retention`, is read by nothing either. It restates in
prose what the enforced `max_retention_hours` holds as a number, so it is not one of the four. Print
them with `make_adapter(id).descriptor.to_schema_dict()['compliance']` and use them for your own
reporting, not for routing. The router guide lists that gap under
[Not built yet](../router/README.md#not-built-yet).

### What each backend charges, and the ceilings on one request

Price is the widest difference between these backends, so settle it before you tune anything else.
The fourth table gives each backend's published rate and the limits it puts on one request. A
page-equivalent is the common unit this project uses to compare vendors that bill in different
things. A vendor that charges per credit or per token declares what its rate works out to for a
single page. That declaration is a range, and `usd_per_page_equiv_low` and
`usd_per_page_equiv_high` are its two ends. Both numbers are per page and never per document, so a
twelve-page document costs twelve times the rate in this table.

The `basis` column says how far to trust that pair. `billed` means the response carries the charge
the vendor made. `estimated` means the adapter projects the rate from a published price
list. `infra_only` means the backend runs on hardware you already pay for, so the response omits
`cost_usd` rather than inventing a number. `unknown` means the vendor publishes no rate at all, and
the adapter declines to guess one.

The three limit columns say how much work one request may carry. Max pages per request is the page
count the vendor accepts, quoted from the descriptor as free text. Nothing in the router or the
batch runner reads it, so check it yourself before you send a long document. The batch concurrency
cap is the ceiling that `--jobs` is reduced to for that backend, which the batch guide demonstrates
in
[walkthrough step 2](../batch/README.md#2-a-single-file-a-glob-several-files---jobs-the-size-guard-a-strategy).
Native batch max items is how many documents the vendor's own bulk endpoint accepts in one job.

| id | low $/page-equiv | high $/page-equiv | `basis` | max pages per request | batch concurrency cap | native batch max items |
|---|---|---|---|---|---|---|
| `anthropic-claude` | 0.01 | 0.08 | estimated | 100 (<1M ctx) / 600 (1M ctx) | none | 100000 |
| `aws-textract` | 0.0015 | 0.07 | estimated | 1 sync / 3000 async | none | none |
| `azure-document-intelligence` | 0.0006 | 0.03 | estimated | 2000 | none | none |
| `chunkr` | 0.008 | 0.03 | estimated | 2000 (soft) | none | none |
| `docling` | 0.0 | none | infra_only | none | none | none |
| `google-document-ai` | 0.0006 | 0.03 | estimated | 15 sync / 500 batch | none | none |
| `google-gemini` | none | none | unknown | 1000 | none | none |
| `mistral-ocr` | 0.004 | 0.005 | estimated | none | none | none |
| `nuextract` | none | none | unknown | none | none | none |
| `open-ocr` | 0.0005 | none | billed | engine-dependent: 200 (tesseract) / 5-20 (vision LLMs) | none | none |
| `pulse` | 0.015 | 0.02 | estimated | none | none | none |
| `pymupdf` | 0.0 | none | infra_only | unbounded | none | none |
| `qwen-vl` | 0.0 | none | infra_only | none | none | none |
| `reducto` | 0.015 | 0.06 | billed | unbounded (async) | none | none |
| `tesseract` | 0.0 | none | infra_only | none | 4 | none |

Source: `Cost`, `Capabilities.max_pages_per_request` and `BatchSupport` in
`src/openreading/types/descriptor.py`, read through `make_adapter(id).descriptor`. Live truth: `uv
run python -c "from openreading.adapters.registry import make_adapter, BUILTIN_ADAPTERS; [print(i,
make_adapter(i).descriptor.to_schema_dict()['cost']) for i in BUILTIN_ADAPTERS]"`, with
`.get('batch')` and `['capabilities'].get('max_pages_per_request')` for the other columns, because
`to_schema_dict()` drops a field the descriptor leaves unset. If the table and that output
disagree, the output is right and the table needs fixing.

A corpus turns that spread into a decision. Two hundred thousand documents averaging twelve pages
is 2.4 million page-equivalents. That corpus bills $1,200 at `open-ocr`'s low end and $24,000 at
`anthropic-claude`'s. Comparing published floors, the factor is 20. At `anthropic-claude`'s high
end the same corpus bills $192,000, so the full published spread is a factor of 160. Every figure
here is a published rate rather than a quote you negotiated. Treat the low and high columns as a
range, not as a price. Every cost estimate elsewhere in this project is built from these two
columns. The `cost/doc` figure that `openreading leaderboard` prints is one of them, and it
multiplies the low end alone by an assumed page count. A corpus that size runs in shards rather
than one invocation. A single run holds every response in memory and has no resume of its own.
[Sizing a large run](../batch/README.md#sizing-a-large-run) gives the ceiling and the shard size.

### What each backend can put in a response

Check that a backend can fill the part of the response you plan to read. A backend that cannot will
omit the field rather than invent one. A channel is one part of a response, such as
`text`, `table_cells` or per-block confidence. Each descriptor grades every channel with one of
three letters. `N` means the backend emits that channel itself. `D` means this project computes it
deterministically from what the backend does emit. `X` means there is no faithful way to produce
it, so the channel is left out. An `X` channel is always missing from the response's
`channel_provenance` map, and only sometimes named in `warnings[]`. `pymupdf` warns about its
missing confidence on every run, while `tesseract` says nothing at all about its missing
`table_cells`. So read this table against `channel_provenance` rather than waiting for a warning
that may never come. [The channel
contract](../derive/README.md#which-signal-to-trust-when-a-channel-is-missing) explains the rules
that grading enforces, C1 to C11.

| id | `text` | `markdown` | `blocks` | `block_bbox` | `block_confidence` | `table_cells` | `typed_fields` |
|---|---|---|---|---|---|---|---|
| `anthropic-claude` | `D` | `N` | `D` | `X` | `X` | `D` | `D` |
| `aws-textract` | `D` | `D` | `N` | `N` | `N` | `N` | `N` |
| `azure-document-intelligence` | `N` | `N` | `N` | `N` | `D` | `N` | `N` |
| `chunkr` | `D` | `N` | `N` | `N` | `N` | `D` | `N` |
| `docling` | `N` | `N` | `N` | `N` | `X` | `N` | `D` |
| `google-document-ai` | `N` | `D` | `N` | `N` | `N` | `N` | `N` |
| `google-gemini` | `D` | `N` | `D` | `X` | `X` | `D` | `N` |
| `mistral-ocr` | `D` | `N` | `N` | `N` | `N` | `D` | `N` |
| `nuextract` | `D` | `N` | `D` | `X` | `X` | `D` | `N` |
| `open-ocr` | `N` | `D` | `X` | `X` | `X` | `X` | `X` |
| `pulse` | `D` | `N` | `N` | `N` | `X` | `D` | `D` |
| `pymupdf` | `N` | `D` | `N` | `N` | `X` | `N` | `X` |
| `qwen-vl` | `D` | `D` | `D` | `D` | `X` | `D` | `D` |
| `reducto` | `D` | `N` | `N` | `N` | `D` | `N` | `N` |
| `tesseract` | `N` | `D` | `N` | `N` | `N` | `X` | `X` |

Source: `OutputChannels` in `src/openreading/types/descriptor.py`, read through
`make_adapter(id).descriptor.output.channels`. Live truth: `uv run python -c "from
openreading.adapters.registry import make_adapter, BUILTIN_ADAPTERS; [print(i,
make_adapter(i).descriptor.to_schema_dict()['output']['channels']) for i in BUILTIN_ADAPTERS]"`. If
the table and that output disagree, the output is right and the table needs fixing.

## Reading the compliance columns

These rules decide whether your policy admits a backend. A policy is the `policy:` block of your
`openreading.yaml`, a short list of compliance requirements the router enforces before it picks a
backend. The rules were checked with `printf 'version: 1\npolicy:\n  require_baa: true\n

- `hipaa_baa: tier_gated` is dropped under `require_baa` unless the id is in `baa_tier_confirmed`
  (drop code `no_baa`). `no` is always dropped.
- `trains_on_customer_data: opt_out` is dropped under `no_train_on_data` unless the id is in
  `train_optout_confirmed` (drop code `trains_on_data`).
- `unverified` is dropped unless your policy sets `allow_unverified_compliance`. The default is to
  drop, so a vendor that said nothing is treated as a no rather than assumed safe.
- `na_local` backends pass every column.
- Your policy sets the eligible set, and three of its keys widen it deliberately. Nothing after the
  policy widens it again, so no request, strategy, fallback or resume can readmit a dropped
  backend. The three keys are the ones in the bullets above. The router guide's
  [How it decides](../router/README.md#how-it-decides) tabulates every drop code alongside them.

## Override form

Set `OPENREADING_<SLUG>_<KEY>` and it wins over the vendor's own variable name. `<SLUG>` is the id
in upper case with `-` replaced by `_`, and `<KEY>` is the descriptor's spec key in upper case. For
example, `OPENREADING_REDUCTO_API_KEY` beats `REDUCTO_API_KEY`. Source: `credentials.py`
(`_slug_env`). The precedence rules are in `uv run python -m pydoc openreading.credentials`.

## Not built yet

- Nothing reads `capabilities.max_pages_per_request`. A document over the vendor's ceiling fails at
  the vendor rather than at the router's stage 2. Reproduce it with
  `grep -rn max_pages_per_request src/openreading/router src/openreading/batch`, which prints
  nothing.
- `framework_loader` is one of the four backend types the schemas allow, and no adapter declares
  it. A wrapper around a framework's document loaders would be the first. Reproduce it with
  `uv run openreading backends`, which prints only `hosted_api`, `oss_library` and
  `self_hosted_model`.

## Maintenance

This section is the bookkeeping a backend owes this page, and it is not the work of building one.
Once the adapter itself works, register it in `BUILTIN_ADAPTERS`. Add one row to each of the five
tables here from its descriptor. Add a block to `.env.example`. Add the extra to `pyproject.toml`.
`scripts/check_extras_parity.py` fails until the extra exists. It also fails until a slug that
differs from its extra name is in `EXTRA_NAME_EXCEPTIONS`. A changed compliance value, format, env
var, rate, limit or channel grade is one cell here. When a "Not built yet" line stops being true,
delete it. The full table of what to update for each kind of change is under *Where a change gets
documented* in [`AGENTS.md`](../../../AGENTS.md).

## See also

- [Docs home](../README.md)
- [`.env.example`](../../../.env.example) lists every var with its signup URL.
- `uv run python -m pydoc openreading.credentials` prints the precedence and the `.env` rules.
- [Routing and keys](../router/README.md#how-it-decides) tabulates every drop code with the policy
  key that triggers it. `uv run python -m pydoc openreading.router.compliance` prints the stage-1
  gate itself, meaning the constraints it reads and the attestations it honours.
- `uv run python -m pydoc openreading.adapters` prints the runbook for adding a backend, and
  [`scripts/new_adapter.py`](../../../scripts/new_adapter.py) scaffolds it.
- [JSON Schemas](../schemas/README.md) describes the response every backend returns.

<sub>[Docs home](../README.md) · [← JSON Schemas](../schemas/README.md)</sub>
