# Deterministic Code Review — 2026-07-10

Four parallel reviews of `execution/` (~14,900 lines, 42 scripts). Live-verified facts:
**962 Published · 452 Image required · 18 stranded at "Image Validated"** · the
`cf_shopify_status` query filter on the Zoho `/items` LIST endpoint **works**
(old memory note was wrong) — this unlocks live selection and status dashboards cheaply.

## Theme A — Stale data sources cause duplicate/limbo work (stages 1–4)

| # | Finding | Where | Fix |
|---|---|---|---|
| A1 | Batch selection trusts a Jan-31 CSV for "unprocessed"; re-selects live items (caused the 32-of-40-already-live batch) | batch_selector.py:137-205 | Select from live Zoho via the (working!) cf_shopify_status filter; read-before-write guard per item |
| A2 | Shopify exclusion uses stale 161-SKU audit CSV with old-scheme SKUs (live: ~962) | batch_selector.py:296-312 | Live `productVariants(query:"sku:")` probe per selected item (~25 calls/batch) |
| A3 | "Bot confirmation" is 4 rules not 12 — duplicate-title check is DEAD CODE (`collection_counts` never read); missing: Shopify SKU probe, ₦100 floor (checks ≤0), view-excluded-category flag, empty native description, option-name lint | validate_enrichment.py:94-119,161-168 | Wire in the 6 missing rules (~5 lines each) |
| A4 | Items not in the current enrichment files are silently skipped forever — 18 items stranded NOW (280-120-xxx, 340-170-xxx, 520-120-001) | validate_enrichment.py:178-188 | Write Needs Review on skip; exit non-zero; triage the 18 |
| A5 | `input()` confirmation crashes non-interactive runs (EOFError confirmed) | batch_selector.py:346 | `--yes` flag + isatty guard |
| A6 | SKU-only join can stamp wrong data; FAIL path still writes enrichment fields | validate_enrichment.py:143-144,229-245 | Join on item_id; FAIL writes status/notes only |
| A7 | One transient download failure ejects item to "Image required" permanently; unbounded 429 recursion | validate_images.py:120-134 | Retry ×3; on persistent failure keep status + note |
| A8 | fix_sku_collisions.py is stale one-shot; running it now can MINT collisions; bypasses allowlist | whole file | Retire; fold live collision check into validate_enrichment |

## Theme B — Silent failures in sync_zoho_to_shopify.py

| # | Finding | Where | Fix |
|---|---|---|---|
| B1 | In-process Zoho token cached forever → runs >55min silently fail ALL status writebacks ("Commit complete" still prints) | :90-92 | Timestamp + refresh (use common.get_zoho_token) |
| B2 | Checkpoint futures discarded — failed Zoho writes invisible; no retry/429 handling | :1289-92,495-6 | Collect futures, retry, hard [HANDSHAKE FAILED] report + non-zero exit |
| B3 | Success path discards note text incl. collision audit notes | :469-470 | Include debug_note in success note |
| B4 | New products upload/attach image TWICE (dup media); first attempt's result ignored | :812-814,831 | Delete the create-path image call; let update pass handle it |
| B5 | Title lookup is tokenized search — can silently merge two product families | :621-649 | Require exact title equality on the match |
| B6 | Failed collision/title query treated as "no collision/product" → duplicate SKUs/products | :553-561,648 | Distinguish query-failure from empty; abort group on read failure |
| B7 | shopify_graphql: no THROTTLED retry, no HTTP check, `data:null` crashes run | :167-176 | Retry throttle, raise non-200, return `data or {}` |
| B8 | Phase 1 reads only page 1 (cap 200) of the view; --skus resolves from stale local JSON | :200-226,283-299 | Pagination loop; resolve SKUs live; reconciliation diff vs status-filtered list |
| B9 | Channel publish only on create; failure = warning yet item Published (invisible product) | :801-810 | Publish idempotently on updates too; failure → Error uploading; verify publishedOnPublication |
| B10 | Media success asserted before async processing; mediaUserErrors queried but never read | :1212-20,1263 | Poll media status → READY; read mediaUserErrors |
| B11 | fileCreate REPLACE keyed on raw Zoho filename — generic names (image.png) can overwrite ANOTHER product's image (likely cause of original swapped-image complaints) | :1159-65 | Content-addressed filename `{sku}-{hash8}.{ext}` |
| B12 | Every image downloaded twice from Zoho (aspect check + upload), even hash-skipped ones | :600-611 vs 1063-69 | Check after hash-skip; pass bytes through |
| B13 | One bad aspect ratio fails whole group with wrong item's error text | :613-618 | Fail only offending item, prefix SKU in note |
| B14 | Collision-path inventory sync userErrors ignored → stale stock, oversell risk | :1024-28 | Mirror main path handling |
| B15 | Cache saved only at exit (lost on kill); --skus has no status gate | :1324-25,311 | Save per micro-batch; status allowlist + --force |
| B16 | Duplicates auth/write logic instead of common.py allowlist | :460-496 | Route through common.zoho_write_fields |

## Theme C — Image fetch hit rate & quota (fetch_images.py)

| # | Finding | Where | Fix |
|---|---|---|---|
| C1 | Queries polluted: em-dashes, `#`, brand duplicated, `Model`/voltage/category strings, doomed quoted phrases | build_queries_v2 :218-278 | Normalize: strip brand prefix from title, drop —/#/(), no quoted phrases, single product-noun instead of category tail |
| C2 | forced_specific pass re-runs byte-identical queries; last-resort re-searches all 5 via DDG — hopeless item burns ~7 CSE + 5 DDG | :648-701 | Only force on actual dupe-block; cache candidates per item |
| C3 | No failure memory across runs — same hopeless SKUs re-burn quota every run | — | `.fetch_failures.json`; skip N days; auto-suggest manual_only after 2 fails |
| C4 | CSE title/snippet/contextLink discarded; no model/shade/size token scoring (direct cause of Jet 1/2 swap, A3-for-A1, M-for-L) | :332-35,541-96 | Required/forbidden token scoring from v1_value + sibling values; hue check for colour variants |
| C5 | Candidates accumulate + re-download across query loop; generic early results outrank specific later ones | :660,676-87 | Tried-URL set + score-based ranking |
| C6 | Manual overrides/saved URLs cost a search query first and can LOSE to search results | :662-687 | Try override alone before any query, return on pass |
| C7 | num=5 when 10 costs the same quota; no imgSize=xlarge prefilter; empty CSE result doesn't fall back to DDG | :77,319,340-45 | num=10, imgSize xlarge, DDG fallback on 0 results |
| C8 | Portrait/landscape hard-rejected though white-canvas padding exists | :485-86 | Pad 0.4–2.5 ratios to square (no bg removal) |
| C9 | collection_hashes empty each run; dupe-accepted hashes unregistered; MD5 misses re-encodes | :1065,749 | Pre-seed from siblings' Zoho images; register all; consider imagehash |
| C10 | sleep(15) after CSE queries (DDG-only need); ~45 min dead time per batch | :687 | Sleep only for DDG |
| C11 | Nits: PNG uploaded as image/jpeg; _normalize_url no 404 fallback; CWD-relative token file; hint domains appended after built-ins (unreachable); recheck-published bypasses manual-only gate; "philips zoom" hint key can never match | various | Small fixes |

## Theme D — Verification blind spots

| # | Finding | Where | Fix |
|---|---|---|---|
| D1 | Variant with no image passes if product has any media; sibling variants sharing one image undetected; no size check | verify_published.py:52-55 | Require variant image on multi-variant products; flag shared image URLs; check dimensions |
| D2 | No publishedOnPublication check (invisible-product mode) | verify_published.py | Add channel check |
| D3 | Audit gate is procedural only — validate_images promotes straight to Image Validated | validate_images.py:138-158 | Optional "Image Pending Audit" status (needs new Zoho dropdown value) |

## Theme E — Script hygiene

- **8 DANGEROUS scripts** (delete Shopify products / purge images / mass status resets): cleanup_legacy_standalone, delete_regroup_burs, pre_migrate_collections, repair_shopify_and_cleanup, purge_consumer_images, clear_batch_images, requeue_published, reset_zoho_for_resync → move to `execution/archive/` (danger ones under `archive/dangerous/`)
- **11 legacy/one-off** scripts superseded → archive
- **Keep as tools**: audit_shopify_catalog, fetch_bicon_store, normalize_shopify_images, patch_missing_descriptions, refetch_catalogue_items, repair_shopify_images, upload_missing_images, verify_zoho_setup
- All stage scripts should import common.py instead of duplicating auth (B16, A8 are live examples of drift)

## Don't touch (verified good)
- update_zoho_status strict-payload safety check; option-value pre-scan; inventorySetOnHandQuantities separation; collision update-in-place philosophy; MD5 hash-skip; micro-batch checkpoint structure; validate_images auto-downscale/upscale fail-safes; dry-run modes everywhere; ALLOWED_WRITE_FIELDS.

## Recommended sequence
1. **Phase 1 — stop the bleeding (small, contained):** B1+B2+B7 (token/handshake/GraphQL wrapper), B4+B10+B11 (image dup/async/filename), C1+C4+C7+C10 (queries/scoring/quota), A5 (--yes), A3 (real validation rules), D1+D2 (verifier)
2. **Phase 2 — live truth:** A1+A2 (live selection), B8 (pagination+reconciliation), C3 (failure memory), A4 (stranded 18), E (archive scripts)
3. **Phase 3 — structural:** D3 (pending-audit status — needs Zoho dropdown change), C9 perceptual hashing, common.py adoption everywhere (B16/A8)
