#!/usr/bin/env python3
"""
Stage 6 — Published-Catalog Reconciler (drift correction)

The pipeline publishes once; after that an item is invisible to the sync (the
custom view only holds Queue for Upload / Update Required / To be Archived).
The native Zoho↔Shopify integration syncs STOCK for channel-mapped items and
orders, but has NO price sync at all (verified against Zoho docs + live drift
scan 2026-07-16: 17 price-drifted SKUs, 24 stock-drifted, 49 missing).

This script closes the gap. Zoho is the source of truth. Every run:
  1. Fetches ALL Zoho items with cf_shopify_status=Published (list endpoint —
     sku, rate, stock, item status come back natively; ~5 pages per 1000 items)
  2. Fetches ALL Shopify variants (price, inventory, product status)
  3. Diffs and reports:
       price drift      — Zoho rate ≠ Shopify price
       stock drift      — Zoho stock_on_hand ≠ Shopify inventoryQuantity
       missing          — Published in Zoho, no Shopify variant with that SKU
       zoho-inactive    — inactive in Zoho but still live on Shopify
       orphans          — ACTIVE Shopify variants with no Published Zoho item
       under-100        — live products priced below the ₦100 floor
  4. With --fix: pushes Zoho prices and stock to Shopify (never the reverse;
     never writes to Zoho). Missing/inactive/orphans are always report-only —
     they need judgment, not blind automation.

Usage:
    python execution/reconcile_published.py              # report only
    python execution/reconcile_published.py --fix        # correct price + stock
    python execution/reconcile_published.py --fix-prices # correct price only
    python execution/reconcile_published.py --json out.json
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_headers, shopify_graphql,
                    ZOHO_API_BASE, ZOHO_ORG_ID)

# Same location the sync writes to (single-location shop)
LOCATION_GID = 'gid://shopify/Location/96401326361'

REPORT_FILE = os.path.join(PROJECT_DIR, '.reconcile_report.json')
EXPIRY_POLICY_FILE = os.path.join(PROJECT_DIR, 'feedback', 'expiry_policy.json')


# ── Expiry watch (policy approved 2026-07-17) ────────────────────────────────

def load_expiry_policy():
    try:
        with open(EXPIRY_POLICY_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def fetch_expiring_items(token):
    """
    Items the team tagged cf_is_batch_item=true, with their nearest expiry.
    Expiry source: min batch expiry (balance>0) for batch-tracked items,
    else cf_expiry_date (the bridge field). Detail-fetched per tagged item —
    the tag keeps this cheap.
    Returns {sku: {'expiry': date, 'purchase_rate': float, 'item_id': str, 'name': str}}
    """
    tagged = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID, 'cf_is_batch_item': 'true',
                    'per_page': 200, 'page': page}, timeout=20)
        if resp.status_code == 429:
            time.sleep(60)
            continue
        data = resp.json()
        tagged.extend(data.get('items', []))
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1

    if len(tagged) > 400:
        print(f'  ⚠ cf_is_batch_item filter returned {len(tagged)} items — '
              'filter may be unsupported; skipping expiry stage this run.')
        return {}

    out = {}
    for it in tagged:
        det_resp = requests.get(
            f'{ZOHO_API_BASE}/items/{it["item_id"]}', headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID}, timeout=15)
        if det_resp.status_code == 429:
            time.sleep(60)
            continue
        det = det_resp.json().get('item', {})
        cf = {c['api_name']: c.get('value', '') for c in det.get('custom_fields', [])}

        expiry = None
        if det.get('track_batch_number'):
            dates = [b.get('expiry_date') for b in det.get('batches', [])
                     if b.get('expiry_date') and float(b.get('balance_quantity') or 0) > 0]
            if dates:
                expiry = min(dates)
        if not expiry:
            expiry = cf.get('cf_expiry_date') or None
        if not expiry:
            continue
        try:
            exp_date = date.fromisoformat(str(expiry)[:10])
        except ValueError:
            continue
        out[det.get('sku', '')] = {
            'expiry': exp_date,
            'purchase_rate': float(det.get('purchase_rate') or 0),
            'item_id': det.get('item_id', ''),
            'name': det.get('name', ''),
        }
        time.sleep(0.25)
    return out


def effective_price(rate, purchase_rate, days_left, policy):
    """(price, markdown_fraction, floored) per the approved tier policy."""
    markdown = 0.0
    for tier in sorted(policy.get('tiers', []), key=lambda t: t['max_days']):
        if days_left <= tier['max_days']:
            markdown = tier['markdown']
            break
    if markdown == 0.0:
        return rate, 0.0, False
    raw = rate * (1 - markdown)
    floored = False
    if purchase_rate and raw < purchase_rate:
        raw = purchase_rate
        floored = True
    step = policy.get('rounding_step', 50)
    price = round(raw / step) * step
    if purchase_rate and price < purchase_rate:
        price += step
    return float(price), markdown, floored


# ── Fetch: Zoho Published items ───────────────────────────────────────────────

def fetch_zoho_published(token):
    items = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID,
                    'cf_shopify_status': 'Published',
                    'per_page': 200, 'page': page},
            timeout=20,
        )
        if resp.status_code == 429:
            print('  Zoho rate limit — waiting 60s...')
            time.sleep(60)
            continue
        data = resp.json()
        items.extend(data.get('items', []))
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return items


# ── Fetch: all Shopify variants ───────────────────────────────────────────────

def fetch_shopify_variants():
    """sku → {variant_id, inv_item_id, product_id, price, qty, product_title, product_status}"""
    variants = {}
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ''
        q = f'''{{
          productVariants(first: 250{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{ node {{
              id sku price compareAtPrice inventoryQuantity
              inventoryItem {{
                id
                inventoryLevel(locationId: "{LOCATION_GID}") {{
                  quantities(names: ["on_hand"]) {{ name quantity }}
                }}
              }}
              product {{ id title status }}
            }} }}
          }}
        }}'''
        data = shopify_graphql(q)
        pv = (data.get('data', {}) or {}).get('productVariants', {})
        for e in pv.get('edges', []):
            n = e['node']
            sku = (n.get('sku') or '').strip()
            if not sku:
                continue
            inv_item = n.get('inventoryItem') or {}
            # Compare ON-HAND, not available: inventoryQuantity subtracts units
            # committed to open orders, which is not drift (Zoho stock_on_hand
            # is an on-hand figure too).
            on_hand = None
            level = inv_item.get('inventoryLevel') or {}
            for qty in level.get('quantities', []) or []:
                if qty.get('name') == 'on_hand':
                    on_hand = qty.get('quantity')
            if on_hand is None:
                on_hand = n.get('inventoryQuantity')  # fallback: available
            variants[sku] = {
                'variant_id':     n['id'],
                'inv_item_id':    inv_item.get('id'),
                'product_id':     n['product']['id'],
                'price':          float(n.get('price') or 0),
                'compare_at':     float(n['compareAtPrice']) if n.get('compareAtPrice') else None,
                'qty':            on_hand,
                'product_title':  n['product']['title'],
                'product_status': n['product']['status'],
            }
        pi = pv.get('pageInfo', {})
        if not pi.get('hasNextPage'):
            break
        cursor = pi['endCursor']
        time.sleep(0.3)
    return variants


# ── Fixers (Shopify-only writes; Zoho is never modified) ─────────────────────

def fix_prices(drifted):
    """Push Zoho rate to Shopify. One productVariantsBulkUpdate per product."""
    by_product = {}
    for d in drifted:
        by_product.setdefault(d['product_id'], []).append(d)

    ok, failed = 0, 0
    for p_id, group in by_product.items():
        variants = [{'id': d['variant_id'], 'price': str(d['zoho_rate']),
                     'compareAtPrice': (str(d['compare_at']) if d.get('compare_at') else None)}
                    for d in group]
        res = shopify_graphql(
            "mutation($pId: ID!, $vs: [ProductVariantsBulkInput!]!) {"
            " productVariantsBulkUpdate(productId: $pId, variants: $vs) {"
            " userErrors { message } } }",
            {'pId': p_id, 'vs': variants})
        errs = (res.get('data', {}).get('productVariantsBulkUpdate', {}) or {}).get('userErrors', [])
        if res.get('errors'):
            errs = errs + res['errors']
        if errs:
            failed += len(group)
            print(f'  ✗ price update failed for {group[0]["product_title"][:50]}: {json.dumps(errs)}')
        else:
            ok += len(group)
            for d in group:
                print(f'  ✓ {d["sku"]}: price {d["shopify_price"]} → {d["zoho_rate"]}')
        time.sleep(0.3)
    return ok, failed


def pull_expired(expired):
    """Set Shopify products with expired stock to DRAFT (off the store)."""
    ok, failed = 0, 0
    for e in expired:
        if e['product_status'] != 'ACTIVE':
            continue
        res = shopify_graphql(
            "mutation($in: ProductInput!) { productUpdate(input: $in) { product { id status } userErrors { message } } }",
            {'in': {'id': e['product_id'], 'status': 'DRAFT'}})
        errs = (res.get('data', {}).get('productUpdate', {}) or {}).get('userErrors', [])
        if res.get('errors'):
            errs = errs + res['errors']
        if errs:
            failed += 1
            print(f'  ✗ pull failed {e["sku"]}: {json.dumps(errs)[:80]}')
        else:
            ok += 1
            print(f'  ✓ {e["sku"]} pulled to DRAFT (expired {e["expiry"]})')
        time.sleep(0.3)
    return ok, failed


def fix_stock(drifted):
    """Set Shopify on-hand quantities to Zoho stock. Batched 100 per mutation."""
    ok, failed = 0, 0
    batch = [d for d in drifted if d.get('inv_item_id')]
    for i in range(0, len(batch), 100):
        chunk = batch[i:i + 100]
        set_qs = [{'inventoryItemId': d['inv_item_id'],
                   'locationId': LOCATION_GID,
                   'quantity': d['zoho_stock']} for d in chunk]
        res = shopify_graphql(
            "mutation($input: InventorySetOnHandQuantitiesInput!) {"
            " inventorySetOnHandQuantities(input: $input) { userErrors { message } } }",
            {'input': {'reason': 'correction', 'setQuantities': set_qs}})
        errs = (res.get('data', {}).get('inventorySetOnHandQuantities', {}) or {}).get('userErrors', [])
        if res.get('errors'):
            errs = errs + res['errors']
        if errs:
            failed += len(chunk)
            print(f'  ✗ stock batch failed: {json.dumps(errs)}')
        else:
            ok += len(chunk)
            for d in chunk:
                print(f'  ✓ {d["sku"]}: stock {d["shopify_stock"]} → {d["zoho_stock"]}')
        time.sleep(0.5)
    return ok, failed


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Stage 6 — Published-catalog reconciler')
    parser.add_argument('--fix',        action='store_true', help='Correct price AND stock drift on Shopify')
    parser.add_argument('--fix-prices', action='store_true', help='Correct price drift only')
    parser.add_argument('--json',       default=REPORT_FILE, help=f'Report path (default {REPORT_FILE})')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 6 — Published-Catalog Reconciler')
    print(f'  Mode: {"FIX price+stock" if args.fix else "FIX prices" if args.fix_prices else "REPORT ONLY"}')
    print('═' * 60)

    token = get_zoho_token()
    print('\n  Fetching Zoho Published items...')
    zoho_items = fetch_zoho_published(token)
    zoho_by_sku = {i['sku']: i for i in zoho_items if i.get('sku')}
    print(f'    {len(zoho_by_sku)} item(s)')

    print('  Fetching Shopify variants...')
    shop = fetch_shopify_variants()
    print(f'    {len(shop)} variant(s)')

    # Expiry watch: nearest expiry per tagged item → effective (marked-down) price
    policy = load_expiry_policy()
    expiring = {}
    if policy:
        print('  Fetching expiry data (cf_is_batch_item items)...')
        expiring = fetch_expiring_items(token)
        print(f'    {len(expiring)} item(s) with an expiry date')

    today = date.today()
    price_drift, stock_drift = [], []
    missing, zoho_inactive, under_100 = [], [], []
    expired_pulls, markdowns, floored_items = [], [], []
    buckets = {b: [] for b in (policy or {}).get('report_buckets_days', [30, 60, 90])}

    for sku, z in zoho_by_sku.items():
        v = shop.get(sku)
        z_rate  = float(z.get('rate') or 0)
        z_stock = int(float(z.get('stock_on_hand') or 0))
        z_active = (z.get('status') == 'active')

        if not v:
            missing.append({'sku': sku, 'name': z.get('name', '')[:60]})
            continue
        if not z_active and v['product_status'] == 'ACTIVE':
            zoho_inactive.append({'sku': sku, 'name': z.get('name', '')[:60],
                                  'product': v['product_title'][:60]})
            continue  # do not push price/stock to something that should come down
        if z_rate < 100:
            under_100.append({'sku': sku, 'rate': z_rate, 'product': v['product_title'][:60]})
            continue  # placeholder pricing — flag, never push

        # Expiry: expired stock is pulled, near-expiry stock is marked down.
        expected_price, expected_compare_at = z_rate, None
        exp = expiring.get(sku)
        if exp and policy:
            days_left = (exp['expiry'] - today).days
            if days_left < 0:
                expired_pulls.append({'sku': sku, 'expiry': exp['expiry'].isoformat(),
                                      'product_id': v['product_id'],
                                      'product_title': v['product_title'][:60],
                                      'product_status': v['product_status']})
                continue  # never price-manage expired stock — it comes off the store
            for b in sorted(buckets):
                if days_left <= b:
                    buckets[b].append({'sku': sku, 'days': days_left,
                                       'expiry': exp['expiry'].isoformat(),
                                       'product': v['product_title'][:60]})
                    break
            eff, pct, floored = effective_price(z_rate, exp['purchase_rate'], days_left, policy)
            if pct > 0:
                expected_price, expected_compare_at = eff, z_rate
                markdowns.append({'sku': sku, 'days': days_left, 'markdown': pct,
                                  'price': eff, 'rate': z_rate})
                if floored:
                    floored_items.append({'sku': sku, 'days': days_left,
                                          'cost': exp['purchase_rate'], 'price': eff,
                                          'product': v['product_title'][:60]})

        price_wrong = abs(expected_price - v['price']) > 0.01
        compare_wrong = ((expected_compare_at is None) != (v.get('compare_at') is None)
                         or (expected_compare_at is not None and v.get('compare_at') is not None
                             and abs(expected_compare_at - v['compare_at']) > 0.01))
        if price_wrong or compare_wrong:
            price_drift.append({'sku': sku, 'zoho_rate': expected_price,
                                'compare_at': expected_compare_at,
                                'shopify_price': v['price'],
                                'variant_id': v['variant_id'], 'product_id': v['product_id'],
                                'product_title': v['product_title']})
        if v['qty'] is not None and z_stock != int(v['qty']):
            stock_drift.append({'sku': sku, 'zoho_stock': z_stock, 'shopify_stock': int(v['qty']),
                                'inv_item_id': v['inv_item_id'],
                                'product_title': v['product_title']})

    # Live Shopify variants whose SKU has no *Published* Zoho item. Split them:
    #   in_pipeline — the Zoho item exists at another status (e.g. reset to
    #                 Image required after an audit); expected churn, low priority
    #   orphans     — no Zoho item with that SKU at all; needs a decision
    unmatched = [sku for sku, v in shop.items()
                 if sku not in zoho_by_sku and v['product_status'] == 'ACTIVE']
    orphans, in_pipeline = [], []
    if unmatched:
        # Classification (exists-in-Zoho vs true orphan) rarely changes — cache it
        # for 7 days so ~120 search calls don't repeat every morning.
        cache_file = os.path.join(PROJECT_DIR, '.orphan_cache.json')
        try:
            with open(cache_file) as f:
                ocache = json.load(f)
        except Exception:
            ocache = {}
        stale_before = (datetime.now() - timedelta(days=7)).isoformat()
        to_check = [s for s in unmatched
                    if s not in ocache or ocache[s].get('checked', '') < stale_before]
        print(f'\n  Classifying {len(unmatched)} unmatched live variant(s) '
              f'({len(to_check)} uncached, {len(unmatched) - len(to_check)} from cache)...')
        for sku in to_check:
            resp = requests.get(
                f'{ZOHO_API_BASE}/items',
                headers=zoho_headers(token),
                params={'organization_id': ZOHO_ORG_ID, 'search_text': sku, 'per_page': 10},
                timeout=15,
            )
            if resp.status_code == 429:
                time.sleep(60)
                continue
            hit = next((it for it in resp.json().get('items', []) if it.get('sku') == sku), None)
            ocache[sku] = {'in_zoho': bool(hit),
                           'checked': datetime.now().isoformat(timespec='seconds')}
            time.sleep(0.2)
        with open(cache_file, 'w') as f:
            json.dump(ocache, f, indent=1)
        for sku in unmatched:
            entry = {'sku': sku, 'product': shop[sku]['product_title'][:60]}
            if ocache.get(sku, {}).get('in_zoho'):
                entry['zoho_status'] = '(exists in Zoho, non-Published)'
                in_pipeline.append(entry)
            else:
                orphans.append(entry)

    print('\n  ── Drift report ──')
    print(f'  Price drift            : {len(price_drift)}')
    print(f'  Stock drift            : {len(stock_drift)}')
    print(f'  Missing on Shopify     : {len(missing)}')
    print(f'  Inactive in Zoho, live : {len(zoho_inactive)}')
    print(f'  Priced under ₦100 live : {len(under_100)}')
    print(f'  Live, pipeline-regressed (in Zoho, non-Published): {len(in_pipeline)}')
    print(f'  Shopify TRUE orphans (no Zoho item): {len(orphans)}')
    if policy:
        print(f'  Expiry: {len(markdowns)} markdown(s), {len(expired_pulls)} expired pull(s), '
              f'{len(floored_items)} floored at cost')
        for b in sorted(buckets):
            if buckets[b]:
                print(f'    expiring ≤{b}d: {len(buckets[b])} — ' +
                      ', '.join(x['sku'] for x in buckets[b][:8]))

    for d in price_drift[:15]:
        print(f'    price  {d["sku"]}: {d["shopify_price"]} → {d["zoho_rate"]}  ({d["product_title"][:45]})')
    for d in stock_drift[:15]:
        print(f'    stock  {d["sku"]}: {d["shopify_stock"]} → {d["zoho_stock"]}  ({d["product_title"][:45]})')

    fixed = {'prices_ok': 0, 'prices_failed': 0, 'stock_ok': 0, 'stock_failed': 0,
             'expired_ok': 0, 'expired_failed': 0}
    if (args.fix or args.fix_prices) and price_drift:
        print('\n  Correcting prices on Shopify...')
        fixed['prices_ok'], fixed['prices_failed'] = fix_prices(price_drift)
    if args.fix and stock_drift:
        print('\n  Correcting stock on Shopify...')
        fixed['stock_ok'], fixed['stock_failed'] = fix_stock(stock_drift)
    if args.fix and expired_pulls:
        print('\n  Pulling expired products to DRAFT...')
        fixed['expired_ok'], fixed['expired_failed'] = pull_expired(expired_pulls)

    report = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'mode': 'fix' if args.fix else 'fix-prices' if args.fix_prices else 'report',
        'zoho_published': len(zoho_by_sku),
        'shopify_variants': len(shop),
        'price_drift': price_drift,
        'stock_drift': stock_drift,
        'missing_on_shopify': missing,
        'zoho_inactive_but_live': zoho_inactive,
        'under_100_live': under_100,
        'live_pipeline_regressed': in_pipeline,
        'shopify_orphans': orphans,
        'expiry': {
            'markdowns': markdowns,
            'expired_pulls': expired_pulls,
            'floored_at_cost': floored_items,
            'buckets': {str(k): v for k, v in buckets.items()},
        },
        'fixed': fixed,
    }
    with open(args.json, 'w') as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    print(f'\n  Report → {args.json}')

    if args.fix or args.fix_prices:
        print(f'\n  Fixed: {fixed["prices_ok"]} price(s), {fixed["stock_ok"]} stock level(s); '
              f'{fixed["prices_failed"] + fixed["stock_failed"]} failure(s)')
    needs_attention = len(missing) + len(zoho_inactive) + len(under_100) + len(orphans)
    if needs_attention:
        print(f'  ⚠ {needs_attention} item(s) need judgment (missing / inactive / under-₦100 / orphans) — see report.')
    print('═' * 60)


if __name__ == '__main__':
    main()
