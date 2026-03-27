#!/usr/bin/env python3
"""
Shopify Catalog Audit

Fetches ALL published products from Shopify, cross-references them against the
local Zoho inventory CSV, and produces a prioritised report highlighting:

  • Products that are standalone but should be grouped as collection variants
  • Products missing images
  • Products whose title doesn't match the expected enriched/collection name
  • Products with no description

Usage:
    python execution/audit_shopify_catalog.py
    python execution/audit_shopify_catalog.py --csv "DS inventory Jan 31 26.csv"
    python execution/audit_shopify_catalog.py --output shopify_audit.csv
"""

import os, sys, csv, json, time, glob, argparse, requests
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_SHOP_URL     = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID    = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET= os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION  = '2025-01'

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
            print("  [RATE LIMIT] Waiting 10s...")
            time.sleep(10)
            continue
        return resp.json()
    raise Exception("Shopify GraphQL: too many retries")


def fetch_all_shopify_products():
    """Paginate through all Shopify products and return a flat list."""
    all_products = []
    cursor = None

    query = """
    query($cursor: String) {
      products(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            title
            handle
            status
            descriptionHtml
            productType
            vendor
            tags
            images(first: 1) { edges { node { url } } }
            variants(first: 100) {
              edges {
                node {
                  id
                  sku
                  price
                  title
                }
              }
            }
          }
        }
      }
    }
    """

    page = 1
    while True:
        print(f"  Fetching page {page}...", end='\r')
        data = shopify_graphql(query, {"cursor": cursor})
        products_data = data.get('data', {}).get('products', {})
        edges = products_data.get('edges', [])

        for edge in edges:
            node = edge['node']
            variants = [v['node'] for v in node.get('variants', {}).get('edges', [])]
            images   = node.get('images', {}).get('edges', [])
            all_products.append({
                'id':           node['id'],
                'title':        node['title'],
                'handle':       node['handle'],
                'status':       node['status'],
                'description':  (node.get('descriptionHtml') or '').strip(),
                'product_type': node.get('productType', ''),
                'vendor':       node.get('vendor', ''),
                'tags':         node.get('tags', []),
                'has_image':    len(images) > 0,
                'image_url':    images[0]['node']['url'] if images else '',
                'variant_count': len(variants),
                'skus':         [v['sku'] for v in variants if v.get('sku')],
                'variants':     variants,
            })

        page_info = products_data.get('pageInfo', {})
        if not page_info.get('hasNextPage'):
            break
        cursor = page_info['endCursor']
        page += 1

    print(f"  Fetched {len(all_products)} products from Shopify.        ")
    return all_products


# ── CSV cross-reference ────────────────────────────────────────────────────────

def find_inventory_csv():
    candidates = glob.glob('DS inventory*.csv') + glob.glob('*.inventory*.csv')
    if not candidates:
        candidates = [f for f in glob.glob('*.csv')
                      if 'inventory' in f.lower() and 'sync' not in f.lower()
                      and 'rules' not in f.lower() and 'sku' not in f.lower()]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_csv_by_sku(csv_path):
    """Returns dict: sku → row (all CSV columns)."""
    by_sku = {}
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get('CF.SKU - new') or row.get('SKU', '')).strip()
            if sku:
                by_sku[sku] = row
    return by_sku


# ── Analysis ──────────────────────────────────────────────────────────────────

ISSUE_STANDALONE_SHOULD_GROUP  = 'Should be grouped (has siblings with same collection)'
ISSUE_MISSING_IMAGE            = 'No product image'
ISSUE_NO_DESCRIPTION           = 'No description'
ISSUE_TITLE_MISMATCH           = 'Product title ≠ expected collection name'
ISSUE_UNKNOWN_SKU              = 'SKU not found in inventory CSV'
ISSUE_MULTIPLE_COLLECTIONS     = 'Variants belong to different expected collections'


def analyse(shopify_products, sku_csv):
    """
    For each Shopify product, determine what issues it has.
    Returns a list of dicts suitable for report output.
    """
    # Build a map: expected_collection → set of SKUs (from CSV)
    collection_skus = defaultdict(set)   # collection_name → {sku, ...}
    sku_collection  = {}                 # sku → collection_name
    for sku, row in sku_csv.items():
        col = (row.get('CF.Shopify Collection') or '').strip()
        if col:
            collection_skus[col].add(sku)
            sku_collection[sku] = col

    # Build a reverse map: sku → shopify product title
    shopify_sku_to_product = {}
    for prod in shopify_products:
        for sku in prod['skus']:
            shopify_sku_to_product[sku] = prod['title']

    rows = []
    for prod in shopify_products:
        issues = []

        # Image check
        if not prod['has_image']:
            issues.append(ISSUE_MISSING_IMAGE)

        # Description check
        if not prod['description']:
            issues.append(ISSUE_NO_DESCRIPTION)

        # SKU cross-reference
        prod_skus = prod['skus']
        if not prod_skus:
            # No SKUs on product — unusual
            issues.append('No SKUs on product')
            rows.append({
                'shopify_title':       prod['title'],
                'shopify_handle':      prod['handle'],
                'shopify_status':      prod['status'],
                'variant_count':       prod['variant_count'],
                'skus':                '',
                'expected_collection': '',
                'has_image':           prod['has_image'],
                'has_description':     bool(prod['description']),
                'issues':              '; '.join(issues) or 'OK',
                'product_type':        prod['product_type'],
                'vendor':              prod['vendor'],
            })
            continue

        # Determine expected collections for this product's SKUs
        expected_collections = set()
        unknown_skus = []
        for sku in prod_skus:
            if sku not in sku_csv:
                unknown_skus.append(sku)
            else:
                col = sku_collection.get(sku, '')
                expected_collections.add(col or '(no collection)')

        if unknown_skus:
            issues.append(f"{ISSUE_UNKNOWN_SKU}: {', '.join(unknown_skus)}")

        # Check if all variants agree on a single collection
        if len(expected_collections) > 1:
            issues.append(f"{ISSUE_MULTIPLE_COLLECTIONS}: {', '.join(expected_collections)}")
            best_collection = ', '.join(sorted(expected_collections))
        else:
            best_collection = list(expected_collections)[0] if expected_collections else ''

        # Check title vs expected collection
        if best_collection and best_collection != '(no collection)':
            if prod['title'] != best_collection:
                issues.append(f"{ISSUE_TITLE_MISMATCH}: expected '{best_collection}'")

            # Check if siblings (same collection) are in DIFFERENT Shopify products
            siblings = collection_skus.get(best_collection, set())
            sibling_products = set()
            for s_sku in siblings:
                s_prod_title = shopify_sku_to_product.get(s_sku)
                if s_prod_title:
                    sibling_products.add(s_prod_title)
            if len(sibling_products) > 1:
                issues.append(
                    f"{ISSUE_STANDALONE_SHOULD_GROUP}: {len(siblings)} SKUs across "
                    f"{len(sibling_products)} Shopify products"
                )

        rows.append({
            'shopify_title':       prod['title'],
            'shopify_handle':      prod['handle'],
            'shopify_status':      prod['status'],
            'variant_count':       prod['variant_count'],
            'skus':                ', '.join(prod['skus']),
            'expected_collection': best_collection,
            'has_image':           prod['has_image'],
            'has_description':     bool(prod['description']),
            'issues':              '; '.join(issues) or 'OK',
            'product_type':        prod['product_type'],
            'vendor':              prod['vendor'],
        })

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Shopify Catalog Audit')
    parser.add_argument('--csv',    default=None, help='Path to inventory CSV')
    parser.add_argument('--output', default='shopify_audit.csv',
                        help='Output CSV path (default: shopify_audit.csv)')
    args = parser.parse_args()

    print('═' * 70)
    print('  Shopify Catalog Audit')
    print('═' * 70)

    if not SHOPIFY_SHOP_URL or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        print('\n✗ SHOPIFY_SHOP_URL, SHOPIFY_CLIENT_ID, or SHOPIFY_CLIENT_SECRET not set in .env')
        sys.exit(1)

    # ── 1. Fetch Shopify products ─────────────────────────────────────────────
    print('\n  Step 1: Fetching all Shopify products...')
    shopify_products = fetch_all_shopify_products()

    # ── 2. Load inventory CSV for cross-reference ─────────────────────────────
    csv_path = args.csv or find_inventory_csv()
    sku_csv  = {}
    if csv_path and os.path.exists(csv_path):
        print(f'\n  Step 2: Loading CSV: {csv_path}')
        sku_csv = load_csv_by_sku(csv_path)
        print(f'  Loaded {len(sku_csv)} SKUs from CSV.')
    else:
        print('\n  Step 2: No inventory CSV found — skipping cross-reference.')

    # ── 3. Analyse ────────────────────────────────────────────────────────────
    print('\n  Step 3: Analysing...')
    report_rows = analyse(shopify_products, sku_csv)

    # ── 4. Print summary ──────────────────────────────────────────────────────
    ok_count    = sum(1 for r in report_rows if r['issues'] == 'OK')
    issue_count = len(report_rows) - ok_count

    issue_types = defaultdict(int)
    for r in report_rows:
        if r['issues'] != 'OK':
            for issue in r['issues'].split('; '):
                # Shorten for grouping
                key = issue.split(':')[0].strip()
                issue_types[key] += 1

    print()
    print('═' * 70)
    print('  SUMMARY')
    print('═' * 70)
    print(f'  Total Shopify products : {len(shopify_products)}')
    print(f'  Products with issues   : {issue_count}')
    print(f'  Products OK            : {ok_count}')
    print()
    print('  Issue breakdown:')
    for issue, count in sorted(issue_types.items(), key=lambda x: -x[1]):
        print(f'    {count:4d}  {issue}')

    # ── 5. Print issues table ─────────────────────────────────────────────────
    print()
    print('═' * 70)
    print('  ISSUES DETAIL')
    print('═' * 70)

    issue_rows = [r for r in report_rows if r['issues'] != 'OK']
    issue_rows.sort(key=lambda r: (
        # Sort by severity: grouping issues first, then image, then other
        0 if ISSUE_STANDALONE_SHOULD_GROUP in r['issues'] else
        1 if ISSUE_MISSING_IMAGE in r['issues'] else
        2 if ISSUE_TITLE_MISMATCH in r['issues'] else 3,
        r['shopify_title']
    ))

    for r in issue_rows:
        title = r['shopify_title'][:50]
        img   = '✓' if r['has_image'] else '✗'
        desc  = '✓' if r['has_description'] else '✗'
        print(f'\n  [{img} img] [{desc} desc]  {title}')
        print(f'    SKUs: {r["skus"][:80]}')
        if r['expected_collection']:
            print(f'    Expected collection: {r["expected_collection"]}')
        for issue_line in r['issues'].split('; '):
            print(f'    ⚠  {issue_line}')

    # ── 6. Write output CSV ───────────────────────────────────────────────────
    fieldnames = [
        'shopify_title', 'shopify_handle', 'shopify_status', 'variant_count',
        'skus', 'expected_collection', 'has_image', 'has_description',
        'issues', 'product_type', 'vendor',
    ]
    with open(args.output, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    print()
    print('═' * 70)
    print(f'  Report written to: {args.output}')
    print('═' * 70)


if __name__ == '__main__':
    main()
