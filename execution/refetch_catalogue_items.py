#!/usr/bin/env python3
"""
Catalogue-guided image re-fetch.

Uses bicon_parts.json to build precise DDG search queries from Bicon part
numbers, then re-uploads images for items that currently have questionable
images (or any item you specify via --sku).

Usage:
    python execution/refetch_catalogue_items.py --dry-run
    python execution/refetch_catalogue_items.py --sku 320-150-007
    python execution/refetch_catalogue_items.py            # all mapped items
"""

import os, sys, io, json, time, re, argparse, requests
from datetime import date
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed. Run: python3 -m pip install Pillow')
    sys.exit(1)

try:
    from duckduckgo_search import DDGS
except ImportError:
    print('✗ duckduckgo-search not installed. Run: python3 -m pip install duckduckgo-search')
    sys.exit(1)

import warnings
warnings.filterwarnings('ignore')

# ── Config ─────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

ENRICHMENT_INPUT = 'enrichment_input.json'
PARTS_MAP_FILE   = 'resources/bicon_parts.json'

ASPECT_MIN  = 0.8
ASPECT_MAX  = 1.2
MIN_WIDTH   = 800
MIN_HEIGHT  = 800

# Items whose current images are known/suspected to be wrong
SUSPECT_SKUS = {
    '320-150-007',   # Got a Bicon Short Implant (healing plug) image instead of Crown Seating Tip
    '320-150-003',   # Got a generic Chinese implant screw image instead of Guide Pin
    '320-150-004',   # Same — Guide Pin should be a long thin pin
}

COMPETITOR_DOMAINS = {
    'straumann', 'nobelbiocare', 'nobel-biocare', 'zimmer', 'biomet',
    'zimmerbiomet', 'dentsply', 'sirona', 'osstem', 'megagen',
    'astratech', 'neodent', 'bredent', 'anthogyr', 'mis-implants',
    'biohorizons', 'keystone-dental', 'hiossen', 'dentiumusa',
}

def is_competitor_url(url):
    u = url.lower()
    return any(c in u for c in COMPETITOR_DOMAINS)

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

def get_item_detail(item_id, token):
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    item = resp.json().get('item', {})
    cfs  = {cf['api_name']: cf.get('value') for cf in item.get('custom_fields', [])}
    return cfs.get('cf_shopify_status', ''), cfs.get('cf_source_url', '')

def delete_zoho_image(item_id, token):
    resp = requests.delete(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    data = resp.json()
    return data.get('code') == 0

def upload_image_to_zoho(item_id, image_bytes, filename, token):
    try:
        resp = requests.post(
            f'{ZOHO_API_BASE}/items/{item_id}/image',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID},
            files={'image': (filename, image_bytes, 'image/jpeg')},
            timeout=30,
        )
        data = resp.json()
        return data.get('code') == 0, data.get('message', f'HTTP {resp.status_code}')
    except requests.RequestException as e:
        return False, str(e)

def write_zoho_status(item_id, new_status, sync_result, notes, source_url, token):
    custom_fields = [
        {'api_name': 'cf_shopify_status',     'value': new_status},
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
    data = resp.json()
    return data.get('code') == 0, data.get('message', '')

# ── Image search + validation ──────────────────────────────────────────────────

def search_ddg(query, max_results=5, _retries=1):
    for attempt in range(_retries + 1):
        try:
            results = list(DDGS(timeout=8).images(query, max_results=max_results))
            return [{'url': r.get('image', ''), 'width': r.get('width', 0), 'height': r.get('height', 0)}
                    for r in results if r.get('image')]
        except Exception as e:
            msg = str(e)
            if 'Ratelimit' in msg or '429' in msg or '202' in msg:
                wait = 90 * (attempt + 1)
                print(f'     ⚠ DDG rate limited — waiting {wait}s...')
                time.sleep(wait)
            else:
                print(f'     ⚠ DDG error: {e}')
                return []
    return []

def download_image(url):
    try:
        resp = requests.get(url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        })
        if resp.status_code != 200 or not resp.content:
            return None
        ct = resp.headers.get('Content-Type', '')
        ext = url.lower().split('?')[0]
        if 'image' not in ct and not any(ext.endswith(e) for e in ('.jpg', '.jpeg', '.png', '.webp', '.gif')):
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

def find_best_image(queries):
    """Try each query in order, return (image_bytes, pil_img, w, h, ratio, url) or None."""
    for query in queries:
        print(f'     Searching: "{query}"')
        candidates = search_ddg(query)
        time.sleep(15)  # rate limit buffer

        for c in candidates:
            url = c.get('url', '')
            if not url or is_competitor_url(url):
                if is_competitor_url(url):
                    print(f'     ✗ Blocked competitor: {url[:60]}')
                continue
            meta_w, meta_h = c.get('width', 0), c.get('height', 0)
            if meta_w and meta_h and (meta_w < MIN_WIDTH or meta_h < MIN_HEIGHT):
                continue
            image_bytes = download_image(url)
            if not image_bytes:
                continue
            passed, w, h, ratio, reason, img = check_image(image_bytes)
            if passed:
                return image_bytes, img, w, h, ratio, url
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Catalogue-guided image re-fetch')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sku', help='Re-fetch a single SKU')
    parser.add_argument('--suspect-only', action='store_true',
                        help='Only re-fetch SKUs known to have wrong images')
    args = parser.parse_args()

    print('═' * 60)
    print('  Catalogue-Guided Image Re-fetch')
    if args.dry_run:
        print('  Mode: DRY RUN')
    print('═' * 60)

    if not os.path.exists(PARTS_MAP_FILE):
        print(f'✗ {PARTS_MAP_FILE} not found')
        sys.exit(1)
    if not os.path.exists(ENRICHMENT_INPUT):
        print(f'✗ {ENRICHMENT_INPUT} not found')
        sys.exit(1)

    with open(PARTS_MAP_FILE) as f:
        parts_map = json.load(f)
    with open(ENRICHMENT_INPUT) as f:
        all_items = json.load(f)

    # Build lookup: sku → item
    item_by_sku = {it['sku']: it for it in all_items}

    # Decide which SKUs to process
    if args.sku:
        target_skus = [args.sku]
    elif args.suspect_only:
        target_skus = list(SUSPECT_SKUS)
    else:
        target_skus = [sku for sku in parts_map if not sku.startswith('_')]

    today_str = date.today().isoformat()
    counts = {'updated': 0, 'would_update': 0, 'no_image': 0, 'skipped': 0, 'error': 0}

    for sku in target_skus:
        entry = parts_map.get(sku, {})
        if sku.startswith('_') or not entry:
            continue

        item = item_by_sku.get(sku)
        if not item:
            print(f'\n  ⚠ {sku} not in {ENRICHMENT_INPUT} — skipping')
            continue

        item_id   = item['item_id']
        name      = item.get('name', '')
        bicon_part = entry.get('bicon_part', '')
        bicon_name = entry.get('bicon_name', '')
        search_hint = entry.get('search_hint', '')
        notes_hint  = entry.get('notes', '')

        print(f'\n  🔍 {sku}  {name[:45]}')
        print(f'     Bicon part: {bicon_part} — {bicon_name}')
        print(f'     ({notes_hint})')

        # Build precision queries using part number first, then hint
        queries = []
        if bicon_part:
            queries.append(f'bicon {bicon_part} {bicon_name}')
            queries.append(f'260-{bicon_part} bicon dental')
        if search_hint:
            queries.append(search_hint)
        if bicon_name:
            queries.append(f'bicon {bicon_name} dental')

        hit = find_best_image(queries)

        if not hit:
            print(f'     ✗ No valid image found')
            counts['no_image'] += 1
            time.sleep(20)
            continue

        image_bytes, img, w, h, ratio, url = hit
        print(f'     ✓ Valid: {w}x{h}px, ratio {ratio:.2f}')
        print(f'       {url[:75]}')

        if args.dry_run:
            counts['would_update'] += 1
            time.sleep(20)
            continue

        try:
            token = get_zoho_token()

            # Delete existing image first
            delete_zoho_image(item_id, token)
            time.sleep(2)

            # Convert to JPEG
            out = io.BytesIO()
            img.save(out, format='JPEG', quality=95)
            jpeg_bytes = out.getvalue()

            # Upload
            filename = f'{sku.replace("/", "-")}.jpg'
            ok_upload, msg_upload = upload_image_to_zoho(item_id, jpeg_bytes, filename, token)
            if not ok_upload:
                print(f'     ✗ Upload failed: {msg_upload}')
                counts['error'] += 1
                time.sleep(20)
                continue

            # Write status
            sync_result = f'Image OK ({w}x{h}px, {ratio:.2f})'
            status_notes = (
                '[IMAGE-CATALOGUE] ({})\n'
                'PASS: Image re-fetched using Bicon catalogue part number {}.\n'
                'Product: {} ({})\n'
                'Source: {}\n'
                'Dimensions: {}x{}px | Aspect: {:.2f}'
            ).format(today_str, bicon_part, bicon_name, notes_hint, url[:200], w, h, ratio)

            ok_write, _ = write_zoho_status(item_id, 'Image Validated', sync_result, status_notes, url, token)
            if ok_write:
                print(f'     → Updated: Image Validated (catalogue-guided)')
                counts['updated'] += 1
            else:
                print(f'     ✗ Status write failed')
                counts['error'] += 1

        except Exception as e:
            print(f'     ✗ Error: {e}')
            counts['error'] += 1

        time.sleep(20)

    print()
    print('═' * 60)
    print('  Done.')
    if args.dry_run:
        print(f'  Would update  : {counts["would_update"]}')
    else:
        print(f'  Updated       : {counts["updated"]}')
    print(f'  No image found: {counts["no_image"]}')
    if counts['error']:
        print(f'  Errors        : {counts["error"]}')
    print('═' * 60)

if __name__ == '__main__':
    main()
