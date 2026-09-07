# Design record: the dumb router and honest MIME types

Status: proposed, not built. Two changes that share one principle and are separable in code.

Measured against `e27ad9d` on 2026-09-07. Verify each line number before editing it.

## The principle

Core does not decide what a backend can read. It tells the backend the truth about the bytes,
sends them, and records what came back.

There are two ways core can be wrong about an input, and they are not symmetric:

- **Wrong about capability** ("this backend cannot read DOCX"). The backend returns an error, the
  chain moves to the next backend, the trail records it. Recoverable, visible, already handled.
- **Wrong about identity** ("this XML is a PDF"). The backend accepts the bytes and returns
  confident output. Nothing raises. No fallback fires. The caller gets a wrong answer with no
  signal that anything happened.

The first is a guess core is allowed to get wrong. The second is a lie core must never tell. Every
decision below follows from that split.

## Part 1: delete the format gate

### What exists

`router.py:180` drops a backend when the request's MIME type maps to a token outside that
descriptor's `input_formats`:

```python
if mime and caps.input_formats:
    token = _format_token(mime)
    allowed = {f.lower() for f in caps.input_formats}
    ...
        return DropReason(2, "unsupported_format", f"{mime} not in {sorted(allowed)}")
```

### Why it goes

It is a per-vendor capability table, and vendors change what they accept without telling us. It is
the same class of unverifiable claim as the compliance profile, with a smaller blast radius.

It is also barely functioning. Measured on the current tree:

```
x.docx   eligible= 6   dropped_for_format= 9
y.svg    eligible=15   dropped_for_format= 0
```

`.docx` drops nine backends only because `.docx` is one of nine entries in `api.py:311`
`_MIME_BY_EXT`. `.svg` drops none, because an unknown extension becomes `application/pdf` before
the router ever sees it. The gate is a projection of our own extension table, not knowledge of
vendors, and for every format outside those nine it is dead code that still looks authoritative.

### Why nothing is lost

`executor.py:219-236` already implements the behaviour the gate was standing in for. Every
exception from a backend, taxonomy or not, including a bare `KeyError` out of `normalize()`, is
appended to the trail and the chain continues:

```python
except _TAXONOMY as e:
    ...
    trail.append(Attempt(desc.id, category, code, str(e)))
    continue
except Exception as e:
    ...
    trail.append(Attempt(desc.id, "terminal", "", str(e)))
    continue
```

The gate buys one saved round-trip and some ordering quality. It does not buy safety.

### What changes

- Delete the stage-2 format branch and the `unsupported_format` drop code from the router.
- `input_formats` stays on the descriptor as documentation for `openreading backends`. Nothing
  branches on it. It is allowed to be stale, and the field's docstring should say so in those
  words, so the next reader does not restore the gate.
- The router keeps a capability stage. Removing the format branch does not empty stage 2: the
  feature checks beside it (`missing_<cap>`, `missing_custom_schema_extraction`,
  `router.py:175-177`) are about what the caller asked for versus what the descriptor claims, and
  they stay. Nor does the router collapse to one stage after
  [`compliance-removal`](compliance-removal.md): what remains is a filter stage and a scoring
  stage. The "3-stage compliance-first router" phrasing in `AGENTS.md` and six READMEs becomes
  "filter, then score", rewritten once, by whichever record lands last.

### The one real consequence: batch intake

`batch/sources.py:38` holds `_KNOWN_DOC_FORMATS`, 26 extensions, used to tell two skip reasons
apart:

```python
SkipReason = Literal["unsupported_format", "unknown_format"]
```

Both are in the published schema (`batch-result.v0.1.json:67`, a CLOSED enum) and in
`types/batch.py:61`. This is what stops `parse ./corpus/` from ingesting `.DS_Store`.

**Decision (Akshay, 2026-09-07): be the dumb router.** Accept everything the caller named, send it
all, and accumulate per-item results and errors. Do not pre-judge a file by its extension.

Consequences to carry:

- `_KNOWN_DOC_FORMATS` and both skip reasons go, and the schema change is larger than just the
  field. In `batch-result.v0.1.json`: `skip_reason` is removed, `skipped` leaves the item-state
  enum (line 51), `summary.skipped` leaves the **required** list (line 95), and the batch-state
  description at line 18 stops referring to "everything skipped". `types/batch.py:61` follows.
  Items that used to be `skipped` become `failed`, carrying whatever the backend said. Any
  `skipped`-related warning text goes with them. This is a `batch-result` v0.2.
- Hidden files stay excluded. `_expand_dir` (`sources.py:142`) already prunes any name starting
  with `.`, so `.DS_Store` never reaches intake and is not the example to reason about. That rule
  is about visibility, not format, and it survives untouched.
- The behaviour that actually changes is a **visible file with an unknown extension**:
  `corpus/notes.rtf`, `corpus/data.parquet`. Today those are skipped without dispatch. After, they
  are sent, and the item reports whatever the backend said, success or failure. That is the case
  to test.
- `pymupdf/adapter.py:261` raises `backend_code="unsupported_format"` on its own behalf. That
  stays. A backend refusing an input it actually cannot read is exactly the mechanism this change
  relies on, and it is a fact the backend knows first-hand.

## Part 2: one honest MIME resolver

### What exists: six tables, five defaults to PDF

| site | mechanism | unknown becomes |
|---|---|---|
| `api.py:311` `_MIME_BY_EXT` | 9-entry dict | `application/pdf` |
| `api.py:344` | bytes input, no filename | `application/pdf` (D-v2-9, documented) |
| `api.py:528` | URL download | `application/pdf` |
| `server/app.py:543` | `mimetypes.guess_type` | `None` |
| `adapters/mistral_ocr:343` | private `_guess_mime_type` | (varies) |
| `adapters/google_gemini:308` | none | `application/pdf` |
| `adapters/anthropic_claude` | `_MEDIA_TYPES`, added 2026-09-07 | `application/pdf` |

The Claude entry is worth naming, because it is the argument against this whole shape. It was
added last week to fix a real defect (images sent as PDFs) and it does not work: `build_request`
always sets `document.mime_type`, and the adapter reads `document.mime_type or _MEDIA_TYPES...`,
so for any file path the table never executes. Measured:

```
a.svg -> block type='document' media_type='application/pdf'
b.xml -> block type='document' media_type='application/pdf'
```

The fix addressed three extensions at the leaf while the trunk kept lying. That is what a
per-adapter table buys.

Meanwhile the reverse direction already does it correctly. `router.py:63` `_format_token` uses
`mimetypes.guess_extension` plus a small override dict, and its docstring explains exactly why
hand-splitting a MIME type is wrong. The pattern exists in the repository; the forward direction
just never used it.

### Library choice

Requirement: no GPL, LGPL or AGPL. Measured from PyPI and the GitHub API on 2026-09-07.

| library | license | stars | last push | open issues | downloads/mo | native dep |
  requires-python |
|---|---|---|---|---|---|---|---|
| `python-magic` | MIT | 2917 | 2026-07-20 | 27 | 32.4M | **yes**, libmagic | >=2.7 |
| `filetype` | MIT | 773 | 2025-05-02 | 65 | 39.2M | no | none declared |
| `puremagic` 2.x | MIT | 242 | 2026-04-09 | 4 | not measured (rate-limited) | no | **>=3.12** |
| `puremagic` 1.30 | MIT | (same repo) | 2025-07-04 | | | no | none declared |

All three are MIT. Reading them against the brief:

- **`filetype` is out.** Popular, but sixteen months without a commit and 65 open issues. Downloads
  measure yesterday's adoption, not whether anyone is home.
- **`python-magic` is the best-maintained and most widely used**, and libmagic is the actual
  industry standard, the same engine behind `file(1)`. Its cost is a system package:
  `brew install libmagic`, `apt install libmagic1`, and a separate wheel on Windows. For a library
  people `pip install`, a native dependency is a support burden that lands on us in issues.
- **`puremagic` is the recommendation.** Pure Python, actively maintained on a small surface, four
  open issues. The small star count is the honest risk; the mitigating fact is that the dependency
  is shallow and replaceable, because it sits behind one function.

**`puremagic` 2.x cannot be used yet.** It declares `requires-python >=3.12` and this project
declares `>=3.11` (`pyproject.toml:6`). Depending on it would silently drop 3.11 support.

Recommended: **`puremagic>=1.30,<2`**, one library, no optional second engine. 1.30 declares no
Python floor, installs and imports cleanly, and was measured against the cases this resolver
exists for (results above). When the project raises its own floor to 3.12, move to 2.x as an
ordinary dependency bump; the API surface this design uses is one call.

Verified in a scratch venv rather than read off metadata:

```
$ pip install "puremagic>=1.30,<2" && python -c "import puremagic"
puremagic 1.30
```

### The resolver

One function, one precedence order. **Content before filename**, because a filename is a claim
and the bytes are the fact:

1. The caller's explicit `mime_type`. Always wins, and is the only override. The caller knows
   something core cannot, and saying so must not be second-guessed.
2. `puremagic` on the leading bytes.
3. `mimetypes.guess_type()` on the filename or path. Stdlib, PSF-licensed, already present.
   This is the fallback for the cases a signature cannot reach, not the first answer.
4. **`None`.** Not `application/pdf`, in every case, including bytes with no filename.

Ordering 2 before 3 is not a preference. Measured with `puremagic` 1.30 against a real PDF
renamed `misnamed.txt`:

```
misnamed.txt    puremagic(bytes)=application/pdf    mimetypes(name)=text/plain
```

Filename-first hands that document to a backend as `text/plain`. The lying-name case is exactly
the one a resolver exists to catch, and putting the extension first means detection never runs on
the inputs that need it.

The worry that signature sniffing is weak on text formats does not survive measurement. The same
run:

```
a.svg   image/svg+xml     b.xml  application/xml    c.txt  text/plain
d.html  text/html         e.png  image/png          real.pdf  application/pdf
```

Step 4 is the substance of the change. Everything else is plumbing.

### What changes

- Delete `_MIME_BY_EXT`, `_MEDIA_TYPES`, `_FORMAT_ALIASES`, `mistral_ocr._guess_mime_type`, and
  the `or "application/pdf"` at `api.py:528` and `google_gemini:308`.
- Adapters read `document.mime_type` and pass it through verbatim. The Claude adapter's whole need
  reduces to `media_type.startswith("image/")` for its block type.
- `api.py:344` (bytes, no filename, no caller MIME) loses its PDF default too. Sniffing runs on
  those bytes like any others, and when it finds nothing the answer is `None`. Keeping a PDF
  default here for D-v2-9's sake would reintroduce the exact lie this change removes, in the one
  case where core knows least about the input. D-v2-9 is superseded and should be marked so where
  it is cited.
- **`mime_type=None` needs a defined contract, not an improvised one.** An adapter that must name
  a media type on the wire sends what its own API requires for its default input and lets the
  vendor refuse. It never substitutes `application/pdf` for an unknown. Where a vendor's API
  demands a media type and the adapter has none, the honest move is `TerminalError`
  (`unsupported_input`), which the chain already turns into a fallback. Nine legitimate
  `"application/pdf"` literals live in adapters today for backends that genuinely send PDFs;
  those stay, and are not what this rule is about.

## Test strategy

The failing test first, in both parts.

- **Format gate:** a test asserting no plan contains an `unsupported_format` drop, run against the
  current tree first, where it fails for `.docx`.
- **Batch:** a test asserting a directory containing `.DS_Store` produces a `failed` item rather
  than a `skipped` one.
- **Resolver, the important one:** a parametrised test over every extension in
  `_KNOWN_DOC_FORMATS`, asserting `build_request` produces the correct MIME type. Against the
  current tree, seventeen of twenty-six fail by returning `application/pdf`. That single test is
  the regression proof for the whole change.
- **No adapter guesses.** Not a blanket string search: nine `"application/pdf"` literals in
  adapters are legitimate, naming what a PDF-sending backend actually sends. The check is
  behavioural, one test per adapter that names a media type: given a request whose `mime_type` is
  `None`, the adapter does not put `application/pdf` on the wire.

## Schema changes

- `batch-result` v0.1 to v0.2: `skip_reason` removed, `skipped` removed from the item-state enum.
- No change to `request`, which already carries an optional `mime_type` that may be absent.
- The `unsupported_format` drop code leaves the route plan's `dropped` map. That map's values are
  an open set, so no schema bump is needed for the code itself.

## Order

Part 2 before Part 1. Honest MIME types are strictly good on their own and fix a live defect. The
format gate becomes more accurate the moment the resolver stops labelling everything PDF, which
makes deleting it a smaller, better-understood change rather than two behaviour shifts at once.

Both are independent of [`compliance-removal`](compliance-removal.md), except for the shared
"N-stage router" prose, which whichever lands last should tidy.
