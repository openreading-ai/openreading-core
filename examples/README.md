# Example documents

You can parse these two documents the moment the clone finishes. Your first run needs no key, no
vendor account, and no documents of your own. A backend is one document parser. Both of the parsers
that run locally with no key read these files. PyMuPDF reads the text layer. Tesseract rasterizes
the page and runs OCR over the pixels. The root [README](../README.md) walks through them. This
page says what they are and where they came from.

| File | Pages | Period | Rows in the transaction table |
|---|---|---|---|
| `john_smith_1000_2026_01.pdf` | 1 | 01/01/2026 – 01/31/2026 | 8 |
| `john_smith_1000_2026_02.pdf` | 1 | 02/01/2026 – 02/28/2026 | 5 |

Each file is a one-page business checking statement. It carries a bank header, an account block,
a four-line balance summary, and a dated transaction table. The table has withdrawal, deposit
and running-balance columns. That mix is the point. The account block is plain key-value text.
The summary puts each label above its value, which a naive left-to-right reader scrambles. The
transaction table is the part backends most often disagree about.

## Where they came from

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

## Why a PDF with a text layer

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

For a page with no text layer at all, build the two-page raster fixture the signal probe uses:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_scanned_pdf; open("scanned.pdf","wb").write(build_scanned_pdf())'
```

Both of its pages are gray raster with no characters behind them. `uv run openreading parse
scanned.pdf --backend tesseract` therefore succeeds with zero blocks on each page. That is what
the fixture is for. It exists to trigger the `scanned_pages_detected` signal, not to hold
readable content.

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

Both documents as a batch. Pointing `parse` at a directory is what turns on batch mode:

```bash
uv run openreading parse examples/ --backend pymupdf --jobs 2 > batch.json
```

The run touches three files, not two. The terminal prints `[1/3] README.md skipped
unsupported_format`. Inside `batch.json`, `summary` records `"total": 3, "succeeded": 2,
"failed": 0, "skipped": 1`. This README sits in the directory, and PyMuPDF does not read
`.md`. The batch records the file with `skip_reason: "unsupported_format"` rather than dropping
it in silence.

Your own test documents belong in `samples/` at the clone root, which is gitignored for that
purpose. `scripts/batch_demo.sh` reads that folder by default, and a path argument such as
`scripts/batch_demo.sh path/to/docs` points it at another one. Either way it parses the whole
folder with both local backends and compares the two runs. The JSON lands in a hidden `.runs/`
folder inside whichever folder it read, so a second run never re-ingests the first. This
`examples/` directory holds only files that ship with the repository.
