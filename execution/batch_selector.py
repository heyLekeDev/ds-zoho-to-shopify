#!/usr/bin/env python3
"""
Stage 1 — Batch Selector (live-truth edition)

Selects unprocessed items directly from LIVE Zoho — never from a CSV snapshot.
"Unprocessed" = active inventory item whose cf_shopify_status is blank
(computed as: all items − every item carrying any pipeline status, fetched via
the cf_shopify_status list filter, which is verified to work).

The final batch is additionally probed against LIVE Shopify by SKU so an
already-published item can never be re-selected (this replaces the stale
shopify_audit.csv exclusion that caused the 32-of-40-already-live incident).
Each write is guarded by a read-before-write status check.

Usage:
    python execution/batch_selector.py                        # default 25 items
    python execution/batch_selector.py --batch-size 40
    python execution/batch_selector.py --dry-run              # preview only
    python execution/batch_selector.py --yes                  # non-interactive
    python execution/batch_selector.py --no-shopify-check     # skip live probe
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

# ── Live Zoho selection ───────────────────────────────────────────────────────

# SKU prefixes excluded entirely (mirror of SKIP_PARENT_CATEGORIES)
SKIP_SKU_PREFIXES = {'560', '620'}

def _fetch_status_item_ids(token, status):
    """All item_ids currently carrying the given cf_shopify_status (paginated)."""
    ids, page = set(), 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID, 'cf_shopify_status': status,
                    'per_page': 200, 'page': page},
            timeout=20,
        )
        if resp.status_code == 429:
            time.sleep(60)
            continue
        data = resp.json()
        items = data.get('items', [])
        ids |= {it['item_id'] for it in items}
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return ids

def load_candidates_live(token):
    """
    Select candidates from LIVE Zoho (the source of truth):
    - not carrying any pipeline status (fetched live, per status, via the
      cf_shopify_status filter)
    - active, Inventory type, not an excluded category (by SKU prefix),
      not a spare part, priced ≥ ₦100
    Returns (candidates, skipped) — candidates shaped like the old CSV rows
    so downstream sorting/preview/write code is unchanged.
    """
    skipped = {
        'already_statusd': 0,
        'inactive':        0,
        'non_inventory':   0,
        'skip_category':   0,
        'spare_part':      0,
        'zero_price':      0,
    }

    print('  Fetching pipeline statuses from live Zoho...')
    statused = set()
    for status in sorted(SKIP_STATUSES):
        ids = _fetch_status_item_ids(token, status)
        if ids:
            print(f'    {status:<22} {len(ids)}')
        statused |= ids
        time.sleep(0.2)
    print(f'    → {len(statused)} items already in the pipeline')

    candidates = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID, 'per_page': 200, 'page': page},
            timeout=20,
        )
        if resp.status_code == 429:
            time.sleep(60)
            continue
        data = resp.json()
        items = data.get('items', [])
        for it in items:
            sku  = (it.get('sku') or '').strip()
            name = it.get('name', '') or ''

            if it['item_id'] in statused:
                skipped['already_statusd'] += 1
                continue
            if (it.get('status') or '').lower() != 'active':
                skipped['inactive'] += 1
                continue
            if (it.get('item_type') or '').lower() != 'inventory':
                skipped['non_inventory'] += 1
                continue
            prefix = sku.split('-')[0] if '-' in sku else ''
            if prefix in SKIP_SKU_PREFIXES:
                skipped['skip_category'] += 1
                continue
            if any(kw in name.lower() for kw in SKIP_NAME_KEYWORDS):
                skipped['spare_part'] += 1
                continue
            if (it.get('rate') or 0) < 100:
                skipped['zero_price'] += 1
                continue

            candidates.append({
                'Item ID':        it['item_id'],
                'Item Name':      name,
                'SKU':            sku,
                'CF.SKU - new':   sku,
                'Parent Category': f"{prefix}-{it.get('category_name', '')}" if prefix else (it.get('category_name', '') or ''),
                'Brand':          it.get('brand', '') or '',
            })
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1
        time.sleep(0.2)

    return candidates, skipped

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

# ── Zoho Write ────────────────────────────────────────────────────────────────

def update_item(item_id, item_name, sku, today_str, token, dry_run):
    """Write Queue for Enrichment status + sync fields to one Zoho item.
    Guarded: reads the live status first and refuses to overwrite a non-blank one."""
    if dry_run:
        return item_id, True, 'dry-run'

    # Read-before-write guard — never clobber an item that entered the
    # pipeline since selection ran (or that selection mis-identified).
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return update_item(item_id, item_name, sku, today_str, token, dry_run)
    detail = resp.json().get('item', {})
    cfs = {c['api_name']: c.get('value') for c in detail.get('custom_fields', [])}
    live_status = (cfs.get('cf_shopify_status') or '').strip()
    if live_status:
        return item_id, False, f'GUARD: live status is "{live_status}" — not overwriting'

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
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument('--dry-run',    action='store_true', help='Preview without writing to Zoho')
    parser.add_argument('--yes',        action='store_true',
                        help='Skip the confirmation prompt (required for non-interactive/scheduled runs)')
    parser.add_argument('--brands',     nargs='+', default=None,
                        help='Only select items matching these brands (case-insensitive)')
    parser.add_argument('--no-shopify-check', action='store_true',
                        help='Skip the live Shopify SKU probe on the final batch (not recommended)')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 1 — Batch Selector (live Zoho selection)')
    print('═' * 60)

    # ── Load & filter from LIVE Zoho ─────────────────────────────
    token = get_zoho_token()
    candidates, skipped = load_candidates_live(token)

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

    if not candidates:
        print('\n  Nothing to select. All items are already in the pipeline.')
        sys.exit(0)

    # ── Sort by priority ──────────────────────────────────────────
    candidates.sort(key=category_sort_key)

    # ── Take batch, probing LIVE Shopify so already-published items
    #    can never be re-selected ─────────────────────────────────
    today_str = date.today().isoformat()
    if args.no_shopify_check:
        batch = candidates[:args.batch_size]
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import shopify_variant_by_sku
        print(f'\n  Probing live Shopify for the top candidates...')
        batch, already_live = [], 0
        for row in candidates:
            if len(batch) >= args.batch_size:
                break
            sku = row.get('SKU', '')
            try:
                existing = shopify_variant_by_sku(sku) if sku else None
            except Exception as e:
                print(f'    ⚠ Probe failed for {sku} ({e}) — including item unprobed')
                existing = None
            if existing:
                already_live += 1
                prod_title = (existing.get('product') or {}).get('title', '?')
                print(f'    ⏭  {sku} already live on Shopify ("{prod_title[:45]}") — skipped')
                continue
            batch.append(row)
            time.sleep(0.2)
        if already_live:
            print(f'    → {already_live} already-live item(s) excluded '
                  f'(these need a Zoho status write-back — flag them for reconciliation)')

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
    if args.yes:
        print(f'  --yes supplied — writing "Queue for Enrichment" to {len(batch)} items.')
    elif not sys.stdin.isatty():
        print('  ✗ Non-interactive run without --yes. Re-run with --yes to confirm the batch.')
        sys.exit(2)
    else:
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
