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

# Categories the ZOHO_VIEW_ID custom view is known to exclude — items in these
# ranges pass validation but the default sync fetch will NOT see them.
VIEW_EXCLUDED_PREFIXES = ('280-', '380-', '480-')

def run_validation_rules(result, source, item, collection_counts, collection_option_names,
                         shopify_lookup=None):
    """
    Run the Antigravity validation rules on a single item.
    Returns (passed: bool, failures: list[str], warnings: list[str])
    """
    failures = []
    warnings = []
    sku        = result.get('sku', '')
    collection = result.get('shopify_collection') or ''
    title      = result.get('enriched_title', '') or ''
    v1_name    = result.get('variant_1_name') or ''
    v2_name    = result.get('variant_2_name') or ''

    # Rule 1: Price floor — sync rejects anything under ₦100, so fail it HERE
    rate = source.get('rate', 0) or 0
    if rate < 100:
        failures.append(f'Price ₦{rate} is below the ₦100 floor (sync will reject)')

    # Rule 2: Description present
    if not result.get('description_html', '').strip():
        failures.append('description_html is empty')

    # Rule 2b: Native description must also be non-empty or sync throws
    # DESCRIPTION ERROR. (Auto-fixed in the write payload — warning only.)
    if not (item.get('description') or '').strip():
        warnings.append('Native description empty — plain-text version will be written automatically')

    # Rule 3: Orphaned variant names (standalone must not have variant names)
    if not collection and v1_name:
        failures.append(f'Standalone item has variant_1_name="{v1_name}" — must be null for standalones')

    # Rule 4: Enriched title present
    if not title.strip():
        failures.append('enriched_title is empty')

    # Rule 5: Duplicate standalone titles — two standalones with the same
    # enriched title will collide into one Shopify product
    if not collection and collection_counts.get('__standalone__' + title, 0) > 1:
        failures.append(f'Duplicate standalone title "{title}" — another item in this batch shares it; group them or retitle')

    # Rule 6: Grouped items must agree on option names across the collection
    if collection:
        expected = collection_option_names.get(collection)
        if expected and (v1_name, v2_name) != expected:
            failures.append(f'Option-name mismatch in collection "{collection}": this item has ({v1_name!r}, {v2_name!r}), others have {expected!r}')
        if not v1_name:
            failures.append(f'Grouped item (collection "{collection}") has no variant_1_name')

    # Rule 7: Shopify rejects " / " in option names; clinical names required
    for oname in (v1_name, v2_name):
        if ' / ' in oname:
            failures.append(f'Option name "{oname}" contains " / " — Shopify rejects it; use "&" or "—"')
        if oname.strip().lower() in ('option 1', 'option 2', 'option 3', 'config'):
            failures.append(f'Option name "{oname}" is a placeholder — use a clinical name (ISO Code, Grit, Size...)')

    # Rule 8: SKU already live on Shopify → this is a write-back, not an upload
    if shopify_lookup is not None:
        try:
            existing = shopify_lookup(sku)
        except Exception as e:
            existing = None
            warnings.append(f'Shopify SKU probe failed ({e}) — collision check skipped')
        if existing:
            prod_title = (existing.get('product') or {}).get('title', '?')
            failures.append(f'SKU already LIVE on Shopify as variant of "{prod_title}" — reconcile with write-back, do not re-sync')

    # Rule 9: View-excluded category — passes, but sync must use --skus
    if sku.startswith(VIEW_EXCLUDED_PREFIXES):
        warnings.append('Category excluded from the sync custom view — sync this SKU with --skus')

    return len(failures) == 0, failures, warnings


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

    # Duplicate-title and option-name consistency maps across the batch
    collection_counts = {}
    collection_option_names = {}   # collection → (v1_name, v2_name) of first member
    for r in output_results:
        col = r.get('shopify_collection') or ''
        key = col if col else '__standalone__' + (r.get('enriched_title') or '')
        collection_counts[key] = collection_counts.get(key, 0) + 1
        if col and col not in collection_option_names:
            collection_option_names[col] = (r.get('variant_1_name') or '',
                                            r.get('variant_2_name') or '')

    # Live Shopify SKU probe (Rule 8) — catches already-live items before re-upload
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from common import shopify_variant_by_sku as _shopify_lookup
    except Exception as e:
        _shopify_lookup = None
        print(f'  ⚠ Shopify probe unavailable ({e}) — Rule 8 skipped this run.')

    for item in validated_items:
        item_id = item.get('item_id') or item.get('zoho_id', '')
        sku     = item.get('sku', '')
        name    = (item.get('name', '') or '')[:40]

        result = output_by_sku.get(sku)
        source = input_by_sku.get(sku)

        if not result or not source:
            # Do NOT skip silently — stranded items are invisible forever otherwise.
            missing = 'enrichment_output.json' if not result else 'enrichment_input.json'
            print(f'  ⚠ {sku}  {name}')
            print(f'    → Not in current {missing} — flagging Needs Review')
            skip_count += 1
            if not args.dry_run:
                requests.put(
                    f'{ZOHO_API_BASE}/items/{item_id}',
                    headers=zoho_headers(token),
                    params={'organization_id': ZOHO_ORG_ID},
                    json={'custom_fields': [
                        {'api_name': 'cf_shopify_status', 'value': 'Needs Review'},
                        {'api_name': 'cf_sync_result',    'value': 'STRANDED: not in current batch'},
                        {'api_name': 'cf_shopify_sync_notes',
                         'value': f'[BOT] ({today_str})\nItem is at Image Validated but absent from the current {missing}. '
                                  'Re-run enrichment prepare for this SKU or reconcile manually.'},
                    ]},
                    timeout=15,
                )
                time.sleep(0.3)
            continue

        passed, failures, warnings = run_validation_rules(
            result, source, item, collection_counts, collection_option_names,
            shopify_lookup=_shopify_lookup)

        collection  = result.get('shopify_collection') or ''
        title       = result.get('enriched_title', '')
        v1_name     = result.get('variant_1_name') or ''
        v1_value    = result.get('variant_1_value') or ''

        if passed:
            icon       = '✓'
            new_status = 'Queue for Upload'
            warn_block = ('\nWARNINGS:\n' + '\n'.join(f'  - {w}' for w in warnings)) if warnings else ''
            notes = (
                '[BOT] ({})\n'
                'Tag: PASS\n'
                'Enriched title: {}\n'
                'Collection: {}\n'
                'Variant 1: {} / {}\n'
                'Antigravity rules: all passed.{}'
            ).format(
                today_str, title,
                collection or 'Standalone',
                v1_name, v1_value,
                warn_block,
            )
            sync_result = 'All rules passed' + (' [view-excluded: use --skus]'
                          if any('--skus' in w for w in warnings) else '')
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
            custom_fields = [
                {'api_name': 'cf_shopify_status',       'value': new_status},
                {'api_name': 'cf_sync_result',          'value': sync_result},
                {'api_name': 'cf_shopify_sync_notes',   'value': notes},
            ]
            if passed:
                # Only stamp enrichment fields on PASS — persisting known-bad
                # enrichment alongside "Needs Review" corrupts good Zoho data.
                custom_fields += [
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
            payload = {'custom_fields': custom_fields}
            # Auto-fix: empty native description makes the sync throw
            # DESCRIPTION ERROR — write a plain-text version derived from the HTML.
            if passed and not (item.get('description') or '').strip():
                import re as _re
                plain = _re.sub(r'<[^>]+>', ' ', result.get('description_html') or '')
                plain = ' '.join(plain.split())[:2000]
                if plain:
                    payload['description'] = plain
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
        print(f'  Stranded → Needs Review : {skip_count}')
    if args.dry_run:
        print('\n  Dry run — no changes written to Zoho.')
    else:
        print('\n  → Review "Queue for Upload" in Zoho, then run:')
        print('     python execution/sync_zoho_to_shopify.py')
    print('═' * 60)
    if skip_count:
        sys.exit(1)   # stranded items need attention — make scheduled runs notice


if __name__ == '__main__':
    main()
