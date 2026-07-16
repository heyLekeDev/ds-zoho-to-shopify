#!/usr/bin/env python3
"""
Gallery images (multi-image products) — Stage 3d

Team pastes manufacturer/distributor image URLs into the item custom fields
`Image 2 URL` / `Image 3 URL` in Zoho. This script:

  --scan   Finds tagged items (cf filter), downloads + validates each URL,
           stages files under audit/<date>/gallery/ and writes a staging
           manifest. NOTHING is published — the images MUST be visually
           reviewed first (image_rules: audit gate applies to every image).
  --push   Publishes previously staged+reviewed images to the product's
           Shopify media gallery (product-level, alt-tagged so re-runs
           skip already-published images).
           --push --all       publish everything in the manifest
           --push --sku X     publish one SKU's staged images

Validation: ≥800px, aspect 0.8–1.2 (undersized square-ish images are padded
to 1080 via image_processing.upscale_to_canvas — never cropped, no bg removal).
Dedupe: alt marker "gallery:<md5-8>" on Shopify media.

Usage:
    python execution/gallery_images.py --scan
    (visually review audit/<date>/gallery/*.jpg)
    python execution/gallery_images.py --push --all
"""

import os
import sys
import io
import json
import time
import hashlib
import argparse
from datetime import date

import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_headers, shopify_graphql,
                    ZOHO_API_BASE, ZOHO_ORG_ID)

MANIFEST = os.path.join(PROJECT_DIR, '.gallery_staging.json')
GALLERY_FIELDS = ('cf_image_2_url', 'cf_image_3_url')
MIN_PX, ASPECT_MIN, ASPECT_MAX = 800, 0.8, 1.2


def find_tagged_items(token):
    """Items with a gallery URL set (cf_*_startswith filter — probed working)."""
    seen = {}
    for field in GALLERY_FIELDS:
        page = 1
        while True:
            r = requests.get(f'{ZOHO_API_BASE}/items', headers=zoho_headers(token),
                             params={'organization_id': ZOHO_ORG_ID, 'per_page': 200,
                                     'page': page, f'{field}_startswith': 'http'},
                             timeout=20)
            if r.status_code == 429:
                time.sleep(60)
                continue
            d = r.json()
            for it in d.get('items', []):
                seen[it['item_id']] = it
            if not d.get('page_context', {}).get('has_more_page'):
                break
            page += 1
    return list(seen.values())


def shopify_product_media(sku):
    """(product_id, [media alts]) for the product owning this SKU, or (None, [])."""
    q = '''query($q: String!) {
      productVariants(first: 5, query: $q) {
        edges { node { sku product { id
          media(first: 30) { nodes { ... on MediaImage { id alt } } } } } }
      }
    }'''
    d = shopify_graphql(q, {'q': f'sku:{sku}'})
    for e in (d.get('data', {}).get('productVariants', {}) or {}).get('edges', []):
        if e['node'].get('sku') == sku:
            prod = e['node']['product']
            alts = [m.get('alt', '') or '' for m in prod.get('media', {}).get('nodes', [])]
            return prod['id'], alts
    return None, []


def download_and_validate(url):
    """Returns (jpeg_bytes, w, h, md5) or (None, reason, 0, '')."""
    try:
        resp = requests.get(url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'})
        if resp.status_code != 200 or 'image' not in resp.headers.get('Content-Type', ''):
            return None, f'HTTP {resp.status_code} / not an image', 0, ''
        img = Image.open(io.BytesIO(resp.content)).convert('RGB')
    except Exception as e:
        return None, f'download/open failed: {e}', 0, ''
    w, h = img.size
    ratio = w / h if h else 0
    if not (ASPECT_MIN <= ratio <= ASPECT_MAX):
        return None, f'aspect {ratio:.2f} outside 0.8-1.2', 0, ''
    if w < MIN_PX or h < MIN_PX:
        try:
            from image_processing import upscale_to_canvas
            up = upscale_to_canvas(img, min_side=1080)  # takes PIL image, returns PNG bytes
            img = Image.open(io.BytesIO(up)).convert('RGB')
            w, h = img.size
        except Exception as e:
            return None, f'too small ({w}x{h}); upscale error: {e}', 0, ''
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=95)
    data = out.getvalue()
    return data, w, h, hashlib.md5(data).hexdigest()


def push_media(product_id, image_bytes, filename, alt):
    """Staged upload → fileCreate → productCreateMedia (product-level)."""
    staged = shopify_graphql("""
      mutation($input: [StagedUploadInput!]!) {
        stagedUploadsCreate(input: $input) {
          stagedTargets { url resourceUrl parameters { name value } }
          userErrors { message } } }""",
      {'input': [{'filename': filename, 'mimeType': 'image/jpeg',
                  'resource': 'IMAGE', 'fileSize': str(len(image_bytes)),
                  'httpMethod': 'POST'}]})
    targets = (staged.get('data', {}).get('stagedUploadsCreate', {}) or {}).get('stagedTargets', [])
    if not targets:
        return False, 'stagedUploadsCreate failed'
    t = targets[0]
    up = requests.post(t['url'], data={p['name']: p['value'] for p in t['parameters']},
                       files={'file': (filename, image_bytes, 'image/jpeg')})
    if not up.ok:
        return False, 'cloud upload failed'
    fc = shopify_graphql("""
      mutation($files: [FileCreateInput!]!) {
        fileCreate(files: $files) {
          files { id ... on MediaImage { image { url } } }
          userErrors { message } } }""",
      {'files': [{'originalSource': t['resourceUrl'], 'filename': filename,
                  'contentType': 'IMAGE', 'duplicateResolutionMode': 'REPLACE'}]})
    files = (fc.get('data', {}).get('fileCreate', {}) or {}).get('files', [])
    if not files:
        return False, f'fileCreate failed: {json.dumps(fc)[:120]}'
    src = (files[0].get('image') or {}).get('url') or t['resourceUrl']
    time.sleep(3)
    cm = shopify_graphql("""
      mutation($media: [CreateMediaInput!]!, $productId: ID!) {
        productCreateMedia(media: $media, productId: $productId) {
          media { id } mediaUserErrors { message } } }""",
      {'productId': product_id,
       'media': [{'originalSource': src, 'mediaContentType': 'IMAGE', 'alt': alt}]})
    errs = (cm.get('data', {}).get('productCreateMedia', {}) or {}).get('mediaUserErrors', [])
    if errs:
        return False, f'productCreateMedia: {json.dumps(errs)[:120]}'
    return True, 'ok'


def cmd_scan():
    token = get_zoho_token()
    items = find_tagged_items(token)
    print(f'  Items with gallery URLs: {len(items)}')
    gallery_dir = os.path.join(PROJECT_DIR, 'audit', date.today().isoformat(), 'gallery')
    os.makedirs(gallery_dir, exist_ok=True)

    staged, skipped = [], 0
    for it in items:
        det = requests.get(f'{ZOHO_API_BASE}/items/{it["item_id"]}',
                           headers=zoho_headers(token),
                           params={'organization_id': ZOHO_ORG_ID}, timeout=15
                           ).json().get('item', {})
        cf = {c['api_name']: c.get('value', '') for c in det.get('custom_fields', [])}
        sku = det.get('sku', '')
        product_id, alts = shopify_product_media(sku)
        if not product_id:
            print(f'  ⚠ {sku}: not on Shopify — gallery URLs ignored until published')
            continue
        for idx, field in enumerate(GALLERY_FIELDS, start=2):
            url = (cf.get(field) or '').strip()
            if not url:
                continue
            data, info, _, md5 = download_and_validate(url)
            if data is None:
                staged.append({'sku': sku, 'slot': idx, 'url': url,
                               'status': 'invalid', 'reason': info})
                print(f'  ✗ {sku} #{idx}: {info}')
                continue
            marker = f'gallery:{md5[:8]}'
            if any(marker in a for a in alts):
                skipped += 1
                continue  # already live
            fname = os.path.join(gallery_dir, f'{sku}-{idx}.jpg')
            with open(fname, 'wb') as f:
                f.write(data)
            staged.append({'sku': sku, 'slot': idx, 'url': url, 'file': fname,
                           'md5': md5, 'product_id': product_id, 'status': 'staged'})
            print(f'  ✓ {sku} #{idx}: staged for review → {os.path.basename(fname)}')
        time.sleep(0.3)

    with open(MANIFEST, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'entries': staged}, f, indent=1)
    n_staged = sum(1 for s in staged if s['status'] == 'staged')
    print(f'\n  {n_staged} image(s) staged, {skipped} already live, '
          f'{sum(1 for s in staged if s["status"] == "invalid")} invalid.')
    if n_staged:
        print(f'  → VISUALLY REVIEW every file in {gallery_dir}, then run --push')


def cmd_push(only_sku=None):
    try:
        with open(MANIFEST) as f:
            manifest = json.load(f)
    except Exception:
        print('  ✗ No staging manifest — run --scan first.')
        sys.exit(1)
    ok_n = fail_n = 0
    for e in manifest.get('entries', []):
        if e.get('status') != 'staged':
            continue
        if only_sku and e['sku'] != only_sku:
            continue
        with open(e['file'], 'rb') as f:
            data = f.read()
        ok, msg = push_media(e['product_id'], data,
                             f"{e['sku']}-gallery{e['slot']}-{e['md5'][:8]}.jpg",
                             alt=f"gallery:{e['md5'][:8]}")
        e['status'] = 'pushed' if ok else f'push failed: {msg}'
        ok_n += ok
        fail_n += (not ok)
        print(f'  {"✓" if ok else "✗"} {e["sku"]} #{e["slot"]}: {msg}')
        time.sleep(0.5)
    with open(MANIFEST, 'w') as f:
        json.dump(manifest, f, indent=1)
    print(f'\n  Pushed {ok_n}, failed {fail_n}.')


def main():
    p = argparse.ArgumentParser(description='Gallery images — scan/stage/push')
    p.add_argument('--scan', action='store_true')
    p.add_argument('--push', action='store_true')
    p.add_argument('--all', action='store_true')
    p.add_argument('--sku')
    a = p.parse_args()
    if a.scan:
        cmd_scan()
    elif a.push:
        if not (a.all or a.sku):
            p.error('--push needs --all or --sku')
        cmd_push(only_sku=a.sku)
    else:
        p.error('use --scan or --push')


if __name__ == '__main__':
    main()
