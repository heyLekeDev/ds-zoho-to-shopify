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


def create_collection(title):
    q = '''mutation($input: CollectionInput!) { collectionCreate(input: $input) {
      collection { id title } userErrors { field message } } }'''
    d = shopify_graphql(q, {'input': {
        'title': title,
        'ruleSet': {'appliedDisjunctively': False,
                    'rules': [{'column': 'TAG', 'relation': 'EQUALS', 'condition': title}]},
    }})
    res = d.get('data', {}).get('collectionCreate') or {}
    errs = res.get('userErrors') or []
    return (res.get('collection') if not errs else None), '; '.join(e['message'] for e in errs)


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
    parser.add_argument('--apply', action='store_true', help='Write changes (default: dry run)')
    args = parser.parse_args()
    if not (args.create_missing or args.show_out_of_stock):
        parser.error('Choose --create-missing and/or --show-out-of-stock')

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
