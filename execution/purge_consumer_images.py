#!/usr/bin/env python3
"""
purge_consumer_images.py — Delete ALL images from every product in the
Consumer Oral Care batch so we can start fresh with correct images.
"""

import os, sys, time, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

SHOPIFY_SHOP_URL   = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID  = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SEC = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VER    = '2025-01'

_token = None

def get_token():
    global _token
    if _token: return _token
    r = requests.post(f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token', json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SEC,
        'grant_type': 'client_credentials'
    }, timeout=10)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f'Auth failed: {data}')
    _token = data['access_token']
    return _token

def headers():
    return {'X-Shopify-Access-Token': get_token(), 'Content-Type': 'application/json'}

def api_get(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.get(url, headers=headers(), timeout=20)
    if r.status_code == 429: time.sleep(10); return api_get(path)
    return r.json()

def api_delete(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.delete(url, headers=headers(), timeout=15)
    return r.status_code

BATCH_HANDLES = [
    "colgate-kids-battery-toothbrush",
    "oral-b-kids-character-toothbrush",
    "oral-b-crossaction-replacement-heads",
    "oral-b-io-ultimate-clean-replacement-heads",
    "oral-b-io-series-2-electric-toothbrush",
    "tepe-interdental-brush-single-pack",
    "tepe-interdental-brush-bulk-pack",
    "dr-fresh-angry-birds-turbo-power-battery-toothbrush",
    "dr-fresh-batman-turbo-power-battery-toothbrush",
    "colgate-microsinic-toothbrush",
    "colgate-triple-action-toothbrush-medium",
    "colgate-ultra-soft-toothbrush",
    "gum-crayola-kids-power-battery-toothbrush",
    "gum-crayola-pip-squeaks-kids-manual-toothbrush",
    "gum-barbie-kids-manual-toothbrush-with-suction-cup",
    "gum-barbie-kids-power-battery-toothbrush",
    "disney-lion-king-turbo-power-battery-toothbrush",
    "gum-crayola-kids-timer-light-toothbrush",
    "oral-b-precision-clean-replacement-toothbrush-heads-4-pack",
    "oral-b-sensi-ultrathin-replacement-toothbrush-heads-2-pack",
    "oral-b-super-floss-pre-cut-strands-50pk",
    "oral-b-kids-stages-2-manual-toothbrush-disney-frozen",
    "oral-b-kids-stages-2-manual-toothbrush-disney-pixar",
    "oral-b-stages-1-baby-manual-toothbrush-winnie-the-pooh",
    "oral-b-stages-4-junior-manual-toothbrush-for-me",
    "oral-b-precision-clean-toothbrush-heads-white-2-pack",
    "oral-b-pro-health-stages-3-kids-manual-toothbrush-spiderman",
    "reach-advance-design-manual-toothbrush-soft-compact",
    "reach-kids-barbie-manual-toothbrush",
    "reach-total-care-manual-toothbrush-soft",
    "tepe-interdental-brush-mixed-pack-multi-size-kit",
    "tepe-interdental-brush-original-orange-045mm",
    "tepe-mini-flosser-pre-loaded-holders-36pk",
    "tepe-professional-dental-study-model",
    "tepe-bio-based-triple-action-tongue-cleaner",
]

def main():
    print(f'\n🗑  Consumer Image Purge — {len(BATCH_HANDLES)} products')
    print(f'   Store: {SHOPIFY_SHOP_URL}\n')

    total_deleted = 0
    errors = []

    for i, handle in enumerate(BATCH_HANDLES, 1):
        data = api_get(f'products.json?handle={handle}&fields=id,title,images')
        products = data.get('products', [])
        if not products:
            print(f'[{i:02}/{len(BATCH_HANDLES)}] ✗ Not found: {handle}')
            errors.append(handle)
            continue

        p = products[0]
        pid = p['id']
        title = p.get('title', handle)
        images = p.get('images', [])

        if not images:
            print(f'[{i:02}/{len(BATCH_HANDLES)}] — No images: {title}')
            continue

        deleted = 0
        for img in images:
            sc = api_delete(f'products/{pid}/images/{img["id"]}.json')
            if sc in (200, 204):
                deleted += 1
            else:
                print(f'  ⚠ Delete failed (status {sc}) for image {img["id"]}')

        total_deleted += deleted
        print(f'[{i:02}/{len(BATCH_HANDLES)}] ✓ Deleted {deleted} image(s) — {title}')
        time.sleep(0.5)

    print(f'\n{"="*60}')
    print(f'Done. {total_deleted} images deleted across {len(BATCH_HANDLES)} products.')
    if errors:
        print(f'Not found ({len(errors)}): {", ".join(errors)}')

if __name__ == '__main__':
    main()
