#!/usr/bin/env python3
"""
Migrate Shopify Variant SKUs — Old Scheme → New Scheme

The Zoho inventory was re-numbered to a new SKU taxonomy. Shopify still holds
the old SKUs. This script reads the old→new mapping from the inventory CSV
(SKU column = old, CF.SKU - new column = new) and updates every affected
Shopify variant SKU in one pass.

After running this, the sync pipeline (sync_zoho_to_shopify.py) can correctly
find and update all existing Shopify products by their new SKUs.

Usage:
    python execution/migrate_shopify_skus.py --dry-run    # preview all changes
    python execution/migrate_shopify_skus.py              # live migration
"""

import os, sys, csv, json, time, glob, argparse, requests
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_SHOP_URL      = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID     = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION   = '2025-01'

_shopify_token = None

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_shopify_token():
    global _shopify_token
    if _shopify_token:
        return _shopify_token
    resp = requests.post(
        f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token",
        json={"client_id": SHOPIFY_CLIENT_ID, "client_secret": SHOPIFY_CLIENT_SECRET,
              "grant_type": "client_credentials"},
        timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f"Shopify auth failed: {data}")
    _shopify_token = data['access_token']
    return _shopify_token

def shopify_graphql(query, variables=None):
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        resp = requests.post(url, headers=headers,
                             json={"query": query, "variables": variables or {}},
                             timeout=30)
        if resp.status_code == 429:
            print("  [RATE LIMIT] Waiting 10s...")
            time.sleep(10)
            continue
        return resp.json()
    raise Exception("Shopify GraphQL: too many retries")

# ── CSV helpers ───────────────────────────────────────────────────────────────

def find_inventory_csv():
    candidates = glob.glob('DS inventory*.csv') + glob.glob('*.inventory*.csv')
    if not candidates:
        candidates = [f for f in glob.glob('*.csv')
                      if 'inventory' in f.lower() and 'sync' not in f.lower()
                      and 'rules' not in f.lower() and 'sku' not in f.lower()]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_sku_mapping(csv_path):
    """
    Returns list of (old_sku, new_sku, item_name) for rows where old != new.
    Also returns dict of new_sku → item_name for all rows.
    """
    mappings = []
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            old_sku = row.get('SKU', '').strip()
            new_sku = (row.get('CF.SKU - new') or '').strip()
            name    = row.get('Item Name', '').strip()
            if old_sku and new_sku and old_sku != new_sku:
                mappings.append((old_sku, new_sku, name))
    return mappings

# ── Shopify operations ────────────────────────────────────────────────────────

def find_variant_by_sku(sku):
    """Returns (variant_gid, inv_item_gid, product_title) or (None, None, None)."""
    data = shopify_graphql("""
        query($q: String!) {
            productVariants(first: 3, query: $q) {
                edges { node { id sku inventoryItem { id } product { title } } }
            }
        }
    """, {"q": f"sku:{sku}"})
    edges = data.get('data', {}).get('productVariants', {}).get('edges', [])
    for edge in edges:
        node = edge['node']
        if node['sku'] == sku:
            inv_id = (node.get('inventoryItem') or {}).get('id')
            return node['id'], inv_id, node['product']['title']
    return None, None, None


def update_variant_sku(inv_item_gid, new_sku, dry_run):
    """Update a variant's SKU via inventoryItemUpdate mutation."""
    if dry_run:
        return True, None
    if not inv_item_gid:
        return False, [{'message': 'No inventoryItem ID available for this variant'}]
    res = shopify_graphql(
        """mutation($id: ID!, $input: InventoryItemInput!) {
             inventoryItemUpdate(id: $id, input: $input) {
               inventoryItem { id sku }
               userErrors { field message }
             }
           }""",
        {"id": inv_item_gid, "input": {"sku": new_sku}}
    )
    errs = res.get('data', {}).get('inventoryItemUpdate', {}).get('userErrors', [])
    if res.get('errors'):
        errs += res['errors']
    return not errs, errs

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Migrate Shopify variant SKUs old→new')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    parser.add_argument('--csv',     default=None,        help='Path to inventory CSV')
    args = parser.parse_args()

    print('═' * 65)
    print('  Shopify SKU Migration — Old Scheme → New Scheme')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 65)

    if not SHOPIFY_SHOP_URL or not SHOPIFY_CLIENT_ID:
        print('\n✗ Shopify credentials not set in .env')
        sys.exit(1)

    # ── Load mapping ──────────────────────────────────────────────
    csv_path = args.csv or find_inventory_csv()
    if not csv_path or not os.path.exists(csv_path):
        print(f'\n✗ No inventory CSV found. Use --csv to specify one.')
        sys.exit(1)
    print(f'\n  CSV: {csv_path}')

    mappings = load_sku_mapping(csv_path)
    print(f'  Found {len(mappings)} old→new SKU pairs to process.\n')

    # ── For each mapping, look up old SKU on Shopify ──────────────
    to_migrate   = []  # (variant_gid, inv_item_gid, old_sku, new_sku, product_title, name)
    not_found    = []  # old SKUs not on Shopify
    already_new  = []  # new SKU already exists (migration already done?)

    print('  Scanning Shopify for old SKUs...')
    for i, (old_sku, new_sku, name) in enumerate(mappings):
        print(f'  [{i+1:3d}/{len(mappings)}] {old_sku} → {new_sku}  {name[:35]}', end='\r')

        # Check if old SKU still exists on Shopify
        v_gid, inv_gid, prod_title = find_variant_by_sku(old_sku)
        if v_gid:
            to_migrate.append((v_gid, inv_gid, old_sku, new_sku, prod_title, name))
        else:
            # Maybe the new SKU is already there?
            v_gid_new, _, _ = find_variant_by_sku(new_sku)
            if v_gid_new:
                already_new.append((old_sku, new_sku, name))
            else:
                not_found.append((old_sku, new_sku, name))

        time.sleep(0.15)  # gentle rate limit

    print(' ' * 80, end='\r')  # clear progress line

    # ── Report ────────────────────────────────────────────────────
    print(f'\n  Results after scan:')
    print(f'    Need migration   : {len(to_migrate)}')
    print(f'    Already migrated : {len(already_new)}')
    print(f'    Not on Shopify   : {len(not_found)}')

    if not to_migrate:
        print('\n  Nothing to migrate.')
        sys.exit(0)

    print(f'\n  SKUs to migrate ({len(to_migrate)}):')
    print(f'  {"OLD SKU":<22} {"NEW SKU":<22} {"Shopify Product":<35} {"Item"}')
    print(f'  {"-"*22} {"-"*22} {"-"*35} {"-"*30}')
    for v_gid, inv_gid, old_sku, new_sku, prod_title, name in to_migrate:
        print(f'  {old_sku:<22} {new_sku:<22} {prod_title[:35]:<35} {name[:30]}')

    if args.dry_run:
        print(f'\n  Dry run — no changes written. Remove --dry-run to proceed.')
        sys.exit(0)

    # ── Confirm ───────────────────────────────────────────────────
    print(f'\n  ⚠  About to update {len(to_migrate)} Shopify variant SKUs.')
    print('     This renames variant SKUs in Shopify to match the new Zoho scheme.')
    confirm = input('  Proceed? [y/N] ').strip().lower()
    if confirm != 'y':
        print('  Aborted.')
        sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────
    print(f'\n  Migrating SKUs...\n')
    success = 0
    failed  = 0

    for v_gid, inv_gid, old_sku, new_sku, prod_title, name in to_migrate:
        ok, errs = update_variant_sku(inv_gid, new_sku, dry_run=False)
        if ok:
            print(f'  ✓ {old_sku} → {new_sku}  ({prod_title[:40]})')
            success += 1
        else:
            print(f'  ✗ {old_sku} → {new_sku}  ERROR: {errs}')
            failed += 1
        time.sleep(0.3)

    print()
    print('═' * 65)
    print(f'  Done. Migrated: {success}   Failed: {failed}')
    if not_found:
        print(f'\n  {len(not_found)} old SKUs were not found on Shopify (may have been')
        print('  deleted or never synced). These are safe to ignore.')
    print()
    print('  Next steps:')
    print('  1. Run the audit again to confirm: python execution/audit_shopify_catalog.py')
    print('  2. Re-queue published items:       python execution/requeue_published.py --all')
    print('  3. Run enrichment:                 python execution/enrich_items.py')
    print('═' * 65)


if __name__ == '__main__':
    main()
