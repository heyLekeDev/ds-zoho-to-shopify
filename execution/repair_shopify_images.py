#!/usr/bin/env python3
"""
Repair Shopify Images — Reusable diagnostic & repair tool.

Detects and fixes common Shopify image failures:
  - Ghost media (status: FAILED) — deletes broken records and re-uploads
  - Oversized images (>20MP) — auto-downscales before uploading
  - Missing featured images — re-attaches media to product and variant
  - Variant media not linked — re-links existing media to variant

Modes:
    --scan              Audit all Shopify products for image issues (no writes)
    --sku SKU           Repair a specific item by Zoho SKU
    --product-id GID    Repair a specific Shopify product by GID
    --title TITLE       Repair a Shopify product by title search

Examples:
    python execution/repair_shopify_images.py --scan
    python execution/repair_shopify_images.py --sku 380-110-027
    python execution/repair_shopify_images.py --title "Medesy Laboratory Instruments"
    python execution/repair_shopify_images.py --scan --fix
"""

import os
import sys
import io
import json
import math
import time
import hashlib
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv

try:
    from PIL import Image
except ImportError:
    print('Pillow not installed. Run: pip install Pillow')
    sys.exit(1)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

SHOPIFY_SHOP_URL     = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID    = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')

# Shopify limits: 20 megapixels max, 20MB file size
MAX_PIXELS = 20_000_000
MAX_FILE_BYTES = 20 * 1024 * 1024

# ── Auth ──────────────────────────────────────────────────────────────────────

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


def get_shopify_token():
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token',
        json={
            'client_id': SHOPIFY_CLIENT_ID,
            'client_secret': SHOPIFY_CLIENT_SECRET,
            'grant_type': 'client_credentials',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    return data['access_token']


_shopify_token = None

def shopify_gql(query, variables=None):
    global _shopify_token
    if not _shopify_token:
        _shopify_token = get_shopify_token()
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/api/2025-01/graphql.json',
        headers={
            'X-Shopify-Access-Token': _shopify_token,
            'Content-Type': 'application/json',
        },
        json={'query': query, 'variables': variables},
        timeout=20,
    )
    return resp.json()


# ── Diagnostics ───────────────────────────────────────────────────────────────

def diagnose_product(product_id):
    """
    Inspect a Shopify product for image issues.
    Returns a dict with:
      - product_id, title
      - has_featured: bool
      - media: list of {id, status, has_image, alt}
      - ghost_media: list of media IDs with FAILED status or null image
      - variants: list of {id, sku, title, has_media}
      - issues: list of human-readable issue strings
    """
    data = shopify_gql("""
        query($id: ID!) {
            product(id: $id) {
                id title
                featuredMedia { id }
                media(first: 50) {
                    edges {
                        node {
                            id alt mediaContentType status
                            ... on MediaImage { image { url width height } }
                        }
                    }
                }
                variants(first: 100) {
                    edges {
                        node {
                            id sku title
                            media(first: 5) { edges { node { id } } }
                        }
                    }
                }
            }
        }
    """, {'id': product_id})

    prod = data.get('data', {}).get('product')
    if not prod:
        return None

    result = {
        'product_id': prod['id'],
        'title': prod['title'],
        'has_featured': prod.get('featuredMedia') is not None,
        'media': [],
        'ghost_media': [],
        'variants': [],
        'issues': [],
    }

    for edge in prod.get('media', {}).get('edges', []):
        node = edge['node']
        status = node.get('status', 'UNKNOWN')
        has_image = node.get('image') is not None
        entry = {
            'id': node['id'],
            'status': status,
            'has_image': has_image,
            'alt': node.get('alt', ''),
        }
        if has_image:
            entry['width'] = node['image'].get('width')
            entry['height'] = node['image'].get('height')
        result['media'].append(entry)

        if status == 'FAILED' or (status != 'PROCESSING' and not has_image):
            result['ghost_media'].append(node['id'])

    for edge in prod.get('variants', {}).get('edges', []):
        vnode = edge['node']
        has_media = bool(vnode.get('media', {}).get('edges'))
        result['variants'].append({
            'id': vnode['id'],
            'sku': vnode.get('sku', ''),
            'title': vnode.get('title', ''),
            'has_media': has_media,
        })

    # Identify issues
    if result['ghost_media']:
        result['issues'].append(f'{len(result["ghost_media"])} ghost/failed media records')
    if not result['has_featured']:
        result['issues'].append('No featured image')
    for v in result['variants']:
        if not v['has_media']:
            result['issues'].append(f'Variant {v["sku"] or v["title"]} has no linked image')

    return result


def scan_all_products():
    """Scan all Shopify products for image issues."""
    print('Scanning all Shopify products for image issues...\n')
    issues_found = []
    cursor = None
    page = 0

    while True:
        page += 1
        query = """
            query($cursor: String) {
                products(first: 50, after: $cursor) {
                    edges {
                        cursor
                        node {
                            id title
                            featuredMedia { id }
                            media(first: 20) {
                                edges {
                                    node {
                                        id status
                                        ... on MediaImage { image { url } }
                                    }
                                }
                            }
                            variants(first: 20) {
                                edges {
                                    node {
                                        id sku
                                        media(first: 1) { edges { node { id } } }
                                    }
                                }
                            }
                        }
                    }
                    pageInfo { hasNextPage }
                }
            }
        """
        data = shopify_gql(query, {'cursor': cursor})
        edges = data.get('data', {}).get('products', {}).get('edges', [])

        if not edges:
            break

        for edge in edges:
            cursor = edge['cursor']
            prod = edge['node']
            problems = []

            # Check for ghost/failed media
            ghost_count = 0
            for m_edge in prod.get('media', {}).get('edges', []):
                mnode = m_edge['node']
                if mnode.get('status') == 'FAILED' or (mnode.get('status') != 'PROCESSING' and mnode.get('image') is None):
                    ghost_count += 1
            if ghost_count:
                problems.append(f'{ghost_count} ghost media')

            # Check for missing featured image
            if not prod.get('featuredMedia'):
                problems.append('no featured image')

            # Check for variants without media
            unlinked = []
            for v_edge in prod.get('variants', {}).get('edges', []):
                vnode = v_edge['node']
                if not vnode.get('media', {}).get('edges'):
                    unlinked.append(vnode.get('sku', 'unknown'))
            if unlinked:
                problems.append(f'{len(unlinked)} variant(s) without image')

            if problems:
                issues_found.append({
                    'product_id': prod['id'],
                    'title': prod['title'],
                    'problems': problems,
                    'unlinked_skus': unlinked,
                })
                flag = ', '.join(problems)
                print(f'  !! {prod["title"]}')
                print(f'     {flag}')

        has_more = data.get('data', {}).get('products', {}).get('pageInfo', {}).get('hasNextPage', False)
        if not has_more:
            break

    if not issues_found:
        print('  All products have valid images.')
    else:
        print(f'\n  Found {len(issues_found)} product(s) with image issues.')

    return issues_found


# ── Repair Actions ────────────────────────────────────────────────────────────

def delete_ghost_media(product_id, ghost_ids):
    """Delete failed/ghost media records from a Shopify product."""
    if not ghost_ids:
        return True
    print(f'    Deleting {len(ghost_ids)} ghost media records...')
    res = shopify_gql(
        """mutation productDeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
            productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
                userErrors { message }
            }
        }""",
        {'productId': product_id, 'mediaIds': ghost_ids}
    )
    errs = res.get('data', {}).get('productDeleteMedia', {}).get('userErrors', [])
    if errs:
        print(f'    Delete errors: {json.dumps(errs)}')
        return False
    print(f'    Ghost media deleted.')
    return True


def download_and_prepare_image(zoho_item_id, zoho_token):
    """
    Download image from Zoho, downscale if over 20MP.
    Returns (image_bytes, filename, width, height, was_downscaled) or None.
    """
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{zoho_item_id}/image',
        headers={'Authorization': f'Zoho-oauthtoken {zoho_token}'},
        timeout=20,
    )
    if not resp.ok or not resp.content:
        print(f'    Zoho image download failed: {resp.status_code}')
        return None

    image_bytes = resp.content
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
    except Exception as e:
        print(f'    Cannot parse image: {e}')
        return None

    was_downscaled = False
    if w * h > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (w * h))
        new_w, new_h = int(w * scale), int(h * scale)
        print(f'    Downscaling from {w}x{h} ({w*h/1_000_000:.1f}MP) to {new_w}x{new_h} ({new_w*new_h/1_000_000:.1f}MP)')
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format='PNG')
        image_bytes = buf.getvalue()
        w, h = new_w, new_h
        was_downscaled = True

    return image_bytes, w, h, was_downscaled


def upload_image_to_shopify(product_id, image_bytes, filename, variant_id=None):
    """
    Full Shopify image upload pipeline:
    1. Staged upload → 2. File create → 3. Attach to product → 4. Link to variant.
    Returns (success, media_id, error_msg).
    """
    file_size = len(image_bytes)
    mime_type = 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'

    # Step 1: Staged upload
    staged_res = shopify_gql(
        """mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
            stagedUploadsCreate(input: $input) {
                stagedTargets { url resourceUrl parameters { name value } }
                userErrors { message }
            }
        }""",
        {'input': [{'filename': filename, 'mimeType': mime_type, 'resource': 'IMAGE',
                     'fileSize': str(file_size), 'httpMethod': 'POST'}]}
    )
    targets = staged_res.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    if not targets:
        return False, None, f'Staged upload failed: {json.dumps(staged_res)}'

    target = targets[0]
    resource_url = target['resourceUrl']

    # Step 2: Upload to cloud
    multipart_data = {p['name']: p['value'] for p in target['parameters']}
    aws_resp = requests.post(target['url'], data=multipart_data,
                             files={'file': (filename, image_bytes, mime_type)})
    if not aws_resp.ok:
        return False, None, f'Cloud upload failed: {aws_resp.status_code}'

    # Step 3: Create file record
    fc_res = shopify_gql(
        """mutation fileCreate($files: [FileCreateInput!]!) {
            fileCreate(files: $files) {
                files { id fileStatus ... on MediaImage { image { url } } }
                userErrors { message }
            }
        }""",
        {'files': [{'originalSource': resource_url, 'filename': filename,
                     'contentType': 'IMAGE', 'duplicateResolutionMode': 'REPLACE'}]}
    )
    created = fc_res.get('data', {}).get('fileCreate', {}).get('files', [])
    if not created:
        return False, None, f'fileCreate empty: {json.dumps(fc_res)}'

    file_url = resource_url
    if isinstance(created[0].get('image'), dict):
        file_url = created[0]['image'].get('url', resource_url)

    print(f'    Waiting for Shopify to process image...')
    time.sleep(5)

    # Step 4: Attach media to product
    media_res = shopify_gql(
        """mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
            productCreateMedia(media: $media, productId: $productId) {
                media { id }
                mediaUserErrors { code message }
            }
        }""",
        {'productId': product_id,
         'media': [{'originalSource': file_url, 'mediaContentType': 'IMAGE', 'alt': filename}]}
    )
    media_nodes = media_res.get('data', {}).get('productCreateMedia', {}).get('media', [])
    media_errors = media_res.get('data', {}).get('productCreateMedia', {}).get('mediaUserErrors', [])
    if media_errors:
        return False, None, f'productCreateMedia errors: {json.dumps(media_errors)}'
    if not media_nodes:
        return False, None, f'No media returned: {json.dumps(media_res)}'

    media_id = media_nodes[0]['id']
    print(f'    Media attached: {media_id}')

    # Step 5: Link to variant (if specified)
    if variant_id:
        for attempt in range(4):
            wait = 4 + attempt * 2
            print(f'    Waiting {wait}s for media readiness (attempt {attempt+1}/4)...')
            time.sleep(wait)

            v_res = shopify_gql(
                """mutation productVariantAppendMedia($productId: ID!, $variantMedia: [ProductVariantAppendMediaInput!]!) {
                    productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
                        product { id }
                        userErrors { field message }
                    }
                }""",
                {'productId': product_id,
                 'variantMedia': [{'variantId': variant_id, 'mediaIds': [media_id]}]}
            )
            v_errs = v_res.get('data', {}).get('productVariantAppendMedia', {}).get('userErrors', [])
            if not v_errs:
                print(f'    Image linked to variant.')
                return True, media_id, None
            if any('ready' in e.get('message', '').lower() for e in v_errs):
                continue
            return False, media_id, f'Variant link failed: {json.dumps(v_errs)}'

        return False, media_id, 'Media readiness timeout — image attached to product but not linked to variant'

    return True, media_id, None


def upload_downscaled_to_zoho(zoho_item_id, image_bytes, filename, zoho_token):
    """Re-upload a downscaled image back to Zoho so future syncs use the correct size."""
    resp = requests.post(
        f'{ZOHO_API_BASE}/items/{zoho_item_id}/image',
        headers={'Authorization': f'Zoho-oauthtoken {zoho_token}'},
        params={'organization_id': ZOHO_ORG_ID},
        files={'image': (filename, image_bytes, 'image/png')},
        timeout=30,
    )
    ok = resp.json().get('code') == 0
    if ok:
        print(f'    Downscaled image uploaded to Zoho.')
    else:
        print(f'    Zoho upload failed: {resp.json()}')
    return ok


# ── Repair Orchestrators ─────────────────────────────────────────────────────

def repair_product(product_id, fix=False):
    """
    Diagnose and optionally repair a single Shopify product.
    """
    diag = diagnose_product(product_id)
    if not diag:
        print(f'  Product not found: {product_id}')
        return False

    print(f'\n  Product: {diag["title"]}')
    print(f'  ID: {diag["product_id"]}')
    print(f'  Featured image: {"yes" if diag["has_featured"] else "NO"}')
    print(f'  Media: {len(diag["media"])} total, {len(diag["ghost_media"])} ghost/failed')
    print(f'  Variants: {len(diag["variants"])}')

    if not diag['issues']:
        print(f'  Status: OK — no issues detected.')
        return True

    print(f'  Issues:')
    for issue in diag['issues']:
        print(f'    - {issue}')

    if not fix:
        print(f'\n  Run with --fix to repair.')
        return False

    # Repair steps
    zoho_token = get_zoho_token()

    # 1. Clean up ghost media
    if diag['ghost_media']:
        delete_ghost_media(product_id, diag['ghost_media'])

    # 2. Re-upload images for variants without media
    for variant in diag['variants']:
        if variant['has_media']:
            continue

        sku = variant['sku']
        if not sku:
            print(f'    Skipping variant {variant["title"]} — no SKU to look up in Zoho.')
            continue

        # Look up Zoho item by SKU
        resp = requests.get(
            f'{ZOHO_API_BASE}/items',
            headers={
                'Authorization': f'Zoho-oauthtoken {zoho_token}',
                'Content-Type': 'application/json',
            },
            params={'organization_id': ZOHO_ORG_ID, 'sku': sku},
            timeout=15,
        )
        items = resp.json().get('items', [])
        if not items:
            print(f'    SKU {sku} not found in Zoho — cannot source image.')
            continue

        zoho_item = items[0]
        zoho_item_id = zoho_item['item_id']

        if not zoho_item.get('image_name'):
            print(f'    SKU {sku} has no image in Zoho.')
            continue

        print(f'\n    Repairing variant {sku}...')
        img_result = download_and_prepare_image(zoho_item_id, zoho_token)
        if not img_result:
            continue

        image_bytes, w, h, was_downscaled = img_result
        filename = f'{sku.replace("/", "-")}_repaired.png'

        # If we downscaled, also update Zoho so future syncs don't hit the same issue
        if was_downscaled:
            upload_downscaled_to_zoho(zoho_item_id, image_bytes, filename, zoho_token)

        success, media_id, err = upload_image_to_shopify(
            product_id, image_bytes, filename, variant_id=variant['id']
        )
        if success:
            print(f'    Variant {sku} repaired.')
        else:
            print(f'    Variant {sku} repair failed: {err}')

    # Verify
    print(f'\n  Verifying repair...')
    diag_after = diagnose_product(product_id)
    if diag_after and not diag_after['issues']:
        print(f'  All issues resolved.')
        return True
    elif diag_after:
        print(f'  Remaining issues:')
        for issue in diag_after['issues']:
            print(f'    - {issue}')
        return False
    return False


def find_product_by_sku(sku):
    """Look up a Shopify product ID by variant SKU."""
    data = shopify_gql(
        """query($q: String!) {
            productVariants(first: 1, query: $q) {
                edges { node { product { id title } } }
            }
        }""",
        {'q': f'sku:{sku}'}
    )
    edges = data.get('data', {}).get('productVariants', {}).get('edges', [])
    if edges:
        return edges[0]['node']['product']['id']
    return None


def find_product_by_title(title):
    """Look up a Shopify product ID by title."""
    data = shopify_gql(
        """query($q: String!) {
            products(first: 1, query: $q) {
                edges { node { id } }
            }
        }""",
        {'q': f'title:"{title}"'}
    )
    edges = data.get('data', {}).get('products', {}).get('edges', [])
    if edges:
        return edges[0]['node']['id']
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Repair Shopify Images — detect and fix ghost media, oversized images, missing variant images.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--scan', action='store_true', help='Audit all products for image issues')
    mode.add_argument('--sku', type=str, help='Repair product containing this SKU')
    mode.add_argument('--product-id', type=str, help='Repair a specific Shopify product GID')
    mode.add_argument('--title', type=str, help='Repair product matching this title')

    parser.add_argument('--fix', action='store_true',
                        help='Apply repairs (without this flag, only diagnoses)')

    args = parser.parse_args()

    print('=' * 60)
    print('  Shopify Image Repair Tool')
    print('=' * 60)

    if args.scan:
        issues = scan_all_products()
        if issues and args.fix:
            print(f'\nRepairing {len(issues)} product(s)...\n')
            for item in issues:
                repair_product(item['product_id'], fix=True)
                time.sleep(0.5)

    elif args.sku:
        print(f'\n  Looking up SKU: {args.sku}')
        pid = find_product_by_sku(args.sku)
        if not pid:
            print(f'  SKU {args.sku} not found on Shopify.')
            sys.exit(1)
        repair_product(pid, fix=args.fix)

    elif args.product_id:
        repair_product(args.product_id, fix=args.fix)

    elif args.title:
        print(f'\n  Searching for: {args.title}')
        pid = find_product_by_title(args.title)
        if not pid:
            print(f'  Product "{args.title}" not found on Shopify.')
            sys.exit(1)
        repair_product(pid, fix=args.fix)

    print('\n' + '=' * 60)


if __name__ == '__main__':
    main()
