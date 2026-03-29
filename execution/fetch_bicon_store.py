#!/usr/bin/env python3
"""
Bicon Store Image Fetcher

Downloads product images directly from store.bicon.com using known part numbers.
For Bicon items without a confirmed store part, falls back to DDG.
For non-Bicon items (UnoDent, Komet), uses DDG with brand-specific queries.

Requires VPN if store.bicon.com is geo-restricted.

Usage:
    python execution/fetch_bicon_store.py
    python execution/fetch_bicon_store.py --dry-run
    python execution/fetch_bicon_store.py --sku 320-150-003
"""

import os, sys, io, json, time, argparse, requests
from datetime import date
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed. Run: python3 -m pip install Pillow')
    sys.exit(1)

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        print('✗ ddgs not installed. Run: python3 -m pip install ddgs')
        sys.exit(1)

import warnings
warnings.filterwarnings('ignore')

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'
ENRICHMENT_INPUT   = 'enrichment_input.json'

ASPECT_MIN = 0.8
ASPECT_MAX = 1.2
MIN_WIDTH  = 800
MIN_HEIGHT = 800

BICON_STORE_BASE = 'https://store.bicon.com/product/image'

# ── store.bicon.com part number map ──────────────────────────────────────────
# SKU → store.bicon.com full part number (260-xxx-xxx)
# Confirmed from live store catalogue March 2026.
#
# Key corrections vs. bicon_parts.json:
#   320-150-003: Guide Pin 2.0mm → 260-101-180  (NOT 260-101-440 which is Sulcus Former)
#   320-150-004: Guide Pin 2.5mm → 260-101-186  (NOT 260-101-450 which is Sulcus Former)
#   320-120-023: 2.5mm Impression Kit → 260-100-414 (NOT 260-100-434)
#
STORE_MAP = {
    # Burs
    '160-120-001': '260-155-704',  # #1557 Latch Type Bur (#4 Round Bur Regular Length)
    '160-120-002': '260-155-701',  # #1557 Regular Length Bur
    '160-140-001': '260-201-011',  # 11.0mm Abutment Cutting Bur

    # Surgical instruments
    '320-130-001': '260-901-460',  # 6.0mm Osteotome
    '320-150-007': '260-101-015',  # Crown Seating Tip
    '320-150-009': '260-101-001',  # 2.0mm Standard Pilot Drill
    '320-150-010': '260-701-001',  # Paralleling Pin
    '320-140-034': '260-101-395',  # Threaded Abutment Prep Holder with Tips

    # Guide Pins
    '320-150-003': '260-101-180',  # 2.0mm Standard Guide Pin (4)
    '320-150-004': '260-101-186',  # 2.5mm Standard Guide Pin (4)

    # Impression & Prosthetics
    '320-120-001': '260-100-433',  # 3.0mm Titanium Impression Post & Sleeve
    '320-120-023': '260-100-414',  # 2.5mm Impression Kit
    '320-120-024': '260-100-434',  # 3.0mm Impression Kit
    '320-150-005': '260-100-399',  # 3.0mm Digital Implant Analog Kit

    # Temporary Sleeves (Non-Shouldered)
    '320-160-005': '260-140-165',  # 4mm Non-Shoulder Temporization Sleeve
    '320-160-006': '260-150-165',  # 5mm Non-Shoulder Temporization Sleeve
}

# DDG fallback queries for items not in STORE_MAP
# SKU → list of queries to try (in order)
DDG_FALLBACK = {
    # Drilling Implant Guides — no individual store part confirmed
    '320-150-006': [
        'bicon 9mm drilling implant guide depth stop surgical',
        'bicon pilot drill depth guide 9mm',
        'site:bicon.com drilling guide 9mm',
    ],
    '320-150-001': [
        'bicon 10.5mm drilling implant guide depth stop surgical',
        'bicon pilot drill depth guide 10.5mm',
        'site:bicon.com drilling guide 10mm',
    ],
    '320-150-002': [
        'bicon 12mm drilling implant guide depth stop surgical',
        'bicon pilot drill depth guide 12mm',
        'site:bicon.com drilling guide 12mm',
    ],
    '320-150-008': [
        'bicon MD guide drilling implant manual driver',
        'bicon MD guide implant surgical',
        'bicon implant depth guide MD',
    ],
    # Non-Bicon accessories — DDG with brand name
    '160-110-001': [
        'UnoDent bur block large round blue dental',
        'dental bur block large round blue 30 hole',
    ],
    '160-110-003': [
        'Komet Pulimant bur cleaner dental',
        'Komet 314012 Pulimant bur cleaning fluid',
    ],
    '160-110-004': [
        'UnoDent metal bur cleaning brush dental',
        'dental metal bur brush stainless steel',
    ],
    '160-110-006': [
        'UnoDent metal mesh bur holder stand dental',
        'dental bur holder metal mesh stand',
    ],
}

COMPETITOR_DOMAINS = {
    'straumann', 'nobelbiocare', 'nobel-biocare', 'zimmer', 'biomet',
    'zimmerbiomet', 'dentsply', 'sirona', 'osstem', 'megagen',
    'astratech', 'neodent', 'bredent', 'anthogyr', 'mis-implants',
    'biohorizons', 'keystone-dental', 'hiossen', 'dentiumusa',
}


# ── Zoho auth ──────────────────────────────────────────────────────────────────

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
        raise Exception(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token


def zoho_headers(token):
    return {'Authorization': f'Zoho-oauthtoken {token}'}


def get_item_status(item_id, token):
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return get_item_status(item_id, token)
    item = resp.json().get('item', {})
    cfs  = {cf['api_name']: cf.get('value') for cf in item.get('custom_fields', [])}
    return cfs.get('cf_shopify_status', '')


def delete_zoho_image(item_id, token):
    resp = requests.delete(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return delete_zoho_image(item_id, token)
    return resp.json().get('code') == 0


def upload_image_to_zoho(item_id, image_bytes, filename, token):
    try:
        resp = requests.post(
            f'{ZOHO_API_BASE}/items/{item_id}/image',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID},
            files={'image': (filename, image_bytes, 'image/jpeg')},
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(60)
            return upload_image_to_zoho(item_id, image_bytes, filename, token)
        data = resp.json()
        return data.get('code') == 0, data.get('message', f'HTTP {resp.status_code}')
    except requests.RequestException as e:
        return False, str(e)


def write_zoho_status(item_id, status, sync_result, notes, source_url, token):
    custom_fields = [
        {'api_name': 'cf_shopify_status',     'value': status},
        {'api_name': 'cf_sync_result',        'value': sync_result},
        {'api_name': 'cf_shopify_sync_notes', 'value': notes},
    ]
    if source_url:
        custom_fields.append({'api_name': 'cf_source_url', 'value': source_url})
    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID},
        json={'custom_fields': custom_fields},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return write_zoho_status(item_id, status, sync_result, notes, source_url, token)
    data = resp.json()
    return data.get('code') == 0


# ── Image download + validation ───────────────────────────────────────────────

def download_image(url):
    try:
        resp = requests.get(url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': 'https://store.bicon.com/',
        })
        if resp.status_code != 200 or not resp.content:
            return None
        ct = resp.headers.get('Content-Type', '')
        ext = url.lower().split('?')[0]
        if 'image' not in ct and not any(ext.endswith(e) for e in ('.jpg', '.jpeg', '.png', '.webp')):
            return None
        return resp.content
    except Exception:
        return None


def check_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
    except Exception as e:
        return False, 0, 0, 0.0, str(e), None
    ratio = round(w / h, 2) if h > 0 else 0.0
    if ratio < ASPECT_MIN or ratio > ASPECT_MAX:
        return False, w, h, ratio, f'Aspect {ratio:.2f}', None
    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return False, w, h, ratio, f'Too small ({w}x{h}px)', None
    return True, w, h, ratio, None, img


# ── Store.bicon.com direct fetch ──────────────────────────────────────────────

def fetch_from_store(store_part):
    """
    Try large then small image from store.bicon.com.
    Returns (image_bytes, pil_img, w, h, ratio, url) or None.
    """
    for size in ('large', 'medium', 'small'):
        url = f'{BICON_STORE_BASE}/{size}/{store_part}_1.jpg'
        print(f'     → {url}')
        data = download_image(url)
        if not data:
            continue
        passed, w, h, ratio, reason, img = check_image(data)
        if passed:
            return data, img, w, h, ratio, url
        else:
            print(f'     ✗ {reason}')
    return None


# ── DDG fallback ──────────────────────────────────────────────────────────────

def search_ddg(query, max_results=5, _retries=3):
    for attempt in range(_retries + 1):
        try:
            results = list(DDGS(timeout=12).images(query, max_results=max_results))
            time.sleep(5)  # cool-down after successful query
            return [{'url': r.get('image', ''), 'width': r.get('width', 0), 'height': r.get('height', 0)}
                    for r in results if r.get('image')]
        except Exception as e:
            msg = str(e)
            if 'Ratelimit' in msg or '429' in msg or '202' in msg:
                wait = 60 * (attempt + 1)
                print(f'     ⚠ DDG rate limited — waiting {wait}s (attempt {attempt+1}/{_retries+1})...')
                time.sleep(wait)
            else:
                print(f'     ⚠ DDG error: {e}')
                return []
    return []


def fetch_from_ddg(queries):
    for query in queries:
        print(f'     Searching DDG: "{query}"')
        candidates = search_ddg(query)
        time.sleep(15)
        for c in candidates:
            url = c.get('url', '')
            if not url:
                continue
            if any(comp in url.lower() for comp in COMPETITOR_DOMAINS):
                continue
            meta_w, meta_h = c.get('width', 0), c.get('height', 0)
            if meta_w and meta_h and (meta_w < MIN_WIDTH or meta_h < MIN_HEIGHT):
                continue
            data = download_image(url)
            if not data:
                continue
            passed, w, h, ratio, reason, img = check_image(data)
            if passed:
                return data, img, w, h, ratio, url
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Fetch Bicon images from store.bicon.com')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sku', help='Process single SKU')
    args = parser.parse_args()

    print('═' * 60)
    print('  Bicon Store Image Fetcher')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    with open(ENRICHMENT_INPUT) as f:
        items = json.load(f)

    if args.sku:
        items = [it for it in items if it.get('sku') == args.sku]
        if not items:
            print(f'✗ SKU {args.sku} not found')
            sys.exit(1)

    token     = get_zoho_token()
    today_str = date.today().isoformat()
    counts    = {'validated': 0, 'no_image': 0, 'skipped': 0, 'error': 0}

    print(f'\n  Processing {len(items)} item(s)...')

    for item in items:
        sku     = item.get('sku', '?')
        name    = item.get('name', '')
        item_id = item['item_id']

        token = get_zoho_token()
        status = get_item_status(item_id, token)

        if status != 'Image required':
            print(f'\n  ⬜ {sku}  {name[:45]}  [{status}]')
            counts['skipped'] += 1
            continue

        store_part = STORE_MAP.get(sku)
        source_tag = 'STORE' if store_part else 'DDG'

        print(f'\n  🔍 {sku}  {name[:45]}')
        if store_part:
            print(f'     Store part: {store_part}')

        hit = None

        # 1. Try store.bicon.com directly (if we have a part number)
        if store_part:
            hit = fetch_from_store(store_part)
            if not hit:
                print(f'     ✗ Store fetch failed — trying DDG fallback...')

        # 2. DDG fallback (for items without store_part, or if store fetch failed)
        if not hit:
            queries = DDG_FALLBACK.get(sku)
            if not queries:
                # Auto-generate generic DDG query from item name
                clean = name.title().replace('-', ' ').replace('*', '').strip()
                brand = item.get('brand', '')
                queries = [f'{brand} {clean}'.strip(), f'{clean} dental implant']
            hit = fetch_from_ddg(queries)
            source_tag = 'DDG'

        if not hit:
            print(f'     ✗ No valid image found')
            counts['no_image'] += 1
            time.sleep(15)
            continue

        image_bytes, img, w, h, ratio, url = hit
        print(f'     ✓ Valid: {w}x{h}px, ratio {ratio:.2f}')
        print(f'       {url[:80]}')

        if args.dry_run:
            counts['validated'] += 1
            continue

        # Delete existing image
        delete_zoho_image(item_id, token)
        time.sleep(1)

        # Convert to JPEG
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=95)
        jpeg_bytes = out.getvalue()

        # Upload
        filename = f'{sku.replace("/", "-")}.jpg'
        ok_upload, msg = upload_image_to_zoho(item_id, jpeg_bytes, filename, token)
        if not ok_upload:
            print(f'     ✗ Upload failed: {msg}')
            counts['error'] += 1
            time.sleep(15)
            continue

        # Write status
        sync_result = f'Image OK ({w}x{h}px, {ratio:.2f})'
        if store_part:
            notes = (
                f'[IMAGE-{source_tag}] ({today_str})\n'
                f'PASS: Image fetched directly from store.bicon.com\n'
                f'Store part: {store_part}\n'
                f'Source: {url[:200]}\n'
                f'Dimensions: {w}x{h}px | Aspect: {ratio:.2f}'
            )
        else:
            notes = (
                f'[IMAGE-{source_tag}] ({today_str})\n'
                f'PASS: Image fetched via DDG (no store part mapped).\n'
                f'Source: {url[:200]}\n'
                f'Dimensions: {w}x{h}px | Aspect: {ratio:.2f}'
            )

        ok_write = write_zoho_status(item_id, 'Image Validated', sync_result, notes, url, token)
        if ok_write:
            print(f'     → Written: Image Validated  [{source_tag}]')
            counts['validated'] += 1
        else:
            print(f'     ✗ Status write failed')
            counts['error'] += 1

        time.sleep(10)

    print()
    print('═' * 60)
    print('  Done.')
    print(f'  Validated      : {counts["validated"]}')
    print(f'  No image found : {counts["no_image"]}')
    print(f'  Skipped        : {counts["skipped"]}')
    if counts['error']:
        print(f'  Errors         : {counts["error"]}')
    if counts['validated']:
        print(f'\n  → Run validate_enrichment.py when all images are ready.')
    if counts['no_image']:
        print(f'  → {counts["no_image"]} item(s) need manual images in Zoho.')
    print('═' * 60)


if __name__ == '__main__':
    main()
