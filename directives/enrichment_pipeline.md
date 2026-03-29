# Enrichment Pipeline — Master SOP

**Project:** Dental Solutions / Zoho → Shopify Sync
**Version:** 1.1
**Status Machine Field:** `cf_shopify_status` (Zoho custom dropdown)
**Quick Result Field:** `cf_sync_result` — single-line, visible in list view columns
**Full Detail Field:** `cf_shopify_sync_notes` — multi-line, overwritten each stage with full detail of what just happened

---

## Overview

The enrichment pipeline transforms raw Zoho inventory items into validated, publish-ready Shopify products. It is divided into four discrete stages. Each stage is an **independently callable script**. Human confirmation is required before advancing between stages — this is enforced by the status field acting as a gate.

```
[Batch Selection] → [AI Enrichment] → [Image Validation] → [Bot Confirmation]
      ↓                    ↓                   ↓                    ↓
Queue for           Enrichment          Image required       Queue for Upload
Enrichment          Complete            (or Complete)         (or Needs Review)
```

---

## Zoho Status Reference

All 12 statuses. Scripts only process items in their input status and write to their output status.

| Status | Pipeline Role |
|---|---|
| `(none)` | Unprocessed — not yet evaluated |
| `Queue for Enrichment` | Stage 1 output — awaiting AI enrichment |
| `Enrichment Complete` | Stage 2 output — awaiting image validation |
| `Image required` | Stage 3 fail — image missing or failed Pillow checks |
| `Image Validated` | Stage 3 pass — awaiting bot confirmation |
| `Queue for Upload` | Stage 4 pass — ready to sync to Shopify |
| `Published` | Live on Shopify |
| `Needs Review` | Any stage fail — human attention required, see `Sync result` + `Shopify sync notes` |
| `Error uploading` | Shopify API rejected during sync |
| `Ignore` | Excluded from all pipeline processing |
| `Update Required` | Manual workflow — scripts skip these |
| `To be Archived` | Manual workflow — scripts skip these |
| `Archived` | Manual workflow — scripts skip these |

---

## Stage 1 — Batch Selection

**Script:** `execution/batch_selector.py`
**Input status:** `(none)` (items with no status set)
**Output status:** `Queue for Enrichment`
**Sync result:** `Batch selected (YYYY-MM-DD)`
**Sync notes:** `[BATCH-YYYY-MM-DD] N items selected. Priority: <category>. Excluded: <count> ignored/already-statusd.`

### How it works

1. Reads the local inventory CSV (e.g., `DS inventory Jan 31 26.csv`) — **no API call needed for selection**.
2. Excludes items with any existing status (blank check on `cf_shopify_status`).
3. Excludes items with status `Ignore`.
4. Sorts candidates by priority:
   - Primary: Sales volume (if available in CSV)
   - Secondary: Category (implants → instruments → consumables → apparel)
5. Takes the top N items (default batch size: 50).
6. Writes `Queue for Enrichment` to `cf_shopify_status` via Zoho API for each selected item.
7. Writes `Batch selected (YYYY-MM-DD)` to `cf_sync_result`.
8. Writes full selection details to `cf_shopify_sync_notes`.

### Human confirmation gate

After running, **review the Zoho view "Queue for Enrichment"** to verify the correct items were selected before running Stage 2.

---

## Stage 2 — AI Enrichment

**Script:** `execution/enrich_items.py`
**Input status:** `Queue for Enrichment`
**Output status:** `Enrichment Complete` (success) or `Needs Review` (AI could not resolve)
**Sync result:** `[MATCH]` / `[INFERRED]` / `[CONFLICT]` / `[LATE ARRIVAL]` — visible in list view
**Sync notes:** Full AI reasoning, field-by-field decisions, late arrival match details
**AI prompt spec:** See `directives/zoho_text_enrichment.md`

### How it works

1. Queries Zoho for all items with status `Queue for Enrichment` using a targeted view (one API call).
2. Fetches item details in batches (detail API required for custom fields).
3. **Shopify Late Arrival Lookup** (before calling AI):
   - Queries Shopify for existing products matching the item's brand/category keywords.
   - If a matching Shopify product is found, extracts its: existing collection name, option names (e.g., `Color`, `Size`), and existing description HTML.
   - This context is passed to the AI prompt.
4. Calls AI (Claude) with a batch of items (up to 10 per call for token efficiency).
   - AI receives: item name, Zoho category, vendor, and — if late arrival — existing Shopify product data.
   - AI outputs the JSON format defined in `directives/zoho_text_enrichment.md`.
5. Writes AI output fields back to Zoho custom fields:
   - `cf_enriched_title` → enriched title
   - `cf_shopify_collection` → collection name
   - `cf_variant_1_name` / `cf_variant_1_value` → variant option 1
   - `cf_variant_2_name` / `cf_variant_2_value` → variant option 2
   - `cf_description_html` → HTML description
   - `cf_sync_result` → short tag: `[MATCH]`, `[INFERRED]`, `[CONFLICT]`, or `[LATE ARRIVAL] → <product title>`
   - `cf_shopify_sync_notes` → full AI reasoning, overwritten with date
6. Sets status to `Enrichment Complete` on success, `Needs Review` if AI flags a conflict it cannot resolve.

### Late Arrival Handling

If a matching Shopify product already exists:
- AI must use the **exact existing collection name** (passed in prompt).
- AI must use the **exact existing option names** (e.g., if Shopify uses `Color`, AI must output `Color` not `Colour`).
- AI **inherits** the existing description HTML — does not generate a new one.
- `cf_sync_result` → `[LATE ARRIVAL] → <existing product title>`
- `cf_shopify_sync_notes` → full match details: which Shopify product matched, option names inherited, description source.

### Human confirmation gate

After running, **review the Zoho view "Enrichment Complete"** to spot-check titles, collections, and variants before running Stage 3. Items flagged `Needs Review` should be resolved manually (edit Zoho fields + reset status) before proceeding.

---

## Stage 3 — Image Validation

**Script:** `execution/validate_images.py`
**Input status:** `Enrichment Complete`
**Output status:** `Image Validated` (pass) or `Image required` (fail)
**Sync result:** `Image OK` / `No image` / `Aspect ratio fail` / `Resolution too low` — visible in list view
**Sync notes:** Full image check details (exact dimensions, ratio, fail reason)
**No AI involved. Fully deterministic.**

### CRITICAL: No Substitute Images

**Every product image must be the EXACT product.** Never use images from similar products, the same category, or generic brand images as substitutes. Dental professionals will immediately notice mismatched images. If the exact product image cannot be found, the item must remain at `Image required` and be reported — do not upload any placeholder or approximation. Precision is more important than coverage.

### How it works

1. Queries Zoho for all items with status `Enrichment Complete`.
2. Downloads the attached image for each item (Zoho Files API).
3. Runs Pillow checks:
   - **Image present**: Fail → `cf_sync_result = No image`
   - **Aspect ratio**: must be between 0.8 and 1.2. Fail → `cf_sync_result = Aspect ratio fail (X.XX)`
   - **Minimum resolution**: must be at least 800x800px. Fail → `cf_sync_result = Resolution too low (WxHpx)`
4. On pass: sets status to `Image Validated`, writes `cf_sync_result = Image OK (WxHpx, ratio X.XX)`.
5. On fail: sets status to `Image required`, writes short fail code to `cf_sync_result`.
6. Full error details (exact dimensions, crop instructions) written to `cf_shopify_sync_notes`.

### Human confirmation gate

After running, **review the Zoho view "Image Validated"** before running Stage 4. Items in `Image required` require a new image uploaded to Zoho, then manually reset to `Enrichment Complete` to re-run image validation.

---

## Stage 4 — Bot Confirmation

**Script:** `execution/validate_enrichment.py`
**Input status:** `Image Validated`
**Output status:** `Queue for Upload` (all rules pass) or `Needs Review` (any rule fails)
**Sync result:** `All rules passed` / `FAIL: <rule name>` — first failure shown, visible in list view
**Sync notes:** Full rule-by-rule log with pass/fail per check and exact error messages
**No AI involved. Fully deterministic.**

### Validation Rules (from `Antigravity_Sync_Rules.csv`)

All 12 rules are run in order:

**Core Infrastructure**
- [ ] Item is `Inventory` type (tracked stock)
- [ ] Description field is not empty (`cf_description_html`)
- [ ] Image is attached (already confirmed in Stage 3, re-verified here)
- [ ] Price > $0.00

**Smart Grouping**
- [ ] If item is Standalone: no variant name/value fields populated
- [ ] If item is Standalone: no other Standalone item shares the exact same enriched title
- [ ] If item is in a collection: all items in that collection have identical option name dimensions
- [ ] No SKU collision — this SKU is not already attached to a different product title on Shopify

**Near-Duplicate Title Check** *(added v1.1 — catches pipeline vs pre-pipeline title divergence)*
- [ ] No existing Shopify product title is a fuzzy match (>80% similarity) for this item's enriched title with the same brand/vendor. Use token-set ratio comparison. If a fuzzy match is found, flag as `Needs Review` with the matching Shopify product handle so a human can confirm whether it is truly a different product or a duplicate.

**New-SKU Uniqueness Check** *(added v1.1 — prevents CF.SKU-new collision)*
- [ ] Before Stage 1 (Batch Selection), always run `python execution/fix_sku_collisions.py --dry-run` and verify output shows 0 collisions. If any collision is found, run without `--dry-run` to repair before proceeding. **Never publish a batch if CF.SKU-new collisions exist.** A collision means two different Zoho items have been assigned the same new SKU, which will cause one to silently overwrite the other on Shopify.

**Visual Integrity**
- [ ] Aspect ratio 0.8–1.2 (re-confirmed from Stage 3)
- [ ] Minimum 800x800px (re-confirmed from Stage 3)

**Late Arrival Validation** *(new rule)*
- [ ] If item is flagged as a late arrival: the collection name exactly matches an existing Shopify product title (case-sensitive)

**Shopify API Fallbacks**
- [ ] GraphQL userErrors check — will be written to this field after sync attempt in Stage 5

On all-pass:
- Status → `Queue for Upload`
- `cf_sync_result` → `All rules passed`
- `cf_shopify_sync_notes` → `[BOT CONFIRMED] All 12 rules passed. (YYYY-MM-DD)`

On any failure:
- Status → `Needs Review`
- `cf_sync_result` → `FAIL: <first failed rule name>` (e.g., `FAIL: Duplicate Single Titles`)
- `cf_shopify_sync_notes` → full list of every failed rule with exact error message and date

### Human confirmation gate

After running, **review the Zoho view "Queue for Upload"** to do a final sanity check before triggering the live sync to Shopify.

---

## Running the Stages

Each stage is a standalone script. Run individually from the project root:

```bash
# 0. Pre-flight: verify no new-SKU collisions exist
python execution/fix_sku_collisions.py --dry-run

# Stage 1 — Select a batch
python execution/batch_selector.py

# Stage 2 — AI enrichment pass
python execution/enrich_items.py

# Stage 3 — Image validation
python execution/validate_images.py

# Stage 4 — Bot confirmation (all Antigravity rules)
python execution/validate_enrichment.py

# Final sync to Shopify (existing script)
python execution/sync_zoho_to_shopify.py
```

Scripts are idempotent — re-running on the same input status is safe. No item is processed twice unless its status is manually reset.

---

## Zoho Views Required

Create one saved view per stage input status. Each view filters on `cf_shopify_status = <value>`:

| View Name | Filters On |
|---|---|
| `Queue for Enrichment` | `cf_shopify_status = Queue for Enrichment` |
| `Enrichment Complete` | `cf_shopify_status = Enrichment Complete` |
| `Image required` | `cf_shopify_status = Image required` |
| `Image Validated` | `cf_shopify_status = Image Validated` |
| `Queue for Upload` | `cf_shopify_status = Queue for Upload` |
| `Needs Review` | `cf_shopify_status = Needs Review` |
| `Published` | `cf_shopify_status = Published` |
| `Error uploading` | `cf_shopify_status = Error uploading` |
| `Update Required` | `cf_shopify_status = Update Required` |
| `Archived` | `cf_shopify_status = Archived` |

---

## Field Convention

### `cf_sync_result` — Quick Scan (list view)

Always **overwritten** by the most recent stage. One line. Tells you what happened at a glance.

| Stage | Pass example | Fail example |
|---|---|---|
| Batch | `Batch selected (2026-03-02)` | — |
| Enrichment | `[MATCH]` / `[INFERRED]` | `[CONFLICT] Vendor mismatch` |
| Late arrival | `[LATE ARRIVAL] → Bicon Scrub Set` | — |
| Image | `Image OK (1200x1200px, 1.00)` | `Aspect ratio fail (0.45)` |
| Bot | `All rules passed` | `FAIL: Duplicate Single Titles` |

### `cf_shopify_sync_notes` — Full Detail (item detail view)

Always **overwritten** by the most recent stage. Shows full context of what just happened — not a history.

Format: `[STAGE] (YYYY-MM-DD)\n<full detail>`

**Enrichment pass example:**
```
[ENRICH] (2026-03-02)
Tag: [MATCH]
Enriched title: "4.5 x 5.0mm Integra-CP™ Implant (3.0mm Well)"
Collection: Integra-CP Implants
Variant 1: Diameter / 4.5mm
Variant 2: Length / 5.0mm
Description: generated from item name + vendor spec.
```

**Conflict example:**
```
[ENRICH] (2026-03-02)
Tag: [CONFLICT]
Reason: Vendor is BICON but Category is "Consumables" — expected "Implants".
Action needed: Verify category in Zoho and reset to Queue for Enrichment.
```

**Bot fail example:**
```
[BOT] (2026-03-02)
FAIL: Duplicate Single Titles
Detail: SKU 200-170-006 shares title "Bicon Healing Cap" with this item.
Fix: Assign a Shopify Collection to group them as variants, then reset to Image Validated.
```

**Image fail example:**
```
[IMAGE] (2026-03-02)
FAIL: Aspect ratio 0.45 (must be 0.8–1.2)
Dimensions: 400x900px
Fix: Crop to square ~800x800px minimum, re-attach in Zoho, reset to Enrichment Complete.
```
