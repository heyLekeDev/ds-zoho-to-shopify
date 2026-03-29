#!/usr/bin/env python3
"""
Stage 3 — Image Validation
Fetches items with 'Enrichment Complete' status, downloads attached images,
runs Pillow checks, and writes Image Validated or Image required status.

Checks:
    1. Image present
    2. Aspect ratio: 0.8 – 1.2
    3. Minimum resolution: 800 x 800px

Usage:
    python execution/validate_images.py
    python execution/validate_images.py --dry-run
"""

import os
import sys
import io
import json
import time
import argparse
import requests
from datetime import date
from dotenv import load_dotenv

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed. Run: pip install Pillow')
    sys.exit(1)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

ASPECT_MIN = 0.8
ASPECT_MAX = 1.2
MIN_WIDTH  = 800
MIN_HEIGHT = 800
# Shopify limit: 20 megapixels. Use 4472x4472 (≈20MP) as max for square images.
MAX_PIXELS = 20_000_000

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except:
            pass
    resp = requests.post(
        'https://accounts.zoho.com/oauth/v2/token',
        params={
            'refresh_token': ZOHO_REFRESH_TOKEN,
            'client_id': ZOHO_CLIENT_ID,
            'client_secret': ZOHO_CLIENT_SECRET,
            'grant_type': 'refresh_token',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token):
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'Content-Type': 'application/json',
    }

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_enrichment_complete(token):
    print('  Fetching items with status "Enrichment Complete"...')
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers=zoho_headers(token),
            params={
                'organization_id': ZOHO_ORG_ID,
                'cf_shopify_status': 'Enrichment Complete',
                'per_page': 200,
                'page': page,
            },
            timeout=15,
        )
        if resp.status_code == 429:
            print('    Rate limited — waiting 60s...')
            time.sleep(60)
            continue
        data = resp.json()
        items = data.get('items', [])
        if not items:
            break
        all_items.extend(items)
        if not data.get('page_context', {}).get('has_more_page'):
            break
        page += 1
    print(f'  Found {len(all_items)} items.')
    return all_items

# ── Image download ─────────────────────────────────────────────────────────────

def download_image(item_id, token):
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers={'Authorization': f'Zoho-oauthtoken {token}'},
        params={'organization_id': ZOHO_ORG_ID},
        timeout=20,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return download_image(item_id, token)
    if resp.status_code != 200 or not resp.content:
        return None
    if 'image' not in resp.headers.get('Content-Type', ''):
        return None
    return resp.content

# ── Pillow checks ─────────────────────────────────────────────────────────────

def check_image(image_bytes):
    """Returns (passed, width, height, ratio, fail_reason, pil_img)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
    except Exception as e:
        return False, 0, 0, 0.0, f'Cannot open image: {e}', None

    ratio = round(w / h, 2) if h > 0 else 0.0

    if ratio < ASPECT_MIN or ratio > ASPECT_MAX:
        return False, w, h, ratio, f'Aspect ratio fail ({ratio:.2f})', None

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return False, w, h, ratio, f'Resolution too low ({w}x{h}px)', img

    # Shopify rejects images over 20 megapixels — flag for auto-downscale
    if w * h > MAX_PIXELS:
        return False, w, h, ratio, f'Resolution too high ({w}x{h}px, {w*h/1_000_000:.1f}MP > 20MP)', img

    return True, w, h, ratio, None, img

# ── Zoho write ────────────────────────────────────────────────────────────────

def upload_upscaled_image(item_id, image_bytes, filename, token):
    """Upload replacement (upscaled) image to Zoho item."""
    try:
        resp = requests.post(
            f'{ZOHO_API_BASE}/items/{item_id}/image',
            headers={'Authorization': f'Zoho-oauthtoken {token}'},
            params={'organization_id': ZOHO_ORG_ID},
            files={'image': (filename, image_bytes, 'image/png')},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f'      Upload error: {e}')
        return False
    if resp.status_code == 429:
        time.sleep(60)
        return upload_upscaled_image(item_id, image_bytes, filename, token)
    try:
        return resp.json().get('code') == 0
    except Exception:
        return resp.status_code == 200

def write_zoho(item_id, new_status, sync_result, notes, token):
    payload = {
        'custom_fields': [
            {'api_name': 'cf_shopify_status',     'value': new_status},
            {'api_name': 'cf_sync_result',        'value': sync_result},
            {'api_name': 'cf_shopify_sync_notes', 'value': notes},
        ]
    }
    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        json=payload,
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return write_zoho(item_id, new_status, sync_result, notes, token)
    data = resp.json()
    return data.get('code') == 0, data.get('message', '')

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Stage 3 — Image Validation')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing to Zoho')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 3 — Image Validation')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    token    = get_zoho_token()
    items    = fetch_enrichment_complete(token)
    today_str = date.today().isoformat()

    if not items:
        print('\n  No items in Enrichment Complete. Run Stage 2 first.')
        sys.exit(0)

    print(f'\n  Checking {len(items)} items...\n')

    validated      = 0
    image_required = 0
    error_count    = 0
    upscaled_count = 0

    for item in items:
        item_id = item['item_id']
        sku     = item.get('sku', '')
        name    = item.get('name', '')[:40]
        has_img = bool(item.get('image_name', ''))

        # ── No image attached ─────────────────────────────────────────────────
        if not has_img:
            sync_result = 'No image'
            notes = (
                '[IMAGE] ({})\n'
                'FAIL: No image attached.\n'
                'Fix: Attach a product image in Zoho, then reset status to Enrichment Complete.'
            ).format(today_str)
            print(f'  ✗ {sku}  {name}')
            print(f'    → No image attached')
            if not args.dry_run:
                ok, msg = write_zoho(item_id, 'Image required', sync_result, notes, token)
                if not ok:
                    print(f'      Write error: {msg}')
                    error_count += 1
                    continue
            image_required += 1
            time.sleep(0.2)
            continue

        # ── Download image ────────────────────────────────────────────────────
        image_bytes = download_image(item_id, token)

        if not image_bytes:
            sync_result = 'No image'
            notes = (
                '[IMAGE] ({})\n'
                'FAIL: Image listed in Zoho but could not be downloaded.\n'
                'Fix: Re-attach the image in Zoho, then reset status to Enrichment Complete.'
            ).format(today_str)
            print(f'  ✗ {sku}  {name}')
            print(f'    → Download failed')
            if not args.dry_run:
                ok, msg = write_zoho(item_id, 'Image required', sync_result, notes, token)
                if not ok:
                    print(f'      Write error: {msg}')
                    error_count += 1
                    continue
            image_required += 1
            time.sleep(0.2)
            continue

        # ── Run Pillow checks ─────────────────────────────────────────────────
        passed, w, h, ratio, fail_reason, pil_img = check_image(image_bytes)

        if passed:
            sync_result = f'Image OK ({w}x{h}px, {ratio:.2f})'
            notes = (
                '[IMAGE] ({})\n'
                'PASS: Image validated.\n'
                'Dimensions: {}x{}px\n'
                'Aspect ratio: {:.2f} (within 0.8–1.2)\n'
                'Resolution: OK (minimum 800x800px met)'
            ).format(today_str, w, h, ratio)
            print(f'  ✓ {sku}  {name}')
            print(f'    → {sync_result}')
            if not args.dry_run:
                ok, msg = write_zoho(item_id, 'Image Validated', sync_result, notes, token)
                if not ok:
                    print(f'      Write error: {msg}')
                    error_count += 1
                    continue
            validated += 1

        elif pil_img is not None and 'Resolution too high' in (fail_reason or ''):
            # Auto-downscale: image exceeds Shopify's 20MP limit
            import math
            scale = math.sqrt(MAX_PIXELS / (w * h))
            new_w = int(w * scale)
            new_h = int(h * scale)
            resized = pil_img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format='PNG')
            downscaled_bytes = buf.getvalue()
            new_ratio = round(new_w / new_h, 2)
            filename = f'{sku.replace("/", "-")}_downscaled.png'
            print(f'  ↓ {sku}  {name}')
            print(f'    → Downscaled from {w}x{h} ({w*h/1_000_000:.1f}MP) to {new_w}x{new_h} ({new_w*new_h/1_000_000:.1f}MP)')
            if not args.dry_run:
                ok_upload = upload_upscaled_image(item_id, downscaled_bytes, filename, token)
                if not ok_upload:
                    print(f'      Upload failed — marking as Image required')
                    ok, msg = write_zoho(item_id, 'Image required', fail_reason,
                        f'[IMAGE] ({today_str})\nFAIL: {fail_reason}\nDownscale upload failed.', token)
                    image_required += 1
                    time.sleep(0.3)
                    continue
                sync_result = f'Image OK ({new_w}x{new_h}px, {new_ratio:.2f}) [downscaled from {w}x{h}]'
                notes = (
                    '[IMAGE] ({})\n'
                    'PASS: Image validated (auto-downscaled for Shopify 20MP limit).\n'
                    'Original: {}x{}px ({:.1f}MP)\n'
                    'Downscaled: {}x{}px ({:.1f}MP)\n'
                    'Aspect ratio: {:.2f} (within 0.8–1.2)\n'
                    'Resolution: OK (under 20MP Shopify limit)'
                ).format(today_str, w, h, w*h/1_000_000, new_w, new_h, new_w*new_h/1_000_000, new_ratio)
                ok, msg = write_zoho(item_id, 'Image Validated', sync_result, notes, token)
                if not ok:
                    print(f'      Write error: {msg}')
                    error_count += 1
                    continue
            validated += 1

        elif pil_img is not None and 'Resolution too low' in (fail_reason or ''):
            # Auto-upscale: good aspect ratio but undersized
            from image_processing import auto_upscale
            result = auto_upscale(image_bytes)
            if result:
                upscaled_bytes, uw, uh = result
                new_ratio = round(uw / uh, 2)
                filename = f'{sku.replace("/", "-")}_upscaled.png'
                print(f'  ↑ {sku}  {name}')
                print(f'    → Upscaled from {w}x{h} to {uw}x{uh}')
                if not args.dry_run:
                    ok_upload = upload_upscaled_image(item_id, upscaled_bytes, filename, token)
                    if not ok_upload:
                        print(f'      Upload failed — marking as Image required')
                        ok, msg = write_zoho(item_id, 'Image required', fail_reason,
                            f'[IMAGE] ({today_str})\nFAIL: {fail_reason}\nUpscale upload failed.', token)
                        image_required += 1
                        time.sleep(0.3)
                        continue
                    sync_result = f'Image OK ({uw}x{uh}px, {new_ratio:.2f}) [upscaled from {w}x{h}]'
                    notes = (
                        '[IMAGE] ({})\n'
                        'PASS: Image validated (auto-upscaled).\n'
                        'Original: {}x{}px\n'
                        'Upscaled: {}x{}px\n'
                        'Aspect ratio: {:.2f} (within 0.8–1.2)\n'
                        'Resolution: OK (minimum 800x800px met after upscale)'
                    ).format(today_str, w, h, uw, uh, new_ratio)
                    ok, msg = write_zoho(item_id, 'Image Validated', sync_result, notes, token)
                    if not ok:
                        print(f'      Write error: {msg}')
                        error_count += 1
                        continue
                upscaled_count += 1
                validated += 1
            else:
                # Upscale failed — fall through to normal failure
                print(f'  ✗ {sku}  {name}')
                print(f'    → {fail_reason} (upscale failed)')
                if not args.dry_run:
                    fix = 'Replace with a minimum 800x800px image (ratio 0.8–1.2).'
                    notes = (
                        '[IMAGE] ({})\nFAIL: {}\nDimensions: {}x{}px\nAspect ratio: {:.2f}\n'
                        'Fix: {}\nRe-attach in Zoho and reset status to Enrichment Complete.'
                    ).format(today_str, fail_reason, w, h, ratio, fix)
                    ok, msg = write_zoho(item_id, 'Image required', fail_reason, notes, token)
                    if not ok:
                        print(f'      Write error: {msg}')
                        error_count += 1
                        continue
                image_required += 1

        else:
            if 'ratio' in fail_reason.lower():
                fix = 'Crop to near-square (ratio 0.8–1.2), minimum 800x800px.'
            else:
                fix = 'Replace with a minimum 800x800px image (ratio 0.8–1.2).'
            sync_result = fail_reason
            notes = (
                '[IMAGE] ({})\n'
                'FAIL: {}\n'
                'Dimensions: {}x{}px\n'
                'Aspect ratio: {:.2f}\n'
                'Fix: {}\n'
                'Re-attach in Zoho and reset status to Enrichment Complete.'
            ).format(today_str, fail_reason, w, h, ratio, fix)
            print(f'  ✗ {sku}  {name}')
            print(f'    → {fail_reason}  ({w}x{h}px, ratio {ratio:.2f})')
            if not args.dry_run:
                ok, msg = write_zoho(item_id, 'Image required', sync_result, notes, token)
                if not ok:
                    print(f'      Write error: {msg}')
                    error_count += 1
                    continue
            image_required += 1

        time.sleep(0.3)

    print()
    print('═' * 60)
    print(f'  Done.')
    print(f'  Image Validated  : {validated}')
    if upscaled_count:
        print(f'    (auto-upscaled): {upscaled_count}')
    print(f'  Image required   : {image_required}')
    if error_count:
        print(f'  Write errors     : {error_count}')
    if args.dry_run:
        print('\n  Dry run — no changes written to Zoho.')
    else:
        print('\n  → Review "Image Validated" view in Zoho before running Stage 4.')
        if image_required:
            print(f'  → {image_required} item(s) need images — fix in Zoho, reset to Enrichment Complete.')
    print('═' * 60)

if __name__ == '__main__':
    main()
