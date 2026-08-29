# Backend adapters

<sub>[Docs home](../README.md) · [← JSON Schemas](../schemas/README.md)</sub>

You want to know which backend can read your file, what key it needs, and whether your policy
allows it. This page answers all three from the descriptor each backend ships. A backend is one
document-processing engine behind OpenReading, for example a hosted API such as Reducto or a
local library such as PyMuPDF. An adapter is the package that wraps one backend. Its descriptor
is the static record in which the backend declares the formats it reads, the environment
variables it needs and its compliance posture.

You get three tables. The first lists the formats each backend reads. The second lists the
install extra and the environment variables each backend needs. The third lists each backend's
compliance posture, license and signup page. You also get the command that shows which backends
are ready on this machine. You need the package installed with `uv sync --all-extras --dev`, and
a key for any hosted backend you want to call. Local backends such as `pymupdf` and `tesseract`
need no key.

## What this is

One command tells you which backends are ready on this machine. Each backend lives in its own
package, and `BUILTIN_ADAPTERS` in `registry.py` is the only registration point. Run
`uv run openreading backends` to see readiness. "Configured" means the backend's install extra
imports and its declared environment variables resolve. It never means the vendor accepted the
key, which `uv run openreading backends --check <id>` measures. After `uv sync --all-extras --dev`,
with no keys set, you should see this output.

```
BACKEND                        TYPE               CONFIGURED  MISSING
anthropic-claude               hosted_api         no          -
aws-textract                   hosted_api         no          -
azure-document-intelligence    hosted_api         no          AZURE_DOCUMENT_INTELLIGENCE_KEY, AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
chunkr                         hosted_api         no          CHUNKR_API_KEY
docling                        oss_library        no          DOCLING_SERVE_URL
google-document-ai             hosted_api         no          GCP_PROJECT_ID, GCP_PROCESSOR_ID
nuextract                      hosted_api         no          NUEXTRACT_API_KEY
open-ocr                       hosted_api         no          OPENOCR_API_KEY
pulse                          hosted_api         no          PULSE_API_KEY
pymupdf                        oss_library        yes         -
qwen-vl                        self_hosted_model  no          QWEN_VL_ENDPOINT
reducto                        hosted_api         no          REDUCTO_API_KEY
tesseract                      oss_library        yes         -
```

Using a backend without its key gives exit code 3, not a crash. `uv run openreading parse sample.pdf
--backend reducto` prints this message.

```
[reducto] missing required credentials/config: REDUCTO_API_KEY. Sign up / configure: https://platform.reducto.ai
```

## Catalog

The three tables below tell you what each backend reads, what it needs from your environment, and
what it promises about your data. A required env var has `required: true` in the backend's
descriptor (its `AdapterDescriptor`). A backend with no required var uses its SDK's own
credential chain when nothing is set.

Source: `src/openreading/adapters/registry.py` (`BUILTIN_ADAPTERS`, lines 27–41) and each
`make_adapter(id).descriptor` (`credentials_spec`, `config_spec`, `compliance`, `runtime.license`,
`signup_url`). The install extra is the `pyproject.toml` extra of the same name. The exception is
the entry in `scripts/check_extras_parity.py` (`EXTRA_NAME_EXCEPTIONS`). Live truth: `uv run
openreading backends` (readiness) and `uv run python -c "from openreading.adapters.registry import
make_adapter; print(make_adapter('reducto').descriptor.to_schema_dict())"` (every field). If a
table and that output disagree, the output is right and the table needs fixing.

The first table gives the formats each backend reads. Every backend declares them in
`capabilities.input_formats` of its descriptor. When a request names the file's MIME type, the
router drops a backend whose list does not contain that format, with the drop code
`unsupported_format`. A batch run skips such a file with the same reason. A format marked
`(rasterized)` means the backend turns each page into an image before it reads it.

Source: `Capabilities.input_formats` in `src/openreading/types/descriptor.py` (line 78), read
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
| `nuextract` | pdf, png, jpg, pptx, odt, txt |
| `open-ocr` | pdf, png, jpg, gif, webp, tiff, bmp |
| `pulse` | pdf, docx, pptx, xlsx, png, jpg |
| `pymupdf` | pdf, xps, epub, mobi, cbz, svg |
| `qwen-vl` | png, jpg, pdf (rasterized) |
| `reducto` | pdf, png, jpg, docx, xlsx, pptx |
| `tesseract` | png, jpg, tiff, bmp, pdf (rasterized) |

The second table gives each backend's install extra and environment variables. An install extra
is a named optional dependency group in `pyproject.toml`, so a backend's client library installs
only when you ask for it. In the env columns, none means the backend declares no variable of
that kind.

| id | type | install extra | required env | optional env |
|---|---|---|---|---|
| `anthropic-claude` | hosted_api | `anthropic-claude` | none | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| `aws-textract` | hosted_api | `textract` | none | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` or `AWS_DEFAULT_REGION`, `OPENREADING_TEXTRACT_S3_BUCKET` |
| `azure-document-intelligence` | hosted_api | `azure-document-intelligence` | `AZURE_DOCUMENT_INTELLIGENCE_KEY`, `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | none |
| `chunkr` | hosted_api | `chunkr` | `CHUNKR_API_KEY` | `CHUNKR_BASE_URL` |
| `docling` | oss_library | `docling` | `DOCLING_SERVE_URL` | none |
| `google-document-ai` | hosted_api | `google-document-ai` | `GCP_PROJECT_ID` or `GOOGLE_CLOUD_PROJECT`, `GCP_PROCESSOR_ID` | `GOOGLE_APPLICATION_CREDENTIALS`, `GCP_LOCATION` |
| `nuextract` | hosted_api | `nuextract` | `NUEXTRACT_API_KEY` or `NUMIND_API_KEY` | `NUEXTRACT_BASE_URL` |
| `open-ocr` | hosted_api | `open-ocr` | `OPENOCR_API_KEY` | `OPENOCR_ENGINE` |
| `pulse` | hosted_api | `pulse` | `PULSE_API_KEY` | none |
| `pymupdf` | oss_library | `pymupdf` | none | none |
| `qwen-vl` | self_hosted_model | `qwen-vl` | `QWEN_VL_ENDPOINT` | `QWEN_VL_MODEL`, `QWEN_VL_API_KEY` |
| `reducto` | hosted_api | `reducto` | `REDUCTO_API_KEY` | `REDUCTO_WEBHOOK_SECRET` |
| `tesseract` | oss_library | `tesseract` | none (needs the system `tesseract` binary) | none |

The third table gives each backend's compliance posture, license and signup page. A backend's
compliance posture is the set of claims its descriptor makes about data handling. A BAA (Business
Associate Agreement) is the contract a vendor signs under HIPAA before it may handle protected
health information. The `hipaa_baa` column records whether the vendor offers one. The license
column quotes each descriptor's `runtime.license` string as written. In the signup column, none
means the backend runs locally and has nothing to sign up for.

| id | `hipaa_baa` | `trains_on_customer_data` | `runs_fully_local` | license | signup |
|---|---|---|---|---|---|
| `anthropic-claude` | yes | no | false | proprietary | https://console.anthropic.com |
| `aws-textract` | yes | opt_out | false | proprietary | https://aws.amazon.com/textract/ |
| `azure-document-intelligence` | yes | no | false | proprietary | https://azure.microsoft.com/products/ai-services/ai-document-intelligence |
| `chunkr` | tier_gated | opt_out | false | proprietary (AGPL-3.0 self-host available) | https://chunkr.ai |
| `docling` | na_local | na_local | true | MIT | none |
| `google-document-ai` | yes | no | false | proprietary | https://cloud.google.com/document-ai |
| `nuextract` | no | unverified | false | proprietary (open-weight NuExtract 2.0 [MIT 2B/8B] self-hostable via vLLM — different wire protocol, separate adapter) | https://nuextract.ai |
| `open-ocr` | no | unverified | false | proprietary | https://open-ocr.com |
| `pulse` | tier_gated | unverified | false | proprietary | https://www.runpulse.com |
| `pymupdf` | na_local | na_local | true | AGPL-3.0 | none |
| `qwen-vl` | na_local | na_local | true | Apache-2.0 (Qwen3-VL; Qwen2.5-VL per-size) | none |
| `reducto` | tier_gated | no | false | proprietary | https://platform.reducto.ai |
| `tesseract` | na_local | na_local | true | Apache-2.0 | none |

## Reading the compliance columns

These rules decide whether your policy admits a backend. A policy is a JSON file of compliance
requirements that the router enforces before it picks a backend. The rules were checked with
`echo '{"require_baa": true, "no_train_on_data": true}' > phi.json` and `uv run
openreading route sample.pdf --policy phi.json`.

- `hipaa_baa: tier_gated` is dropped under `require_baa` unless the id is in `baa_tier_confirmed`
  (drop code `no_baa`). `no` is always dropped.
- `trains_on_customer_data: opt_out` is dropped under `no_train_on_data` unless the id is in
  `train_optout_confirmed` (drop code `trains_on_data`).
- `unverified` is dropped unless `allow_unverified_compliance` is set. Unverified compliance always
  fails closed, which means the backend is dropped rather than assumed safe.
- `na_local` backends pass every column. Nothing in a request, a strategy or a fallback can widen
  this set.

## Override form

You can set one OpenReading-specific variable and it wins over the vendor's own variable name.
`OPENREADING_<SLUG>_<KEY>` beats the service-native var. `<SLUG>` is the id with `-` replaced by
`_`, and `<KEY>` is the descriptor's spec key. For example, `OPENREADING_REDUCTO_API_KEY` beats
`REDUCTO_API_KEY`. Source: `credentials.py` (`_slug_env`). The precedence rules are in `uv run
python -m pydoc openreading.credentials`.

## Known gaps

- `uv run openreading backends` prints `-` under MISSING for `anthropic-claude` and `aws-textract`
  while reporting `no`. `GET /v1/backends` names the vars. Reproduce it with `uv run openreading
  backends | grep -E "anthropic-claude|aws-textract"`.
- The `openreading.credentials` docstring says `openreading backends` "still reports them ready"
  with no env set. They are reported `no`. Reproduce it with `grep -n "reports them ready"
  src/openreading/credentials.py`.
- `CHANGELOG.md` says `google-document-ai` declares an empty `credentials_spec`. The descriptor has
  one optional entry. Reproduce it with `uv run python -c "from openreading.adapters.registry import
  make_adapter;
  print(make_adapter('google-document-ai').descriptor.to_schema_dict()['credentials_spec'])"`.

## See also

- [Docs home](../README.md) is the documentation home. It lists every guide and explains how to
  use OpenReading from an agent.
- [`.env.example`](../../../.env.example) lists every var with its signup URL.
- `uv run python -m pydoc openreading.credentials` prints the precedence and the `.env` rules.
- `uv run python -m pydoc openreading.router.compliance` prints every drop code.
- `uv run python -m pydoc openreading.adapters` prints the runbook for adding a backend, and
  [`scripts/new_adapter.py`](../../../scripts/new_adapter.py) scaffolds it.
- [JSON Schemas](../schemas/README.md) describes the response every backend returns.

## Maintenance

To add a backend, register it in `BUILTIN_ADAPTERS`. Add one row to each table here from its
descriptor. Add a block to `.env.example`. Add the extra to `pyproject.toml`.
`scripts/check_extras_parity.py` fails until the extra exists. It also fails until a slug that
differs from its extra name is in `EXTRA_NAME_EXCEPTIONS`. A changed compliance value, format or
env var is one cell here. When a Known-gaps line stops being true, delete it. The full table of
what to update for each kind of change is under *Where a change gets documented* in
[`AGENTS.md`](../../../AGENTS.md).

<sub>[Docs home](../README.md) · [← JSON Schemas](../schemas/README.md)</sub>
