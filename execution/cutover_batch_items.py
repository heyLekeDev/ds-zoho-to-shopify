#!/usr/bin/env python3
"""
Stage 5b — Automated batch cutover (approved by Leke 2026-07-17).

Converts a tagged expiring item to a born-batch-tracked replacement with the
same SKU and name, leaving the Shopify product untouched. The team's ONLY
inputs are cf_is_batch_item and cf_expiry_date (loaded from their sheet by
tag_expiring_items.py); everything else is automated.

Per item (tagged, not yet batch-tracked, not cf_batch_migrated):
  1. guards: needs cf_expiry_date if stock > 0; skip otherwise with a reason
  2. rename old item: name + " -X", sku + "-X"      (frees the identity)
  3. create replacement: original name + sku, track_batch_number on, all
     fields + custom fields copied, image copied
  4. stock > 0: adjustment -stock on old, +stock on new as ONE opening batch
     (batch OPEN-<expiry>, expiry = cf_expiry_date)  [needs the
     inventoryadjustments OAuth scope — until the token regen, stocked items
     are skipped with a clear reason; zero-stock items convert fully]
  5. old item: cf_batch_migrated = true, cf_shopify_status = Ignore, deactivate
  6. append the full step trail to cutover_log.jsonl

SANCTIONED EXCEPTIONS (Leke, 2026-07-17): this script may write name/sku
(rename, step 2), move stock (step 4), and deactivate (step 5) — ONLY here,
only for tagged unmigrated items, capped by --limit, default dry-run.
ROLLBACK: if the replacement creation fails after the rename, the rename is
reverted so the item is never left in a half-cutover state.

Usage:
  python3 execution/cutover_batch_items.py                  # dry run, all eligible
  python3 execution/cutover_batch_items.py --skus A,B       # dry run, subset
  python3 execution/cutover_batch_items.py --live           # convert (max --limit)
  python3 execution/cutover_batch_items.py --live --limit 5
"""
import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_headers, zoho_write_fields,
                    zoho_download_image, ZOHO_API_BASE, ZOHO_ORG_ID)

LOG_FILE = os.path.join(PROJECT_DIR, 'cutover_log.jsonl')

# Fields copied verbatim from the old item onto the replacement. Identity
# (name/sku) is handled separately; accounting fields come from org defaults.
COPY_FIELDS = ['rate', 'purchase_rate', 'unit', 'product_type', 'brand',
               'manufacturer', 'description', 'category_id', 'tax_id',
               'purchase_description']

# Custom fields copied onto the replacement (the full store identity plus the
# expiry pair so the markdown engine keeps watching the new item seamlessly).
COPY_CF = ['cf_shopify_status', 'cf_enriched_title', 'cf_shopify_collection',
           'cf_shopify_product_type', 'cf_shopify_tags', 'cf_description_html',
           'cf_source_url', 'cf_shopify_var_1_name', 'cf_shopify_var_1_value',
           'cf_shopify_var_2_name', 'cf_shopify_var_2_value',
           'cf_shopify_var_3_name', 'cf_shopify_var_3_value',
           'cf_is_batch_item', 'cf_expiry_date']


def api(method, path, token, payload=None, files=None, data=None):
    fn = {'GET': requests.get, 'POST': requests.post,
          'PUT': requests.put, 'DELETE': requests.delete}[method]
    kw = {'headers': zoho_headers(token),
          'params': {'organization_id': ZOHO_ORG_ID}, 'timeout': 30}
    if payload is not None:
        kw['json'] = payload
    if files is not None:
        kw['files'] = files
        kw['data'] = data or {}
    r = fn(f'{ZOHO_API_BASE}{path}', **kw)
    if r.status_code == 429:
        time.sleep(60)
        return api(method, path, token, payload, files, data)
    try:
        body = r.json()
    except Exception:
        body = {}
    ok = r.status_code in (200, 201) and body.get('code', 0) == 0
    return ok, body


def fetch_eligible(token, skus_filter):
    """Tagged + active + not batch-tracked + not migrated."""
    tagged, page = [], 1
    while True:
        r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID,
                                 'cf_is_batch_item': 'true',
                                 'per_page': 200, 'page': page}, timeout=20)
        if r.status_code == 429:
            time.sleep(60)
            continue
        j = r.json()
        tagged += j.get('items', [])
        if not j.get('page_context', {}).get('has_more_page'):
            break
        page += 1
        time.sleep(0.3)

    out = []
    for it in tagged:
        if it.get('status') != 'active':
            continue
        if skus_filter and it.get('sku') not in skus_filter:
            continue
        ok, b = api('GET', f'/items/{it["item_id"]}', token)
        if not ok:
            continue
        det = b['item']
        cf = {c['api_name']: c.get('value', '') for c in det.get('custom_fields', [])}
        if det.get('track_batch_number') or cf.get('cf_batch_migrated'):
            continue
        out.append((det, cf))
        time.sleep(0.25)
    return out


def log_event(rec):
    rec['ts'] = datetime.now().isoformat(timespec='seconds')
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def adjustments_scope_ok(token):
    """One cheap probe per run: does the token carry the inventoryadjustments
    scope? An empty POST answers without side effects — 401 code 57 means the
    scope is missing; any other error (e.g. 400 validation) means it exists."""
    ok, b = api('POST', '/inventoryadjustments', token, payload={})
    return b.get('code') != 57


def cutover_one(det, cf, token, live, adj_ok):
    sku, name = det['sku'], det['name']
    item_id = det['item_id']
    stock = float(det.get('stock_on_hand') or 0)
    expiry = (cf.get('cf_expiry_date') or '')[:10]
    trail = {'sku': sku, 'name': name, 'old_item_id': item_id,
             'stock': stock, 'expiry': expiry, 'steps': [], 'live': live}

    # ── guards ────────────────────────────────────────────────────────────
    if stock > 0 and not expiry:
        trail['result'] = 'skip: stocked item without cf_expiry_date'
        return trail
    if stock > 0 and not adj_ok:
        trail['result'] = ('skip: stocked item, inventoryadjustments scope '
                           'missing from the Zoho token (regen pending)')
        return trail

    if not live:
        trail['result'] = (f'DRY RUN: would convert (stock {stock:g}'
                           + (f', opening batch OPEN-{expiry} exp {expiry}' if stock > 0 else ', no stock')
                           + ')')
        return trail

    # ── 2. rename old to free the identity ────────────────────────────────
    ok, b = api('PUT', f'/items/{item_id}', token,
                {'name': f'{name} -X', 'sku': f'{sku}-X'})
    trail['steps'].append({'rename_old': ok, 'msg': b.get('message', '')[:80]})
    if not ok:
        trail['result'] = f'FAIL at rename: {b.get("message", "")[:120]}'
        return trail

    # ── 3. create the replacement ─────────────────────────────────────────
    payload = {'name': name, 'sku': sku, 'item_type': 'inventory',
               'track_batch_number': True}
    for fld in COPY_FIELDS:
        if det.get(fld) not in (None, '', 0):
            payload[fld] = det[fld]
    payload['custom_fields'] = [{'api_name': k, 'value': cf[k]}
                                for k in COPY_CF if cf.get(k)]
    ok, b = api('POST', '/items', token, payload)
    trail['steps'].append({'create_new': ok, 'msg': b.get('message', '')[:80]})
    if not ok:
        # ROLLBACK the rename so the item is never left in a half state
        rb_ok, rb = api('PUT', f'/items/{item_id}', token,
                        {'name': name, 'sku': sku})
        trail['steps'].append({'rollback_rename': rb_ok})
        trail['result'] = f'FAIL at create (rename rolled back): {b.get("message", "")[:120]}'
        return trail
    new_id = b['item']['item_id']
    trail['new_item_id'] = new_id

    # ── 3b. copy the image ────────────────────────────────────────────────
    if det.get('image_name'):
        img = zoho_download_image(item_id, token)
        if img:
            ok, b = api('POST', f'/items/{new_id}/image', token,
                        files={'image': (det.get('image_name', 'image.jpg'), img)})
            trail['steps'].append({'copy_image': ok})

    # ── 4. move stock as one opening batch ────────────────────────────────
    if stock > 0:
        ok, b = api('POST', '/inventoryadjustments', token, {
            'date': date.today().isoformat(), 'reason': 'Batch cutover: stock out of retired item',
            'adjustment_type': 'quantity',
            'line_items': [{'item_id': item_id, 'quantity_adjusted': -stock}]})
        trail['steps'].append({'stock_out_old': ok, 'msg': b.get('message', '')[:80]})
        ok2, b2 = api('POST', '/inventoryadjustments', token, {
            'date': date.today().isoformat(), 'reason': 'Batch cutover: opening batch',
            'adjustment_type': 'quantity',
            'line_items': [{'item_id': new_id, 'quantity_adjusted': stock,
                            'batches': [{'batch_number': f'OPEN-{expiry.replace("-", "")}',
                                         'quantity_in': stock,
                                         'expiry_date': expiry}]}]})
        trail['steps'].append({'opening_batch_new': ok2, 'msg': b2.get('message', '')[:80]})
        if not (ok and ok2):
            trail['result'] = 'PARTIAL: stock move failed — check both items in Zoho before retrying'
            return trail

    # ── 5. retire the old item ────────────────────────────────────────────
    ok, msg = zoho_write_fields(item_id, {'cf_batch_migrated': True,
                                          'cf_shopify_status': 'Ignore'}, token)
    trail['steps'].append({'flag_old': ok, 'msg': str(msg)[:80]})
    ok, b = api('POST', f'/items/{item_id}/inactive', token)
    trail['steps'].append({'deactivate_old': ok})

    trail['result'] = 'CONVERTED'
    return trail


def main():
    ap = argparse.ArgumentParser(description='Stage 5b — automated batch cutover')
    ap.add_argument('--live', action='store_true', help='Actually convert (default: dry run)')
    ap.add_argument('--limit', type=int, default=10, help='Max conversions per live run (default 10)')
    ap.add_argument('--skus', help='Comma-separated SKU subset')
    args = ap.parse_args()
    skus_filter = set(s.strip() for s in args.skus.split(',')) if args.skus else None

    print('═' * 60)
    print(f'  Stage 5b — Batch cutover  {"LIVE" if args.live else "[DRY RUN]"}')
    print('═' * 60)

    token = get_zoho_token()
    adj_ok = adjustments_scope_ok(token)
    if not adj_ok:
        print('  ⚠ inventoryadjustments scope missing — stocked items will be '
              'skipped until the token regen; zero-stock items convert fully.')
    eligible = fetch_eligible(token, skus_filter)
    print(f'  Eligible (tagged, active, not batch-tracked, not migrated): {len(eligible)}')

    converted, would = 0, 0
    for det, cf in eligible:
        if args.live and converted >= args.limit:
            print(f'  … per-run cap ({args.limit}) reached; the rest convert next run.')
            break
        trail = cutover_one(det, cf, token, args.live, adj_ok)
        log_event(trail)
        mark = '✓' if trail['result'] == 'CONVERTED' else \
               '·' if trail['result'].startswith('DRY') else '✗'
        print(f'  {mark} {trail["sku"]}: {trail["result"]}')
        if trail['result'] == 'CONVERTED':
            converted += 1
        if trail['result'].startswith('DRY'):
            would += 1
        time.sleep(0.4)

    print(f'\n  {"Converted" if args.live else "Would convert"}: '
          f'{converted if args.live else would}  (log: {LOG_FILE})')
    print('═' * 60)


if __name__ == '__main__':
    main()
