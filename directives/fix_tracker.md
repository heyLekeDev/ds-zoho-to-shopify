# Directive: Fix Tracker Workflow

**Goal**: Process product issues logged by the team in the "DS - Zoho - Shopify - Fix" Google Sheet, fix them on Zoho and Shopify, and feed every lesson back into the pipeline so the same class of error cannot recur.

**Sheet**: `DS - Zoho - Shopify - Fix` (ID `1xnsCpSKXcHTjJIGZ2OeTku1QoONXVcgDm97A6YYj4rw`, tab `Issues`)
**Executor script**: `execution/process_fix_tracker.py`
**Feedback stores**: `feedback/image_hints.json`, `feedback/enrichment_examples.json`

## Image Queue tab (same spreadsheet, tab "Image Queue")

The work queue for items at `Image required` that auto-search cannot solve. Claude populates
rows (SKU, name, enriched title, why manual, suggested source); the team pastes an image URL
in column F. On each run (daily maintenance or on demand):
1. For each row with a URL and Status "Open": download → validate (square/1080+, no bg removal)
   → **visually review** → delete old Zoho image → upload → `Queue for Upload` → row Status "Fetched".
   Bad URL → Status "URL Rejected" + reason in Notes.
2. Every accepted URL's domain gets added to `brand_preferred_sources` in
   `feedback/image_hints.json` so future auto-fetches learn the source.
3. Whenever any run resets an item to `Image required`, append it to this tab.

## Daily maintenance routine

Scheduled task `ds-daily-maintenance` (07:07 daily, runs while the Claude app is open):
Image Queue → auto-retry fetch (fresh CSE quota) → audit gate → sync (+ `--skus` for
view-excluded categories) → verify → Fix Tracker rows → Shopify hygiene report.
It never publishes an unaudited image.

## Sheet columns

| Col | Field | Who fills it |
|---|---|---|
| A | Date Logged | Team |
| B | SKU | Team |
| C | Issue Type (Image / Description / Title / Tags / Collection / Variant / Price / Other) | Team |
| D | Issue Details | Team |
| E | Status (Open / In Progress / Fixed / Reopened / Needs Manual / Won't Fix) | Claude |
| F | Resolution Notes | Claude |
| G | Date Resolved | Claude |

## Process (run on demand)

1. **Read the sheet** — process every row with Status `Open` OR `Reopened`.
2. **Diagnose first** — write a `diagnose` action per SKU into an actions JSON and run
   `python execution/process_fix_tracker.py actions.json` to get live Zoho state
   (status, image presence, source URL, native brand). Never guess.
3. **Translate issues into actions** (judgment stays with the orchestrator; execution is
   deterministic):
   - Wrong/bad image → `reset_for_refetch` (deletes image first — mandatory), then
     `build_batch_files` for the affected SKUs, then run `fetch_images.py`.
   - Text issues (title/description/tags) → `write_fields` with corrected values.
   - Ready to sync → `queue_upload`, then run `sync_zoho_to_shopify.py`.
4. **Audit gate** — after any image fetch, run `execution/audit_batch_images.py` and
   visually verify every image against its product BEFORE queueing for upload.
   An image that passes size checks can still show the wrong product.
5. **Verify** — after sync, run
   `python execution/verify_published.py --skus <fixed skus>` to confirm the fix is
   actually live (image present, description non-empty, product ACTIVE).
6. **Write back to the sheet** — Status, Resolution Notes (say exactly what was done and
   what the team should verify), Date Resolved.
7. **Feed the lesson back** — every issue must produce at least one of:
   - a blocked domain / brand source / manual-only entry in `feedback/image_hints.json`
   - a corrected example in `feedback/enrichment_examples.json`
   - a script fix (then update the relevant directive)

## Reopened escalation (fix didn't work)

`Reopened` means the team checked the live site and the previous fix failed. Never repeat
the previous approach:

| First attempt | Escalation |
|---|---|
| Auto-fetch returned wrong image | Add SKU to `manual_only_skus`; mark Needs Manual |
| Cached source URL was bad | Block the domain in `image_hints.json`; retry with manufacturer-direct queries |
| Background/transparency artefacts | Different source required; never process the image |
| Image missing on Shopify | `verify_published.py` to confirm, then delete Zoho image → re-upload → re-sync |

After a second failed attempt → `Needs Manual` with a clear explanation of what was tried.

## Hard rules (from project memory — non-negotiable)

- ALWAYS delete the Zoho image before uploading a replacement. If DELETE fails, stop.
- NEVER substitute a similar/generic image. No exact image → `Image required` + report.
- NEVER run background removal. A bad cut-out is worse than no image.
- All images square 1:1, ≥1080×1080 (pad with white, never crop).
- Only write allowlisted `cf_*` fields (enforced by `execution/common.py`).

## What was learned the hard way

- The Zoho **native `brand` field** is populated on ~88% of items; `cf_brand` is usually
  empty. Always read `item['brand']`, never `cf_brand`. (`build_batch_files` does this.)
- Sync reporting success ≠ live on Shopify — always run `verify_published.py` after fixes.
- `image_processing.py` used to auto-run background removal — that was the root cause of
  every "product is transparent" complaint. Removed 2026-07-10. Do not reintroduce.
- Google CSE (`SEARCH_API_KEY`/`GOOGLE_CX`) works on the FREE tier (100 queries/day,
  no billing). It is the primary search; DDG is fallback. Quota in `.cse_quota.json`.
