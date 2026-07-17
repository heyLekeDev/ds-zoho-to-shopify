#!/usr/bin/env python3
"""
Category tagger — makes Shopify smart collections actually work.

Every smart collection is defined as TAG EQUALS '<Parent Category>', but the
AI enrichment wrote free-form descriptive tags ("composite", "Oral Surgery",
"150-Toothbrushes & Tongue Cleaners"), so almost nothing matched: the
Biomaterials collection held 1 product instead of all of them.

This adds the ONE canonical category tag each product needs, derived from its
SKU prefix via 'DS SKU map.csv' (the authoritative taxonomy). Existing tags are
preserved — the canonical tag is appended, never a replacement.

Zoho stays the source of truth: cf_shopify_tags is updated too, so the next
sync does not undo the change.

Usage:
    python execution/tag_categories.py --dry-run     # report only (default)
    python execution/tag_categories.py --apply       # write to Shopify + Zoho
    python execution/tag_categories.py --apply --shopify-only
"""

import sys
import os
import csv
import time
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from common import (PROJECT_DIR, shopify_graphql, get_zoho_token, zoho_headers,
                    zoho_item_detail, zoho_write_fields, ZOHO_API_BASE, ZOHO_ORG_ID)

SKU_MAP_CSV = os.path.join(PROJECT_DIR, 'DS SKU map.csv')

# Categories whose collection is named differently on Shopify
CATEGORY_TO_COLLECTION = {
    'Radiography Supplies': 'X-ray',
}

# Categories that must never be published/tagged (not sellable)
NON_SELLABLE = {'Services', 'Training'}


def load_prefix_map():
    """
    Returns (by_pair, by_prefix):
      by_pair   : 'CCC-SSS' → canonical tag   (full validated taxonomy pair)
      by_prefix : 'CCC'     → canonical tag

    Validating the FULL pair matters: several live products still carry
    pre-renumbering SKUs (e.g. BeeSure Gloves as 180-160-005). Prefix 180 maps
    to Cosmetics, so a prefix-only lookup confidently tags gloves "Cosmetics".
    A stale SKU has no valid pair, so it is reported instead of mis-tagged.
    """
    by_pair, by_prefix = {}, {}
    with open(SKU_MAP_CSV, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            code = (row.get('Cat Code') or '').strip()
            cat = (row.get('Category') or '').strip()
            pref = (row.get('Prefix') or '').strip()   # e.g. '200-170-xxx'
            if not ('-' in code and cat):
                continue
            tag = CATEGORY_TO_COLLECTION.get(cat, cat)
            by_prefix[code.split('-')[0]] = tag
            parts = pref.split('-')
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                by_pair[f'{parts[0]}-{parts[1]}'] = tag
    return by_pair, by_prefix


def fetch_zoho_sku_index(token):
    """One paginated sweep: sku → item_id. Replaces a search call per SKU."""
    idx, page = {}, 1
    while True:
        r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID, 'per_page': 200,
                                 'page': page}, timeout=20)
        if r.status_code == 429:
            time.sleep(60)
            continue
        d = r.json()
        for it in d.get('items', []):
            if it.get('sku'):
                idx[it['sku']] = it['item_id']
        if not d.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return idx


def fetch_active_products():
    q = '''query($c: String) { products(first: 250, after: $c, query: "status:active") {
      pageInfo { hasNextPage endCursor }
      edges { node { id title tags totalInventory
        variants(first: 20) { edges { node { sku } } } } } } }'''
    out, cursor = [], None
    while True:
        d = shopify_graphql(q, {'c': cursor})
        pd = d['data']['products']
        for e in pd['edges']:
            n = e['node']
            skus = [v['node']['sku'] for v in n['variants']['edges'] if v['node']['sku']]
            out.append({'id': n['id'], 'title': n['title'], 'tags': n['tags'],
                        'skus': skus, 'inv': n['totalInventory'] or 0})
        if not pd['pageInfo']['hasNextPage']:
            break
        cursor = pd['pageInfo']['endCursor']
    return out


def category_for(prod, maps):
    """
    Canonical tag for a product, from its variants' SKUs.
    Only a SKU whose full 'CCC-SSS' pair exists in the taxonomy is trusted;
    a valid prefix with an unknown sub-code means the SKU predates the
    renumbering and must be reported, never guessed.
    Returns (tag, reason).
    """
    by_pair, by_prefix = maps
    pairs, stale = [], []
    for s in prod['skus']:
        parts = s.split('-')
        if len(parts) < 2:
            continue
        pair = f'{parts[0]}-{parts[1]}'
        if pair in by_pair:
            pairs.append(pair)
        else:
            stale.append(s)

    if not pairs:
        if stale:
            bad = stale[0]
            pfx = bad.split('-')[0]
            hint = (f'prefix {pfx}={by_prefix[pfx]} but sub-code unknown — stale SKU'
                    if pfx in by_prefix else f'prefix {pfx} not in SKU map')
            return None, f'{bad}: {hint}'
        return None, 'no parseable SKU'

    tags = {by_pair[p] for p in pairs}
    top_pair, _ = Counter(pairs).most_common(1)[0]
    cat = by_pair[top_pair]
    if cat in NON_SELLABLE:
        return None, f'{cat} is not sellable'
    if stale:
        return None, f'mixed valid+stale SKUs (stale: {stale[0]}) — needs SKU fix first'
    if len(tags) > 1:
        return None, f'variants span categories {sorted(tags)} — needs review'
    return cat, ''


def shopify_set_tags(product_id, tags):
    q = '''mutation($input: ProductInput!) { productUpdate(input: $input) {
      product { id } userErrors { field message } } }'''
    d = shopify_graphql(q, {'input': {'id': product_id, 'tags': tags}})
    errs = (d.get('data', {}).get('productUpdate') or {}).get('userErrors') or []
    return (not errs), '; '.join(e['message'] for e in errs)


def main():
    parser = argparse.ArgumentParser(description='Add canonical category tags for smart collections')
    parser.add_argument('--apply', action='store_true', help='Write changes (default: dry run)')
    parser.add_argument('--shopify-only', action='store_true',
                        help='Skip the Zoho cf_shopify_tags write-back')
    parser.add_argument('--limit', type=int, help='Only process the first N products needing a tag')
    parser.add_argument('--fix-stale', action='store_true',
                        help='Also REMOVE category tags that contradict the product\'s current SKU '
                             '(needed after a SKU correction — a tag applied from a drifted SKU '
                             'survives otherwise, e.g. a bib holder left tagged Cosmetics)')
    args = parser.parse_args()

    maps = load_prefix_map()
    print('═' * 64)
    print('  Category Tagger' + ('' if args.apply else '  (DRY RUN)'))
    print('═' * 64)

    prods = fetch_active_products()
    print(f'\n  {len(prods)} active products on Shopify')

    all_cats = set(maps[1].values()) | set(maps[0].values())

    todo, skipped, stale = [], [], []
    for p in prods:
        cat, reason = category_for(p, maps)
        if not cat:
            skipped.append((p, reason))
            continue
        wrong = [t for t in p['tags'] if t in all_cats and t != cat]
        if wrong:
            stale.append((p, cat, wrong))
        if cat in p['tags']:
            continue   # already correct
        todo.append((p, cat, reason))

    if stale:
        print(f'\n  Products carrying a CONTRADICTORY category tag: {len(stale)}')
        for p, cat, wrong in stale[:12]:
            print(f"    {p['title'][:38]:<40} has {wrong} — should be [{cat}]")
        if not args.fix_stale:
            print('    (re-run with --fix-stale to remove them)')

    after = Counter()
    for p in prods:
        cat, _ = category_for(p, maps)
        if cat:
            after[cat] += 1

    print(f'  Need the canonical tag : {len(todo)}')
    print(f'  Already tagged         : {len(prods) - len(todo) - len(skipped)}')
    print(f'  Skipped (unmappable)   : {len(skipped)}')

    print('\n  Collection membership after tagging (in-stock / total):')
    instock = Counter()
    for p in prods:
        cat, _ = category_for(p, maps)
        if cat and p['inv'] > 0:
            instock[cat] += 1
    for cat in sorted(after):
        print(f'    {cat:<24} {instock[cat]:>4} / {after[cat]:<4}')

    if skipped:
        print(f'\n  Skipped products ({len(skipped)}):')
        for p, reason in skipped[:12]:
            print(f'    {p["title"][:44]:<46} {reason}')
        if len(skipped) > 12:
            print(f'    ... and {len(skipped) - 12} more')

    if not args.apply:
        print(f'\n  Dry run — nothing written. Re-run with --apply to tag {len(todo)} product(s).')
        print('═' * 64)
        return

    work = todo[:args.limit] if args.limit else todo
    print(f'\n  Applying canonical tag to {len(work)} product(s)...')
    token = sku_index = None
    if not args.shopify_only:
        token = get_zoho_token()
        print('  Indexing Zoho SKUs...')
        sku_index = fetch_zoho_sku_index(token)
        print(f'  {len(sku_index)} Zoho SKUs indexed.\n')
    ok_shop = ok_zoho = fail = 0

    if args.fix_stale and stale:
        print(f'\n  Removing contradictory category tags from {len(stale)} product(s)...')
        for p, cat, wrong in stale:
            cleaned = sorted((set(p['tags']) - set(wrong)) | {cat})
            ok, err = shopify_set_tags(p['id'], cleaned)
            print(f"    {'✓' if ok else '✗'} {p['title'][:38]:<40} −{wrong} +[{cat}]")
            p['tags'] = cleaned          # keep in-memory state truthful
            time.sleep(0.2)
        # a stale-fixed product already has its correct tag now
        todo = [t for t in todo if t[0]['id'] not in {p['id'] for p, _, _ in stale}]
        work = todo[:args.limit] if args.limit else todo

    for p, cat, _ in work:
        new_tags = sorted(set(p['tags']) | {cat})
        ok, err = shopify_set_tags(p['id'], new_tags)
        if ok:
            ok_shop += 1
            print(f'    ✓ {p["title"][:44]:<46} +{cat}')
        else:
            fail += 1
            print(f'    ✗ {p["title"][:44]:<46} {err}')
            continue

        # Keep Zoho in step so the next sync does not overwrite the tag
        if not args.shopify_only:
            for sku in p['skus']:
                item_id = sku_index.get(sku)
                if not item_id:
                    continue
                detail = zoho_item_detail(item_id, token)
                existing = [t.strip() for t in (detail['cf'].get('cf_shopify_tags') or '').split(',') if t.strip()]
                if cat in existing:
                    continue
                merged = ', '.join(sorted(set(existing) | {cat}))
                okz, _ = zoho_write_fields(item_id, {'cf_shopify_tags': merged}, token)
                ok_zoho += 1 if okz else 0
                time.sleep(0.2)
        time.sleep(0.15)

    print()
    print('═' * 64)
    print(f'  Shopify tagged : {ok_shop}')
    if not args.shopify_only:
        print(f'  Zoho updated   : {ok_zoho}')
    if fail:
        print(f'  Failed         : {fail}')
    print('═' * 64)
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
