#!/usr/bin/env python3
"""
Image audit gate — runs BETWEEN image fetch (Stage 3b) and Queue for Upload.

Downloads the Zoho image for every batch item at 'Image Validated' status
into audit/<date>/ and generates a contact sheet (contact_sheet.html) showing
each image beside its product name, enriched title, and source URL.

Purpose: no image reaches Shopify unseen. The technical checks (size, ratio)
cannot tell whether an image shows the RIGHT product — a human or Claude
must look at every image against its product before the batch syncs.

Workflow:
  1. python execution/audit_batch_images.py
  2. Review audit/<date>/contact_sheet.html (or have Claude Read each image)
  3. Reset any wrong image to 'Image required' (delete image first!)
  4. Only then advance the batch to Queue for Upload

Usage:
    python execution/audit_batch_images.py                 # batch from enrichment_input.json
    python execution/audit_batch_images.py --skus A B C
"""

import sys
import os
import json
import time
import argparse
import html
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROJECT_DIR, get_zoho_token, zoho_item_by_sku, zoho_download_image


def main():
    parser = argparse.ArgumentParser(description='Image audit gate — contact sheet generator')
    parser.add_argument('--skus', nargs='+', help='Audit specific SKUs (default: batch from enrichment_input.json)')
    parser.add_argument('--all-statuses', action='store_true',
                        help="Include items regardless of status (default: only 'Image Validated')")
    args = parser.parse_args()

    if args.skus:
        skus = args.skus
    else:
        with open(os.path.join(PROJECT_DIR, 'enrichment_input.json')) as f:
            skus = [it['sku'] for it in json.load(f) if it.get('sku')]

    token = get_zoho_token()
    audit_dir = os.path.join(PROJECT_DIR, 'audit', date.today().isoformat())
    os.makedirs(audit_dir, exist_ok=True)

    print('═' * 60)
    print(f'  Image Audit Gate — {len(skus)} SKU(s)')
    print(f'  Output: {audit_dir}')
    print('═' * 60)

    rows = []
    counts = {'downloaded': 0, 'skipped': 0, 'no_image': 0}

    for sku in skus:
        item = zoho_item_by_sku(sku, token)
        if not item:
            print(f'  ✗ {sku}  not found in Zoho')
            counts['skipped'] += 1
            continue

        status = item['cf'].get('cf_shopify_status', '')
        if not args.all_statuses and status != 'Image Validated':
            print(f'  ⬜ {sku}  [{status}] — skipped')
            counts['skipped'] += 1
            continue

        img = zoho_download_image(item['item_id'], token)
        if not img:
            print(f'  ⚠  {sku}  no image attached')
            counts['no_image'] += 1
            rows.append({'sku': sku, 'item': item, 'file': None})
            continue

        ext = 'png' if img[:8].startswith(b'\x89PNG') else 'jpg'
        fname = f'{sku}.{ext}'
        with open(os.path.join(audit_dir, fname), 'wb') as f:
            f.write(img)
        print(f'  ✓ {sku}  {fname} ({len(img)//1024} KB)')
        counts['downloaded'] += 1
        rows.append({'sku': sku, 'item': item, 'file': fname})
        time.sleep(0.4)

    # Contact sheet
    cards = []
    for r in rows:
        it = r['item']
        cf = it['cf']
        img_tag = (f'<img src="{r["file"]}" loading="lazy">' if r['file']
                   else '<div class="noimg">NO IMAGE</div>')
        src = cf.get('cf_source_url', '')
        src_link = f'<a href="{html.escape(src)}" target="_blank">{html.escape(src[:70])}</a>' if src else '—'
        cards.append(f'''
        <div class="card">
          {img_tag}
          <div class="meta">
            <div class="sku">{html.escape(r["sku"])}</div>
            <div class="name">{html.escape(it.get("name", ""))}</div>
            <div class="title">{html.escape(cf.get("cf_enriched_title", "") or "(no enriched title)")}</div>
            <div class="brand">Brand: {html.escape(it.get("brand", "") or "—")}</div>
            <div class="src">{src_link}</div>
          </div>
        </div>''')

    sheet = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Image Audit — {date.today().isoformat()}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; margin: 20px; }}
  h1 {{ font-size: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.15); }}
  .card img {{ width: 100%; aspect-ratio: 1; object-fit: contain; border: 1px solid #eee; background: #fff; }}
  .noimg {{ width: 100%; aspect-ratio: 1; display: flex; align-items: center; justify-content: center;
            background: #fee; color: #c00; font-weight: bold; border: 1px dashed #c00; }}
  .sku {{ font-weight: 700; font-family: monospace; margin-top: 8px; }}
  .name {{ color: #444; font-size: 13px; }}
  .title {{ color: #06c; font-size: 13px; margin-top: 4px; }}
  .brand {{ color: #666; font-size: 12px; margin-top: 4px; }}
  .src {{ font-size: 11px; color: #999; margin-top: 4px; word-break: break-all; }}
</style></head><body>
<h1>Image Audit — {date.today().isoformat()} — {counts["downloaded"]} image(s), {counts["no_image"]} missing</h1>
<p>Check every image against its product name and enriched title. Any mismatch: delete the Zoho image, reset to <b>Image required</b>.</p>
<div class="grid">{''.join(cards)}</div>
</body></html>'''

    sheet_path = os.path.join(audit_dir, 'contact_sheet.html')
    with open(sheet_path, 'w') as f:
        f.write(sheet)

    print()
    print('═' * 60)
    print(f'  Downloaded : {counts["downloaded"]}')
    print(f'  No image   : {counts["no_image"]}')
    print(f'  Skipped    : {counts["skipped"]}')
    print(f'\n  Contact sheet: {sheet_path}')
    print('═' * 60)


if __name__ == '__main__':
    main()
