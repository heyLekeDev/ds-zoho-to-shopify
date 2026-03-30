#!/usr/bin/env python3
"""
Stage 1 — Batch Selector
Reads the local inventory CSV, selects unprocessed items by priority,
and tags them as 'Queue for Enrichment' in Zoho.

Usage:
    python execution/batch_selector.py                        # default 50 items
    python execution/batch_selector.py --batch-size 25
    python execution/batch_selector.py --dry-run             # preview only, no writes
    python execution/batch_selector.py --csv "DS inventory Jan 31 26.csv"
"""

import os
import csv
import sys
import json
import time
import glob
import argparse
import requests
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID       = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID    = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET= os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN= os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE     = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE        = '.zoho_token.json'

DEFAULT_BATCH_SIZE = 25
MAX_WORKERS        = 5   # parallel Zoho writes

# Statuses that mean "already in the pipeline — skip"
SKIP_STATUSES = {
    'Queue for Enrichment', 'Enrichment Complete', 'Image required',
    'Image Validated', 'Queue for Upload', 'Published',
    'Needs Review', 'Error uploading', 'Ignore',
    'Update Required', 'To be Archived', 'Archived',
}

# Parent categories excluded entirely — not sellable on Shopify
SKIP_PARENT_CATEGORIES = {
    '560-Services',    # delivery fees, repair charges, call-out fees, spare rotors
    '620-Training',    # training courses
}

# Keywords in item name that flag non-sellable items (case-insensitive)
SKIP_NAME_KEYWORDS = [
    'spare part', 'spare rotor', 'spare ',  # spare parts in any category
]

# Category priority — lower number = higher priority
# Based on Parent Category column in CSV (prefix number used for sorting)
CATEGORY_PRIORITY = {
    '320': 1,   # Implantology
    '310': 2,   # Instruments / Osteotomes / Surgical
    '130': 2,   # Osteotomes (sub)
    '160': 3,   # Burs
    '140': 4,   # Biomaterials
    '260': 5,   # Endodontics
    '240': 6,   # Emergency
    '200': 7,   # Disposable Products
    '100': 8,   # Anaesthetics
    '180': 9,   # Cosmetics
    '360': 10,  # Janitorial
    '120': 11,  # Apparel
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except:
            pass
    resp = requests.post(
        'https://accounts.zoho.com/oauth/v2/token',
        params={
            'refresh_token': ZOHO_REFRESH_TOKEN,
            'client_id': ZOHO_CLIENT_ID,
            'client_secret': ZOHO_CLIENT_SECRET,
            'grant_type': 'refresh_token',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f"Zoho auth failed: {data}")
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token):
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'Content-Type': 'application/json',
    }

# ── CSV ───────────────────────────────────────────────────────────────────────

def find_inventory_csv():
    """Auto-detect the most recently modified inventory CSV in the project root."""
    candidates = glob.glob('DS inventory*.csv') + glob.glob('*.inventory*.csv')
    if not candidates:
        # fallback — any CSV that looks like inventory
        candidates = [f for f in glob.glob('*.csv')
                      if 'inventory' in f.lower() and 'sync' not in f.lower()
                      and 'rules' not in f.lower() and 'sku' not in f.lower()]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)

def category_sort_key(row):
    """Returns a (priority, category_name) tuple for sorting."""
    parent = row.get('Parent Category', '')
    # Extract numeric prefix, e.g. "320-Implantology" → "320"
    prefix = parent.split('-')[0].strip() if parent else '999'
    priority = CATEGORY_PRIORITY.get(prefix, 50)
    return (priority, parent)

def is_spare_part(row):
    """Return True if the item name contains a spare part keyword."""
    name = row.get('Item Name', '').lower()
    return any(kw in name for kw in SKIP_NAME_KEYWORDS)

def load_candidates(csv_path):
    """
    Read the CSV and return items eligible for enrichment:
    - Active status
    - Item Type = Inventory
    - CF.Shopify status is blank
    - Not in a skipped parent category
    - Not a spare part (by name keyword)
    """
    candidates = []
    skipped = {
        'already_statusd': 0,
        'inactive':        0,
        'non_inventory':   0,
        'skip_category':   0,
        'spare_part':      0,
        'zero_price':      0,
    }

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            shopify_status = row.get('CF.Shopify status', '').strip()
            item_status    = row.get('Status', '').strip()
            item_type      = row.get('Item Type', '').strip()
            parent_cat     = row.get('Parent Category', '').strip()

            # Skip inactive
            if item_status.lower() != 'active':
                skipped['inactive'] += 1
                continue

            # Skip non-inventory types
            if item_type.lower() != 'inventory':
                skipped['non_inventory'] += 1
                continue

            # Skip already-in-pipeline
            if shopify_status in SKIP_STATUSES:
                skipped['already_statusd'] += 1
                continue

            # Skip entire excluded categories (services, training, etc.)
            if parent_cat in SKIP_PARENT_CATEGORIES:
                skipped['skip_category'] += 1
                continue

            # Skip spare parts anywhere in the inventory
            if is_spare_part(row):
                skipped['spare_part'] += 1
                continue

            # Skip items priced under ₦100 — zero, placeholder (₦1), or nonsensical prices
            try:
                raw_price = row.get('Selling Price', '0') or '0'
                # Strip currency prefix (e.g. "NGN 29900.00" → "29900.00")
                raw_price = raw_price.replace(',', '')
                for prefix in ('NGN ', 'USD ', 'GBP ', '£', '$', '₦'):
                    raw_price = raw_price.replace(prefix, '')
                price = float(raw_price.strip())
            except ValueError:
                price = 0.0
            if price < 100:
                skipped['zero_price'] += 1
                continue

            candidates.append(row)

    return candidates, skipped

# ── Zoho Write ────────────────────────────────────────────────────────────────

def update_item(item_id, item_name, sku, today_str, token, dry_run):
    """Write Queue for Enrichment status + sync fields to one Zoho item."""
    if dry_run:
        return item_id, True, 'dry-run'

    payload = {
        'custom_fields': [
            {'api_name': 'cf_shopify_status',     'value': 'Queue for Enrichment'},
            {'api_name': 'cf_sync_result',         'value': f'Batch selected ({today_str})'},
            {'api_name': 'cf_shopify_sync_notes',  'value': (
                f'[BATCH] ({today_str})\n'
                f'Selected for enrichment.\n'
                f'Item: {item_name}\n'
                f'SKU: {sku}'
            )},
        ]
    }

    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        json=payload,
        timeout=15,
    )

    if resp.status_code == 429:
        time.sleep(60)
        resp = requests.put(
            f'{ZOHO_API_BASE}/items/{item_id}',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID},
            json=payload,
            timeout=15,
        )

    data = resp.json()
    ok = data.get('code') == 0
    msg = data.get('message', f'HTTP {resp.status_code}')
    return item_id, ok, msg

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Stage 1 — Batch Selector')
    parser.add_argument('--csv',        default=None, help='Path to inventory CSV')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument('--dry-run',    action='store_true', help='Preview without writing to Zoho')
    parser.add_argument('--brands',     nargs='+', default=None,
                        help='Only select items matching these brands (case-insensitive)')
    parser.add_argument('--exclude-shopify', default=None,
                        help='Path to shopify_audit.csv — skip SKUs already on Shopify')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 1 — Batch Selector')
    print('═' * 60)

    # ── Locate CSV ────────────────────────────────────────────────
    csv_path = args.csv or find_inventory_csv()
    if not csv_path or not os.path.exists(csv_path):
        print(f'\n✗ No inventory CSV found. Use --csv to specify one.')
        sys.exit(1)
    print(f'\n  CSV: {csv_path}')

    # ── Load & filter ─────────────────────────────────────────────
    candidates, skipped = load_candidates(csv_path)

    print(f'\n  Skipped:')
    print(f'    Already in pipeline : {skipped["already_statusd"]}')
    print(f'    Inactive items      : {skipped["inactive"]}')
    print(f'    Non-inventory type  : {skipped["non_inventory"]}')
    print(f'    Excluded categories : {skipped["skip_category"]}  (Services, Training)')
    print(f'    Spare parts         : {skipped["spare_part"]}')
    print(f'    Under ₦100          : {skipped["zero_price"]}  (zero, ₦1 placeholders, or nonsensical)')
    print(f'  Eligible candidates   : {len(candidates)}')

    # ── Brand filter ─────────────────────────────────────────────
    if args.brands:
        brand_set = {b.lower() for b in args.brands}
        before = len(candidates)
        candidates = [r for r in candidates
                      if r.get('Brand', '').strip().lower() in brand_set]
        print(f'\n  Brand filter: {args.brands}')
        print(f'    Before: {before}  →  After: {len(candidates)}')

    # ── Exclude SKUs already on Shopify ──────────────────────────
    if args.exclude_shopify:
        shopify_path = args.exclude_shopify
        if os.path.exists(shopify_path):
            shopify_skus = set()
            with open(shopify_path, newline='', encoding='utf-8-sig') as sf:
                for row in csv.DictReader(sf):
                    for sku in row.get('skus', '').split(', '):
                        sku = sku.strip()
                        if sku:
                            shopify_skus.add(sku)
            before = len(candidates)
            candidates = [r for r in candidates
                          if r.get('SKU', '').strip() not in shopify_skus]
            print(f'\n  Shopify exclusion ({len(shopify_skus)} live SKUs):')
            print(f'    Before: {before}  →  After: {len(candidates)}')
        else:
            print(f'\n  ⚠ Shopify audit file not found: {shopify_path}')

    if not candidates:
        print('\n  Nothing to select. All items are already in the pipeline.')
        sys.exit(0)

    # ── Sort by priority ──────────────────────────────────────────
    candidates.sort(key=category_sort_key)

    # ── Take batch ────────────────────────────────────────────────
    batch = candidates[:args.batch_size]
    today_str = date.today().isoformat()

    print(f'\n  Batch size  : {args.batch_size}')
    print(f'  Selected    : {len(batch)}')
    if args.dry_run:
        print(f'  Mode        : DRY RUN — no writes to Zoho')
    print()

    # ── Preview table ─────────────────────────────────────────────
    print(f'  {"#":<4} {"SKU":<18} {"Category":<30} {"Item Name"}')
    print(f'  {"-"*4} {"-"*18} {"-"*30} {"-"*35}')
    for i, row in enumerate(batch, 1):
        sku      = row.get('CF.SKU - new') or row.get('SKU', '')
        category = row.get('Parent Category', '')
        name     = row.get('Item Name', '')[:45]
        print(f'  {i:<4} {sku:<18} {category:<30} {name}')

    if args.dry_run:
        print(f'\n  Dry run complete. Run without --dry-run to write to Zoho.')
        sys.exit(0)

    # ── Confirm ───────────────────────────────────────────────────
    print()
    confirm = input(f'  Write "Queue for Enrichment" to these {len(batch)} items? [y/N] ').strip().lower()
    if confirm != 'y':
        print('  Aborted.')
        sys.exit(0)

    # ── Write to Zoho ─────────────────────────────────────────────
    print(f'\n  Writing to Zoho...')
    token = get_zoho_token()

    success_count = 0
    fail_count = 0

    tasks = [
        (row.get('Item ID', ''), row.get('Item Name', ''),
         row.get('CF.SKU - new') or row.get('SKU', ''))
        for row in batch
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(update_item, item_id, name, sku, today_str, token, False): (sku, name)
            for item_id, name, sku in tasks
        }
        for future in as_completed(futures):
            sku, name = futures[future]
            try:
                _, ok, msg = future.result()
                if ok:
                    success_count += 1
                    print(f'    ✓ {sku}  {name[:40]}')
                else:
                    fail_count += 1
                    print(f'    ✗ {sku}  {name[:40]}  → {msg}')
            except Exception as e:
                fail_count += 1
                print(f'    ✗ {sku}  → Exception: {e}')

    # ── Summary ───────────────────────────────────────────────────
    print()
    print('═' * 60)
    print(f'  Done. {success_count} tagged / {fail_count} failed')
    print(f'  → Review "Queue for Enrichment" view in Zoho before running Stage 2.')
    print('═' * 60)

if __name__ == '__main__':
    main()
