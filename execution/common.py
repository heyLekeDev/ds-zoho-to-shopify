"""
Shared helpers for pipeline scripts: Zoho/Shopify auth, item lookup,
and safety-enforced custom-field writes.

New scripts should import from here instead of re-implementing auth and
lookups — duplicated ad-hoc implementations are where field-name bugs
(e.g. reading the empty cf_brand instead of the native brand field) creep in.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(PROJECT_DIR, '.env'))

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = os.path.join(PROJECT_DIR, '.zoho_token.json')

SHOPIFY_SHOP_URL      = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID     = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION   = '2025-01'

# Fields scripts may write to Zoho. Anything else aborts.
# (From directives/enrichment_pipeline.md — Safety enforcement.)
ALLOWED_WRITE_FIELDS = {
    'cf_source_url', 'cf_shopify_status', 'cf_shopify_collection',
    'cf_shopify_var_1_name', 'cf_shopify_var_1_value',
    'cf_shopify_var_2_name', 'cf_shopify_var_2_value',
    'cf_shopify_var_3_name', 'cf_shopify_var_3_value',
    'cf_shopify_sync_notes', 'cf_sync_result',
    'cf_enriched_title', 'cf_shopify_tags',
    'cf_shopify_product_type', 'cf_description_html',
}

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
        }, timeout=10,
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token=None):
    return {'Authorization': f'Zoho-oauthtoken {token or get_zoho_token()}'}

# ── Zoho lookups ──────────────────────────────────────────────────────────────

def zoho_find_by_sku(sku, token=None):
    """Exact-SKU lookup via search_text. Returns list-endpoint item dict or None.
    Note: list results include native brand/manufacturer but NO custom fields."""
    resp = requests.get(
        f'{ZOHO_API_BASE}/items',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID, 'search_text': sku},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return zoho_find_by_sku(sku, token)
    items = resp.json().get('items', [])
    for it in items:
        if it.get('sku') == sku:
            return it
    return None

def zoho_item_detail(item_id, token=None):
    """Full item detail including custom fields (as a dict) and native fields."""
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return zoho_item_detail(item_id, token)
    item = resp.json().get('item', {})
    item['cf'] = {cf['api_name']: cf.get('value', '') for cf in item.get('custom_fields', [])}
    return item

def zoho_item_by_sku(sku, token=None):
    """SKU → full detail dict (with ['cf'] map) or None."""
    it = zoho_find_by_sku(sku, token)
    if not it:
        return None
    return zoho_item_detail(it['item_id'], token)

# ── Zoho writes (safety-enforced) ─────────────────────────────────────────────

def zoho_write_fields(item_id, fields: dict, token=None):
    """Write custom fields with allowlist enforcement. Returns (ok, message)."""
    for key in fields:
        if key not in ALLOWED_WRITE_FIELDS:
            raise ValueError(f'SAFETY ABORT: attempt to write prohibited field {key!r}')
    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID},
        json={'custom_fields': [{'api_name': k, 'value': v} for k, v in fields.items()]},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return zoho_write_fields(item_id, fields, token)
    data = resp.json()
    return data.get('code') == 0, data.get('message', '')

def zoho_delete_image(item_id, token=None):
    """Delete the item image. Returns (ok, message). ok is True when the
    image was deleted OR there was no image to delete."""
    resp = requests.delete(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    data = resp.json()
    if data.get('code') == 0:
        return True, 'deleted'
    if 'no image' in str(data.get('message', '')).lower():
        return True, 'no image to delete'
    return False, str(data.get('message', data))

def zoho_download_image(item_id, token=None):
    """Download the item's attached image. Returns bytes or None."""
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}/image',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=30,
    )
    if resp.status_code == 200 and resp.content and 'image' in resp.headers.get('Content-Type', ''):
        return resp.content
    return None

# ── Shopify ───────────────────────────────────────────────────────────────────

_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token:
        return _shopify_token
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token',
        json={'client_id': SHOPIFY_CLIENT_ID, 'client_secret': SHOPIFY_CLIENT_SECRET,
              'grant_type': 'client_credentials'},
        timeout=10,
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_token = data['access_token']
    return _shopify_token

def shopify_graphql(query, variables=None):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    headers = {'X-Shopify-Access-Token': get_shopify_token(),
               'Content-Type': 'application/json'}
    for _ in range(3):
        resp = requests.post(url, headers=headers,
                             json={'query': query, 'variables': variables or {}},
                             timeout=30)
        if resp.status_code == 429:
            time.sleep(10)
            continue
        return resp.json()
    raise Exception('Shopify GraphQL: too many retries')

def shopify_variant_by_sku(sku):
    """Look up a variant (and its product) by SKU. Returns dict or None."""
    q = '''
    query($q: String!) {
      productVariants(first: 5, query: $q) {
        edges { node {
          id sku price
          image { url }
          product {
            id title handle status descriptionHtml
            featuredMedia { preview { image { url } } }
            mediaCount { count }
          }
        } }
      }
    }'''
    data = shopify_graphql(q, {'q': f'sku:{sku}'})
    edges = (data.get('data', {}).get('productVariants', {}) or {}).get('edges', [])
    for e in edges:
        if e['node'].get('sku') == sku:
            return e['node']
    return None
