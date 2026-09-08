# Example documents

You can parse these five documents the moment the clone finishes. Your first run needs no key, no
vendor account, and no documents of your own. A backend is one document parser. Both of the parsers
that run locally with no key read these files. PyMuPDF reads the text layer. Tesseract rasterizes
the page and runs OCR over the pixels. [The tutorial](../tutorial/README.md) walks the whole tool over them
in seventeen steps, and the root [README](../README.md) gives the short tour. This page says what
they are and where they came from.

| File | Pages | What it is | Text layer |
|---|---|---|---|
| `john_smith_1000_2026_01.pdf` | 1 | a business checking statement, 01/01/2026 – 01/31/2026, 8 transaction rows | yes |
| `john_smith_1000_2026_02.pdf` | 1 | a business checking statement, 02/01/2026 – 02/28/2026, 5 transaction rows | yes |
| `schedule_a_2024.pdf` | 1 | Schedule A (Form 1040) 2024, Itemized Deductions, filled in | yes |
| `1040_2024.pdf` | 2 | Form 1040 (2024), U.S. Individual Income Tax Return, filled in | yes |
| `1040-1988.pdf` | 5 | a scan of a filled Form 1040 (1988) and its schedules | **no** |

Those five split into three groups, and each group teaches something different.

## The two bank statements

Each file is a one-page business checking statement. It carries a bank header, an account block,
a four-line balance summary, and a dated transaction table. The table has withdrawal, deposit
and running-balance columns. That mix is the point. The account block is plain key-value text.
The summary puts each label above its value, which a naive left-to-right reader scrambles. The
transaction table is the part backends most often disagree about.

### Where they came from

Both files are synthetic, generated with ReportLab for backend testing. The account holder, the
account number, the balances and every transaction amount are invented. "John Smith" at "100 Main
Street" holds account `*1000`. Neither the person nor the account exists. The text around those
numbers is not all invented. The bank header carries a real street address in Kerrville, Texas.
The transaction rows name real companies such as DoorDash, ADP and United Rentals. None of
that is anyone's account data. You can send these files to a hosted backend, paste their output
into an issue, or build a regression fixture from them.

They are a parser fixture, not an accounting fixture. The two months do not chain. February ends
at `$10,596.78`, which is where January begins, so the pair reads as one statement out of order.
Nothing in OpenReading depends on the balances being consistent. The backends are compared on
whether they read the digits off the page, not on whether the digits add up.

### Why a PDF with a text layer

These files carry real text rather than a scanned image. That difference is what makes the pair
worth comparing. PyMuPDF lifts the characters the generator put in the file and gets them right.
Tesseract renders the page to a 150-DPI bitmap and reads it back with OCR. That is the same work
it would do on a photograph of a statement. The two paths disagree in a small, legible way. On
the January statement, Tesseract turns `Account Holder:` into `; Account Hotder-`. That one line
is the whole case for comparing backends.

Reading the same page with Tesseract shows it on the fourth line of the text channel:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend tesseract 2>/dev/null \
  | jq -r '.document.text' | sed -n '4p'
```
```text
; Account Hotder-
```

For a page with no text layer at all, use `1040-1988.pdf` below, or build the two-page raster
fixture the signal probe uses:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_scanned_pdf; open("scanned.pdf","wb").write(build_scanned_pdf())'
```

Both of its pages are gray raster with no characters behind them. `uv run openreading parse
scanned.pdf --backend tesseract` therefore succeeds with zero blocks on each page. That is what
the fixture is for. It exists to trigger the `scanned_pages_detected` signal, not to hold
readable content.

## The two 2024 tax forms

`schedule_a_2024.pdf` and `1040_2024.pdf` are official IRS forms for tax year 2024, downloaded
from irs.gov and filled in. They are born-digital, meaning software wrote the characters into the
file, so PyMuPDF reads them exactly. The entered values were flattened into the page, so the
files carry no interactive form fields for a backend to read as fields.

They are here because a tax form is laid out as a dense grid rather than as paragraphs. PyMuPDF
returns the Schedule A body as a single `table` block and the whole 1040 as six of them, where a
statement comes back as ordinary text blocks. That difference is what makes the pair useful for
testing the `table_cells` channel and the `table_shape_mismatch` finding that `compare` raises
when one backend finds a grid and another does not.

| File | Pages | Characters PyMuPDF reads | Blocks | Block types |
|---|---|---|---|---|
| `schedule_a_2024.pdf` | 1 | 4204 | 2 | 1 table, 1 text |
| `1040_2024.pdf` | 2 | 8857 | 6 | 6 tables |

Every entered value is invented. The taxpayer is "Priya Rai" at a downtown Chicago office
address, and both forms print each social security number as a row of `X` characters rather than
digits. The dollar amounts do not reconcile between the two forms, because these are a parser
fixture rather than an accounting one. The blank forms are United States government works and
carry no copyright.

## The 1988 scan

`1040-1988.pdf` is five pages of scanned paper: a filled Form 1040 for tax year 1988 and the
schedules attached to it. Each page is one 2560x3300 image and nothing else. **It has no text
layer at all**, which is what makes it the most useful document in this folder.

```bash
uv run openreading parse examples/1040-1988.pdf --backend pymupdf \
  | jq -r '.status.state, (.document.text | length)'
```
```text
succeeded
8
```

Eight characters across five pages, and every block has `"type": "image"`. The run succeeded,
because PyMuPDF read the text layer correctly and there is no text layer. That gap between
`succeeded` and useful is the whole argument for a strategy, and
[The tutorial](../tutorial/README.md) builds one around this file. Tesseract returns about 19,000
characters from the same document in roughly twenty seconds.

The entered values are synthetic and deliberately impossible. Both social security numbers begin
with a letter, which no real one does, and the ZIP code does not belong to the state beside it.
The scan therefore exercises OCR against handwriting-era print without carrying anyone's tax data.
It is also the slowest document here, so a command that OCRs it takes seconds rather than
milliseconds.

## Running them

Parse one document with one backend:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf > pymupdf.json
```

Every command on this page prints one or two notices on stderr from PyMuPDF itself, a `fitz`
deprecation warning and a suggestion to use `pymupdf_layout`. Both are PyMuPDF's advice rather
than errors, and neither reaches the JSON. The command above sends stdout to a file, so those
notices are all you see. Confirm the run with `jq -r .status.state pymupdf.json`, which prints
`succeeded`.

Every document as a batch. Pointing `parse` at a directory is what turns on batch mode:

```bash
uv run openreading parse examples/ --backend pymupdf > batch.json
```

The run touches six files, not five. The terminal prints `[3/6] README.md failed
unsupported_format`. Inside `batch.json`, `summary` records `"total": 6, "succeeded": 5,
"failed": 1`. This README sits in the directory too, and PyMuPDF does not read `.md`. Every source
is offered to the backend, so it comes back carrying PyMuPDF's own reason rather than being
filtered out before it was tried, and the batch exits 4 as partial.

Your own test documents belong in `samples/` at the clone root, which is gitignored for that
purpose. `scripts/batch_demo.sh` reads that folder by default, and a path argument such as
`scripts/batch_demo.sh path/to/docs` points it at another one. Either way it parses the whole
folder with both local backends and compares the two runs. The JSON lands in a hidden `.runs/`
folder inside whichever folder it read, so a second run never re-ingests the first. This
`examples/` directory holds only files that ship with the repository.
