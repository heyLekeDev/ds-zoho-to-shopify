#!/usr/bin/env python3
"""
One-shot remediation: remove em/en dashes from all customer-facing copy,
in BOTH systems, keeping them consistent:

  Zoho (per affected variant item): cf_enriched_title, cf_shopify_collection,
      cf_shopify_var_*_value, cf_description_html
  Shopify (per affected product): title, descriptionHtml, option values

Both sides must move together — a Shopify-only rename would break the sync's
exact-title matching and create duplicate products on the next late-arrival.

Rules (store copy standard, see directives/zoho_text_enrichment.md):
  - short trailing qualifier  → parentheses   "Pilot Drill — 2.0mm" → "Pilot Drill (2.0mm)"
  - compound values           → comma         "10mm — 2mm Well"     → "10mm, 2mm Well"
  - numeric ranges            → "to"          "15–40"               → "15 to 40"

Usage:
    python execution/fix_dashes_live.py --dry-run   # preview all changes
    python execution/fix_dashes_live.py             # apply
"""

import os
import sys
import re
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, shopify_graphql,
                    zoho_find_by_sku, zoho_item_detail, zoho_write_fields,
                    DASH_RE, scrub_dashes)

REPORT = os.path.join(PROJECT_DIR, '.dash_fix_report.json')


def fix_title(t):
    """Short trailing qualifier → parentheses; else standard scrub.
    Qualifiers already containing parentheses get a comma instead —
    nested parens read badly: "Gloves (Large (100/Box))"."""
    t2 = re.sub(r'(?<=\d)\s*[—–]\s*(?=\d)', ' to ', t)
    m = re.match(r'^(.*?)\s+[—–]\s+([^—–]{1,30})$', t2)
    if m and len(m.group(2).split()) <= 4 and '(' not in m.group(2):
        return f'{m.group(1)} ({m.group(2)})'
    return scrub_dashes(t2)


def fetch_affected_products():
    """All products with a dash in title, description, or option values."""
    products, cursor = [], None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        q = f'''{{
          products(first: 50{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{ node {{
              id title descriptionHtml
              options {{ id name optionValues {{ id name }} }}
              variants(first: 100) {{ edges {{ node {{ sku }} }} }}
            }} }}
          }}
        }}'''
        data = shopify_graphql(q)
        block = (data.get('data', {}) or {}).get('products', {})
        for e in block.get('edges', []):
            n = e['node']
            has_dash = (DASH_RE.search(n.get('title', '') or '')
                        or DASH_RE.search(n.get('descriptionHtml', '') or '')
                        or any(DASH_RE.search(v.get('name', ''))
                               for o in n.get('options', []) or []
                               for v in o.get('optionValues', []) or []))
            if has_dash:
                products.append(n)
        pi = block.get('pageInfo', {})
        if not pi.get('hasNextPage'):
            break
        cursor = pi['endCursor']
        time.sleep(0.3)
    return products


ZOHO_COPY_FIELDS = ('cf_enriched_title', 'cf_shopify_collection',
                    'cf_shopify_var_1_value', 'cf_shopify_var_2_value',
                    'cf_shopify_var_3_value', 'cf_description_html')


def fix_zoho_item(sku, token, dry_run, log):
    """Scrub dash-carrying cf copy fields on the Zoho item behind a SKU."""
    listed = zoho_find_by_sku(sku, token)
    if not listed:
        log.append({'sku': sku, 'zoho': 'NOT FOUND'})
        return
    det = zoho_item_detail(listed['item_id'], token)
    updates = {}
    for f in ZOHO_COPY_FIELDS:
        val = det['cf'].get(f, '')
        if val and DASH_RE.search(str(val)):
            # collection/title use title-style fixing; values/description standard
            new = fix_title(val) if f in ('cf_enriched_title', 'cf_shopify_collection') \
                  else scrub_dashes(val)
            updates[f] = new
    if not updates:
        return
    if dry_run:
        log.append({'sku': sku, 'zoho_would_update': updates})
        return
    ok, msg = zoho_write_fields(det['item_id'], updates, token)
    log.append({'sku': sku, 'zoho_updated': list(updates), 'ok': ok,
                **({} if ok else {'error': msg})})
    time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description='Remove em/en dashes from live copy (Zoho + Shopify)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    print('═' * 60)
    print(f'  Dash remediation {"(DRY RUN)" if args.dry_run else ""}')
    print('═' * 60)

    token = get_zoho_token()
    products = fetch_affected_products()
    print(f'  Affected products: {len(products)}')

    log = []
    counts = {'titles': 0, 'descriptions': 0, 'option_values': 0,
              'zoho_items': 0, 'errors': 0}

    for p in products:
        pid = p['id']
        old_title = p.get('title', '') or ''
        new_title = fix_title(old_title) if DASH_RE.search(old_title) else old_title
        old_desc = p.get('descriptionHtml', '') or ''
        new_desc = scrub_dashes(old_desc) if DASH_RE.search(old_desc) else old_desc

        print(f'\n  {old_title[:60]}')
        if new_title != old_title:
            print(f'    title → {new_title[:60]}')
        if new_desc != old_desc:
            print(f'    description: dashes scrubbed')

        # 1. Zoho first (source of truth moves first)
        skus = [v['node']['sku'] for v in p.get('variants', {}).get('edges', [])
                if v['node'].get('sku')]
        for sku in skus:
            before = len(log)
            fix_zoho_item(sku, token, args.dry_run, log)
            if len(log) > before:
                counts['zoho_items'] += 1

        if args.dry_run:
            if new_title != old_title:
                counts['titles'] += 1
            if new_desc != old_desc:
                counts['descriptions'] += 1
            for o in p.get('options', []) or []:
                for v in o.get('optionValues', []) or []:
                    if DASH_RE.search(v.get('name', '')):
                        counts['option_values'] += 1
                        print(f'    option value → {scrub_dashes(v["name"])}')
            continue

        # 2. Shopify product title + description
        if new_title != old_title or new_desc != old_desc:
            res = shopify_graphql(
                "mutation($in: ProductInput!) { productUpdate(input: $in) { product { id } userErrors { message } } }",
                {'in': {'id': pid, 'title': new_title, 'descriptionHtml': new_desc}})
            errs = (res.get('data', {}).get('productUpdate', {}) or {}).get('userErrors', [])
            if res.get('errors'):
                errs = errs + res['errors']
            if errs:
                counts['errors'] += 1
                log.append({'product': old_title, 'shopify_error': errs})
                print(f'    ✗ productUpdate failed: {json.dumps(errs)[:100]}')
                continue
            if new_title != old_title:
                counts['titles'] += 1
            if new_desc != old_desc:
                counts['descriptions'] += 1

        # 3. Shopify option values
        for o in p.get('options', []) or []:
            to_update = [{'id': v['id'], 'name': scrub_dashes(v['name'])}
                         for v in o.get('optionValues', []) or []
                         if DASH_RE.search(v.get('name', ''))]
            if not to_update:
                continue
            res = shopify_graphql("""
                mutation($productId: ID!, $option: OptionUpdateInput!, $vals: [OptionValueUpdateInput!]) {
                  productOptionUpdate(productId: $productId, option: $option, optionValuesToUpdate: $vals) {
                    userErrors { code message }
                  }
                }""", {'productId': pid, 'option': {'id': o['id']}, 'vals': to_update})
            errs = (res.get('data', {}).get('productOptionUpdate', {}) or {}).get('userErrors', [])
            if res.get('errors'):
                errs = errs + res['errors']
            if errs:
                counts['errors'] += 1
                log.append({'product': old_title, 'option': o['name'], 'shopify_error': errs})
                print(f'    ✗ option update failed: {json.dumps(errs)[:100]}')
            else:
                counts['option_values'] += len(to_update)
                for u in to_update:
                    print(f'    option value → {u["name"]}')
        time.sleep(0.4)

    with open(REPORT, 'w') as f:
        json.dump({'dry_run': args.dry_run, 'counts': counts, 'log': log}, f,
                  indent=1, ensure_ascii=False)

    print('\n' + '═' * 60)
    print(f'  Titles fixed        : {counts["titles"]}')
    print(f'  Descriptions fixed  : {counts["descriptions"]}')
    print(f'  Option values fixed : {counts["option_values"]}')
    print(f'  Zoho items touched  : {counts["zoho_items"]}')
    print(f'  Errors              : {counts["errors"]}')
    print(f'  Log → {REPORT}')
    print('═' * 60)


if __name__ == '__main__':
    main()
