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
    Items the team tagged cf_is_batch_item=true. Detail-fetched per tagged
    item — the tag keeps this cheap.

    Returns (expiring, cutover_ready):
      expiring: {sku: {'batches': [{'expiry': date, 'qty': float}] sorted by
                 expiry (batch-tracked, live-balance only), 'bridge_expiry':
                 date|None (cf_expiry_date fallback), 'purchase_rate', 'name'}}
      cutover_ready: tagged items NOT yet batch-tracked whose stock is at or
                 below their reorder level (or zero when none set) — the
                 moment to cut over is just before the resupply arrives.
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
        return {}, []

    out, cutover_ready = {}, []
    for it in tagged:
        det_resp = requests.get(
            f'{ZOHO_API_BASE}/items/{it["item_id"]}', headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID}, timeout=15)
        if det_resp.status_code == 429:
            time.sleep(60)
            continue
        det = det_resp.json().get('item', {})
        cf = {c['api_name']: c.get('value', '') for c in det.get('custom_fields', [])}
        sku = det.get('sku', '')
        stock = float(det.get('stock_on_hand') or 0)

        # Cutover trigger: tagged, not yet batch-tracked, not already migrated,
        # and stock at/below reorder level — i.e. about to be resupplied.
        if not det.get('track_batch_number') and not cf.get('cf_batch_migrated'):
            reorder = float(det.get('reorder_level') or 0)
            if stock <= reorder:
                cutover_ready.append({'sku': sku, 'name': det.get('name', '')[:50],
                                      'stock': stock, 'reorder_level': reorder})

        batches = []
        if det.get('track_batch_number'):
            for b in det.get('batches', []):
                qty = float(b.get('balance_quantity') or 0)
                if qty <= 0 or not b.get('expiry_date'):
                    continue
                try:
                    batches.append({'expiry': date.fromisoformat(str(b['expiry_date'])[:10]),
                                    'qty': qty})
                except ValueError:
                    continue
            batches.sort(key=lambda b: b['expiry'])

        bridge_expiry = None
        if not batches and cf.get('cf_expiry_date'):
            try:
                bridge_expiry = date.fromisoformat(str(cf['cf_expiry_date'])[:10])
            except ValueError:
                pass

        if not batches and not bridge_expiry:
            time.sleep(0.25)
            continue
        out[sku] = {
            'batches': batches,
            'bridge_expiry': bridge_expiry,
            'purchase_rate': float(det.get('purchase_rate') or 0),
            'name': det.get('name', ''),
        }
        time.sleep(0.25)
    return out, cutover_ready


def pick_driver_batch(exp, today):
    """
    One listing sells ONE batch at a time (Leke's design, 2026-07-17): the
    soonest live batch drives BOTH the price and the exposed Shopify quantity.
    When those units sell out, the next morning's run flips the listing to the
    next batch (its price, its quantity). FEFO enforced by the storefront.

    Returns (driver_date_or_None, driver_qty_or_None, expired_units):
      - driver_qty: units of the driving batch (all live batches sharing the
        driver's expiry date). None for bridge items — no batch data, no cap.
      - expired batches never pull a listing that still has live stock; their
        units are reported for disposal/stock adjustment instead
      - driver None + expired_units means ALL stock is expired → pull listing
    """
    if not exp['batches']:
        return exp['bridge_expiry'], None, []

    live = [b for b in exp['batches'] if (b['expiry'] - today).days >= 0]
    expired_units = [b for b in exp['batches'] if (b['expiry'] - today).days < 0]
    if not live:
        return None, None, expired_units

    driver = live[0]['expiry']  # soonest first (list is expiry-sorted)
    driver_qty = sum(b['qty'] for b in live if b['expiry'] == driver)
    return driver, driver_qty, expired_units


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
              product {{ id title status tags }}
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
                'product_tags':   n['product'].get('tags') or [],
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


def fix_tags(tag_ops):
    """Add/remove the 'short-dated' tag so the store mirrors the markdown state."""
    ok, failed = 0, 0
    for pid, op in tag_ops.items():
        tags = [t for t in op['tags'] if t != 'short-dated']
        if op['add']:
            tags.append('short-dated')
        res = shopify_graphql(
            "mutation($in: ProductInput!) { productUpdate(input: $in) { product { id } userErrors { message } } }",
            {'in': {'id': pid, 'tags': tags}})
        errs = (res.get('data', {}).get('productUpdate', {}) or {}).get('userErrors', [])
        if res.get('errors'):
            errs = errs + res['errors']
        if errs:
            failed += 1
            print(f'  ✗ tag update failed for {op["title"]}: {json.dumps(errs)[:80]}')
        else:
            ok += 1
            print(f'  ✓ {op["title"]}: short-dated tag {"added" if op["add"] else "removed"}')
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
    parser.add_argument('--max-fixes',  type=int, default=50,
                        help='Safety ceiling for unattended runs: if pending price+stock '
                             'fixes exceed this, report only and exit 2 — mass drift means '
                             'something upstream broke (default 50)')
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

    # Expiry watch: batch mix per tagged item → effective (marked-down) price
    policy = load_expiry_policy()
    expiring, cutover_ready = {}, []
    if policy:
        print('  Fetching expiry data (cf_is_batch_item items)...')
        expiring, cutover_ready = fetch_expiring_items(token)
        print(f'    {len(expiring)} item(s) with expiry data, '
              f'{len(cutover_ready)} cutover-ready')

    today = date.today()
    sliver_threshold = (policy or {}).get('sliver_threshold', 0.20)
    price_drift, stock_drift = [], []
    missing, zoho_inactive, under_100 = [], [], []
    expired_pulls, markdowns, floored_items = [], [], []
    sliver_batches, expired_on_shelf = [], []
    tag_ops = {}   # product_id → add/remove the 'short-dated' tag
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

        # Expiry: one listing sells one batch at a time. The soonest live batch
        # sets the price AND the exposed Shopify quantity; when it sells out,
        # the next morning flips the listing to the next batch. Expired batches
        # on a mixed shelf are reported, never pull a listing with live stock.
        expected_price, expected_compare_at = z_rate, None
        expected_qty = None   # None → expose full Zoho stock (default behavior)
        markdown_active = False
        exp = expiring.get(sku)
        if exp and policy:
            driver, driver_qty, expired_units = pick_driver_batch(exp, today)

            for b in expired_units:
                expired_on_shelf.append({'sku': sku, 'qty': b['qty'],
                                         'expiry': b['expiry'].isoformat(),
                                         'product': v['product_title'][:60]})

            if driver is None:
                # every unit on the shelf is expired → off the store
                expired_pulls.append({'sku': sku,
                                      'expiry': (expired_units[-1]['expiry'].isoformat()
                                                 if expired_units else ''),
                                      'product_id': v['product_id'],
                                      'product_title': v['product_title'][:60],
                                      'product_status': v['product_status']})
                continue  # never price-manage expired stock — it comes off the store

            days_left = (driver - today).days
            if days_left < 0:
                # bridge item whose single date passed → pull (no batch granularity)
                expired_pulls.append({'sku': sku, 'expiry': driver.isoformat(),
                                      'product_id': v['product_id'],
                                      'product_title': v['product_title'][:60],
                                      'product_status': v['product_status']})
                continue
            for b in sorted(buckets):
                if days_left <= b:
                    buckets[b].append({'sku': sku, 'days': days_left,
                                       'expiry': driver.isoformat(),
                                       'product': v['product_title'][:60]})
                    break
            eff, pct, floored = effective_price(z_rate, exp['purchase_rate'], days_left, policy)
            if pct > 0:
                expected_price, expected_compare_at = eff, z_rate
                markdown_active = True
                if driver_qty is not None:
                    # cap the storefront to the discounted batch only —
                    # fresh stock stays hidden until this batch sells out
                    expected_qty = int(driver_qty)
                    live_total = sum(b['qty'] for b in exp['batches']
                                     if (b['expiry'] - today).days >= 0)
                    if live_total > 0 and driver_qty / live_total < sliver_threshold:
                        sliver_batches.append({'sku': sku, 'qty': driver_qty,
                                               'expiry': driver.isoformat(),
                                               'days': days_left,
                                               'product': v['product_title'][:60]})
                markdowns.append({'sku': sku, 'days': days_left, 'markdown': pct,
                                  'price': eff, 'rate': z_rate,
                                  'exposed_qty': expected_qty})
                if floored:
                    floored_items.append({'sku': sku, 'days': days_left,
                                          'cost': exp['purchase_rate'], 'price': eff,
                                          'product': v['product_title'][:60]})

        # Shopify 'short-dated' tag mirrors the markdown state (store-side marker;
        # also enables an automatic Short-dated deals collection)
        has_tag = 'short-dated' in (v.get('product_tags') or [])
        if markdown_active != has_tag:
            tag_ops.setdefault(v['product_id'], {
                'title': v['product_title'][:60],
                'tags': list(v.get('product_tags') or []),
                'add': markdown_active,
            })

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
        # Exposed quantity: capped to the discounted batch when a markdown is
        # active on a batch-tracked item; otherwise the full Zoho stock.
        target_qty = expected_qty if expected_qty is not None else z_stock
        if v['qty'] is not None and target_qty != int(v['qty']):
            stock_drift.append({'sku': sku, 'zoho_stock': target_qty, 'shopify_stock': int(v['qty']),
                                'inv_item_id': v['inv_item_id'],
                                'capped': expected_qty is not None,
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
        for m in markdowns:
            if m.get('exposed_qty') is not None:
                print(f'    batch cap {m["sku"]}: exposing {m["exposed_qty"]} unit(s) at '
                      f'-{m["markdown"]:.0%} ({m["days"]}d left); fresh stock hidden until sold out')
        for s in sliver_batches:
            print(f'    small batch watch {s["sku"]}: only {s["qty"]:g} discounted unit(s) '
                  f'({s["days"]}d left) gate the listing — if they linger, sell offline to unblock fresh stock')
        for e in expired_on_shelf:
            print(f'    ⚠ EXPIRED ON SHELF {e["sku"]}: {e["qty"]:g} unit(s) (batch expired '
                  f'{e["expiry"]}) — remove from sellable stock; listing stays live on fresh batches')
        for c in cutover_ready:
            print(f'    cutover ready: {c["sku"]} {c["name"]} (stock {c["stock"]:g} ≤ '
                  f'reorder level {c["reorder_level"]:g}) — cut over before the resupply arrives')

    for d in price_drift[:15]:
        print(f'    price  {d["sku"]}: {d["shopify_price"]} → {d["zoho_rate"]}  ({d["product_title"][:45]})')
    for d in stock_drift[:15]:
        print(f'    stock  {d["sku"]}: {d["shopify_stock"]} → {d["zoho_stock"]}  ({d["product_title"][:45]})')

    fixed = {'prices_ok': 0, 'prices_failed': 0, 'stock_ok': 0, 'stock_failed': 0,
             'expired_ok': 0, 'expired_failed': 0, 'tags_ok': 0, 'tags_failed': 0}
    mass_drift = len(price_drift) + len(stock_drift) > args.max_fixes
    if mass_drift and (args.fix or args.fix_prices):
        print(f'\n  ⚠ MASS DRIFT: {len(price_drift)} price + {len(stock_drift)} stock fixes '
              f'pending, over the safety ceiling of {args.max_fixes}. Something upstream '
              'likely broke — refusing to fix. Review the report; re-run with a higher '
              '--max-fixes only if the drift is genuinely intentional.')
        args.fix = args.fix_prices = False
    if (args.fix or args.fix_prices) and price_drift:
        print('\n  Correcting prices on Shopify...')
        fixed['prices_ok'], fixed['prices_failed'] = fix_prices(price_drift)
    if args.fix and stock_drift:
        print('\n  Correcting stock on Shopify...')
        fixed['stock_ok'], fixed['stock_failed'] = fix_stock(stock_drift)
    if args.fix and expired_pulls:
        print('\n  Pulling expired products to DRAFT...')
        fixed['expired_ok'], fixed['expired_failed'] = pull_expired(expired_pulls)
    if args.fix and tag_ops:
        print('\n  Syncing short-dated tags...')
        fixed['tags_ok'], fixed['tags_failed'] = fix_tags(tag_ops)

    report = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'mode': 'fix' if args.fix else 'fix-prices' if args.fix_prices else 'report',
        'mass_drift_refusal': mass_drift,
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
            'small_batch_watch': sliver_batches,
            'expired_on_shelf': expired_on_shelf,
            'cutover_ready': cutover_ready,
            'tag_changes': {pid: op['add'] for pid, op in tag_ops.items()},
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
    if mass_drift:
        sys.exit(2)   # unattended wrappers (run_ops.sh) surface this as a failed run


if __name__ == '__main__':
    main()
