#!/usr/bin/env python3
"""
Re-Queue Published Items for Re-Processing

Takes items currently in "Published" status in Zoho and resets them back to
"Queue for Enrichment" so they can be re-enriched, re-imaged, and re-synced
to Shopify with improved data (better grouping, descriptions, images, tags).

Supports filtering by:
  --category    Parent category prefix (e.g. "320" for Implantology)
  --collection  Zoho collection name (e.g. "BICON INTEGRA CP")
  --sku         One specific SKU
  --all         All published items (requires explicit confirmation)

Usage:
    python execution/requeue_published.py --dry-run
    python execution/requeue_published.py --category 320 --dry-run
    python execution/requeue_published.py --collection "BICON INTEGRA CP"
    python execution/requeue_published.py --sku 320-120-001
    python execution/requeue_published.py --all
"""

import os, sys, csv, json, time, glob, argparse, requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
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
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token):
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'Content-Type': 'application/json',
    }

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


def load_published_from_csv(csv_path, category=None, collection=None, sku_filter=None):
    """
    Load all Published items from the CSV.
    Returns a list of dicts with keys: item_id, sku, name, category, collection
    """
    items = []
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            shopify_status = row.get('CF.Shopify status', '').strip()
            if shopify_status != 'Published':
                continue

            sku       = (row.get('CF.SKU - new') or row.get('SKU', '')).strip()
            item_id   = row.get('Item ID', '').strip()
            name      = row.get('Item Name', '').strip()
            parent_cat = row.get('Parent Category', '').strip()
            col_field  = (row.get('CF.Shopify Collection') or '').strip()

            # Filter by category prefix
            if category:
                prefix = parent_cat.split('-')[0].strip()
                if prefix != category:
                    continue

            # Filter by collection name
            if collection and col_field.lower() != collection.lower():
                continue

            # Filter by single SKU
            if sku_filter and sku != sku_filter:
                continue

            items.append({
                'item_id':    item_id,
                'sku':        sku,
                'name':       name,
                'category':   parent_cat,
                'collection': col_field,
            })
    return items

# ── Zoho write ────────────────────────────────────────────────────────────────

def reset_item(item_id, sku, name, today_str, token, dry_run):
    """Reset one item to Queue for Enrichment and clear enrichment fields."""
    if dry_run:
        return True, 'dry-run'

    payload = {
        'custom_fields': [
            {'api_name': 'cf_shopify_status',     'value': 'Queue for Enrichment'},
            {'api_name': 'cf_sync_result',         'value': f'Re-queued for enrichment ({today_str})'},
            {'api_name': 'cf_shopify_sync_notes',  'value': (
                f'[RE-QUEUE] ({today_str})\n'
                f'Reset from Published → Queue for Enrichment.\n'
                f'Reason: Manual re-processing for improved grouping, images, or metadata.\n'
                f'SKU: {sku}  |  Name: {name}'
            )},
            # Clear enrichment fields so Stage 2 generates fresh values
            {'api_name': 'cf_enriched_title',       'value': ''},
            {'api_name': 'cf_shopify_collection',   'value': ''},
            {'api_name': 'cf_shopify_product_type', 'value': ''},
            {'api_name': 'cf_shopify_tags',         'value': ''},
            {'api_name': 'cf_shopify_var_1_name',   'value': ''},
            {'api_name': 'cf_shopify_var_1_value',  'value': ''},
            {'api_name': 'cf_shopify_var_2_name',   'value': ''},
            {'api_name': 'cf_shopify_var_2_value',  'value': ''},
            {'api_name': 'cf_description_html',     'value': ''},
        ]
    }

    for attempt in range(2):
        resp = requests.put(
            f'{ZOHO_API_BASE}/items/{item_id}',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID},
            json=payload,
            timeout=15,
        )
        if resp.status_code == 429:
            print('  [RATE LIMIT] Waiting 60s...')
            time.sleep(60)
            continue
        data = resp.json()
        ok  = data.get('code') == 0
        msg = data.get('message', f'HTTP {resp.status_code}')
        return ok, msg

    return False, 'Too many retries'

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Re-Queue Published Items for Re-Processing')
    parser.add_argument('--dry-run',    action='store_true', help='Preview without writing to Zoho')
    parser.add_argument('--all',        action='store_true', help='Process ALL published items')
    parser.add_argument('--category',   default=None, help='Filter by category prefix (e.g. "320")')
    parser.add_argument('--collection', default=None, help='Filter by collection name')
    parser.add_argument('--sku',        default=None, help='Process a single SKU')
    parser.add_argument('--csv',        default=None, help='Path to inventory CSV')
    args = parser.parse_args()

    # Must specify at least one filter or --all
    if not any([args.all, args.category, args.collection, args.sku]):
        print('\n✗ Specify a filter: --category, --collection, --sku, or --all')
        print('  Use --dry-run first to preview the selection.')
        sys.exit(1)

    print('═' * 60)
    print('  Re-Queue Published Items')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    # ── Locate CSV ────────────────────────────────────────────────
    csv_path = args.csv or find_inventory_csv()
    if not csv_path or not os.path.exists(csv_path):
        print(f'\n✗ No inventory CSV found. Use --csv to specify one.')
        sys.exit(1)
    print(f'\n  CSV: {csv_path}')

    # ── Load items ────────────────────────────────────────────────
    items = load_published_from_csv(
        csv_path,
        category   = args.category,
        collection = args.collection,
        sku_filter = args.sku,
    )

    if not items:
        print('\n  No Published items match the specified filters.')
        sys.exit(0)

    print(f'\n  Found {len(items)} Published item(s) to re-queue:\n')
    print(f'  {"SKU":<20} {"Category":<30} {"Collection":<25} {"Name"}')
    print(f'  {"-"*20} {"-"*30} {"-"*25} {"-"*30}')
    for it in items:
        print(f'  {it["sku"]:<20} {it["category"]:<30} {it["collection"]:<25} {it["name"][:35]}')

    if args.dry_run:
        print(f'\n  Dry run — no changes written. Remove --dry-run to proceed.')
        sys.exit(0)

    # ── Confirm ───────────────────────────────────────────────────
    print(f'\n  ⚠  This will reset {len(items)} items from "Published" to "Queue for Enrichment".')
    print('     Enrichment fields (title, collection, variants, description, tags) will be CLEARED.')
    print('     The items will re-enter the enrichment pipeline from the beginning.\n')
    confirm = input(f'  Proceed? [y/N] ').strip().lower()
    if confirm != 'y':
        print('  Aborted.')
        sys.exit(0)

    # ── Write to Zoho ─────────────────────────────────────────────
    print(f'\n  Writing to Zoho...\n')
    token      = get_zoho_token()
    today_str  = date.today().isoformat()
    success    = 0
    failed     = 0

    for it in items:
        ok, msg = reset_item(it['item_id'], it['sku'], it['name'], today_str, token, dry_run=False)
        if ok:
            print(f'  ✓ {it["sku"]}  {it["name"][:45]}')
            success += 1
        else:
            print(f'  ✗ {it["sku"]}  → {msg}')
            failed += 1
        time.sleep(0.3)

    print()
    print('═' * 60)
    print(f'  Done. Reset: {success}   Failed: {failed}')
    print()
    print('  Next steps:')
    print('  1. Review "Queue for Enrichment" view in Zoho')
    print('  2. Run:  python execution/enrich_items.py')
    print('  3. Run:  python execution/validate_images.py')
    print('  4. Run:  python execution/validate_enrichment.py')
    print('  5. ⚠  Before syncing, run pre_migrate_collections.py')
    print('       (removes old standalone Shopify products being merged into groups)')
    print('  6. Run:  python execution/sync_zoho_to_shopify.py')
    print('═' * 60)


if __name__ == '__main__':
    main()
