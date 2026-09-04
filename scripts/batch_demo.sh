#!/usr/bin/env bash
# Parse a whole folder with two local backends, then compare the two runs across the folder.
# Put your own test documents in ./samples. Any document a backend can read works there, and
# each backend lists the formats it reads in src/openreading/adapters/README.md.
# Pointing `parse` at a directory instead of one file is what turns on batch mode.
#
# Every output JSON lands in a .runs/ folder inside the directory you parsed. The leading dot
# hides that folder from the next run, so a second batch never re-ingests its own output.
# The repo's .gitignore already excludes samples/, so the default demo leaves nothing to commit.
#
#   scripts/batch_demo.sh                 # local backends only (pymupdf, tesseract), free, no keys
#   HOSTED=1 scripts/batch_demo.sh        # also runs reducto, pulse and nuextract (your .env keys, billed)
#   scripts/batch_demo.sh path/to/docs    # your own folder, output in path/to/docs/.runs/
set -euo pipefail

SRC="${1:-samples}"
OUT="$SRC/.runs"
mkdir -p "$OUT"

om() { uv run openreading "$@"; }

summary() {  # print a one-line summary of a batch-result JSON
  uv run python -c "
import json,sys; e=json.load(open(sys.argv[1])); s=e['summary']
print(f\"  {e['status']['state']:<9} total={s['total']} ok={s['succeeded']} failed={s['failed']} skipped={s['skipped']} backends={s.get('backends',{})}\")
" "$1"
}

echo "### batch → $SRC  (outputs in $OUT/)"

# --- single-backend batch: local backends (no keys, always runnable) --------------------
for be in pymupdf tesseract; do
  echo; echo "== $be =="
  om parse "$SRC" --backend "$be" --jobs 4 > "$OUT/$be.json"
  summary "$OUT/$be.json"
done

# --- corpus compare (the payoff): which backend is better across the whole folder -------
echo; echo "== corpus compare: pymupdf vs tesseract =="
om compare "$OUT/pymupdf.json" "$OUT/tesseract.json" --format table
echo
echo "   (drill into divergent docs:  uv run openreading compare $OUT/pymupdf.json $OUT/tesseract.json --format diffs)"

# --- hosted backends: opt-in (HOSTED=1), skips per-backend without a key -----------------
if [ "${HOSTED:-0}" = "1" ]; then
  for be in reducto pulse nuextract; do
    echo; echo "== $be (hosted backend, billed to your account) =="
    if om parse "$SRC" --backend "$be" --jobs 2 > "$OUT/$be.json" 2>/tmp/om_$be.err; then
      summary "$OUT/$be.json"
    else
      echo "  skipped ($be): $(tail -1 /tmp/om_$be.err)"
    fi
  done
  echo; echo "== corpus compare: reducto vs pulse (if both ran) =="
  [ -s "$OUT/reducto.json" ] && [ -s "$OUT/pulse.json" ] && \
    om compare "$OUT/reducto.json" "$OUT/pulse.json" --format diffs || echo "  (need both reducto + pulse runs)"
else
  echo; echo "### hosted backends skipped. Re-run with  HOSTED=1 scripts/batch_demo.sh  (uses .env keys, billed)"
fi
