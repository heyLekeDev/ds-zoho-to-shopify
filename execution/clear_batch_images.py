#!/usr/bin/env python3
"""
Clear all Zoho images for the enrichment batch and reset status to 'Image required'.

Reads all items from enrichment_input.json, deletes their Zoho image,
and sets cf_shopify_status → 'Image required' regardless of current status.

Usage:
    python execution/clear_batch_images.py
    python execution/clear_batch_images.py --dry-run
    python execution/clear_batch_images.py --sku 320-150-007
"""

import os, sys, json, time, argparse, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'
ENRICHMENT_INPUT   = 'enrichment_input.json'


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
    return {'Authorization': f'Zoho-oauthtoken {token}'}


def delete_image(item_id, token):
    resp = requests.delete(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return delete_image(item_id, token)
    data = resp.json()
    return data.get('code') == 0


def set_image_required(item_id, token):
    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID},
        json={'custom_fields': [
            {'api_name': 'cf_shopify_status', 'value': 'Image required'},
            {'api_name': 'cf_sync_result',    'value': 'Image cleared for re-fetch'},
        ]},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return set_image_required(item_id, token)
    data = resp.json()
    return data.get('code') == 0


def main():
    parser = argparse.ArgumentParser(description='Clear all batch images and reset to Image required')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    parser.add_argument('--sku', help='Clear a single SKU only')
    args = parser.parse_args()

    print('═' * 60)
    print('  Clear Batch Images')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    if not os.path.exists(ENRICHMENT_INPUT):
        print(f'✗ {ENRICHMENT_INPUT} not found')
        sys.exit(1)

    with open(ENRICHMENT_INPUT) as f:
        items = json.load(f)

    if args.sku:
        items = [it for it in items if it.get('sku') == args.sku]
        if not items:
            print(f'✗ SKU {args.sku} not found')
            sys.exit(1)

    print(f'\n  Clearing {len(items)} item(s)...\n')
    token = get_zoho_token()
    cleared = 0
    errors  = 0

    for item in items:
        sku     = item.get('sku', '?')
        name    = item.get('name', '')
        item_id = item['item_id']

        if args.dry_run:
            print(f'  [DRY RUN] Would clear: {sku}  {name[:45]}')
            continue

        token = get_zoho_token()

        # 1. Delete image
        img_ok = delete_image(item_id, token)
        time.sleep(1)

        # 2. Set Image required
        status_ok = set_image_required(item_id, token)

        if img_ok and status_ok:
            print(f'  ✓ {sku}  {name[:45]}')
            cleared += 1
        else:
            print(f'  ✗ {sku}  img={img_ok} status={status_ok}')
            errors += 1

        time.sleep(2)

    print()
    print('═' * 60)
    print('  Done.')
    if args.dry_run:
        print(f'  Would clear: {len(items)}')
    else:
        print(f'  Cleared : {cleared}')
        if errors:
            print(f'  Errors  : {errors}')
    print('═' * 60)


if __name__ == '__main__':
    main()
