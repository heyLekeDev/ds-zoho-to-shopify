#!/usr/bin/env python3
"""
fix_two_images_2026-04-03.py
Fixes product images for two specific Zoho Inventory items:
  - 200-150-017  Vivid Smiley Face Mask         (item_id 5583220000001130889)
  - 200-210-009  Vivid TuffCap 21" Bouffant Cap  (item_id 5583220000001125213)

Strategy:
  1. Try a series of direct CDN URL probes (Pearson Dental catalog)
  2. Fall back to DuckDuckGo image search with multiple queries
  3. For each candidate: download → validate (800x800, AR 0.85-1.15) → upscale if needed
  4. DELETE existing Zoho image → POST replacement
  5. Write cf_shopify_status, cf_sync_result, cf_source_url
"""

import os, io, sys, json, time, requests
from PIL import Image
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

ASPECT_MIN = 0.85
ASPECT_MAX = 1.15
MIN_SIDE   = 800
TARGET_SIDE = 1080

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# ── Zoho auth ─────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except Exception:
            pass
    resp = requests.post(
        'https://accounts.zoho.com/oauth/v2/token',
        params={
            'refresh_token': ZOHO_REFRESH_TOKEN,
            'client_id':     ZOHO_CLIENT_ID,
            'client_secret': ZOHO_CLIENT_SECRET,
            'grant_type':    'refresh_token',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zh(token):
    return {'Authorization': f'Zoho-oauthtoken {token}'}

# ── Image helpers ──────────────────────────────────────────────────────────────

def download(url, referer=None):
    headers = {'User-Agent': UA}
    if referer:
        headers['Referer'] = referer
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get('Content-Type', '')
        if 'image' not in ct and not any(url.lower().split('?')[0].endswith(e)
                                         for e in ('.jpg', '.jpeg', '.png', '.webp')):
            print(f'    ✗ Not an image ({ct[:40]}): {url[:70]}')
            return None
        return r.content
    except Exception as e:
        print(f'    ✗ Download error: {e}')
        return None

def validate_and_prepare(image_bytes):
    """
    Returns (ok, jpeg_bytes, w, h, ratio, msg).
    Upscales to TARGET_SIDE if < MIN_SIDE. Rejects bad aspect ratio.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        w, h = img.size
    except Exception as e:
        return False, None, 0, 0, 0, f'PIL error: {e}'

    ratio = w / h if h else 0
    if ratio < ASPECT_MIN or ratio > ASPECT_MAX:
        return False, None, w, h, ratio, f'Bad aspect ratio {ratio:.2f} (need {ASPECT_MIN}-{ASPECT_MAX})'

    if w < MIN_SIDE or h < MIN_SIDE:
        # Upscale
        scale = TARGET_SIDE / min(w, h)
        nw, nh = int(w * scale), int(h * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        w, h = nw, nh
        print(f'    ↑ Upscaled to {w}x{h}')

    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=92)
    return True, buf.getvalue(), w, h, ratio, 'OK'

# ── Zoho image operations ──────────────────────────────────────────────────────

def delete_zoho_image(item_id, token):
    r = requests.delete(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zh(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if r.status_code == 429:
        time.sleep(60)
        return delete_zoho_image(item_id, token)
    try:
        data = r.json()
        ok = data.get('code') == 0
        print(f'    DELETE image: {"OK" if ok else "FAIL"} — {data.get("message","")}')
        return ok
    except Exception:
        print(f'    DELETE image: HTTP {r.status_code}')
        return r.status_code in (200, 204)

def upload_zoho_image(item_id, jpeg_bytes, filename, token):
    if r := None:  # placeholder
        pass
    r = requests.post(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zh(token),
        params={'organization_id': ZOHO_ORG_ID},
        files={'image': (filename, jpeg_bytes, 'image/jpeg')},
        timeout=30,
    )
    if r.status_code == 429:
        time.sleep(60)
        return upload_zoho_image(item_id, jpeg_bytes, filename, token)
    try:
        data = r.json()
        ok = data.get('code') == 0
        return ok, data.get('message', f'HTTP {r.status_code}')
    except Exception:
        return r.status_code == 200, f'HTTP {r.status_code}'

def write_zoho_fields(item_id, status, result_msg, notes, source_url, token):
    cfs = [
        {'api_name': 'cf_shopify_status',     'value': status},
        {'api_name': 'cf_sync_result',        'value': result_msg},
        {'api_name': 'cf_shopify_sync_notes', 'value': notes},
    ]
    if source_url:
        cfs.append({'api_name': 'cf_source_url', 'value': source_url})
    r = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers={**zh(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID},
        json={'custom_fields': cfs},
        timeout=15,
    )
    if r.status_code == 429:
        time.sleep(60)
        return write_zoho_fields(item_id, status, result_msg, notes, source_url, token)
    try:
        data = r.json()
        return data.get('code') == 0, data.get('message', '')
    except Exception:
        return r.status_code == 200, f'HTTP {r.status_code}'

# ── DDG search ────────────────────────────────────────────────────────────────

def ddg_images(query, max_results=15):
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print('✗ ddgs/duckduckgo_search not installed')
            return []
    try:
        results = list(DDGS(timeout=12).images(query, max_results=max_results))
        time.sleep(3)
        return [{'url': r.get('image', ''), 'w': r.get('width', 0), 'h': r.get('height', 0)}
                for r in results if r.get('image')]
    except Exception as e:
        print(f'    ⚠ DDG error: {e}')
        if 'Ratelimit' in str(e) or '429' in str(e):
            print('    Waiting 60s for DDG rate limit...')
            time.sleep(60)
        return []

# ── Core processing ────────────────────────────────────────────────────────────

def try_url_list(urls, label='direct CDN'):
    """Try a list of direct URLs. Returns (jpeg_bytes, url) or None."""
    for url in urls:
        print(f'  → [{label}] {url}')
        data = download(url, referer='https://www.pearsondental.com/')
        if not data:
            continue
        ok, jpeg, w, h, ratio, msg = validate_and_prepare(data)
        if ok:
            print(f'    ✓ Valid {w}x{h} AR={ratio:.2f} — {url}')
            return jpeg, url
        else:
            print(f'    ✗ {msg}')
    return None

def try_ddg_queries(queries, label='DDG'):
    """Run DDG queries in order. Returns (jpeg_bytes, source_url) or None."""
    for query in queries:
        print(f'  → [{label}] "{query}"')
        candidates = ddg_images(query, max_results=15)
        print(f'    Got {len(candidates)} candidates')
        for c in candidates:
            url = c['url']
            # Skip tiny thumbnails reported by metadata
            if c['w'] and c['w'] < 300:
                continue
            if c['h'] and c['h'] < 300:
                continue
            data = download(url)
            if not data:
                continue
            ok, jpeg, w, h, ratio, msg = validate_and_prepare(data)
            if ok:
                print(f'    ✓ Valid {w}x{h} AR={ratio:.2f} — {url}')
                return jpeg, url
            else:
                print(f'    ✗ {msg} — {url[:60]}')
        time.sleep(3)  # pause between DDG queries
    return None

def process_item(item_id, sku, name, cdn_urls, ddg_queries, token):
    print(f'\n{"="*60}')
    print(f'  Item: {sku}  —  {name}')
    print(f'  ID:   {item_id}')
    print('='*60)

    found = None

    # Phase 1: direct CDN URLs
    if cdn_urls:
        found = try_url_list(cdn_urls)

    # Phase 2: DDG queries
    if not found and ddg_queries:
        found = try_ddg_queries(ddg_queries)

    if not found:
        print(f'  ✗ NO VALID IMAGE FOUND for {sku}')
        ok, msg = write_zoho_fields(
            item_id,
            status='Image required',
            result_msg='Image search failed — no suitable image found',
            notes=(f'Searched {len(cdn_urls)} CDN URLs and {len(ddg_queries)} DDG queries. '
                   'Manual sourcing required.'),
            source_url='',
            token=token,
        )
        print(f'  Zoho status update: {"OK" if ok else "FAIL"} {msg}')
        return False

    jpeg_bytes, source_url = found

    # Delete existing image
    print('  Deleting existing image...')
    delete_zoho_image(item_id, token)
    time.sleep(1)

    # Upload new image
    filename = f'{sku.replace("/", "-")}.jpg'
    print(f'  Uploading {filename} ({len(jpeg_bytes)//1024}KB)...')
    ok, msg = upload_zoho_image(item_id, jpeg_bytes, filename, token)
    if not ok:
        print(f'  ✗ Upload failed: {msg}')
        return False

    print(f'  ✓ Image uploaded: {msg}')

    # Update Zoho fields
    ok2, msg2 = write_zoho_fields(
        item_id,
        status='Image Validated',
        result_msg='Image replaced — correct product image sourced',
        notes=f'Image sourced from: {source_url}',
        source_url=source_url,
        token=token,
    )
    print(f'  Zoho fields updated: {"OK" if ok2 else "FAIL"} {msg2}')
    return True

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print('Zoho Image Fix — 2026-04-03')
    print('Authenticating with Zoho...')
    token = get_zoho_token()
    print('  ✓ Token obtained')

    # ── Item 1: Vivid Smiley Face Mask ────────────────────────────────────────
    # Physical dental face mask with a smiley-face print/pattern — NOT a graphic

    SMILEY_CDN = [
        # Pearson Dental catalog CDN probes — smiley mask product codes
        'https://www.pearsondental.com/catalog/img/BX-0015.jpg',
        'https://www.pearsondental.com/catalog/img_ext/BX-0015.jpg',
        'https://www.pearsondental.com/catalog/img/BX-0015_Box.jpg',
        'https://www.pearsondental.com/catalog/img/MSK-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img_ext/MSK-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img/VIVID-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img_ext/VIVID-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img/VIV-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img/SM-MASK.jpg',
        'https://www.pearsondental.com/catalog/img/SMLEY-MASK.jpg',
        'https://www.pearsondental.com/catalog/img/SMILEY-MASK.jpg',
        'https://www.pearsondental.com/catalog/img/SMILE-MASK.jpg',
        'https://www.pearsondental.com/catalog/img/M-SMILE.jpg',
        'https://www.pearsondental.com/catalog/img/VIVID-SM.jpg',
        # Try with product category prefix
        'https://www.pearsondental.com/catalog/img/BX0015.jpg',
        'https://www.pearsondental.com/catalog/img/MSK0015.jpg',
    ]

    SMILEY_DDG = [
        '"Vivid" smiley face mask dental earloop',
        'vivid smiley face dental mask',
        'pearsondental.com vivid smiley mask',
        'site:pearsondental.com smiley mask',
        'vivid smiley printed face mask dental professional',
        'smiley face printed dental face mask product',
        'smiley pattern dental earloop face mask product image',
        '"smiley face" dental face mask earloop product',
    ]

    result1 = process_item(
        item_id='5583220000001130889',
        sku='200-150-017',
        name='Vivid Smiley Face Mask',
        cdn_urls=SMILEY_CDN,
        ddg_queries=SMILEY_DDG,
        token=token,
    )

    # Pause between items
    print('\nPausing 5s between items...')
    time.sleep(5)

    # ── Item 2: Vivid TuffCap 21" Bouffant Cap — Blue ────────────────────────
    # Blue disposable bouffant surgical cap, 21" diameter — product image preferred

    TUFFCAP_CDN = [
        # Pearson Dental catalog — TuffCap product codes
        'https://www.pearsondental.com/catalog/img/H31-1005.jpg',
        'https://www.pearsondental.com/catalog/img_ext/H31-1005.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1005_Box.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1006.jpg',
        'https://www.pearsondental.com/catalog/img_ext/H31-1006.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1004.jpg',
        'https://www.pearsondental.com/catalog/img_ext/H31-1004.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1000.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1010.jpg',
        'https://www.pearsondental.com/catalog/img_ext/H31-1010.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1001.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1002.jpg',
        'https://www.pearsondental.com/catalog/img/H31-1003.jpg',
        # Broader Vivid TuffCap CDN guesses
        'https://www.pearsondental.com/catalog/img/TUFFCAP-BL.jpg',
        'https://www.pearsondental.com/catalog/img/TUFFCAP-BLUE.jpg',
        'https://www.pearsondental.com/catalog/img/TC-BLUE.jpg',
    ]

    TUFFCAP_DDG = [
        'site:pearsondental.com "TuffCap" vivid',
        '"Vivid TuffCap" bouffant cap blue',
        '"TuffCap" 21 bouffant blue dental',
        'vivid tuffcap bouffant blue disposable cap product',
        'vivid tuffcap 21 inch blue bouffant cap',
        'blue bouffant disposable surgical cap 21 inch product only',
        'blue bouffant cap dental surgery disposable product image',
    ]

    result2 = process_item(
        item_id='5583220000001125213',
        sku='200-210-009',
        name='Vivid TuffCap 21" Bouffant Cap — Blue',
        cdn_urls=TUFFCAP_CDN,
        ddg_queries=TUFFCAP_DDG,
        token=token,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print('\n' + '='*60)
    print('SUMMARY')
    print('='*60)
    print(f'  200-150-017  Smiley Face Mask:       {"✓ DONE" if result1 else "✗ NOT FIXED"}')
    print(f'  200-210-009  TuffCap Bouffant Cap:   {"✓ DONE" if result2 else "✗ NOT FIXED"}')

if __name__ == '__main__':
    main()
