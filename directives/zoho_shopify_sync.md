# Directive: Zoho-to-Shopify Overhaul Sync

**Goal**: Synchronize product data from Zoho to Shopify while consolidating multiple Zoho records into single Shopify products (parent-child relationship).

## Core Principles

1.  **Targeted Sync**: Only process Zoho records with a status of `Queue for Upload`, `Update Required`, or `To be Archived`.
2.  **Consolidation (Collection Rule)**: Records are grouped by the `Shopify Collection` field, which serves as the Shopify Product Title.
3.  **Conflict Audit**: All records in a group must share identical variant names (`var 1-name`, `var 2-name`, `var 3-name`). If a mismatch is found, the group is skipped and marked as `Error uploading`.
4.  **GraphQL Primary**: All Shopify actions (Lookup, Create, Update, Delete) are executed via Shopify's GraphQL API for maximum precision and performance.
5.  **State Handshake**: Every action updates the Zoho record status and appends a timestamped note to the `Shopify sync notes` field.

## Sync Architecture (Script: `sync_zoho_to_shopify.py`)

### Phase 1: Targeted Fetch

- Queries the Zoho View (`ZOHO_VIEW_ID`) — the view IS the filter. Only pending items should be in it.
- Calls the Detail endpoint for each item in the view (unavoidable — Zoho List API does not return custom fields).
- **API calls = `(list pages) + (N items × 1 detail call)`** — this is the absolute minimum.
- Reports `"No items pending sync"` if the view is empty.

### Phase 2: Group & Audit

- Groups items by `Shopify Collection`.
- Checks for variant name consistency.
- Fails the group if internal conflicts exist.

### Phase 3: Shopify Actions (GraphQL)

- **SKU Search**: Checks if SKU already exists in Shopify.
- **`productCreate`**: Creates parent product and all variants if the title is new.
- **`productVariantsCreate`**: Adds new variants to an existing product.
- **`productVariantUpdate`**: Syncs price, inventory, and option values for existing variants.
- **`productVariantDelete`**: Removes variants when status is `To be Archived`.

### Phase 4: State Handshake

- Updates Zoho status to `Published` or `Archived`.
- Logs timestamped status/error notes in `CF.Shopify sync notes`.

## Performance & Resilience

- **Rate Limits**: Includes automatic 60s backoff for Zoho (429) and exponential backoff for Shopify.
- **Authentication**: Uses dynamic OAuth Refresh flow (Zoho) and Client Credentials flow (Shopify) to ensure tokens never expire during long runs.
- **Validation**: Strict conflict audit ensures variant names match within collections before any Shopify writes occur.
