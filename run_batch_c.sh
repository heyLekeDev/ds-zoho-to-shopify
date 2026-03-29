#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run_batch_c.sh — Batch C Pipeline Runner
# Runs Stages 2→5 for the 30-item Batch C bur inventory.
#
# PRE-CONDITIONS (already done by Claude):
#   ✓ enrichment_input.json  — 30 clean Batch C items (no A/B/Shopify overlap)
#   ✓ enrichment_output.json — AI-enriched titles, descriptions, tags (all [INFERRED])
#
# RUN FROM: ~/Antigravity/SNL/Dental Solutions/Zoho
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Batch C Pipeline — Stages 2→5"
echo "════════════════════════════════════════════════════════"
echo ""

# ── Stage 2 Write ─────────────────────────────────────────────────────────────
echo "▶ Stage 2 — Write enrichment to Zoho..."
echo "  (Sets 30 items to 'Enrichment Complete' with titles + descriptions)"
echo ""
python execution/enrich_items.py --write
echo ""
echo "  ✓ Stage 2 done. Check Zoho 'Enrichment Complete' view."
echo "  Press Enter to continue to Stage 3 (Image Validation)..."
read

# ── Stage 3 Image Validation ──────────────────────────────────────────────────
echo ""
echo "▶ Stage 3 — Image Validation..."
echo "  (Checks existing Zoho images: resolution 800x800+, aspect ratio 0.8–1.2)"
echo ""
python execution/validate_images.py
echo ""
echo "  ✓ Stage 3 done."
echo "  Items with 'Image required' status need images attached in Zoho first."
echo "  Press Enter to run Image Fetch (auto-finds images via DuckDuckGo)..."
read

# ── Stage 3b Image Fetch ──────────────────────────────────────────────────────
echo ""
echo "▶ Stage 3b — Image Fetch (for items that failed image validation)..."
echo ""
python execution/fetch_images.py
echo ""
echo "  ✓ Stage 3b done. Check 'Image required' view for any remaining gaps."
echo "  Press Enter to continue to Stage 4 (Bot Confirmation)..."
read

# ── Stage 4 Bot Confirmation ──────────────────────────────────────────────────
echo ""
echo "▶ Stage 4 — Bot Confirmation (12-rule validation)..."
echo "  (Checks: type, description, image, price, grouping, SKU collision, aspect ratio)"
echo ""
python execution/validate_enrichment.py
echo ""
echo "  ✓ Stage 4 done."
echo "  Items that passed move to 'Queue for Upload'."
echo "  Items that failed are in 'Needs Review' — check sync notes for details."
echo "  Press Enter to run final Shopify Sync..."
read

# ── Stage 5 Shopify Sync ──────────────────────────────────────────────────────
echo ""
echo "▶ Stage 5 — Shopify Sync..."
echo ""
python execution/sync_zoho_to_shopify.py
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Batch C complete!"
echo "  Check shopify_audit.csv and Sync_Execution_Log.csv for results."
echo "════════════════════════════════════════════════════════"
echo ""
