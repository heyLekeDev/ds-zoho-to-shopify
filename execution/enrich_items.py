#!/usr/bin/env python3
"""
Stage 2 — AI Enrichment
Two-mode design — no Anthropic API key required. Claude Code IS the AI step.

Modes:
    --prepare   Fetch items from Zoho + Shopify lookup → write enrichment_input.json
                Then ask Claude Code to enrich the items.

    --write     Read enrichment_output.json (Claude's output) → write results to Zoho

Usage:
    python execution/enrich_items.py --prepare
    # → Claude Code enriches enrichment_input.json → enrichment_output.json
    python execution/enrich_items.py --write
    python execution/enrich_items.py --write --dry-run   # preview without writing
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

SHOPIFY_SHOP_URL     = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_ACCESS_TOKEN = os.getenv('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_API_VERSION  = '2024-01'

INPUT_FILE       = 'enrichment_input.json'
OUTPUT_FILE      = 'enrichment_output.json'
ENRICHMENT_FEEDBACK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'feedback', 'enrichment_examples.json'
)

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

# ── Zoho: Fetch queued items ──────────────────────────────────────────────────

def fetch_queued_items(token):
    print('  Fetching items from Zoho (Queue for Enrichment)...')
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={
                'organization_id': ZOHO_ORG_ID,
                'cf_shopify_status': 'Queue for Enrichment',
                'per_page': 200,
                'page': page,
            },
            timeout=15,
        )
        if resp.status_code == 429:
            print('    Rate limited — waiting 60s...')
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
    print(f'  Found {len(all_items)} items.')
    return all_items

def fetch_item_detail(item_id, token):
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return fetch_item_detail(item_id, token)
    return resp.json().get('item', {})

# ── Shopify: Late arrival lookup ──────────────────────────────────────────────

def search_shopify_products(query):
    gql = """
    query searchProducts($query: String!) {
      products(first: 5, query: $query) {
        edges {
          node {
            id
            title
            descriptionHtml
            options { name values }
          }
        }
      }
    }
    """
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json',
        headers={
            'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
            'Content-Type': 'application/json',
        },
        json={'query': gql, 'variables': {'query': query}},
        timeout=15,
    )
    edges = resp.json().get('data', {}).get('products', {}).get('edges', [])
    return [e['node'] for e in edges]

def find_late_arrival_match(item_name, brand):
    keywords = []
    if brand:
        keywords.append(brand)
    words = [w for w in item_name.split() if len(w) > 3 and not w.replace('.', '').replace('*', '').isdigit()]
    if words:
        keywords.append(' '.join(words[:3]))
    for kw in keywords:
        results = search_shopify_products(kw)
        if results:
            return results[0]
    return None

# ── MODE 1: Prepare ───────────────────────────────────────────────────────────

def run_prepare():
    print('═' * 60)
    print('  Stage 2 — Prepare (Fetch + Shopify Lookup)')
    print('═' * 60)

    token = get_zoho_token()
    queued = fetch_queued_items(token)

    if not queued:
        print('\n  No items in Queue for Enrichment. Run Stage 1 first.')
        sys.exit(0)

    print(f'\n  Fetching full item details + Shopify late arrival checks...')
    items_data = []

    for item in queued:
        item_id  = item['item_id']
        detail   = fetch_item_detail(item_id, token)
        name     = detail.get('name', '')
        brand    = detail.get('brand', '')
        category = detail.get('category_name', '')
        parent   = detail.get('parent_category_name', '')
        rate     = detail.get('rate', 0)
        sku      = detail.get('sku', '')

        existing = None
        if SHOPIFY_ACCESS_TOKEN and SHOPIFY_SHOP_URL:
            existing = find_late_arrival_match(name, brand)

        late_tag = ' → [LATE ARRIVAL]' if existing else ''
        print(f'    {sku}  {name[:45]}{late_tag}')

        entry = {
            'item_id':         item_id,
            'sku':             sku,
            'name':            name,
            'brand':           brand,
            'category':        category,
            'parent_category': parent,
            'rate':            rate,
        }
        if existing:
            entry['existing_shopify'] = {
                'title':           existing['title'],
                'descriptionHtml': existing.get('descriptionHtml', ''),
                'options':         existing.get('options', []),
            }

        items_data.append(entry)
        time.sleep(0.2)

    # ── Inject feedback context ────────────────────────────────────────────────
    # Prepend a special feedback_context object so Claude can learn from past
    # corrections without this affecting the --write step (which skips objects
    # that have no item_id key).
    output_payload = list(items_data)
    if os.path.exists(ENRICHMENT_FEEDBACK_FILE):
        try:
            with open(ENRICHMENT_FEEDBACK_FILE) as f:
                feedback = json.load(f)
            examples   = feedback.get('examples', [])
            grp_rules  = feedback.get('grouping_rules', [])
            var_rules  = feedback.get('variant_naming_rules', [])
            if examples or grp_rules or var_rules:
                context_block = {
                    '_type':               'feedback_context',
                    '_description':        (
                        'Past corrections and standing rules. '
                        'Read these BEFORE enriching the items below. '
                        'Apply the same patterns and naming conventions.'
                    ),
                    'examples':            examples,
                    'grouping_rules':      grp_rules,
                    'variant_naming_rules': var_rules,
                }
                output_payload.insert(0, context_block)
                print(f'  ✓ Feedback context injected ({len(examples)} example(s), '
                      f'{len(grp_rules)} grouping rule(s)).')
        except Exception as e:
            print(f'  ⚠  Could not load enrichment feedback: {e}')

    with open(INPUT_FILE, 'w') as f:
        json.dump(output_payload, f, indent=2)

    print()
    print('═' * 60)
    print(f'  Saved {len(items_data)} items to {INPUT_FILE}')
    print()
    print('  Next step:')
    print('  Tell Claude Code: "enrich the items in enrichment_input.json"')
    print('  Then run: python execution/enrich_items.py --write')
    print('═' * 60)

# ── MODE 2: Write ─────────────────────────────────────────────────────────────

def run_write(dry_run):
    print('═' * 60)
    print('  Stage 2 — Write (Zoho Update)')
    if dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    if not os.path.exists(OUTPUT_FILE):
        print(f'\n  ✗ {OUTPUT_FILE} not found.')
        print('  Run --prepare first, then ask Claude to enrich, then run --write.')
        sys.exit(1)

    if not os.path.exists(INPUT_FILE):
        print(f'\n  ✗ {INPUT_FILE} not found. Run --prepare first.')
        sys.exit(1)

    with open(INPUT_FILE) as f:
        input_items = json.load(f)
    with open(OUTPUT_FILE) as f:
        output_results = json.load(f)

    # Build lookup maps
    input_by_sku = {item['sku']: item for item in input_items}

    today_str = date.today().isoformat()
    token     = get_zoho_token()

    complete_count = 0
    review_count   = 0
    error_count    = 0

    print(f'\n  Writing {len(output_results)} enriched items to Zoho...\n')

    for result in output_results:
        sku     = result.get('sku', '')
        source  = input_by_sku.get(sku, {})
        item_id = source.get('item_id', '')
        name    = source.get('name', '')[:40]

        if not item_id:
            print(f'  ✗ {sku}  — item_id not found in input file, skipping')
            error_count += 1
            continue

        existing = source.get('existing_shopify')
        tag_raw  = result.get('enrichment_comments', '[MATCH]')
        tag      = tag_raw.split(']')[0].lstrip('[').strip() if ']' in tag_raw else 'MATCH'

        is_conflict = result.get('processing_status', '').lower() == 'conflict' or tag == 'CONFLICT'
        is_late     = existing is not None

        # Sync result short tag
        if is_late:
            sync_result = '[LATE ARRIVAL] → {}'.format(existing['title'])
        elif is_conflict:
            conflict_msg = tag_raw.split('] ', 1)[1] if '] ' in tag_raw else tag_raw
            sync_result = '[CONFLICT] {}'.format(conflict_msg[:55])
        else:
            sync_result = '[{}]'.format(tag)

        # Sync notes detail
        if is_late:
            option_names = ', '.join(o['name'] for o in existing.get('options', []))
            notes = (
                '[ENRICH] ({})\n'
                'Tag: [LATE ARRIVAL]\n'
                'Matched Shopify product: {}\n'
                'Options inherited: {}\n'
                'Description: inherited from existing product.\n'
                'Enriched title: {}\n'
                'Variant 1: {} / {}'
            ).format(
                today_str,
                existing['title'],
                option_names,
                result.get('enriched_title', ''),
                result.get('variant_1_name', ''),
                result.get('variant_1_value', ''),
            )
        elif is_conflict:
            notes = (
                '[ENRICH] ({})\n'
                'Tag: [CONFLICT]\n'
                'Reason: {}\n'
                'Action needed: Verify in Zoho and reset to Queue for Enrichment.'
            ).format(today_str, tag_raw)
        else:
            notes = (
                '[ENRICH] ({})\n'
                'Tag: [{}]\n'
                'Enriched title: {}\n'
                'Collection: {}\n'
                'Variant 1: {} / {}\n'
                'Variant 2: {} / {}\n'
                'AI reasoning: {}'
            ).format(
                today_str, tag,
                result.get('enriched_title', ''),
                result.get('shopify_collection') or 'Standalone',
                result.get('variant_1_name', ''), result.get('variant_1_value', ''),
                result.get('variant_2_name', ''), result.get('variant_2_value', ''),
                tag_raw,
            )

        new_status = 'Needs Review' if is_conflict else 'Enrichment Complete'

        # Resolve description
        desc = result.get('description_html', '')
        if desc == '__INHERIT__' and existing:
            desc = existing.get('descriptionHtml', '')

        status_icon = '✓' if new_status == 'Enrichment Complete' else '⚠'
        print(f'  {status_icon} {sku}  {name}')
        print(f'    → {sync_result}')
        print(f'       Title: {result.get("enriched_title", "")}')

        if not dry_run:
            payload = {
                'custom_fields': [
                    {'api_name': 'cf_shopify_status',       'value': new_status},
                    {'api_name': 'cf_sync_result',          'value': sync_result},
                    {'api_name': 'cf_shopify_sync_notes',   'value': notes},
                    {'api_name': 'cf_enriched_title',       'value': result.get('enriched_title') or ''},
                    {'api_name': 'cf_shopify_collection',   'value': result.get('shopify_collection') or ''},
                    {'api_name': 'cf_shopify_product_type', 'value': result.get('shopify_product_type') or ''},
                    {'api_name': 'cf_shopify_tags',         'value': result.get('shopify_tags') or ''},
                    {'api_name': 'cf_shopify_var_1_name',   'value': result.get('variant_1_name') or ''},
                    {'api_name': 'cf_shopify_var_1_value',  'value': result.get('variant_1_value') or ''},
                    {'api_name': 'cf_shopify_var_2_name',   'value': result.get('variant_2_name') or ''},
                    {'api_name': 'cf_shopify_var_2_value',  'value': result.get('variant_2_value') or ''},
                    {'api_name': 'cf_shopify_var_3_name',   'value': result.get('variant_3_name') or ''},
                    {'api_name': 'cf_shopify_var_3_value',  'value': result.get('variant_3_value') or ''},
                ]
            }
            if desc:
                payload['custom_fields'].append(
                    {'api_name': 'cf_description_html', 'value': desc}
                )
            if result.get('brand'):
                payload['brand'] = result['brand']

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
                print(f'  ✗ API error for {sku}: {api_data.get("message")} (code {api_data.get("code")})')
                error_count += 1
                continue

        if new_status == 'Enrichment Complete':
            complete_count += 1
        else:
            review_count += 1

    print()
    print('═' * 60)
    print(f'  Done.')
    print(f'  Enrichment Complete : {complete_count}')
    print(f'  Needs Review        : {review_count}')
    if error_count:
        print(f'  Errors              : {error_count}')
    if dry_run:
        print('\n  Dry run — no changes written to Zoho.')
    else:
        print('\n  → Review "Enrichment Complete" view in Zoho before running Stage 3.')
        if review_count:
            print(f'  → Check "Needs Review" — {review_count} item(s) need manual attention.')
    print('═' * 60)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Stage 2 — AI Enrichment')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true', help='Fetch items from Zoho → enrichment_input.json')
    group.add_argument('--write',   action='store_true', help='Write enrichment_output.json results → Zoho')
    parser.add_argument('--dry-run', action='store_true', help='With --write: preview without writing to Zoho')
    args = parser.parse_args()

    if args.prepare:
        run_prepare()
    elif args.write:
        run_write(args.dry_run)

if __name__ == '__main__':
    main()
