#!/usr/bin/env python3
"""
SKU reconciler — repairs Shopify variants carrying stale/wrong SKUs.

Zoho is the source of truth for SKUs. Some Shopify variants still carry
pre-renumbering SKUs. Two failure classes:

  ORPHAN : variant SKU does not exist in Zoho at all
  DRIFT  : variant SKU now belongs to a COMPLETELY DIFFERENT Zoho item

DRIFT is the dangerous one: when the Zoho item that legitimately owns the SKU
syncs, the sync finds the SKU already on Shopify, takes the collision path
("update in place, never re-parent") and writes that item's image/price onto
the unrelated product. This is how Cataflam images landed on Philips Oral
Hygiene (variant 'BREATH RX TONGUE SCRAPPER' held 460-110-001, which is
Cataflam in Zoho; the real scraper is 480-110-004).

Repair = point the variant at the Zoho item it actually IS, matched by name.
Matching is fuzzy, so anything below --min-score is reported, never guessed.

Usage:
    python execution/reconcile_shopify_skus.py                 # dry run report
    python execution/reconcile_shopify_skus.py --apply
    python execution/reconcile_shopify_skus.py --apply --min-score 0.7
"""

import sys
import os
import re
import json
import time
import argparse
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from common import (shopify_graphql, get_zoho_token, zoho_headers,
                    ZOHO_API_BASE, ZOHO_ORG_ID)

DRIFT_THRESHOLD = 0.34    # below this, the SKU's Zoho item is a different product
EXACT_SCORE     = 0.95    # name match this good is trusted on its own

# Zoho contains junk SKU values that are truthy strings — never treat as real
INVALID_SKUS = {'null', 'none', 'n/a', 'na', '-', '--', 'tbd', 'xxx'}

# Words that carry no distinguishing meaning when comparing a variant to an item
STOPWORDS = {'default', 'title', 'standard', 'the', 'and', 'of', 'pk', 'pcs', 'pack'}


def norm(s):
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', (s or '').lower()).split())


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def tokens(s):
    return {t for t in norm(s).split() if t and t not in STOPWORDS}


def variant_tokens_supported(v, zoho_name):
    """
    Every distinguishing token in the Shopify variant label must appear in the
    Zoho item name. This is the guard that fuzzy score alone cannot provide:
      'Monoject Needles / 30G Blue Short' vs 'TRANSCOJECT NEEDLES 25G SHORT'
        → '30g','blue','monoject' missing → REJECT (wrong brand AND gauge)
      'Plasdent Patient Bibs / Pink'      vs 'PLASDENT BIBS 2-PLY PINK'
        → all tokens present → accept
    """
    zt = tokens(zoho_name)
    vt = tokens(v['variant'])
    # Brand/product identity from the Shopify product title must survive too
    pt = tokens(v['product'])
    missing_variant = {t for t in vt if t not in zt and not any(t in z for z in zt)}
    missing_product = {t for t in pt if t not in zt and not any(t in z for z in zt)}
    # Product titles are wordier than item names; require the majority to land
    product_ok = (not pt) or (len(missing_product) <= len(pt) / 2)
    return (not missing_variant) and product_ok, missing_variant | missing_product


def fetch_zoho_items(token):
    """[{sku, name, item_id}] for every Zoho item with a SKU."""
    out, page = [], 1
    while True:
        r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID, 'per_page': 200,
                                 'page': page}, timeout=20)
        if r.status_code == 429:
            time.sleep(60)
            continue
        d = r.json()
        for it in d.get('items', []):
            sku = (it.get('sku') or '').strip()
            if sku and sku.lower() not in INVALID_SKUS:
                out.append({'sku': sku, 'name': it.get('name', ''),
                            'item_id': it['item_id']})
        if not d.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return out


def fetch_shopify_variants():
    q = '''query($c: String) { products(first: 250, after: $c, query: "status:active") {
      pageInfo { hasNextPage endCursor }
      edges { node { id title handle
        variants(first: 40) { edges { node { id sku title } } } } } } }'''
    out, cursor = [], None
    while True:
        d = shopify_graphql(q, {'c': cursor})
        pd = d['data']['products']
        for e in pd['edges']:
            n = e['node']
            for v in n['variants']['edges']:
                vn = v['node']
                if vn['sku']:
                    out.append({'product_id': n['id'], 'product': n['title'],
                                'handle': n['handle'], 'variant_id': vn['id'],
                                'sku': vn['sku'], 'variant': vn['title']})
        if not pd['pageInfo']['hasNextPage']:
            break
        cursor = pd['pageInfo']['endCursor']
    return out


def best_zoho_match(v, zoho_items, taken):
    """Best Zoho item for a Shopify variant, by name similarity."""
    # 'Product / Variant' reads closest to a Zoho item name
    cands = [f"{v['product']} {v['variant']}", v['variant'], v['product']]
    best, best_score = None, 0.0
    for z in zoho_items:
        if z['sku'] in taken:
            continue
        score = max(sim(z['name'], c) for c in cands)
        if score > best_score:
            best, best_score = z, score
    return best, best_score


def set_variant_sku(product_id, variant_id, sku):
    q = '''mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id sku } userErrors { field message } } }'''
    d = shopify_graphql(q, {'productId': product_id,
                            'variants': [{'id': variant_id, 'inventoryItem': {'sku': sku}}]})
    res = d.get('data', {}).get('productVariantsBulkUpdate') or {}
    errs = res.get('userErrors') or []
    return (not errs), '; '.join(e['message'] for e in errs)


def main():
    ap = argparse.ArgumentParser(description='Reconcile Shopify variant SKUs against Zoho')
    ap.add_argument('--apply', action='store_true', help='Write changes (default: dry run)')
    ap.add_argument('--min-score', type=float, default=0.62,
                    help='Minimum name-match confidence to auto-correct (default 0.62)')
    ap.add_argument('--out', default='sku_reconcile_report.json')
    args = ap.parse_args()

    print('═' * 68)
    print('  Shopify SKU Reconciler' + ('' if args.apply else '  (DRY RUN)'))
    print('═' * 68)

    token = get_zoho_token()
    zoho_items = fetch_zoho_items(token)
    zoho_by_sku = {z['sku']: z for z in zoho_items}
    variants = fetch_shopify_variants()
    print(f'\n  Zoho SKUs: {len(zoho_items)}   Shopify variants: {len(variants)}')

    live_skus = {v['sku'] for v in variants}
    broken = []
    for v in variants:
        z = zoho_by_sku.get(v['sku'])
        if z is None:
            v['problem'] = 'ORPHAN'
            v['zoho_name_for_sku'] = None
            broken.append(v)
        else:
            score = max(sim(z['name'], f"{v['product']} {v['variant']}"),
                        sim(z['name'], v['variant']), sim(z['name'], v['product']))
            if score < DRIFT_THRESHOLD:
                v['problem'] = 'DRIFT'
                v['zoho_name_for_sku'] = z['name']
                broken.append(v)

    print(f'  Broken variants: {len(broken)} '
          f"({sum(1 for b in broken if b['problem']=='ORPHAN')} orphan, "
          f"{sum(1 for b in broken if b['problem']=='DRIFT')} drift)\n")

    # Never reassign a SKU that another (healthy) variant already uses
    healthy_skus = live_skus - {b['sku'] for b in broken}
    taken = set(healthy_skus)

    auto, manual = [], []
    for b in sorted(broken, key=lambda x: x['problem']):
        z, score = best_zoho_match(b, zoho_items, taken)
        rec = {**b, 'proposed_sku': z['sku'] if z else None,
               'proposed_name': z['name'] if z else None, 'score': round(score, 2)}
        if not z:
            rec['reject_reason'] = 'no candidate'
            manual.append(rec)
            continue

        supported, missing = variant_tokens_supported(b, z['name'])
        # Trust an (almost) exact name match outright; otherwise the variant's
        # distinguishing tokens must be backed by the item name.
        if score >= EXACT_SCORE or (score >= args.min_score and supported):
            rec['reject_reason'] = ''
            auto.append(rec)
            taken.add(z['sku'])
        else:
            rec['reject_reason'] = (f"unsupported tokens: {', '.join(sorted(missing))}"
                                    if not supported else f'score {rec["score"]} below {args.min_score}')
            manual.append(rec)

    print(f'  AUTO-CORRECT ({len(auto)}, score ≥ {args.min_score}):')
    for r in auto:
        print(f"    [{r['problem']:<6}] {r['sku']:<16} → {r['proposed_sku']:<16} ({r['score']})")
        print(f"             {r['product'][:30]:<32} / {r['variant'][:20]:<22} ≈ {r['proposed_name'][:34]}")

    print(f'\n  MANUAL REVIEW ({len(manual)}, low confidence):')
    for r in manual:
        pn = (r["proposed_name"] or "(no candidate)")[:28]
        print(f"    [{r['problem']:<6}] {r['sku']:<16} {r['product'][:26]:<28} / {r['variant'][:16]:<18} "
              f"best={pn} ({r['score']}) — {r.get('reject_reason','')[:40]}")

    with open(args.out, 'w') as f:
        json.dump({'auto': auto, 'manual': manual}, f, indent=1)
    print(f'\n  Report → {args.out}')

    if not args.apply:
        print(f'\n  Dry run — nothing written. Re-run with --apply to correct {len(auto)} SKU(s).')
        print('═' * 68)
        return

    print(f'\n  Correcting {len(auto)} variant SKU(s) on Shopify...')
    ok = fail = 0
    for r in auto:
        good, err = set_variant_sku(r['product_id'], r['variant_id'], r['proposed_sku'])
        if good:
            ok += 1
            print(f"    ✓ {r['sku']} → {r['proposed_sku']}  ({r['product'][:34]})")
        else:
            fail += 1
            print(f"    ✗ {r['sku']}: {err}")
        time.sleep(0.3)

    print()
    print('═' * 68)
    print(f'  Corrected: {ok}   Failed: {fail}   Manual: {len(manual)}')
    print('═' * 68)
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    main()
