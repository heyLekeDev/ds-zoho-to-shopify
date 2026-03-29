#!/usr/bin/env python3
"""
normalize_shopify_images.py — Re-upload all product images as 800×800 white-padded squares.

For each product handle in BATCH:
  1. Get current images from Shopify
  2. Download each image
  3. Resize / pad to TARGET_SIZE × TARGET_SIZE on white background
  4. If the image is already the right size AND square, skip it
  5. Otherwise delete old image and re-upload normalized version

Usage:
    python execution/normalize_shopify_images.py             # all products
    python execution/normalize_shopify_images.py --dry-run   # preview only
    python execution/normalize_shopify_images.py --handle HANDLE
"""

import os, sys, io, base64, time, argparse, requests
from PIL import Image
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

SHOPIFY_SHOP_URL   = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID  = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SEC = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VER    = '2025-01'
TARGET_SIZE        = 800

REQUESTS_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# All products that had images uploaded (main batch + extras)
HANDLES = [
    "colgate-kids-battery-toothbrush",
    "oral-b-kids-character-toothbrush",
    "oral-b-crossaction-replacement-heads",
    "oral-b-io-series-2-electric-toothbrush",
    "tepe-interdental-brush-single-pack",
    "tepe-interdental-brush-bulk-pack",
    "colgate-triple-action-toothbrush-medium",
    "colgate-ultra-soft-toothbrush",
    "gum-crayola-kids-power-battery-toothbrush",
    "gum-crayola-pip-squeaks-kids-manual-toothbrush",
    "gum-barbie-kids-manual-toothbrush-with-suction-cup",
    "gum-barbie-kids-power-battery-toothbrush",
    "gum-crayola-kids-timer-light-toothbrush",
    "oral-b-precision-clean-replacement-toothbrush-heads-4-pack",
    "oral-b-sensi-ultrathin-replacement-toothbrush-heads-2-pack",
    "oral-b-super-floss-pre-cut-strands-50pk",
    "oral-b-kids-stages-2-manual-toothbrush-disney-frozen",
    "oral-b-kids-stages-2-manual-toothbrush-disney-pixar",
    "oral-b-precision-clean-toothbrush-heads-white-2-pack",
    "reach-total-care-manual-toothbrush-soft",
    "tepe-interdental-brush-mixed-pack-multi-size-kit",
    "tepe-interdental-brush-original-orange-045mm",
    "tepe-mini-flosser-pre-loaded-holders-36pk",
    "tepe-bio-based-triple-action-tongue-cleaner",
    "oral-b-pro-health-stages-3-kids-manual-toothbrush-princesses",
]

# ── Shopify auth ──────────────────────────────────────────────────────────────

_token = None

def get_token():
    global _token
    if _token: return _token
    r = requests.post(f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token', json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SEC,
        'grant_type': 'client_credentials',
    }, timeout=10)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _token = data['access_token']
    return _token

def hdrs():
    return {'X-Shopify-Access-Token': get_token(), 'Content-Type': 'application/json'}

def api_get(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.get(url, headers=hdrs(), timeout=20)
    if r.status_code == 429: time.sleep(10); return api_get(path)
    return r.json()

def api_post(path, payload):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.post(url, headers=hdrs(), json=payload, timeout=30)
    if r.status_code == 429: time.sleep(10); return api_post(path, payload)
    return r.json()

def api_delete(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.delete(url, headers=hdrs(), timeout=15)
    return r.status_code

# ── Image helpers ─────────────────────────────────────────────────────────────

def download(url):
    try:
        r = requests.get(url, timeout=20, headers={'User-Agent': REQUESTS_UA})
        return r.content if r.status_code == 200 and r.content else None
    except Exception:
        return None

def normalize(img_bytes, size=TARGET_SIZE):
    """Fit image inside size×size on white background, return JPEG bytes."""
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')
    ratio = min(size / img.width, size / img.height)
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new('RGB', (size, size), (255, 255, 255))
    canvas.paste(img, ((size - new_w) // 2, (size - new_h) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format='JPEG', quality=92)
    return buf.getvalue()

def is_already_normalized(img_bytes, size=TARGET_SIZE, tolerance=4):
    """True if image is already size×size (within tolerance pixels)."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        return abs(img.width - size) <= tolerance and abs(img.height - size) <= tolerance
    except Exception:
        return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--handle', metavar='HANDLE')
    args = parser.parse_args()

    handles = [args.handle] if args.handle else HANDLES

    print(f'\n🖼  Normalize Shopify Images → {TARGET_SIZE}×{TARGET_SIZE}px')
    print(f'   Products: {len(handles)}  |  Dry run: {args.dry_run}')
    print(f'   Store: {SHOPIFY_SHOP_URL}\n')

    normalized, skipped, errors = 0, 0, 0

    for i, handle in enumerate(handles, 1):
        data = api_get(f'products.json?handle={handle}&fields=id,title,images')
        products = data.get('products', [])
        if not products:
            print(f'[{i:02}/{len(handles)}] ✗ Not found: {handle}')
            errors += 1
            continue

        p = products[0]
        pid, title = p['id'], p.get('title', handle)
        images = p.get('images', [])

        if not images:
            print(f'[{i:02}/{len(handles)}] — No images: {title}')
            skipped += 1
            continue

        print(f'[{i:02}/{len(handles)}] {title}  ({len(images)} image(s))')

        for img in images:
            src = img.get('src', '')
            img_id = img['id']

            raw = download(src)
            if not raw:
                print(f'  ✗ Could not download: {src[:60]}')
                errors += 1
                continue

            try:
                orig = Image.open(io.BytesIO(raw))
                orig_w, orig_h = orig.size
            except Exception as e:
                print(f'  ✗ Cannot open image: {e}')
                errors += 1
                continue

            if is_already_normalized(raw):
                print(f'  ✓ Already {orig_w}×{orig_h} — skip')
                skipped += 1
                continue

            norm_bytes = normalize(raw)
            print(f'  ↳ {orig_w}×{orig_h} → {TARGET_SIZE}×{TARGET_SIZE}px', end='')

            if args.dry_run:
                print(f'  [DRY RUN]')
                skipped += 1
                continue

            # Delete old image
            sc = api_delete(f'products/{pid}/images/{img_id}.json')
            if sc not in (200, 204):
                print(f'\n  ✗ Delete failed (HTTP {sc})')
                errors += 1
                continue

            # Upload normalized
            encoded = base64.b64encode(norm_bytes).decode()
            result = api_post(f'products/{pid}/images.json', {
                'image': {'attachment': encoded, 'filename': f'{handle}.jpg'}
            })
            new_src = result.get('image', {}).get('src')
            if new_src:
                print(f'  ✅')
                normalized += 1
            else:
                print(f'\n  ✗ Upload failed: {result.get("errors")}')
                errors += 1

            time.sleep(0.5)

        print()

    print('=' * 60)
    print(f'SUMMARY:')
    print(f'  ✅ Normalized: {normalized}')
    print(f'  ↷  Skipped:   {skipped}')
    print(f'  ✗  Errors:    {errors}')

if __name__ == '__main__':
    main()
