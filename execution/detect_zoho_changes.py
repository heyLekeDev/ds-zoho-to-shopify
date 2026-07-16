#!/usr/bin/env python3
"""
Stage 3d — Zoho change watch (daily delta detection)

Finds Published items the team edited in the last N hours and flags the ones
whose IMAGE or COPY actually changed as 'Update Required', so the same run's
full sync (Stage 4) pushes the change to Shopify. Price/stock changes are
deliberately NOT flagged here — the reconciler (Stage 6) already corrects
those without a re-sync.

How: items are listed newest-modified-first (sort_column=last_modified_time,
verified working) and read only until the cutoff — the daily delta is a page
or two, not the whole catalog. Each candidate's current image MD5 and a
copy-hash (title/collection/variants/description) are compared against
.change_watch.json baselines; the baseline seeds itself on first sight
(no flag), so day one is silent and every later edit is caught.

Usage:
    python execution/detect_zoho_changes.py               # detect + flag
    python execution/detect_zoho_changes.py --dry-run
    python execution/detect_zoho_changes.py --hours 48
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_headers, zoho_write_fields,
                    zoho_download_image, ZOHO_API_BASE, ZOHO_ORG_ID)

BASELINE_FILE = os.path.join(PROJECT_DIR, '.change_watch.json')


def copy_hash(det):
    """Hash of the customer-facing copy on a Zoho item detail."""
    cf = {c['api_name']: c.get('value', '') for c in det.get('custom_fields', [])}
    relevant = {
        'title':      cf.get('cf_enriched_title', ''),
        'collection': cf.get('cf_shopify_collection', ''),
        'v1': cf.get('cf_shopify_var_1_value', ''),
        'v2': cf.get('cf_shopify_var_2_value', ''),
        'v3': cf.get('cf_shopify_var_3_value', ''),
        'desc': cf.get('cf_description_html', '') or det.get('description', ''),
        'type': cf.get('cf_shopify_product_type', ''),
        'tags': cf.get('cf_shopify_tags', ''),
    }
    return hashlib.md5(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def fetch_published_skus(token):
    skus = set()
    page = 1
    while True:
        r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID,
                                 'cf_shopify_status': 'Published',
                                 'per_page': 200, 'page': page}, timeout=20)
        if r.status_code == 429:
            time.sleep(60)
            continue
        d = r.json()
        skus |= {i['sku'] for i in d.get('items', []) if i.get('sku')}
        if not d.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    return skus


def fetch_modified_items(token, cutoff):
    """Items modified since cutoff — newest first, stop at the cutoff."""
    out = []
    page = 1
    while True:
        r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID, 'per_page': 200,
                                 'page': page, 'sort_column': 'last_modified_time',
                                 'sort_order': 'D'}, timeout=20)
        if r.status_code == 429:
            time.sleep(60)
            continue
        d = r.json()
        items = d.get('items', [])
        for it in items:
            ts = it.get('last_modified_time', '')
            try:
                mod = datetime.fromisoformat(ts).replace(tzinfo=None)
            except ValueError:
                continue
            if mod < cutoff:
                return out
            out.append(it)
        if not d.get('page_context', {}).get('has_more_page'):
            return out
        page += 1


def main():
    parser = argparse.ArgumentParser(description='Stage 3d — Zoho change watch')
    parser.add_argument('--hours', type=int, default=26,
                        help='Look-back window (default 26h, covers a daily run + slack)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    print('═' * 60)
    print(f'  Stage 3d — Zoho change watch (last {args.hours}h)'
          f'{"  [DRY RUN]" if args.dry_run else ""}')
    print('═' * 60)

    try:
        with open(BASELINE_FILE) as f:
            baseline = json.load(f)
    except Exception:
        baseline = {}

    token = get_zoho_token()
    cutoff = datetime.now() - timedelta(hours=args.hours)
    modified = fetch_modified_items(token, cutoff)
    print(f'  Items modified since cutoff: {len(modified)}')

    published = fetch_published_skus(token)
    candidates = [i for i in modified if i.get('sku') in published]
    print(f'  Of those, Published (checkable): {len(candidates)}')

    flagged, seeded, unchanged = [], 0, 0
    for it in candidates:
        sku = it['sku']
        r = requests.get(f'{ZOHO_API_BASE}/items/{it["item_id"]}',
                         headers=zoho_headers(token),
                         params={'organization_id': ZOHO_ORG_ID}, timeout=15)
        if r.status_code == 429:
            time.sleep(60)
            continue
        det = r.json().get('item', {})

        c_hash = copy_hash(det)
        img_md5 = ''
        if det.get('image_name'):
            img = zoho_download_image(it['item_id'], token)
            if img:
                img_md5 = hashlib.md5(img).hexdigest()

        base = baseline.get(sku)
        if base is None:
            baseline[sku] = {'copy': c_hash, 'img': img_md5,
                             'seen': datetime.now().isoformat(timespec='seconds')}
            seeded += 1
            time.sleep(0.25)
            continue

        changes = []
        if base.get('copy') != c_hash:
            changes.append('copy')
        if img_md5 and base.get('img') and base['img'] != img_md5:
            changes.append('image')
        if img_md5 and not base.get('img'):
            changes.append('image added')

        # Update the baseline either way — a flagged item re-syncs today and
        # must not re-flag tomorrow.
        baseline[sku] = {'copy': c_hash, 'img': img_md5,
                         'seen': datetime.now().isoformat(timespec='seconds')}

        if not changes:
            unchanged += 1
            time.sleep(0.25)
            continue

        what = ' + '.join(changes)
        print(f'  ➜ {sku}: {what} changed')
        flagged.append({'sku': sku, 'changes': what})
        if not args.dry_run:
            ok, msg = zoho_write_fields(it['item_id'], {
                'cf_shopify_status': 'Update Required',
                'cf_sync_result':    f'[CHANGE WATCH] {what} changed — auto-requeued',
                'cf_shopify_sync_notes':
                    f'[CHANGE WATCH] ({datetime.now().strftime("%Y-%m-%d %H:%M")})\n'
                    f'Detected {what} change on a Published item. Set to Update Required '
                    f'so the daily sync pushes it to Shopify.',
            }, token)
            if not ok:
                print(f'     ✗ flag failed: {msg}')
        time.sleep(0.25)

    if not args.dry_run:
        with open(BASELINE_FILE, 'w') as f:
            json.dump(baseline, f, indent=1)

    print('\n' + '═' * 60)
    print(f'  Flagged Update Required : {len(flagged)}')
    print(f'  Baselines seeded        : {seeded}')
    print(f'  Checked, unchanged      : {unchanged}')
    print('═' * 60)
    if flagged and not args.dry_run:
        print('  → Run the full sync (Stage 4) to push these to Shopify.')


if __name__ == '__main__':
    main()
