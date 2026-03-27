#!/usr/bin/env python3
"""
Stage 4 — Enrichment Validation + Queue for Upload

Reads items in "Image Validated" status, re-applies corrected enrichment fields
from enrichment_output.json (collection, variants, tags, product type), runs the
Antigravity validation rules, and advances passing items to "Queue for Upload".

Usage:
    python execution/validate_enrichment.py --dry-run   # preview without writing
    python execution/validate_enrichment.py             # apply and advance
"""

import os, sys, json, time, requests, argparse
from datetime import date
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

INPUT_FILE  = 'enrichment_input.json'
OUTPUT_FILE = 'enrichment_output.json'

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

def fetch_image_validated_items(token):
    """Fetch all items currently in 'Image Validated' status."""
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={
                'organization_id': ZOHO_ORG_ID,
                'cf_shopify_status': 'Image Validated',
                'per_page': 200,
                'page': page,
            },
            timeout=15,
        )
        if resp.status_code == 429:
            print('  Rate limited — waiting 60s...')
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

def run_validation_rules(result, source):
    """
    Run the Antigravity validation rules on a single item.
    Returns (passed: bool, failures: list[str])
    """
    failures = []

    # Rule 1: Price > 0
    if (source.get('rate', 0) or 0) <= 0:
        failures.append('Price is 0 or missing')

    # Rule 2: Description present
    if not result.get('description_html', '').strip():
        failures.append('description_html is empty')

    # Rule 3: Orphaned variant names (standalone must not have variant names)
    collection = result.get('shopify_collection') or ''
    v1_name    = result.get('variant_1_name') or ''
    if not collection and v1_name:
        failures.append(f'Standalone item has variant_1_name="{v1_name}" — must be null for standalones')

    # Rule 4: Enriched title present
    if not result.get('enriched_title', '').strip():
        failures.append('enriched_title is empty')

    return len(failures) == 0, failures


def main():
    parser = argparse.ArgumentParser(description='Stage 4 — Enrichment Validation')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing to Zoho')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 4 — Enrichment Validation')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    for f in (INPUT_FILE, OUTPUT_FILE):
        if not os.path.exists(f):
            print(f'\n  ✗ {f} not found.')
            sys.exit(1)

    with open(INPUT_FILE) as f:
        input_items = json.load(f)
    with open(OUTPUT_FILE) as f:
        output_results = json.load(f)

    input_by_sku  = {it['sku']: it for it in input_items}
    output_by_sku = {it['sku']: it for it in output_results}

    token = get_zoho_token()

    print('\n  Fetching items with status "Image Validated"...')
    validated_items = fetch_image_validated_items(token)
    print(f'  Found {len(validated_items)} items.\n')

    if not validated_items:
        print('  No items in Image Validated status.')
        sys.exit(0)

    today_str    = date.today().isoformat()
    pass_count   = 0
    fail_count   = 0
    skip_count   = 0

    # Build a set of collection names to check for duplicate title conflicts
    collection_counts = {}
    for r in output_results:
        col = r.get('shopify_collection') or ''
        if not col:
            # Standalone — check enriched_title uniqueness
            col = '__standalone__' + (r.get('enriched_title') or '')
        collection_counts[col] = collection_counts.get(col, 0) + 1

    for item in validated_items:
        item_id = item.get('item_id') or item.get('zoho_id', '')
        sku     = item.get('sku', '')
        name    = (item.get('name', '') or '')[:40]

        result = output_by_sku.get(sku)
        source = input_by_sku.get(sku)

        if not result:
            print(f'  ⚠ {sku}  {name}')
            print(f'    → Not in enrichment_output.json — skipping')
            skip_count += 1
            continue

        if not source:
            print(f'  ⚠ {sku}  {name}')
            print(f'    → Not in enrichment_input.json — skipping')
            skip_count += 1
            continue

        passed, failures = run_validation_rules(result, source)

        collection  = result.get('shopify_collection') or ''
        title       = result.get('enriched_title', '')
        v1_name     = result.get('variant_1_name') or ''
        v1_value    = result.get('variant_1_value') or ''

        if passed:
            icon       = '✓'
            new_status = 'Queue for Upload'
            notes = (
                '[BOT] ({})\n'
                'Tag: PASS\n'
                'Enriched title: {}\n'
                'Collection: {}\n'
                'Variant 1: {} / {}\n'
                'Antigravity rules: all passed.'
            ).format(
                today_str, title,
                collection or 'Standalone',
                v1_name, v1_value,
            )
            sync_result = 'All rules passed'
        else:
            icon       = '⚠'
            new_status = 'Needs Review'
            fail_detail = '; '.join(failures)
            notes = (
                '[BOT] ({})\n'
                'Tag: FAIL\n'
                'Enriched title: {}\n'
                'FAILED RULES:\n{}'
            ).format(today_str, title, '\n'.join(f'  - {f}' for f in failures))
            sync_result = f'FAIL: {failures[0]}'

        print(f'  {icon} {sku}  {name}')
        print(f'    → {new_status}  |  {sync_result}')
        print(f'       Title: {title}  |  Collection: {collection or "(standalone)"}')

        if not args.dry_run:
            payload = {
                'custom_fields': [
                    {'api_name': 'cf_shopify_status',       'value': new_status},
                    {'api_name': 'cf_sync_result',          'value': sync_result},
                    {'api_name': 'cf_shopify_sync_notes',   'value': notes},
                    # Re-apply corrected enrichment fields
                    {'api_name': 'cf_enriched_title',       'value': result.get('enriched_title') or ''},
                    {'api_name': 'cf_shopify_collection',   'value': collection},
                    {'api_name': 'cf_shopify_product_type', 'value': result.get('shopify_product_type') or ''},
                    {'api_name': 'cf_shopify_tags',         'value': result.get('shopify_tags') or ''},
                    {'api_name': 'cf_shopify_var_1_name',   'value': result.get('variant_1_name') or ''},
                    {'api_name': 'cf_shopify_var_1_value',  'value': result.get('variant_1_value') or ''},
                    {'api_name': 'cf_shopify_var_2_name',   'value': result.get('variant_2_name') or ''},
                    {'api_name': 'cf_shopify_var_2_value',  'value': result.get('variant_2_value') or ''},
                    {'api_name': 'cf_description_html',     'value': result.get('description_html') or ''},
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
            api_data = resp.json()
            if api_data.get('code') != 0:
                print(f'  ✗ API error: {api_data.get("message")} (code {api_data.get("code")})')
                fail_count += 1
                continue

        if passed:
            pass_count += 1
        else:
            fail_count += 1

        time.sleep(0.3)

    print()
    print('═' * 60)
    print('  Done.')
    print(f'  Queue for Upload : {pass_count}')
    print(f'  Needs Review     : {fail_count}')
    if skip_count:
        print(f'  Skipped          : {skip_count}')
    if args.dry_run:
        print('\n  Dry run — no changes written to Zoho.')
    else:
        print('\n  → Review "Queue for Upload" in Zoho, then run:')
        print('     python execution/sync_zoho_to_shopify.py')
    print('═' * 60)


if __name__ == '__main__':
    main()
