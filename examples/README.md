# Example documents

Two documents you can parse the moment the clone finishes, so that your first run needs no key,
no vendor account, and no document of your own. Both local backends read them: PyMuPDF from the
text layer, Tesseract by rasterizing the page and running OCR over the pixels. The root
[README](../README.md) walks through them; this page says what they are and where they came from.

| File | Pages | Period | Rows in the transaction table |
|---|---|---|---|
| `john_smith_1000_2026_01.pdf` | 1 | 01/01/2026 – 01/31/2026 | 8 |
| `john_smith_1000_2026_02.pdf` | 1 | 02/01/2026 – 02/28/2026 | 5 |

Each is a one-page business checking statement: a bank header, an account block, a four-line
summary of balances, and a dated transaction table with withdrawal, deposit and running-balance
columns. That mix is the point. The account block is key-value text, the summary is label-above-value
pairs that a naive left-to-right reader scrambles, and the transaction table is the part backends
most often disagree about.

## Where they came from

Both files are synthetic. They were generated with ReportLab for backend testing, and every name,
address, account number and amount in them is invented — "John Smith" at "100 Main Street" holds
account `*1000` at a "First National Bank" that does not exist. Nothing here is anyone's data, so
you can send these files to a hosted backend, paste their output into an issue, or commit a
regression fixture built from them without a second thought.

They are a parser fixture, not an accounting fixture. The two months do not chain: February ends
at `$10,596.78`, which is where January *begins*, so reading them as a consecutive ledger gives a
statement out of order. Nothing in OpenReading depends on the balances being consistent — the
backends are being compared on whether they read the digits off the page, not on whether the
digits add up.

## Why a PDF with a text layer

These carry real text, not a scan. That is what makes the pair worth comparing: PyMuPDF lifts the
characters the generator put in the file and gets them exactly right, while Tesseract renders the
page to a 150-DPI bitmap and reads it back with OCR, which is the same work it would do on a
photograph of a statement. The two paths disagree in a small, legible way — on the January
statement, Tesseract turns `Account Holder:` into `; Account Hotder-` — and that one line is a
whole demonstration of why the comparison exists.

For a scanned page with no text layer at all, generate one:

```bash
uv run python -c 'from openreading.testing.sample_pdf import build_scanned_pdf; open("scanned.pdf","wb").write(build_scanned_pdf())'
```

## Running them

One document, one backend:

```bash
uv run openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf > pymupdf.json
```

Both documents, as a batch — pointing `parse` at a directory is what turns on batch mode:

```bash
uv run openreading parse examples/ --backend pymupdf --jobs 2 > batch.json
```

The batch reports `total=3 … skipped=1`: this README is in the directory, and a `.md` file is not
something PyMuPDF reads, so it is recorded with `skip_reason: "unsupported_format"` rather than
silently dropped. Add your own documents to a folder of your own and run
`scripts/batch_demo.sh path/to/docs` to parse a whole corpus with both local backends and compare
the results.

Your own test documents belong in `samples/`, which is gitignored for exactly that purpose. This
directory holds only files that ship with the repository.
