#!/usr/bin/env python3
"""
Pre-Migration: Remove Standalone Shopify Products Being Merged into Collections

When items that were previously published as STANDALONE Shopify products are
re-enriched and assigned to a COLLECTION (group), the sync script will encounter
a SKU Collision (the SKU exists under a different product title on Shopify).

This script resolves that by:
  1. Reading items in "Queue for Upload" status in Zoho (or from a provided list)
  2. For each item with a cf_shopify_collection set, querying Shopify by SKU
  3. If the SKU currently belongs to a product with a DIFFERENT title than the
     target collection:
       - If that old product has only 1 variant → DELETE the old product entirely
       - If that old product has multiple variants → DELETE just that variant
  4. After deletion, the normal sync can create/add the SKU to the correct
     collection product without a collision.

Run this BEFORE sync_zoho_to_shopify.py whenever you have items that are moving
from standalone products to collection groups.

Usage:
    python execution/pre_migrate_collections.py --dry-run   # preview only
    python execution/pre_migrate_collections.py             # live execution
"""

import os, sys, json, time, argparse, requests
from dotenv import load_dotenv

load_dotenv()

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
ZOHO_TOKEN_FILE    = '.zoho_token.json'

SHOPIFY_SHOP_URL      = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID     = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION   = '2025-01'

_shopify_token = None

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

# ── Zoho Auth ─────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(ZOHO_TOKEN_FILE):
        try:
            with open(ZOHO_TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except Exception:
            pass
    resp = requests.post(
        'https://accounts.zoho.com/oauth/v2/token',
        params={
            'refresh_token': ZOHO_REFRESH_TOKEN,
            'client_id':     ZOHO_CLIENT_ID,
            'client_secret': ZOHO_CLIENT_SECRET,
            'grant_type':    'refresh_token',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(ZOHO_TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token):
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'Content-Type': 'application/json',
    }

def fetch_queue_for_upload(token):
    """Fetch all items in 'Queue for Upload' status from Zoho."""
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={
                'organization_id':   ZOHO_ORG_ID,
                'cf_shopify_status': 'Queue for Upload',
                'per_page':          200,
                'page':              page,
            },
            timeout=15,
        )
        if resp.status_code == 429:
            print('  [RATE LIMIT] Waiting 60s...')
            time.sleep(60)
            continue
        data = resp.json()
        items = data.get('items', [])
        if not items:
            break
        all_items.extend(items)
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return all_items

# ── Shopify GraphQL ────────────────────────────────────────────────────────────

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
            print('  [RATE LIMIT] Waiting 10s...')
            time.sleep(10)
            continue
        return resp.json()
    raise Exception("Shopify GraphQL: too many retries")


def find_shopify_variant_by_sku(sku):
    """Returns the variant node (id, sku, product.id, product.title, product.variantCount)."""
    data = shopify_graphql("""
        query($q: String!) {
            productVariants(first: 5, query: $q) {
                edges {
                    node {
                        id
                        sku
                        product {
                            id
                            title
                            variants(first: 1) { edges { node { id } } }
                            totalVariants
                        }
                    }
                }
            }
        }
    """, {"q": f"sku:{sku}"})
    edges = data.get('data', {}).get('productVariants', {}).get('edges', [])
    return edges[0]['node'] if edges else None


def delete_shopify_product(product_gid, dry_run):
    """Delete an entire Shopify product."""
    if dry_run:
        return True
    res = shopify_graphql(
        "mutation($id: ID!) { productDelete(input: {id: $id}) { deletedProductId userErrors { message } } }",
        {"id": product_gid}
    )
    errs = res.get('data', {}).get('productDelete', {}).get('userErrors', [])
    return not errs


def delete_shopify_variant(product_gid, variant_gid, dry_run):
    """Delete a single variant from a Shopify product."""
    if dry_run:
        return True
    res = shopify_graphql(
        "mutation($pId: ID!, $vIds: [ID!]!) { productVariantsBulkDelete(productId: $pId, variantIds: $vIds) { userErrors { message } } }",
        {"pId": product_gid, "vIds": [variant_gid]}
    )
    errs = res.get('data', {}).get('productVariantsBulkDelete', {}).get('userErrors', [])
    return not errs

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Pre-Migration: Remove standalone Shopify products being merged into collections')
    parser.add_argument('--dry-run', action='store_true', help='Preview without deleting')
    args = parser.parse_args()

    print('═' * 70)
    print('  Pre-Migration: Standalone → Collection Cleanup')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 70)

    if not SHOPIFY_SHOP_URL or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        print('\n✗ SHOPIFY_SHOP_URL, SHOPIFY_CLIENT_ID, or SHOPIFY_CLIENT_SECRET not set in .env')
        sys.exit(1)

    # ── 1. Fetch Queue for Upload items from Zoho ─────────────────────────────
    print('\n  Fetching "Queue for Upload" items from Zoho...')
    token = get_zoho_token()
    zoho_items = fetch_queue_for_upload(token)
    print(f'  Found {len(zoho_items)} items.')

    if not zoho_items:
        print('  Nothing to do — no items in "Queue for Upload".')
        sys.exit(0)

    # ── 2. Find items being moved to a collection ─────────────────────────────
    # These are items with a cf_shopify_collection value set.
    # For standalone items (no collection), no migration needed.
    migration_candidates = []
    for item in zoho_items:
        sku        = item.get('sku', '')
        collection = item.get('cf_shopify_collection', '').strip()
        name       = item.get('name', '')
        if collection:  # only grouped items need migration checks
            migration_candidates.append({'sku': sku, 'name': name, 'target_collection': collection})

    if not migration_candidates:
        print('\n  No items with a target collection found — no pre-migration needed.')
        sys.exit(0)

    print(f'\n  {len(migration_candidates)} items have a target collection.')
    print('  Checking Shopify for existing products to migrate...\n')

    # ── 3. For each candidate, check if SKU is on a different Shopify product ─
    to_delete_product  = []  # (product_gid, product_title, sku, target_collection) — whole product
    to_delete_variant  = []  # (product_gid, variant_gid, product_title, sku, target_collection) — one variant
    already_correct    = []  # SKUs already in the right product
    not_on_shopify     = []  # SKUs not yet on Shopify

    for cand in migration_candidates:
        sku              = cand['sku']
        target_col       = cand['target_collection']
        node             = find_shopify_variant_by_sku(sku)
        time.sleep(0.2)  # gentle rate limit

        if not node:
            not_on_shopify.append(sku)
            continue

        current_title    = node['product']['title']
        product_gid      = node['product']['id']
        variant_gid      = node['id']
        total_variants   = node['product'].get('totalVariants', 1)

        if current_title == target_col:
            already_correct.append(sku)
            continue

        # This SKU needs to MOVE from current_title → target_col
        if total_variants <= 1:
            to_delete_product.append((product_gid, current_title, sku, target_col))
        else:
            to_delete_variant.append((product_gid, variant_gid, current_title, sku, target_col))

    # ── 4. Report findings ────────────────────────────────────────────────────
    print(f'  Already in correct product : {len(already_correct)}')
    print(f'  Not yet on Shopify         : {len(not_on_shopify)}')
    print(f'  Products to delete (whole) : {len(to_delete_product)}')
    print(f'  Variants to remove only    : {len(to_delete_variant)}')

    if to_delete_product:
        print('\n  WHOLE PRODUCTS to delete (last variant → whole product goes):')
        for gid, title, sku, target in to_delete_product:
            print(f'    [{sku}]  "{title}"  →  will join  "{target}"')

    if to_delete_variant:
        print('\n  VARIANTS to remove (product stays, just this variant leaves):')
        for gid, v_gid, title, sku, target in to_delete_variant:
            print(f'    [{sku}]  from  "{title}"  →  will join  "{target}"')

    if not to_delete_product and not to_delete_variant:
        print('\n  Nothing needs to be migrated. Proceed directly to sync.')
        sys.exit(0)

    if args.dry_run:
        print('\n  Dry run — no deletions performed.')
        print('  Remove --dry-run to execute the migration.')
        sys.exit(0)

    # ── 5. Confirm ────────────────────────────────────────────────────────────
    total_actions = len(to_delete_product) + len(to_delete_variant)
    print(f'\n  ⚠  About to perform {total_actions} deletion(s) on Shopify.')
    print('     This is IRREVERSIBLE. Run --dry-run first if you have not already.')
    confirm = input('  Proceed? [y/N] ').strip().lower()
    if confirm != 'y':
        print('  Aborted.')
        sys.exit(0)

    # ── 6. Execute deletions ──────────────────────────────────────────────────
    print()
    deleted = 0
    errors  = 0

    for product_gid, title, sku, target in to_delete_product:
        print(f'  Deleting product "{title}" (SKU: {sku})...', end=' ')
        ok = delete_shopify_product(product_gid, dry_run=False)
        if ok:
            print('✓')
            deleted += 1
        else:
            print('✗ FAILED')
            errors += 1
        time.sleep(0.5)

    for product_gid, variant_gid, title, sku, target in to_delete_variant:
        print(f'  Removing variant SKU {sku} from "{title}"...', end=' ')
        ok = delete_shopify_variant(product_gid, variant_gid, dry_run=False)
        if ok:
            print('✓')
            deleted += 1
        else:
            print('✗ FAILED')
            errors += 1
        time.sleep(0.5)

    print()
    print('═' * 70)
    print(f'  Done. Deleted/removed: {deleted}   Errors: {errors}')
    print()
    print('  Next step: Run the sync to create properly grouped products.')
    print('     python execution/sync_zoho_to_shopify.py')
    print('═' * 70)


if __name__ == '__main__':
    main()
