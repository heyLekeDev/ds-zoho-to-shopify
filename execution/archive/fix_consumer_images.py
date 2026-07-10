#!/usr/bin/env python3
"""
fix_consumer_images.py — Re-fetches images for the Consumer Oral Care batch
and uploads them directly to Shopify via the Admin REST API.

Strategy (per product):
  1. Search brand manufacturer site first (via DDG site: query)
  2. Fall back to Amazon.co.uk (via DDG site: query)
  3. Fall back to Boots.com / general retail search
  4. Apply quality filters:
     - Min 800x800px, aspect ratio 0.8–1.2
     - Border-pixel whiteness check (rejects marketing graphics with coloured bg)
     - Cross-product duplicate hash rejection
  5. Upload to Shopify Admin API; delete old bad image

Usage:
    python execution/fix_consumer_images.py
    python execution/fix_consumer_images.py --dry-run
    python execution/fix_consumer_images.py --handle colgate-kids-battery-toothbrush
"""

import os, sys, io, hashlib, json, time, argparse, base64, requests
from datetime import date
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed. Run: pip install Pillow'); sys.exit(1)

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        print('✗ ddgs not installed. Run: pip install ddgs'); sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SHOPIFY_SHOP_URL    = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID   = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SEC  = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION = '2025-01'

_shopify_access_token = None

def _get_shopify_token():
    global _shopify_access_token
    if _shopify_access_token:
        return _shopify_access_token
    url = f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token'
    r = requests.post(url, json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SEC,
        'grant_type': 'client_credentials'
    }, timeout=10)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_access_token = data['access_token']
    return _shopify_access_token

ASPECT_MIN     = 0.8
ASPECT_MAX     = 1.2
MIN_WIDTH      = 800
MIN_HEIGHT     = 800
MAX_CANDIDATES = 8
WHITE_THRESHOLD = 0.35   # fraction of border pixels that must be near-white

# ── The Consumer Oral Care batch (35 products, oldest → newest) ───────────────

BATCH = [
    {"handle": "colgate-kids-battery-toothbrush",
     "title": "Colgate Kids Battery Toothbrush", "vendor": "Colgate"},
    {"handle": "oral-b-kids-character-toothbrush",
     "title": "Oral-B Kids Character Toothbrush", "vendor": "Oral-B"},
    {"handle": "oral-b-crossaction-replacement-heads",
     "title": "Oral-B CrossAction Replacement Heads", "vendor": "Oral-B"},
    {"handle": "oral-b-io-ultimate-clean-replacement-heads",
     "title": "Oral-B iO Ultimate Clean Replacement Heads", "vendor": "Oral-B"},
    {"handle": "oral-b-io-series-2-electric-toothbrush",
     "title": "Oral-B iO Series 2 Electric Toothbrush", "vendor": "Oral-B"},
    {"handle": "tepe-interdental-brush-single-pack",
     "title": "TePe Interdental Brush Single Pack", "vendor": "Tepe"},
    {"handle": "tepe-interdental-brush-bulk-pack",
     "title": "TePe Interdental Brush Bulk Pack", "vendor": "Tepe"},
    {"handle": "dr-fresh-angry-birds-turbo-power-battery-toothbrush",
     "title": "Dr Fresh Angry Birds Turbo Power Battery Toothbrush", "vendor": "Dr. Fresh"},
    {"handle": "dr-fresh-batman-turbo-power-battery-toothbrush",
     "title": "Dr Fresh Batman Turbo Power Battery Toothbrush", "vendor": "Dr. Fresh"},
    {"handle": "colgate-microsinic-toothbrush",
     "title": "Colgate Microsinic Toothbrush", "vendor": "Colgate"},
    {"handle": "colgate-triple-action-toothbrush-medium",
     "title": "Colgate Triple Action Toothbrush Medium", "vendor": "Colgate"},
    {"handle": "colgate-ultra-soft-toothbrush",
     "title": "Colgate Ultra Soft Toothbrush", "vendor": "Colgate"},
    {"handle": "gum-crayola-kids-power-battery-toothbrush",
     "title": "GUM Crayola Kids Power Battery Toothbrush", "vendor": "GUM"},
    {"handle": "gum-crayola-pip-squeaks-kids-manual-toothbrush",
     "title": "GUM Crayola Pip-SQUEAKs Kids Manual Toothbrush", "vendor": "GUM"},
    {"handle": "gum-barbie-kids-manual-toothbrush-with-suction-cup",
     "title": "GUM Barbie Kids Manual Toothbrush with Suction Cup", "vendor": "GUM"},
    {"handle": "gum-barbie-kids-power-battery-toothbrush",
     "title": "GUM Barbie Kids Power Battery Toothbrush", "vendor": "GUM"},
    {"handle": "disney-lion-king-turbo-power-battery-toothbrush",
     "title": "Disney Lion King Turbo Power Battery Toothbrush", "vendor": "Lion King"},
    {"handle": "gum-crayola-kids-timer-light-toothbrush",
     "title": "GUM Crayola Kids Timer Light Toothbrush", "vendor": "GUM"},
    {"handle": "oral-b-precision-clean-replacement-toothbrush-heads-4-pack",
     "title": "Oral-B Precision Clean Replacement Toothbrush Heads 4 Pack", "vendor": "Oral-B"},
    {"handle": "oral-b-sensi-ultrathin-replacement-toothbrush-heads-2-pack",
     "title": "Oral-B Sensi UltraThin Replacement Toothbrush Heads 2 Pack", "vendor": "Oral-B"},
    {"handle": "oral-b-super-floss-pre-cut-strands-50pk",
     "title": "Oral-B Super Floss Pre-Cut Strands 50 Pack", "vendor": "Oral-B"},
    {"handle": "oral-b-kids-stages-2-manual-toothbrush-disney-frozen",
     "title": "Oral-B Kids Stages 2 Manual Toothbrush Disney Frozen", "vendor": "Oral-B"},
    {"handle": "oral-b-kids-stages-2-manual-toothbrush-disney-pixar",
     "title": "Oral-B Kids Stages 2 Manual Toothbrush Disney Pixar", "vendor": "Oral-B"},
    {"handle": "oral-b-stages-1-baby-manual-toothbrush-winnie-the-pooh",
     "title": "Oral-B Stages 1 Baby Manual Toothbrush Winnie the Pooh", "vendor": "Oral-B"},
    {"handle": "oral-b-stages-4-junior-manual-toothbrush-for-me",
     "title": "Oral-B Stages 4 Junior Manual Toothbrush For Me", "vendor": "Oral-B"},
    {"handle": "oral-b-precision-clean-toothbrush-heads-white-2-pack",
     "title": "Oral-B Precision Clean Toothbrush Heads White 2 Pack", "vendor": "Oral-B"},
    {"handle": "oral-b-pro-health-stages-3-kids-manual-toothbrush-spiderman",
     "title": "Oral-B Pro-Health Stages 3 Kids Manual Toothbrush Spiderman", "vendor": "Oral-B"},
    {"handle": "reach-advance-design-manual-toothbrush-soft-compact",
     "title": "Reach Advance Design Manual Toothbrush Soft Compact", "vendor": "Reach"},
    {"handle": "reach-kids-barbie-manual-toothbrush",
     "title": "Reach Kids Barbie Manual Toothbrush", "vendor": "Reach"},
    {"handle": "reach-total-care-manual-toothbrush-soft",
     "title": "Reach Total Care Manual Toothbrush Soft", "vendor": "Reach"},
    {"handle": "tepe-interdental-brush-mixed-pack-multi-size-kit",
     "title": "TePe Interdental Brush Mixed Pack Multi-Size Kit", "vendor": "Tepe"},
    {"handle": "tepe-interdental-brush-original-orange-045mm",
     "title": "TePe Interdental Brush Original Orange 0.45mm", "vendor": "Tepe"},
    {"handle": "tepe-mini-flosser-pre-loaded-holders-36pk",
     "title": "TePe Mini Flosser Pre-Loaded Holders 36 Pack", "vendor": "Tepe"},
    {"handle": "tepe-professional-dental-study-model",
     "title": "TePe Professional Dental Study Model", "vendor": "Tepe"},
    {"handle": "tepe-bio-based-triple-action-tongue-cleaner",
     "title": "TePe Bio-Based Triple Action Tongue Cleaner", "vendor": "Tepe"},
]

# ── Brand-aware search query priority list ────────────────────────────────────
# {t} = product title, {v} = vendor

BRAND_QUERIES = {
    'oral-b':    [
        'site:amazon.co.uk {t}',
        'site:boots.com {t}',
        '{t} toothbrush packaging white background -"replace every" -"better plaque"',
    ],
    'colgate':   [
        'site:amazon.co.uk {t}',
        'site:colgate.com {t}',
        '{t} toothbrush product shot white background',
    ],
    'reach':     [
        'site:amazon.co.uk {t}',
        'site:amazon.com {t}',
        '{t} toothbrush product white background',
    ],
    'tepe':      [
        'site:tepe.com {t}',
        'site:amazon.co.uk {t}',
        '{t} product image',
    ],
    'gum':       [
        'site:amazon.co.uk {t}',
        'site:sunstargum.com {t}',
        '{t} toothbrush product image white background',
    ],
    'default':   [
        'site:amazon.co.uk {t}',
        'site:amazon.com {t}',
        '{t} product image white background',
    ],
}

VENDOR_ALIAS = {
    'io':        'oral-b',
    'stages':    'oral-b',
    'dc comics': 'default',
    'lion king': 'default',
    'dr. fresh': 'default',
    'barbie':    'gum',
    'crayola':   'gum',
}

# Manual image URL overrides for products that automated search can't find.
# Key = handle, value = direct image URL.
MANUAL_OVERRIDES = {
    'dr-fresh-batman-turbo-power-battery-toothbrush':
        'https://m.media-amazon.com/images/I/51LvJwpsg0L.jpg',
    'disney-lion-king-turbo-power-battery-toothbrush':
        'https://m.media-amazon.com/images/I/71MgE0y13QL.jpg',
}

# ── Shopify Admin REST helpers ────────────────────────────────────────────────

def _shopify_headers():
    return {
        'X-Shopify-Access-Token': _get_shopify_token(),
        'Content-Type': 'application/json',
    }

def shopify_get(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/{path}'
    resp = requests.get(url, headers=_shopify_headers(), timeout=20)
    if resp.status_code == 429:
        time.sleep(10); return shopify_get(path)
    return resp.json()

def shopify_post(path, payload):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/{path}'
    resp = requests.post(url, headers=_shopify_headers(), json=payload, timeout=30)
    if resp.status_code == 429:
        time.sleep(10); return shopify_post(path, payload)
    return resp.json()

def shopify_delete(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/{path}'
    resp = requests.delete(url, headers=_shopify_headers(), timeout=15)
    return resp.status_code

def get_product_by_handle(handle):
    data = shopify_get(f'products.json?handle={handle}&fields=id,title,images')
    products = data.get('products', [])
    if not products:
        return None, None, None
    p = products[0]
    imgs = p.get('images', [])
    return p['id'], (imgs[0]['id'] if imgs else None), (imgs[0]['src'] if imgs else None)

def upload_image_to_shopify(product_id, img_bytes, filename):
    """Upload via base64 attachment — works for any image source."""
    encoded = base64.b64encode(img_bytes).decode('utf-8')
    data = shopify_post(f'products/{product_id}/images.json', {
        'image': {'attachment': encoded, 'filename': filename}
    })
    img = data.get('image', {})
    return img.get('id'), img.get('src'), data.get('errors')

def delete_image(product_id, image_id):
    return shopify_delete(f'products/{product_id}/images/{image_id}.json') in (200, 204)

# ── Image search and quality checks ──────────────────────────────────────────

def md5_of(data): return hashlib.md5(data).hexdigest()

def white_border_score(img, w, h):
    """Fraction of border pixels that are near-white (R,G,B > 200)."""
    border = max(5, min(30, w // 15, h // 15))
    pixels = []
    step_x = max(1, w // 60)
    step_y = max(1, h // 60)
    for x in range(0, w, step_x):
        for y_pos in [border, h - border - 1]:
            try: pixels.append(img.getpixel((x, y_pos))[:3])
            except: pass
    for y in range(0, h, step_y):
        for x_pos in [border, w - border - 1]:
            try: pixels.append(img.getpixel((x_pos, y))[:3])
            except: pass
    if not pixels:
        return 1.0
    return sum(1 for r, g, b in pixels if r > 200 and g > 200 and b > 200) / len(pixels)

def check_image(image_bytes):
    """Returns (passed, w, h, ratio, fail_reason)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
    except Exception as e:
        return False, 0, 0, 0.0, f'Cannot open: {e}'

    ratio = round(w / h, 2) if h > 0 else 0.0

    if not (ASPECT_MIN <= ratio <= ASPECT_MAX):
        return False, w, h, ratio, f'Bad aspect ratio {ratio:.2f}'

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return False, w, h, ratio, f'Too small {w}x{h}px'

    score = white_border_score(img, w, h)
    if score < WHITE_THRESHOLD:
        return False, w, h, ratio, f'Dark/coloured background (score {score:.2f})'

    return True, w, h, ratio, None

import re

def _normalize_url(url):
    """Strip Amazon CDN resolution suffixes to get the full-size image."""
    # e.g. https://m.media-amazon.com/images/I/61XYZ._AC_UF350,350_QL50_.jpg
    #   → https://m.media-amazon.com/images/I/61XYZ.jpg
    if 'm.media-amazon.com' in url or 'images-na.ssl-images-amazon.com' in url:
        url = re.sub(r'\._[A-Z0-9_,]+_(\.[a-z]+)$', r'\1', url)
    return url

def download_image(url):
    url = _normalize_url(url)
    try:
        resp = requests.get(url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        if resp.status_code != 200 or not resp.content:
            return None
        ct = resp.headers.get('Content-Type', '')
        ext = url.lower().split('?')[0]
        if 'image' not in ct and not any(ext.endswith(e) for e in ('.jpg','.jpeg','.png','.webp')):
            return None
        return resp.content
    except Exception:
        return None

GOOGLE_API_KEY = os.getenv('SEARCH_API_KEY')
GOOGLE_CX      = os.getenv('GOOGLE_CX')

def search_images(query, max_results=MAX_CANDIDATES):
    """Google Custom Search API (primary). Falls back to DDG if quota exceeded."""
    if GOOGLE_API_KEY and GOOGLE_CX:
        try:
            url = 'https://www.googleapis.com/customsearch/v1'
            params = {
                'key': GOOGLE_API_KEY, 'cx': GOOGLE_CX,
                'q': query, 'searchType': 'image',
                'num': min(max_results, 10), 'imgType': 'photo',
            }
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            if 'items' in data:
                return [{'url': item['link'], 'width': int(item.get('image',{}).get('width',0)),
                         'height': int(item.get('image',{}).get('height',0))}
                        for item in data['items'] if item.get('link')]
            err = data.get('error', {})
            if err.get('code') in (403, 429):
                print(f'     ⚠ Google quota exceeded — falling back to DDG')
            else:
                print(f'     ⚠ Google error: {err.get("message", data)}')
        except Exception as e:
            print(f'     ⚠ Google search error: {e}')

    # DDG fallback
    for attempt in range(3):
        try:
            results = list(DDGS(timeout=12).images(query, max_results=max_results))
            time.sleep(3)
            return [{'url': r.get('image',''), 'width': r.get('width',0),
                     'height': r.get('height',0)} for r in results if r.get('image')]
        except Exception as e:
            msg = str(e)
            if 'Ratelimit' in msg or '429' in msg:
                wait = 30 * (attempt + 1)
                print(f'     ⚠ DDG rate limit — waiting {wait}s...'); time.sleep(wait)
            else:
                print(f'     ⚠ DDG error: {e}'); return []
    return []

def find_best_image(title, vendor, used_hashes, handle=None):
    """Try prioritised queries. Returns (bytes, url, w, h) or None."""
    # Check manual override first
    if handle and handle in MANUAL_OVERRIDES:
        override_url = MANUAL_OVERRIDES[handle]
        print(f'     📌 Manual override: {override_url[:80]}')
        data = download_image(override_url)
        if data:
            passed, w, ht, ratio, reason = check_image(data)
            if passed:
                return data, override_url, w, ht
            print(f'     ✗ Override failed: {reason}')

    vendor_key = VENDOR_ALIAS.get(vendor.lower(), vendor.lower())
    queries_tmpl = BRAND_QUERIES.get(vendor_key, BRAND_QUERIES['default'])

    for tmpl in queries_tmpl:
        query = tmpl.format(t=title, v=vendor)
        print(f'     🔍 {query}')
        candidates = search_images(query)

        for cand in candidates:
            url = cand.get('url', '')
            if not url:
                continue
            # Skip obviously tiny images
            mw, mh = cand.get('width', 0), cand.get('height', 0)
            if mw and mh and (mw < 300 or mh < 300):
                continue

            data = download_image(url)
            if not data:
                continue

            h = md5_of(data)
            if h in used_hashes:
                print(f'     ⚠ Duplicate — skipping')
                continue

            passed, w, ht, ratio, reason = check_image(data)
            if passed:
                return data, url, w, ht
            else:
                print(f'     ✗ {reason}')

    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Find images but do not upload to Shopify')
    parser.add_argument('--handle', metavar='HANDLE',
                        help='Process only this product handle')
    args = parser.parse_args()

    today = date.today().isoformat()
    batch = [p for p in BATCH if not args.handle or p['handle'] == args.handle]

    print(f'\n🦷 Consumer Image Fix — {today}')
    print(f'   Products: {len(batch)}  |  Dry run: {args.dry_run}')
    print(f'   Store: {SHOPIFY_SHOP_URL}\n')

    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SEC:
        print('✗ SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET not set in .env'); sys.exit(1)

    fixed, no_image, errors = [], [], []
    used_hashes = set()

    for i, product in enumerate(batch, 1):
        handle = product['handle']
        title  = product['title']
        vendor = product['vendor']

        print(f'[{i:02d}/{len(batch)}] {title}  [{vendor}]')

        product_id, old_img_id, old_img_src = get_product_by_handle(handle)
        if not product_id:
            print(f'  ✗ Not found on Shopify\n')
            errors.append({'handle': handle, 'reason': 'Not found on Shopify'})
            continue

        print(f'  ID: {product_id} | Current: {(old_img_src or "none")[:65]}')

        result = find_best_image(title, vendor, used_hashes, handle=handle)

        if not result:
            print(f'  ✗ No suitable image found\n')
            no_image.append({'handle': handle, 'title': title})
            continue

        img_bytes, img_url, w, h = result
        used_hashes.add(md5_of(img_bytes))
        print(f'  ✓ Image found: {w}x{h}px')
        print(f'    Source: {img_url[:80]}')

        if args.dry_run:
            print(f'  ↷ DRY RUN — skipping upload\n')
            fixed.append({'handle': handle, 'title': title,
                          'source': img_url, 'dry_run': True})
            continue

        # Upload new image
        new_img_id, new_img_src, upload_errors = upload_image_to_shopify(
            product_id, img_bytes, f'{handle}.jpg'
        )

        if not new_img_id:
            print(f'  ✗ Upload failed: {upload_errors}\n')
            errors.append({'handle': handle, 'reason': f'Upload failed: {upload_errors}'})
            continue

        # Delete old image
        if old_img_id:
            ok = delete_image(product_id, old_img_id)
            print(f'  {"✓" if ok else "⚠"} Old image {"deleted" if ok else "could not delete"}')

        print(f'  ✅ Done → {(new_img_src or "")[:70]}\n')
        fixed.append({'handle': handle, 'title': title, 'new_src': new_img_src})
        time.sleep(1.5)

    # ── Summary ───────────────────────────────────────────────────────────────
    print('=' * 60)
    print(f'SUMMARY ({today}):')
    print(f'  ✅ Fixed:     {len(fixed)}')
    print(f'  ✗  No image: {len(no_image)}')
    print(f'  ✗  Errors:   {len(errors)}')

    if no_image:
        print('\nNeeds manual image sourcing:')
        for p in no_image:
            print(f'  - {p["title"]}')

    if errors:
        print('\nErrors:')
        for e in errors:
            print(f'  - {e["handle"]}: {e["reason"]}')

    out = {'date': today, 'fixed': fixed, 'no_image': no_image, 'errors': errors}
    out_file = f'fix_consumer_images_{today}.json'
    with open(out_file, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'\nLog saved → {out_file}')

if __name__ == '__main__':
    main()
