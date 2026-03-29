#!/usr/bin/env python3
"""
patch_missing_descriptions.py — Push descriptions to Shopify products that have none.

Reads from the local Zoho inventory CSV (Sales Description field) and the
shopify_audit.csv to identify live Shopify products with no description.
For each one, finds the Zoho sales description, formats it as HTML, and
pushes it to Shopify via the Admin API.

Products with no description AND no Zoho sales description are reported
but skipped (require manual copy).

Usage:
    python execution/patch_missing_descriptions.py --dry-run
    python execution/patch_missing_descriptions.py
"""

import os, sys, csv, json, time, argparse, requests
from dotenv import load_dotenv

load_dotenv('.env')

SHOPIFY_SHOP_URL    = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID   = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION = '2025-01'

INVENTORY_CSV       = 'DS inventory Jan 31 26.csv'
SHOPIFY_AUDIT_CSV   = 'shopify_audit.csv'

_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token:
        return _shopify_token
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token',
        json={'client_id': SHOPIFY_CLIENT_ID,
              'client_secret': SHOPIFY_CLIENT_SECRET,
              'grant_type': 'client_credentials'},
        timeout=10)
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_token = data['access_token']
    return _shopify_token

def shopify_graphql(query, variables=None):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    headers = {'X-Shopify-Access-Token': get_shopify_token(),
               'Content-Type': 'application/json'}
    for attempt in range(3):
        resp = requests.post(url, headers=headers,
                             json={'query': query, 'variables': variables or {}},
                             timeout=30)
        if resp.status_code == 429:
            print('  [RATE LIMIT] Waiting 10s...')
            time.sleep(10)
            continue
        return resp.json()
    raise Exception('Shopify GraphQL: too many retries')

# ── Load inventory ────────────────────────────────────────────────────────────

def load_inventory():
    inv = {}
    with open(INVENTORY_CSV) as f:
        for row in csv.DictReader(f):
            for sku_field in ('SKU', 'CF.SKU - new'):
                sku = row.get(sku_field, '').strip()
                if sku and sku not in inv:
                    inv[sku] = {
                        'name': row['Item Name'],
                        'brand': row.get('Brand', ''),
                        'category': row.get('Category Name', ''),
                        'sales_desc': row.get('Sales Description', '').strip(),
                        'price': row.get('Selling Price', ''),
                    }
    return inv

# ── Find Shopify product ID by handle ────────────────────────────────────────

def get_product_id_by_handle(handle):
    q = '''
    query($handle: String!) {
      productByHandle(handle: $handle) { id title descriptionHtml }
    }'''
    result = shopify_graphql(q, {'handle': handle})
    p = result.get('data', {}).get('productByHandle')
    return p

# ── Update product description ────────────────────────────────────────────────

def update_description(product_id, description_html):
    mutation = '''
    mutation($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id title }
        userErrors { field message }
      }
    }'''
    result = shopify_graphql(mutation, {
        'input': {'id': product_id, 'descriptionHtml': description_html}
    })
    errors = result.get('data', {}).get('productUpdate', {}).get('userErrors', [])
    return errors

def make_html(sales_desc, name, brand, category):
    """Wrap Zoho sales description in simple HTML paragraph."""
    if not sales_desc:
        return None
    text = sales_desc.strip()
    return f'<p>{text}</p>'

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    print('Loading inventory...')
    inventory = load_inventory()

    print('Loading Shopify audit...')
    targets = []
    with open(SHOPIFY_AUDIT_CSV) as f:
        for row in csv.DictReader(f):
            has_desc = row.get('has_description', 'True')
            issues = row.get('issues', '')
            if has_desc == 'False' or 'No description' in issues:
                skus = [s.strip() for s in row.get('skus', '').split(',') if s.strip()]
                targets.append({
                    'title': row['shopify_title'],
                    'handle': row['shopify_handle'],
                    'skus': skus,
                })

    print(f'Found {len(targets)} products needing descriptions.\n')

    patched = []
    skipped_no_copy = []
    skipped_no_sku  = []

    for p in targets:
        # Find a Zoho description from any matching SKU
        desc_text = None
        zoho_item = None
        for sku in p['skus']:
            item = inventory.get(sku)
            if item and item['sales_desc']:
                desc_text = item['sales_desc']
                zoho_item = item
                break

        if not desc_text:
            if p['skus']:
                skipped_no_copy.append(p)
            else:
                skipped_no_sku.append(p)
            continue

        desc_html = make_html(desc_text, zoho_item['name'],
                              zoho_item['brand'], zoho_item['category'])

        print(f'  → {p["title"]}')
        print(f'     Handle:  /{p["handle"]}')
        print(f'     Copy:    {desc_text[:80]}...' if len(desc_text) > 80 else f'     Copy:    {desc_text}')

        if args.dry_run:
            print(f'     [DRY RUN — skipping write]')
            patched.append(p)
            print()
            continue

        # Get Shopify product ID
        shopify_product = get_product_id_by_handle(p['handle'])
        if not shopify_product:
            print(f'     ✗ Product not found on Shopify')
            skipped_no_copy.append(p)
            continue

        if shopify_product.get('descriptionHtml', '').strip():
            print(f'     ✓ Already has description — skipping')
            continue

        errors = update_description(shopify_product['id'], desc_html)
        if errors:
            print(f'     ✗ Error: {errors}')
        else:
            print(f'     ✓ Updated')
            patched.append(p)

        print()
        time.sleep(0.5)

    # ── Summary ───────────────────────────────────────────────────────────────
    print('=' * 60)
    print(f'SUMMARY{"  [DRY RUN]" if args.dry_run else ""}')
    print('=' * 60)
    print(f'  Updated:              {len(patched)}')
    print(f'  Skipped (no Zoho copy): {len(skipped_no_copy)}')
    print(f'  Skipped (no SKU):     {len(skipped_no_sku)}')

    if skipped_no_copy:
        print('\n  Need manual copy written:')
        for p in skipped_no_copy:
            print(f'    • {p["title"]} (/{p["handle"]})')

    if skipped_no_sku:
        print('\n  No SKUs — probably ghost/training products:')
        for p in skipped_no_sku:
            print(f'    • {p["title"]} (/{p["handle"]})')

if __name__ == '__main__':
    main()
