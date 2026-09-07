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
- The router goes from three stages to two, and then to one once
  [`compliance-removal`](compliance-removal.md) lands. Worth sequencing the two records so the
  "N-stage router" phrase in `AGENTS.md` and six READMEs is rewritten once.

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

- `_KNOWN_DOC_FORMATS` and both skip reasons go. `skip_reason` becomes an empty enum, so
  `batch-result` bumps to v0.2 with the field removed and `skipped` removed from the item-state
  enum. Items that used to be `skipped` become `failed`, carrying whatever the backend said.
- A directory run now attempts `.DS_Store` and reports a failed item for it. That is noisier and
  it is the honest report: the caller asked for the directory. Directory expansion still skips
  dotfiles for the hidden-file reason (`_expand_dir`), which is a separate rule and stays.
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
|---|---|---|---|---|---|---|
| `python-magic` | MIT | 2917 | 2026-07-20 | 27 | 32.4M | **yes**, libmagic |
| `filetype` | MIT | 773 | 2025-05-02 | 65 | 39.2M | no |
| `puremagic` | MIT | 242 | 2026-04-09 | 4 | not measured (rate-limited) | no |

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

Recommended: `puremagic` as a base dependency, with `python-magic` as an optional extra for
deployments that want libmagic's depth and can carry the system package. If exactly one is wanted,
take `puremagic` and revisit if sniffing accuracy ever shows up as a real bug rather than a
theoretical one.

### The resolver

One function, one precedence order:

1. The caller's explicit `mime_type`. Always wins. The caller knows.
2. `mimetypes.guess_type()` on the filename or path. Stdlib, PSF-licensed, already present, and it
   already knows `.svg`, `.xml`, `.epub`, `.heic`, `.avif`, `.odt`, `.xps`, `.mobi`, every format
   the nine-entry table loses.
3. `puremagic` sniffing the leading bytes. This is the case a filename cannot answer: bytes with
   no name, or a name that lies.
4. **`None`.** Not `application/pdf`.

Step 4 is the substance of the change. Everything else is plumbing.

### What changes

- Delete `_MIME_BY_EXT`, `_MEDIA_TYPES`, `_FORMAT_ALIASES`, `mistral_ocr._guess_mime_type`, and
  the `or "application/pdf"` at `api.py:528` and `google_gemini:308`.
- Adapters read `document.mime_type` and pass it through verbatim. The Claude adapter's whole need
  reduces to `media_type.startswith("image/")` for its block type.
- `api.py:344` (bytes, no filename, no caller MIME) is the one defensible default and it should
  survive, because after sniffing fails there is genuinely nothing to infer from. Keep it, keep
  the D-v2-9 reference, and add the sentence explaining why this case differs from the others.
  If sniffing is added, this default becomes the fourth fallback rather than the second.
- A backend receiving `mime_type=None` sends what its API requires and lets the vendor decide.
  That is the dumb-router principle applied to the same question.

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
- **No adapter guesses:** `grep -rn "application/pdf" src/openreading/adapters/` returns nothing.

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
