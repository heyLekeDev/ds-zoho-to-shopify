#!/usr/bin/env python3
"""
fix_sku_collisions.py — Resolve CF.SKU-new collisions in Zoho Inventory

Audit found 4 new-SKU collisions where 2 different items share the same
CF.SKU - new value (all in the 180-130-xxx range). This script:

  1. Identifies which item in each collision pair has the WRONG new SKU
     (the item whose parent category doesn't match the SKU prefix)
  2. Calculates the correct new SKU for that item
  3. Updates CF.SKU - new in Zoho for the affected items
  4. Writes a report of all changes made

Collisions found:
  180-130-001: 320-130-001 (Osteotome)  ←correct  vs  200-190-012 (Bib Holder) ←wrong
  180-130-002: 160-120-001 (Carbide Bur) ←correct vs  200-190-013 (Bib Holder) ←wrong
  180-130-003: 260-150-007 (Reamer)     ←correct  vs  200-190-014 (Bib Holder) ←wrong
  180-130-004: 260-150-010 (Reamer)     ←correct  vs  200-190-001 (Bib Holder) ←wrong

Root cause: The SKU remapping used prefix 180-130-xxx for both
  - 320-Implantology / 130-Osteotomes
  - 200-Disposables / 190-Patient Bibs
  The Patient Bibs items should use prefix 180-190-xxx.

Usage:
    python execution/fix_sku_collisions.py --dry-run   # preview changes
    python execution/fix_sku_collisions.py             # apply to Zoho
"""

import os, sys, csv, json, time, argparse, requests
from dotenv import load_dotenv

load_dotenv('.env')

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

INVENTORY_CSV      = 'DS inventory Jan 31 26.csv'

# ── The 4 known collisions: (wrong_old_sku, correct_new_prefix) ─────────────
# Wrong = the Patient Bibs items that got an Implantology prefix
COLLISIONS = [
    {'wrong_old_sku': '200-190-012', 'wrong_new_sku': '180-130-001', 'correct_prefix': '180-190'},
    {'wrong_old_sku': '200-190-013', 'wrong_new_sku': '180-130-002', 'correct_prefix': '180-190'},
    {'wrong_old_sku': '200-190-014', 'wrong_new_sku': '180-130-003', 'correct_prefix': '180-190'},
    {'wrong_old_sku': '200-190-001', 'wrong_new_sku': '180-130-004', 'correct_prefix': '180-190'},
]

# ── Auth ─────────────────────────────────────────────────────────────────────

def get_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except Exception:
            pass
    resp = requests.post('https://accounts.zoho.com/oauth/v2/token', params={
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    })
    token = resp.json().get('access_token')
    if not token:
        raise Exception(f'Token refresh failed: {resp.json()}')
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_get(path, params=None):
    params = params or {}
    params['organization_id'] = ZOHO_ORG_ID
    resp = requests.get(f'{ZOHO_API_BASE}{path}',
                        headers={'Authorization': f'Zoho-oauthtoken {get_token()}'},
                        params=params)
    return resp.json()

def zoho_put(path, payload):
    resp = requests.put(f'{ZOHO_API_BASE}{path}',
                        headers={'Authorization': f'Zoho-oauthtoken {get_token()}',
                                 'Content-Type': 'application/json'},
                        params={'organization_id': ZOHO_ORG_ID},
                        json=payload)
    return resp.json()

# ── Load inventory CSV ────────────────────────────────────────────────────────

def load_inventory():
    items = {}
    with open(INVENTORY_CSV) as f:
        for row in csv.DictReader(f):
            old_sku = row.get('SKU', '').strip()
            if old_sku:
                items[old_sku] = {
                    'item_id': row.get('Item ID', '').strip(),
                    'name': row['Item Name'],
                    'old_sku': old_sku,
                    'new_sku': row.get('CF.SKU - new', '').strip(),
                    'category': row.get('Category Name', ''),
                }
    return items

# ── Find next available SKU in a prefix range ────────────────────────────────

def next_available_sku(prefix, existing_new_skus):
    """Find lowest unused NNN in prefix-NNN."""
    used = set()
    for sku in existing_new_skus:
        if sku.startswith(prefix + '-'):
            try:
                used.add(int(sku.split('-')[-1]))
            except ValueError:
                pass
    n = 1
    while n in used:
        n += 1
    return f'{prefix}-{n:03d}'

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Preview only, no writes')
    args = parser.parse_args()

    print('Loading inventory CSV...')
    inventory = load_inventory()

    # All currently assigned new SKUs (for uniqueness check)
    all_new_skus = {e['new_sku'] for e in inventory.values() if e['new_sku']}

    print(f'  {len(inventory)} items loaded, {len(all_new_skus)} new SKUs assigned')
    print()

    changes = []

    for collision in COLLISIONS:
        wrong_old = collision['wrong_old_sku']
        wrong_new = collision['wrong_new_sku']
        prefix    = collision['correct_prefix']

        item = inventory.get(wrong_old)
        if not item:
            print(f'  ✗ {wrong_old}: not found in inventory CSV')
            continue

        # Generate a safe new SKU
        new_correct_sku = next_available_sku(prefix, all_new_skus)
        all_new_skus.add(new_correct_sku)  # reserve it

        print(f'  Item: {item["name"]}')
        print(f'    Old SKU:       {wrong_old}')
        print(f'    Wrong new SKU: {wrong_new}  ← COLLISION')
        print(f'    Correct new:   {new_correct_sku}')
        print(f'    Zoho item_id:  {item["item_id"] or "(not in CSV)"}')
        print()

        changes.append({
            'item': item,
            'old_new_sku': wrong_new,
            'correct_new_sku': new_correct_sku,
        })

    if not changes:
        print('No changes to make.')
        return

    if args.dry_run:
        print('DRY RUN — no changes written to Zoho.')
        print(f'{len(changes)} items would be updated.')
        return

    # ── Apply changes ────────────────────────────────────────────────────────
    print(f'Applying {len(changes)} changes to Zoho...')
    report = []
    for c in changes:
        item = c['item']
        if not item['item_id']:
            print(f'  ✗ {item["old_sku"]}: no item_id in CSV, skipping — update manually')
            continue

        payload = {'cf_sku_new': c['correct_new_sku']}
        result = zoho_put(f'/items/{item["item_id"]}', payload)
        code = result.get('code', -1)
        if code == 0:
            print(f'  ✓ {item["old_sku"]} ({item["name"][:40]})')
            print(f'      {c["old_new_sku"]}  →  {c["correct_new_sku"]}')
        else:
            print(f'  ✗ {item["old_sku"]}: Zoho error {code}: {result.get("message")}')

        report.append({
            'old_sku': item['old_sku'],
            'name': item['name'],
            'wrong_new_sku': c['old_new_sku'],
            'correct_new_sku': c['correct_new_sku'],
            'zoho_result': code,
        })
        time.sleep(0.3)

    import json as _json
    report_file = f'fix_sku_collisions_{time.strftime("%Y-%m-%d")}.json'
    with open(report_file, 'w') as f:
        _json.dump(report, f, indent=2)
    print(f'\nReport saved to {report_file}')

if __name__ == '__main__':
    main()
