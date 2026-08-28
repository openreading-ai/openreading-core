#!/usr/bin/env bash
# Batch + corpus demo over ./samples — a gitignored folder of your own test documents
# (sample1.pdf, …; add more anytime, any format: PDF/PNG/DOCX/…). It just points `parse` at the
# directory, which is what triggers batch mode.
#
# All output JSON lands in samples/.runs/ — a HIDDEN dir, so the directory batch never re-ingests
# its own output, and it's gitignored along with samples/. Nothing here is committed.
#
#   scripts/batch_demo.sh                 # local backends only (pymupdf, tesseract) — free, no keys
#   HOSTED=1 scripts/batch_demo.sh        # also batch reducto/pulse/nuextract (uses your .env keys — billed)
#   scripts/batch_demo.sh path/to/docs    # point at a different folder
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
    echo; echo "== $be (hosted — billed to your account) =="
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
  echo; echo "### hosted backends skipped — re-run with  HOSTED=1 scripts/batch_demo.sh  (uses .env keys, billed)"
fi
