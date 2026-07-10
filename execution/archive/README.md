# Archived scripts — do not run

- `dangerous/` — scripts that DELETE Shopify products, purge images, or mass-reset
  statuses. They are superseded one-offs kept only for reference. Running one
  against the live store can destroy data. If you think you need one, you almost
  certainly want an active pipeline script instead (see directives/).
- Everything else here is a completed one-off or was superseded by the active
  pipeline (batch_selector, enrich_items, validate_images, fetch_images,
  audit_batch_images, validate_enrichment, sync_zoho_to_shopify,
  verify_published, process_fix_tracker, common, image_processing).
- fix_sku_collisions.py is retired: it read a stale CSV and could MINT new
  collisions. Live SKU-collision checking now runs inside validate_enrichment.
