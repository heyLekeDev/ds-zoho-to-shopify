#!/usr/bin/env python3
"""
Vendor normalizer — makes brand (vendor-rule) collections work.

Brand collections match VENDOR EQUALS '<Brand>'. The Shopify vendor field has
drifted from the authoritative Zoho `brand`: typos ('Carestream dental'),
case ('Ethoss'), sub-brands split from parents ('PHILIPS ZOOM'), and plain
wrong values ('Hu-Friedy'/'Generic' on Medesy instruments). So brand
collections silently miss products.

Rule: Zoho `brand` is the source of truth. Canonical vendor =
  1. ALIASES[vendor-or-brand]     (sub-brand → parent, case/typo fixes)
  2. else the Zoho brand           (when non-blank and not 'Generic')
  3. else keep the current vendor  (don't downgrade a real vendor to Generic)

Writes BOTH Shopify vendor and Zoho `brand` so the next sync won't undo it.

Usage:
    python execution/normalize_vendors.py            # dry run
    python execution/normalize_vendors.py --apply
"""

import sys
import os
import time
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from common import (shopify_graphql, get_zoho_token, zoho_headers,
                    ZOHO_API_BASE, ZOHO_ORG_ID)

# Sub-brand / typo / case → canonical parent brand. Keys are lowercase.
ALIASES = {
    'carestream dental': 'Carestream',
    'ethoss':            'EthOss',
    'philips zoom':      'Philips',
    'philips healthcare':'Philips',
}

GENERIC_VENDORS = {'', 'generic', 'n/a', 'na', 'unknown', 'assorted'}

# Recognized manufacturers. Zoho `brand` is only allowed to OVERRIDE a real
# Shopify vendor when the Zoho brand is one of these — otherwise a product-line
# value in the Zoho brand field (e.g. 'One-Touch', 'Sponge-Style') would wrongly
# replace the true manufacturer (UnoDent, Plasdent). Lowercase.
KNOWN_BRANDS = {
    # exclusive suppliers
    'bicon', 'medesy', 'geistlich', 'ethoss', 'coltene', 'carestream',
    'philips', 'waterpik', 'cefla', 'tavom', 'cattani', 'planmeca',
    'unodent', 'komet',
    # other genuine manufacturers seen in the catalogue
    'vivid', 'pearson', 'hu-friedy', 'oral-b', 'maillefer', 'tepe', 'plasdent',
    'colgate', 'deprag', 'roeko', 'ss white', "n'sure", 'zirc', 'septodont',
    'sultan', 'two striper', 'beesure', 'monoject', 'dentsply', 'kerr', 'gc',
    'dux dental', 'prevest denpro', 'dedeco', 'lewa', 'dfs', 'castellini',
    'ethicon', 'swann-morton', 'bonart', 'young', 'kodak', 'diatech',
    'stoddard', 'stddard', 'transcoject', 'dentaurum', 'plasdent',
    # confirmed real manufacturers added 2026-07-11
    'miltex', 'kulzer', 'renfert', 'eschmann', 'keystone', 'coricama', 'fixodent',
}


def fetch_zoho_brand_and_id(token):
    """sku → (brand, item_id) from live Zoho."""
    out, page = {}, 1
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
                out[it['sku']] = ((it.get('brand') or '').strip(), it['item_id'])
        if not d.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return out


def fetch_products():
    q = '''query($c:String){ products(first:250, after:$c, query:"status:active"){
      pageInfo{hasNextPage endCursor}
      edges{node{id title vendor variants(first:20){edges{node{sku}}}}}}}'''
    out, cursor = [], None
    while True:
        d = shopify_graphql(q, {'c': cursor})
        pd = d['data']['products']
        for e in pd['edges']:
            n = e['node']
            skus = [v['node']['sku'] for v in n['variants']['edges'] if v['node']['sku']]
            out.append({'id': n['id'], 'title': n['title'],
                        'vendor': (n['vendor'] or '').strip(), 'skus': skus})
        if not pd['pageInfo']['hasNextPage']:
            break
        cursor = pd['pageInfo']['endCursor']
    return out


def canonical_vendor(p, zoho):
    """Return (canonical_vendor, zoho_brand, zoho_item_ids)."""
    zbrand = ''
    item_ids = []
    for s in p['skus']:
        b, iid = zoho.get(s, ('', None))
        item_ids.append((s, iid))
        if not zbrand and b:
            zbrand = b
    # Zoho brand wins only when it is a real manufacturer, OR when the current
    # vendor is generic/blank (nothing better to keep). Otherwise keep vendor.
    vendor_generic = p['vendor'].lower() in GENERIC_VENDORS
    zoho_is_brand = bool(zbrand) and zbrand.lower() in KNOWN_BRANDS
    if zoho_is_brand or (vendor_generic and zbrand and zbrand.lower() not in GENERIC_VENDORS):
        base = zbrand
    else:
        base = p['vendor']
    canon = ALIASES.get(base.lower(), base)
    canon = ALIASES.get(canon.lower(), canon)  # one more hop (brand→alias)
    return canon.strip(), zbrand, item_ids


def set_shopify_vendor(product_id, vendor):
    q = '''mutation($input: ProductInput!) { productUpdate(input: $input) {
      product { id vendor } userErrors { message } } }'''
    d = shopify_graphql(q, {'input': {'id': product_id, 'vendor': vendor}})
    res = d.get('data', {}).get('productUpdate') or {}
    errs = res.get('userErrors') or []
    return (not errs), '; '.join(e['message'] for e in errs)


def set_zoho_brand(item_id, brand, token):
    # brand is a NATIVE field, not a custom field — allowed exception
    r = requests.put(f'{ZOHO_API_BASE}/items/{item_id}',
                     headers={**zoho_headers(token), 'Content-Type': 'application/json'},
                     params={'organization_id': ZOHO_ORG_ID},
                     json={'brand': brand}, timeout=15)
    return r.json().get('code') == 0


def main():
    ap = argparse.ArgumentParser(description='Normalize Shopify vendor to canonical brand')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--zoho-too', action='store_true',
                    help="Also write the corrected brand back to Zoho's native brand field")
    args = ap.parse_args()

    print('═' * 64)
    print('  Vendor Normalizer' + ('' if args.apply else '  (DRY RUN)'))
    print('═' * 64)

    token = get_zoho_token()
    zoho = fetch_zoho_brand_and_id(token)
    prods = fetch_products()
    print(f'\n  {len(prods)} active products')

    changes = []
    for p in prods:
        canon, zbrand, item_ids = canonical_vendor(p, zoho)
        if canon and canon != p['vendor']:
            changes.append((p, canon, zbrand, item_ids))

    print(f'  Vendor changes proposed: {len(changes)}\n')
    by_transition = Counter()
    for p, canon, _, _ in changes:
        by_transition[f"{p['vendor'] or '(blank)'} → {canon}"] += 1
    for t, n in by_transition.most_common():
        print(f'    {n:>3}  {t}')

    if not args.apply:
        print('\n  Sample products that would change:')
        for p, canon, zb, _ in changes[:15]:
            print(f"    {p['title'][:40]:<42} '{p['vendor']}' → '{canon}'")
        print(f'\n  Dry run — nothing written. --apply to change {len(changes)} product(s).')
        print('═' * 64)
        return

    print(f'\n  Applying {len(changes)} vendor change(s)...')
    ok = fail = zoho_ok = 0
    for p, canon, zbrand, item_ids in changes:
        good, err = set_shopify_vendor(p['id'], canon)
        if good:
            ok += 1
        else:
            fail += 1
            print(f"    ✗ {p['title'][:40]}: {err}")
            continue
        if args.zoho_too and canon != zbrand:
            for _, iid in item_ids:
                if iid and set_zoho_brand(iid, canon, token):
                    zoho_ok += 1
                time.sleep(0.15)
        time.sleep(0.15)

    print()
    print('═' * 64)
    print(f'  Shopify vendors set : {ok}')
    if args.zoho_too:
        print(f'  Zoho brand writes   : {zoho_ok}')
    if fail:
        print(f'  Failed              : {fail}')
    print('═' * 64)
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
