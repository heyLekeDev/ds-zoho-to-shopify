#!/usr/bin/env python3
"""
Collection fixer — creates missing category collections and adjusts their rules.

Two jobs, both driven by 'DS SKU map.csv' (the authoritative taxonomy):

1. --create-missing : create a smart collection for any sellable category that
   has none. Rule matches the existing house pattern: TAG EQUALS <category>.

2. --show-out-of-stock : drop the 'inventory > 0' rule from category
   collections, so published-but-out-of-stock products stay browsable
   instead of vanishing from the storefront.

Usage:
    python execution/fix_collections.py --create-missing              # dry run
    python execution/fix_collections.py --create-missing --apply
    python execution/fix_collections.py --show-out-of-stock --apply
"""

import sys
import os
import csv
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROJECT_DIR, shopify_graphql

SKU_MAP_CSV = os.path.join(PROJECT_DIR, 'DS SKU map.csv')
CATEGORY_TO_COLLECTION = {'Radiography Supplies': 'X-ray'}
NON_SELLABLE = {'Services', 'Training'}

# Collections that are brand/catch-all, not category — leave their rules alone
NON_CATEGORY_TITLES = {
    'Bicon Implants', 'Geistlich', 'Medesy', 'Waterpik', 'Carestream Dental',
    'Philips Healthcare', 'Coltene', 'EthOss', 'All Products', 'Training',
}


def sellable_categories():
    cats = set()
    with open(SKU_MAP_CSV, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            cat = (row.get('Category') or '').strip()
            if cat and cat not in NON_SELLABLE:
                cats.add(CATEGORY_TO_COLLECTION.get(cat, cat))
    return sorted(cats)


def fetch_collections():
    q = '''{ collections(first: 100) { edges { node {
      id title handle productsCount { count }
      ruleSet { appliedDisjunctively rules { column relation condition } }
    } } } }'''
    d = shopify_graphql(q)
    return [e['node'] for e in d['data']['collections']['edges']]


def create_collection(title, column='TAG', condition=None):
    q = '''mutation($input: CollectionInput!) { collectionCreate(input: $input) {
      collection { id title } userErrors { field message } } }'''
    d = shopify_graphql(q, {'input': {
        'title': title,
        'ruleSet': {'appliedDisjunctively': False,
                    'rules': [{'column': column, 'relation': 'EQUALS',
                               'condition': condition or title}]},
    }})
    res = d.get('data', {}).get('collectionCreate') or {}
    errs = res.get('userErrors') or []
    return (res.get('collection') if not errs else None), '; '.join(e['message'] for e in errs)


def fetch_vendor_counts():
    """Active-product count per vendor."""
    from collections import Counter
    q = '''query($c:String){ products(first:250, after:$c, query:"status:active"){
      pageInfo{hasNextPage endCursor} edges{node{vendor}}}}'''
    counts, cursor = Counter(), None
    while True:
        d = shopify_graphql(q, {'c': cursor})
        pd = d['data']['products']
        for e in pd['edges']:
            counts[(e['node']['vendor'] or '').strip()] += 1
        if not pd['pageInfo']['hasNextPage']:
            break
        cursor = pd['pageInfo']['endCursor']
    return counts


def set_rules(collection_id, rules, disjunctive=False):
    q = '''mutation($input: CollectionInput!) { collectionUpdate(input: $input) {
      collection { id } userErrors { field message } } }'''
    d = shopify_graphql(q, {'input': {
        'id': collection_id,
        'ruleSet': {'appliedDisjunctively': disjunctive, 'rules': rules},
    }})
    res = d.get('data', {}).get('collectionUpdate') or {}
    errs = res.get('userErrors') or []
    return (not errs), '; '.join(e['message'] for e in errs)


def main():
    parser = argparse.ArgumentParser(description='Create missing / fix category collections')
    parser.add_argument('--create-missing', action='store_true')
    parser.add_argument('--show-out-of-stock', action='store_true',
                        help="Remove the 'inventory > 0' rule from category collections")
    parser.add_argument('--create-brands', action='store_true',
                        help='Create VENDOR collections for real brands with >= --min-products')
    parser.add_argument('--min-products', type=int, default=2)
    parser.add_argument('--apply', action='store_true', help='Write changes (default: dry run)')
    args = parser.parse_args()
    if not (args.create_missing or args.show_out_of_stock or args.create_brands):
        parser.error('Choose --create-missing, --show-out-of-stock and/or --create-brands')

    print('═' * 64)
    print('  Collection Fixer' + ('' if args.apply else '  (DRY RUN)'))
    print('═' * 64)

    cols = fetch_collections()
    by_title = {c['title']: c for c in cols}

    if args.create_missing:
        missing = [c for c in sellable_categories() if c not in by_title]
        print(f'\n  Missing category collections: {len(missing)}')
        for t in missing:
            print(f'    + {t}')
        if args.apply and missing:
            print()
            for t in missing:
                col, err = create_collection(t)
                print(f"    {'✓' if col else '✗'} {t}" + (f'  {err}' if err else ''))
                time.sleep(0.4)

    if args.create_brands:
        from normalize_vendors import KNOWN_BRANDS
        # Existing VENDOR-rule collections and the vendor string each targets
        vendor_cols = {}
        for c in cols:
            rs = c.get('ruleSet')
            if rs:
                for r in rs['rules']:
                    if r['column'] == 'VENDOR':
                        vendor_cols[r['condition'].lower()] = c

        counts = fetch_vendor_counts()
        # 1. Fix existing VENDOR rules whose condition no longer matches any product
        print('\n  Existing brand collections with a stale vendor rule:')
        stale_rule = 0
        for cond, c in vendor_cols.items():
            if counts.get(c['title'], 0) == 0 and cond != c['title'].lower():
                # rule condition differs from the collection title and matches nothing
                actual = next((v for v in counts if v.lower() == c['title'].lower()), None)
                if actual and counts[actual]:
                    print(f"    {c['title']:<20} rule='{cond}' → '{actual}' ({counts[actual]} products)")
                    stale_rule += 1
                    if args.apply:
                        set_rules(c['id'], [{'column': 'VENDOR', 'relation': 'EQUALS',
                                             'condition': actual}], False)
        if not stale_rule:
            print('    (none)')

        # 2. Create collections for real brands lacking one
        make = []
        for vendor, n in sorted(counts.items(), key=lambda x: -x[1]):
            if n < args.min_products:
                continue
            if not vendor or vendor.lower() in {'generic', 'assorted', 'n/a'}:
                continue
            if vendor.lower() in vendor_cols or vendor in by_title:
                continue
            if vendor.lower() in KNOWN_BRANDS:
                make.append((vendor, n))

        skipped = [(v, n) for v, n in sorted(counts.items(), key=lambda x: -x[1])
                   if n >= args.min_products and v and v.lower() not in {'generic', 'assorted', 'n/a'}
                   and v.lower() not in vendor_cols and v not in by_title
                   and v.lower() not in KNOWN_BRANDS]

        print(f'\n  Brand collections to CREATE ({len(make)}):')
        for v, n in make:
            print(f'    + {v:<22} ({n} products)')
        if args.apply and make:
            print()
            for v, n in make:
                col, err = create_collection(v, column='VENDOR', condition=v)
                print(f"    {'✓' if col else '✗'} {v}" + (f'  {err}' if err else ''))
                time.sleep(0.4)

        print(f'\n  NOT created — not in known-brand list, confirm if real ({len(skipped)}):')
        for v, n in skipped[:40]:
            print(f'    ? {v:<26} ({n})')

    if args.show_out_of_stock:
        targets = []
        for c in cols:
            if c['title'] in NON_CATEGORY_TITLES or not c.get('ruleSet'):
                continue
            rules = c['ruleSet']['rules']
            if not any(r['column'] == 'TAG' for r in rules):
                continue
            if any(r['column'] == 'VARIANT_INVENTORY' for r in rules):
                targets.append(c)

        print(f'\n  Category collections hiding out-of-stock items: {len(targets)}')
        for c in targets:
            print(f"    {c['title']:<24} {(c.get('productsCount') or {}).get('count', 0):>4} visible now")

        if args.apply and targets:
            print()
            for c in targets:
                kept = [{'column': r['column'], 'relation': r['relation'], 'condition': r['condition']}
                        for r in c['ruleSet']['rules'] if r['column'] != 'VARIANT_INVENTORY']
                if not kept:
                    print(f"    ⚠ {c['title']}: only rule was the stock rule — skipped")
                    continue
                ok, err = set_rules(c['id'], kept, c['ruleSet']['appliedDisjunctively'])
                print(f"    {'✓' if ok else '✗'} {c['title']}" + (f'  {err}' if err else ''))
                time.sleep(0.4)

    if not args.apply:
        print('\n  Dry run — nothing written. Re-run with --apply.')
    print('═' * 64)


if __name__ == '__main__':
    main()
