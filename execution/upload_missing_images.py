#!/usr/bin/env python3
"""
Upload Missing Images — batch-upload sourced images to Shopify products that have no image.

Usage:
    python execution/upload_missing_images.py
    python execution/upload_missing_images.py --dry-run   # show what would be done
"""

import os
import sys
import io
import json
import time
import base64
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

try:
    from PIL import Image
except ImportError:
    print('Pillow not installed. Run: pip install Pillow')
    sys.exit(1)

load_dotenv()

SHOPIFY_SHOP_URL      = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID     = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')

# ── Products with sourced image URLs ─────────────────────────────────────────
# Format: (partial title for search, image URL, slug for filename)

MISSING_IMAGE_PRODUCTS = [
    (
        'Lion King',
        'https://www.instacart.com/image-server/1200x1200/www.instacart.com/assets/domains/product-image/file/large_7fa7502f-21ec-47d0-a5a4-8ef554287e18.png',
        'oral-b-stages-lion-king',
    ),
    (
        'Angry Birds',
        'https://just4teeth.com/images/a/angry-birds-power-turbo-toothbrush.jpg',
        'dr-fresh-angry-birds-toothbrush',
    ),
    (
        'iO Ultimate Clean',
        'https://cdn11.bigcommerce.com/s-2idmiil7bp/images/stencil/1280x1280/products/892/13430/pdp_iO_replacement_brush_head_white_80338266_Ultimate_Clean_LOW_PRICE__90585__45322.1774539155.jpg?c=1',
        'oral-b-io-ultimate-clean-heads',
    ),
    (
        'Galaxy',
        'https://cdn11.bigcommerce.com/s-83f53/images/stencil/1280x1280/products/11476/60352/kids_6_galaxy_graphics_mtb_bc_80366100_upc_0-300410-10563-1_front_facing_1__88797.1710373553.jpg?c=2',
        'oral-b-stages-4-galaxy',
    ),
    (
        'Flossing Tape 50m',
        'https://www.ddgroup.com/globalassets/productimages/pco048/pco048_1.jpg',
        'oral-b-pro-expert-flossing-tape-50m',
    ),
    (
        'Stages 1',
        'https://cdn11.bigcommerce.com/s-xk4uicgdlw/images/stencil/1280x1280/products/25413/58283/oralb-prohealth-stages-1-disney-baby-winnie-d-148068__11908.1693369053.jpg?c=2',
        'oral-b-pro-health-stages-1-pooh',
    ),
    (
        'Stages 3',
        'https://crestoralbproshop.azureedge.net/media/catalog/product/cache/ea94e816ba63b633d1874b186e459cfb/8/0/80321928_1.jpg',
        'oral-b-pro-health-stages-3-spiderman',
    ),
    (
        'Baby Winnie',
        'https://cdn11.bigcommerce.com/s-2idmiil7bp/images/stencil/1280x1280/products/617/5396/00300416632148_C1N1__81361.1663618735.jpg?c=1',
        'oral-b-stages-1-baby-winnie',
    ),
    (
        'Junior For Me',
        'https://images.ctfassets.net/toytjhguj5jr/1tRPcXXV5aUDeLsMvfXCIe/8ecfae299f050dd6c993bee9b1385307/03014260099268_C1N1__1_.jpeg',
        'oral-b-stages-4-junior-for-me',
    ),
    (
        'Advanced Design',
        'https://reachtoothbrush.com/cdn/shop/files/JWU91-Reach_AdvancedDesignLargeTuftSoft6ctFrontPC_1.jpg?v=1711400058',
        'reach-advanced-design-toothbrush',
    ),
    (
        'Reach',
        'https://cdn11.bigcommerce.com/s-i5q5a5nhp2/images/stencil/760x760/products/31469/31556/barbie_183327__38688.1607960297.jpg?c=1',
        'reach-kids-barbie-toothbrush',
    ),
    (
        'Total Care',
        'https://newarkdentalpemco.com/cdn/shop/files/595-9223__74854.png?v=1770753056',
        'reach-total-care-toothbrush',
    ),
    (
        'Dental Study Model',
        'https://prod.tepe.com/contentassets/6548ed0d9c3c42d89472dc5c24866147/tepe_dental_model_idb_size_3_6406.jpg?preset=desktop-1-2',
        'tepe-dental-study-model',
    ),
]

# ── Shopify auth ──────────────────────────────────────────────────────────────

_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token:
        return _shopify_token
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token',
        json={
            'client_id': SHOPIFY_CLIENT_ID,
            'client_secret': SHOPIFY_CLIENT_SECRET,
            'grant_type': 'client_credentials',
        }, timeout=10,
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_token = data['access_token']
    return _shopify_token


def shopify_gql(query, variables=None):
    token = get_shopify_token()
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/api/2025-01/graphql.json',
        headers={
            'X-Shopify-Access-Token': token,
            'Content-Type': 'application/json',
        },
        json={'query': query, 'variables': variables or {}},
        timeout=30,
    )
    return resp.json()


# ── Image helpers ─────────────────────────────────────────────────────────────

def normalize_to_square(image_bytes, size=800):
    """Resize to size×size white-padded JPEG canvas."""
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new('RGB', (size, size), (255, 255, 255))
    offset = ((size - img.width) // 2, (size - img.height) // 2)
    canvas.paste(img, offset)
    buf = io.BytesIO()
    canvas.save(buf, format='JPEG', quality=92)
    return buf.getvalue()


def download_image(url):
    """Download image bytes from URL. Returns bytes or None."""
    try:
        resp = requests.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; DentalSolutionsPipeline/1.0)',
        })
        if resp.ok and resp.content:
            return resp.content
        print(f'    Download failed: HTTP {resp.status_code}')
    except Exception as e:
        print(f'    Download error: {e}')
    return None


# ── Shopify product search ────────────────────────────────────────────────────

def find_product_by_title(search_term):
    """Returns list of (id, title, has_image) for products matching search_term."""
    data = shopify_gql(
        """query($q: String!) {
            products(first: 5, query: $q) {
                edges {
                    node {
                        id title
                        featuredMedia { id }
                        media(first: 1) { edges { node { id status } } }
                    }
                }
            }
        }""",
        {'q': search_term},
    )
    results = []
    for edge in data.get('data', {}).get('products', {}).get('edges', []):
        prod = edge['node']
        has_image = bool(prod.get('featuredMedia'))
        results.append({
            'id': prod['id'],
            'title': prod['title'],
            'has_image': has_image,
        })
    return results


# ── Shopify image upload pipeline ─────────────────────────────────────────────

def upload_image_to_product(product_id, image_bytes, filename):
    """
    Upload image to Shopify product via staged upload.
    Returns (success, error_msg).
    """
    file_size = len(image_bytes)
    mime_type = 'image/jpeg'

    # 1. Staged upload
    staged = shopify_gql(
        """mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
            stagedUploadsCreate(input: $input) {
                stagedTargets { url resourceUrl parameters { name value } }
                userErrors { message }
            }
        }""",
        {'input': [{'filename': filename, 'mimeType': mime_type, 'resource': 'IMAGE',
                    'fileSize': str(file_size), 'httpMethod': 'POST'}]},
    )
    errs = staged.get('data', {}).get('stagedUploadsCreate', {}).get('userErrors', [])
    targets = staged.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    if errs or not targets:
        return False, f'Staged upload failed: {errs or staged}'

    target = targets[0]
    resource_url = target['resourceUrl']

    # 2. Upload to cloud storage
    params = {p['name']: p['value'] for p in target['parameters']}
    aws = requests.post(
        target['url'],
        data=params,
        files={'file': (filename, image_bytes, mime_type)},
        timeout=60,
    )
    if not aws.ok:
        return False, f'Cloud upload failed: HTTP {aws.status_code}'

    # 3. fileCreate (register in Shopify)
    fc = shopify_gql(
        """mutation fileCreate($files: [FileCreateInput!]!) {
            fileCreate(files: $files) {
                files { id fileStatus }
                userErrors { message }
            }
        }""",
        {'files': [{'originalSource': resource_url, 'filename': filename,
                    'contentType': 'IMAGE', 'duplicateResolutionMode': 'REPLACE'}]},
    )
    fc_errs = fc.get('data', {}).get('fileCreate', {}).get('userErrors', [])
    if fc_errs:
        return False, f'fileCreate errors: {fc_errs}'

    time.sleep(4)

    # 4. Attach to product
    attach = shopify_gql(
        """mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
            productCreateMedia(media: $media, productId: $productId) {
                media { id }
                mediaUserErrors { code message }
            }
        }""",
        {
            'productId': product_id,
            'media': [{'originalSource': resource_url, 'mediaContentType': 'IMAGE', 'alt': filename}],
        },
    )
    media_errs = attach.get('data', {}).get('productCreateMedia', {}).get('mediaUserErrors', [])
    media_nodes = attach.get('data', {}).get('productCreateMedia', {}).get('media', [])
    if media_errs:
        return False, f'productCreateMedia errors: {media_errs}'
    if not media_nodes:
        return False, f'No media returned from productCreateMedia'

    return True, None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Upload sourced images to missing-image Shopify products.')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without uploading')
    args = parser.parse_args()

    print('=' * 60)
    print('  Upload Missing Product Images')
    print('=' * 60)
    if args.dry_run:
        print('  [DRY RUN — no uploads will be made]')
    print()

    results = {'ok': [], 'skipped': [], 'failed': [], 'not_found': []}

    for search_term, image_url, slug in MISSING_IMAGE_PRODUCTS:
        print(f'── {search_term}')
        print(f'   Searching Shopify...')

        matches = find_product_by_title(search_term)

        if not matches:
            print(f'   NOT FOUND on Shopify. Skipping.\n')
            results['not_found'].append(search_term)
            continue

        # Pick the best match (first result without an image, or first result)
        target = next((m for m in matches if not m['has_image']), matches[0])

        print(f'   Found: "{target["title"]}"')

        if target['has_image']:
            print(f'   Already has image — skipping.\n')
            results['skipped'].append(target['title'])
            continue

        if args.dry_run:
            print(f'   [DRY RUN] Would upload: {image_url[:80]}...\n')
            continue

        # Download image
        print(f'   Downloading image...')
        raw = download_image(image_url)
        if not raw:
            print(f'   FAILED to download image.\n')
            results['failed'].append(target['title'])
            continue

        # Normalize
        print(f'   Normalizing to 800×800...')
        try:
            normalized = normalize_to_square(raw)
        except Exception as e:
            print(f'   Image normalize error: {e}\n')
            results['failed'].append(target['title'])
            continue

        print(f'   Normalized: {len(normalized):,} bytes')

        # Upload
        filename = f'{slug}.jpg'
        print(f'   Uploading to Shopify ({filename})...')
        ok, err = upload_image_to_product(target['id'], normalized, filename)

        if ok:
            print(f'   ✅ Uploaded successfully.\n')
            results['ok'].append(target['title'])
        else:
            print(f'   ❌ Upload failed: {err}\n')
            results['failed'].append(target['title'])

        time.sleep(2)

    # Summary
    print('=' * 60)
    print('  Summary')
    print('=' * 60)
    print(f'  ✅ Uploaded:   {len(results["ok"])}')
    print(f'  ⏭  Skipped:    {len(results["skipped"])} (already had image)')
    print(f'  ❌ Failed:     {len(results["failed"])}')
    print(f'  🔍 Not found:  {len(results["not_found"])}')

    if results['ok']:
        print('\n  Uploaded:')
        for t in results['ok']:
            print(f'    - {t}')
    if results['failed']:
        print('\n  Failed:')
        for t in results['failed']:
            print(f'    - {t}')
    if results['not_found']:
        print('\n  Not found on Shopify:')
        for t in results['not_found']:
            print(f'    - {t}')

    print()


if __name__ == '__main__':
    main()
