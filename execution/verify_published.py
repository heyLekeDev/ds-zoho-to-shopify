#!/usr/bin/env python3
"""
Post-sync verification — confirms items marked Published in Zoho are
actually live and complete on Shopify.

Checks per SKU:
  1. Variant exists on Shopify
  2. Product status is ACTIVE
  3. Variant (or its product) has an image
  4. Product description is not empty
  5. Variant price matches Zoho rate (warns on mismatch, never writes price)

Discrepancies are reported and (unless --no-flag) written back to Zoho:
  status → 'Needs Review', cf_sync_result → 'VERIFY FAIL: <reason>'

Usage:
    python execution/verify_published.py --skus 600-160-001 400-140-004
    python execution/verify_published.py --batch              # SKUs from enrichment_input.json
    python execution/verify_published.py --batch --no-flag    # report only
"""

import sys
import os
import json
import time
import argparse
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_item_by_sku,
                    zoho_write_fields, shopify_variant_by_sku)


def verify_sku(sku, token):
    """Returns (ok, reasons, detail_line)."""
    reasons = []

    zoho = zoho_item_by_sku(sku, token)
    if not zoho:
        return False, ['SKU not found in Zoho'], ''
    zoho_status = zoho['cf'].get('cf_shopify_status', '')
    rate = zoho.get('rate') or 0

    v = shopify_variant_by_sku(sku)
    if not v:
        return False, ['Not found on Shopify'], f'Zoho status: {zoho_status}'

    prod = v.get('product') or {}
    if prod.get('status') != 'ACTIVE':
        reasons.append(f"Product status is {prod.get('status')}")

    # Invisible-product mode: exists and ACTIVE but never published to the Online Store
    if not prod.get('publishedAt'):
        reasons.append('Not published to Online Store channel')

    n_variants = (prod.get('variantsCount') or {}).get('count', 1)
    variant_img = (v.get('image') or {}).get('url')
    has_product_img = bool(prod.get('featuredMedia')) or (prod.get('mediaCount') or {}).get('count', 0) > 0

    if n_variants > 1:
        # On multi-variant products a product-level image is NOT enough —
        # this variant needs its own linked image.
        if not variant_img:
            reasons.append('Variant has no linked image (multi-variant product)')
        else:
            # Sibling variants sharing this exact image = likely wrong/duplicate image
            siblings = [e['node'] for e in (prod.get('variants') or {}).get('edges', [])]
            shared = [s['sku'] for s in siblings
                      if s.get('sku') != sku and (s.get('image') or {}).get('url') == variant_img]
            if shared:
                reasons.append(f'Image shared with sibling variant(s): {", ".join(shared[:4])}')
    elif not variant_img and not has_product_img:
        reasons.append('No image on Shopify')

    img = v.get('image') or {}
    if img.get('width') and (img['width'] < 800 or (img.get('height') or 0) < 800):
        reasons.append(f"Image below 800x800 ({img['width']}x{img.get('height')})")

    desc = (prod.get('descriptionHtml') or '').strip()
    if not desc:
        reasons.append('Empty description')

    try:
        shop_price = float(v.get('price') or 0)
        if rate and abs(shop_price - float(rate)) > 0.01:
            reasons.append(f'Price mismatch: Shopify {shop_price} vs Zoho {rate}')
    except (TypeError, ValueError):
        pass

    detail = f"product: {prod.get('title','')[:50]}  [{prod.get('status')}]"
    return (len(reasons) == 0), reasons, detail


def main():
    parser = argparse.ArgumentParser(description='Post-sync Shopify verification')
    parser.add_argument('--skus', nargs='+', help='SKUs to verify')
    parser.add_argument('--batch', action='store_true',
                        help='Verify all SKUs in enrichment_input.json')
    parser.add_argument('--no-flag', action='store_true',
                        help='Report only — do not write Needs Review back to Zoho')
    args = parser.parse_args()

    skus = list(args.skus or [])
    if args.batch:
        with open(os.path.join(PROJECT_DIR, 'enrichment_input.json')) as f:
            skus += [it['sku'] for it in json.load(f) if it.get('sku')]
    if not skus:
        parser.error('Provide --skus or --batch')

    token = get_zoho_token()
    today = date.today().isoformat()

    print('═' * 60)
    print(f'  Post-Sync Verification — {len(skus)} SKU(s)')
    print('═' * 60)

    failures = []
    for sku in skus:
        ok, reasons, detail = verify_sku(sku, token)
        if ok:
            print(f'  ✓ {sku}  {detail}')
        else:
            reason_str = '; '.join(reasons)
            print(f'  ✗ {sku}  {reason_str}')
            failures.append((sku, reason_str))
        time.sleep(0.4)

    if failures and not args.no_flag:
        print(f'\n  Flagging {len(failures)} item(s) as Needs Review in Zoho...')
        for sku, reason_str in failures:
            zoho = zoho_item_by_sku(sku, token)
            if not zoho:
                continue
            ok, msg = zoho_write_fields(zoho['item_id'], {
                'cf_shopify_status':     'Needs Review',
                'cf_sync_result':        f'VERIFY FAIL: {reason_str[:80]}',
                'cf_shopify_sync_notes': f'[VERIFY] ({today})\n{reason_str}',
            }, token)
            print(f'    {"✓" if ok else "✗"} {sku} flagged')
            time.sleep(0.3)

    print()
    print('═' * 60)
    print(f'  Verified OK : {len(skus) - len(failures)}')
    print(f'  Failures    : {len(failures)}')
    print('═' * 60)
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
