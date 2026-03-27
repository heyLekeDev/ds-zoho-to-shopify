#!/usr/bin/env python3
"""
Verify Zoho environment is correctly configured for the enrichment pipeline.
Checks: custom fields, status dropdown options, and basic API connectivity.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

ZOHO_ORG_ID = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE = '.zoho_token.json'

# ── Expected configuration ──────────────────────────────────────────────────

REQUIRED_STATUSES = [
    'Queue for Enrichment',
    'Enrichment Complete',
    'Image required',
    'Image Validated',
    'Queue for Upload',
    'Published',
    'Needs Review',
    'Error uploading',
    'Ignore',
    'Update Required',
    'To be Archived',
    'Archived',
]

REQUIRED_CUSTOM_FIELDS = {
    'cf_shopify_status':     {'label': 'Shopify status',     'type': 'dropdown'},
    'cf_sync_result':        {'label': 'Sync result',        'type': 'text'},
    'cf_shopify_sync_notes': {'label': 'Shopify sync notes', 'type': 'multiline'},
}

# ── Auth ─────────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
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
        },
        timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f"Auth failed: {data}")
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_get(path, token, params=None):
    headers = {'Authorization': f'Zoho-oauthtoken {token}'}
    p = {'organization_id': ZOHO_ORG_ID}
    if params:
        p.update(params)
    return requests.get(f'{ZOHO_API_BASE}{path}', headers=headers, params=p, timeout=15)

# ── Checks ───────────────────────────────────────────────────────────────────

def check_connectivity(token):
    print('\n── 1. API Connectivity ──────────────────────────────')
    resp = zoho_get('/items', token, {'per_page': 1})
    if resp.status_code == 200:
        print('  ✓ Zoho Inventory API reachable')
        return True
    else:
        print(f'  ✗ API error {resp.status_code}: {resp.text[:200]}')
        return False

def check_custom_fields(token):
    print('\n── 2. Custom Fields ─────────────────────────────────')
    resp = zoho_get('/settings/customfields', token, {'module': 'items'})

    if resp.status_code != 200:
        print(f'  ✗ Could not fetch custom fields: {resp.status_code}')
        return

    fields = resp.json().get('customfields', [])
    found_api_names = {f.get('api_name', '').lower(): f for f in fields}
    found_labels    = {f.get('label', '').lower(): f for f in fields}

    for api_name, meta in REQUIRED_CUSTOM_FIELDS.items():
        field = found_api_names.get(api_name.lower()) or found_labels.get(meta['label'].lower())
        if field:
            print(f"  ✓ '{meta['label']}' (api: {field.get('api_name', 'unknown')})")
        else:
            print(f"  ✗ MISSING: '{meta['label']}' (expected api_name: {api_name})")

    # Print all found field names for reference
    print(f'\n  All custom fields on Items ({len(fields)} total):')
    for f in fields:
        print(f"    - {f.get('label')} → {f.get('api_name')} [{f.get('data_type', '?')}]")

def check_status_options(token):
    print('\n── 3. Shopify Status Dropdown Options ───────────────')
    resp = zoho_get('/settings/customfields', token, {'module': 'items'})

    if resp.status_code != 200:
        print(f'  ✗ Could not fetch custom fields: {resp.status_code}')
        return

    fields = resp.json().get('customfields', [])
    status_field = next(
        (f for f in fields if 'shopify' in f.get('label', '').lower() and 'status' in f.get('label', '').lower()),
        None
    )

    if not status_field:
        print('  ✗ Shopify status field not found')
        return

    options = [o.get('value', '') for o in status_field.get('dropdown_options', [])]
    print(f"  Found {len(options)} options in '{status_field.get('label')}':")

    missing = []
    for expected in REQUIRED_STATUSES:
        if expected in options:
            print(f'    ✓ {expected}')
        else:
            print(f'    ✗ MISSING: {expected}')
            missing.append(expected)

    extra = [o for o in options if o not in REQUIRED_STATUSES]
    if extra:
        print(f'\n  Extra options (not in pipeline spec):')
        for o in extra:
            print(f'    ~ {o}')

    if not missing:
        print('\n  ✓ All required statuses present')
    else:
        print(f'\n  ✗ {len(missing)} required status(es) missing — add them to the cf_shopify_status dropdown')

def check_sample_item(token):
    print('\n── 4. Sample Item Field Check ───────────────────────')
    resp = zoho_get('/items', token, {'per_page': 1})
    if resp.status_code != 200:
        return

    items = resp.json().get('items', [])
    if not items:
        print('  No items found to sample')
        return

    item_id = items[0]['item_id']
    detail = zoho_get(f'/items/{item_id}', token)
    if detail.status_code != 200:
        print(f'  ✗ Could not fetch item detail: {detail.status_code}')
        return

    item = detail.json().get('item', {})
    cf = item.get('custom_fields', [])
    cf_names = [f.get('api_name') for f in cf]

    print(f"  Sample item: {item.get('name', 'unknown')} ({item_id})")
    print(f'  Custom fields present on item:')
    for f in cf:
        print(f"    - {f.get('label')} = '{f.get('value', '')}'")

    for api_name in REQUIRED_CUSTOM_FIELDS:
        if api_name in cf_names:
            print(f'  ✓ {api_name} is accessible on items')
        else:
            print(f'  ~ {api_name} not set on this item (may just be empty — OK)')

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('═' * 55)
    print('  Zoho Enrichment Pipeline — Setup Verification')
    print('═' * 55)

    if not all([ZOHO_ORG_ID, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN]):
        print('\n✗ Missing env vars. Check .env for:')
        print('  ZOHO_ORGANIZATION_ID, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN')
        exit(1)

    token = get_zoho_token()
    print(f'\n  ✓ Auth token acquired')

    check_connectivity(token)
    check_custom_fields(token)
    check_status_options(token)
    check_sample_item(token)

    print('\n' + '═' * 55)
    print('  Verification complete.')
    print('═' * 55)
