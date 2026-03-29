#!/usr/bin/env python3
"""
Zoho-to-Shopify Master Sync (Overhaul v2)
- Targeted Fetch: Only processes pending items.
- Consolidation: Groups by Collection (Product Title).
- Conflict Audit: Ensures consistent variant options.
- GraphQL Powered: Uses mutations for all Shopify actions.
- State Handshake: Updates Zoho status and logs sync notes.
"""

import os
import csv
import time
import json
import requests
import argparse
import base64
import hashlib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import io
import re
from PIL import Image

# Load env vars
load_dotenv()

# --- Config ---
ZOHO_ORG_ID = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE = 'https://www.zohoapis.com/inventory/v1'
ZOHO_VIEW_ID = os.getenv('ZOHO_VIEW_ID')

CACHE_FILE = '.sync_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except: return {}
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def get_item_hash(item):
    """Generates a stable hash of the item data fields that affect the Shopify sync."""
    relevant_data = {
        'rate': item.get('rate'),
        'description': item.get('description'),
        'v1_val': item.get('v1_val'),
        'v2_val': item.get('v2_val'),
        'v3_val': item.get('v3_val'),
        'collection': item.get('collection'),
        'status': item.get('status')
    }
    dump = json.dumps(relevant_data, sort_keys=True)
    return hashlib.md5(dump.encode()).hexdigest()

def log_sync_action(sku, initial_status, action, final_status):
    """Writes a record to the Sync_Execution_Log.csv."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['SKU', 'Initial Status', 'Action Taken', 'Final Status', 'Timestamp'])
        writer.writerow([sku, initial_status, action, final_status, timestamp])
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE = 'https://www.zohoapis.com/inventory/v1'

SHOPIFY_SHOP_URL = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_ACCESS_TOKEN = os.getenv('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_API_VERSION = '2025-01'
SHOPIFY_URL = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

LOG_FILE = 'Sync_Execution_Log.csv'

# --- Zoho Auth ---
TOKEN_FILE = '.zoho_token.json'
_zoho_token = None

def get_zoho_token():
    global _zoho_token
    if _zoho_token: return _zoho_token
    
    # 1. Try disk cache
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                t_data = json.load(f)
                # Access tokens expire in 1 hour. Use 55 mins for safety.
                if time.time() - t_data.get('timestamp', 0) < 3300:
                    _zoho_token = t_data.get('access_token')
                    if _zoho_token:
                        return _zoho_token
        except:
            pass

    # 2. Fetch new token
    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    
    # Simple retry for throttling
    for attempt in range(3):
        resp = requests.post(url, params=params, timeout=10)
        data = resp.json()
        
        if 'access_token' in data:
            _zoho_token = data['access_token']
            # Save to disk
            try:
                with open(TOKEN_FILE, 'w') as f:
                    json.dump({'access_token': _zoho_token, 'timestamp': time.time()}, f)
            except:
                pass
            return _zoho_token
        
        if 'error' in data and 'Too many requests' in str(data.get('error_description', '')):
            wait_time = (attempt + 1) * 60
            print(f"\n  Zoho Auth Throttled. Waiting {wait_time}s (Attempt {attempt+1}/3)...")
            time.sleep(wait_time)
            continue
        break

    raise Exception(f"Zoho Auth Failed: {data}")

def zoho_headers():
    return {
        'Authorization': f'Zoho-oauthtoken {get_zoho_token()}',
        'X-com-zoho-inventory-organizationid': ZOHO_ORG_ID,
        'Content-Type': 'application/json'
    }

# --- Shopify API ---
_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token: return _shopify_token
    
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": os.getenv('SHOPIFY_CLIENT_ID'),
        "client_secret": os.getenv('SHOPIFY_CLIENT_SECRET'),
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    if 'access_token' in data:
        _shopify_token = data['access_token']
        return _shopify_token
    raise Exception(f"Shopify Auth Failed: {data}")

def shopify_graphql(query, variables=None):
    headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }
    resp = requests.post(SHOPIFY_URL, headers=headers, json={"query": query, "variables": variables}, timeout=20)
    data = resp.json()
    if 'errors' in data:
        print(f"GraphQL Error: {json.dumps(data['errors'], indent=2)}")
    return data

def get_active_publications():
    """Fetches all active Shopify sales channel IDs (e.g., Online Store, POS)."""
    query = "query { publications(first: 10) { edges { node { id name } } } }"
    res = shopify_graphql(query)
    edges = res.get('data', {}).get('publications', {}).get('edges', [])
    return [{"publicationId": e['node']['id']} for e in edges]

# --- Phase 1: Targeted Fetch (Zero-Detail) ---
def fetch_pending_items():
    """
    Phase 1: Mandatory Fetch Protocol (Targeted Discovery via Custom View).
    Bypasses individual detail calls. Reads directly from the predefined Custom View.
    Enforces required columns presence and caps at 200 records.
    """
    if not ZOHO_VIEW_ID:
        raise Exception("ZOHO_VIEW_ID is missing from environment variables.")
        
    print(f"Phase 1: Fetching pending items from Zoho Custom View ({ZOHO_VIEW_ID})...")
    
    url = f"{ZOHO_API_BASE}/items"
    pending = []
    
    params = {
        'customview_id': ZOHO_VIEW_ID,  # Use the specified Custom View
        'page': 1,
        'per_page': 200               # Fetch up to max cap per page
    }
    
    resp = requests.get(url, headers=zoho_headers(), params=params, timeout=15)
    
    if resp.status_code == 429:
        print("    Zoho Rate Limit (429). Sleeping 60s and retrying...")
        time.sleep(60)
        resp = requests.get(url, headers=zoho_headers(), params=params, timeout=15)
        
    if not resp.ok:
        raise Exception(f"Error fetching Custom View: {resp.status_code} {resp.text}")

    data = resp.json()
    items = data.get('items', [])
    
    if not items:
        print("\nPhase 1 Complete: Found total 0 items to sync.")
        return []

    print(f"    Found {len(items)} items in Custom View.")
    
    # Process up to the first 200 items (Phase 1 Cap)
    items_to_process = items[:200]
    
    if items_to_process:
        first_item = items_to_process[0]
        # Validate that custom fields are generally being returned in the View.
        # Since Zoho dynamically omits empty keys, we cannot strictly check for everything. 
        # However, 'cf_shopify_status' must exist since the view explicitly filters on it.
        if 'cf_shopify_status' not in first_item:
             print(f"  [ERROR] Missing 'cf_shopify_status' column in Custom View response.")
             raise Exception("Add Column to Zoho View. Required columns are not visible in the List View API response.")

    for item in items_to_process:
        sku = item.get('sku', 'Unknown SKU')
        status_str = item.get('cf_shopify_status', 'Unknown')
        
        flat_item = {
            'zoho_id': item['item_id'],
            'sku': sku,
            'name': item['name'],
            'rate': item['rate'],
            'description': item.get('cf_description_html', '') or item.get('description', ''),
            'brand': item.get('brand', ''),
            'enriched_title': item.get('cf_enriched_title', ''),
            'shopify_product_type': item.get('cf_shopify_product_type', ''),
            'shopify_tags': item.get('cf_shopify_tags', ''),
            'category': item.get('category_name', ''),
            'subcategory': item.get('sub_category_name', ''),
            'status': status_str,
            'collection': item.get('cf_shopify_collection', ''),
            'v1_name': item.get('cf_shopify_var_1_name', ''),
            'v1_val': item.get('cf_shopify_var_1_value', ''),
            'v2_name': item.get('cf_shopify_var_2_name', ''),
            'v2_val': item.get('cf_shopify_var_2_value', ''),
            'v3_name': item.get('cf_shopify_var_3_name', ''),
            'v3_val': item.get('cf_shopify_var_3_value', ''),
            'image_name': item.get('image_name', ''),
            'image_type': item.get('image_type', ''),
            'notes': item.get('cf_shopify_sync_notes', ''),
            'item_type': item.get('item_type', ''),
            'stock_on_hand': item.get('stock_on_hand', 0)
        }
        
        pending.append(flat_item)
        print(f"      ✓ {sku} [{status_str}] (Zero-Detail Fetch)")

    print(f"\nPhase 1 Complete: Found total {len(pending)} items to sync (max 200).")
    return pending

# --- Phase 2: Group & Audit ---
def group_and_audit(items):
    """
    Phase 2: Collection-Based Logic.
    Groups items by 'Shopify Collection' (Product Title) and validates schema.
    """
    print("\nPhase 2: Grouping and Schema Validation...")
    groups = {}
    for item in items:
        # User mandate: Group by 'Shopify Collection' field (Product Title)
        # If empty, treat as standalone using SKU as the group key
        key = item['collection'] if item['collection'] else f"STANDALONE-{item['sku']}"
        if key not in groups:
            groups[key] = []
        groups[key].append(item)
    
    audited_groups = {}
    
    # Track standalone titles to catch duplicates
    standalone_titles = {}
    
    for key, group in groups.items():
        if key.startswith("STANDALONE-"):
            item = group[0]
            
            # 1. Orphaned Variant Name Check
            if item.get('v1_name') or item.get('v2_name') or item.get('v3_name'):
                print(f"  [ORPHANED VARIANT ERROR] {item['sku']} is standalone but has variant names.")
                update_zoho_status(item['zoho_id'], "Error uploading", "[ERROR] Data Mismatch: Single products should not have variant names/values.", existing_note=item.get('notes', ''))
                continue
                
            # 2. Duplicate Single Title Check preparation
            title = item['name']
            if title not in standalone_titles:
                standalone_titles[title] = []
            standalone_titles[title].append(item)
            continue
            
        # Schema Validation: Verify all variants share identical option names
        first_item = group[0]
        v_names = (first_item['v1_name'], first_item['v2_name'], first_item['v3_name'])
        
        conflict = False
        for item in group[1:]:
            if (item['v1_name'], item['v2_name'], item['v3_name']) != v_names:
                conflict = True
                break
        
        if conflict:
            print(f"  [CONFLICT] Group '{key}' has inconsistent variant names.")
            # Mandate: Flag as 'Error uploading' and log conflict in notes
            error_msg = f"Conflict: Inconsistent option names in group '{key}'. Expected {v_names}."
            for item in group:
                update_zoho_status(item['zoho_id'], "Error uploading", error_msg, existing_note=item.get('notes', ''))
            continue
            
        # Description Validation: Group Primary MUST have a description
        if not first_item.get('description', '').strip():
            print(f"  [DESCRIPTION ERROR] Group '{key}' is missing a primary description.")
            error_msg = "[ERROR]: Missing Description. Items must have detailed content before syncing to Shopify."
            for item in group:
                update_zoho_status(item['zoho_id'], "Error uploading", error_msg, existing_note=item.get('notes', ''))
            continue
            
        valid_group = []
        for item in group:
            if item.get('item_type') != 'inventory':
                print(f"  [INVENTORY ERROR] {item['sku']} is not an inventory type.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item must be 'Inventory' type to track on Shopify.", existing_note=item.get('notes', ''))
            elif not item.get('image_name'):
                print(f"  [IMAGE ERROR] {item['sku']} is missing an image.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item is missing an image. Shopify products must have an image.", existing_note=item.get('notes', ''))
            elif float(item.get('rate') or 0) == 0:
                print(f"  [PRICE ERROR] {item['sku']} has a price of 0.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item has a price of 0. Shopify products must have a valid price.", existing_note=item.get('notes', ''))
            else:
                valid_group.append(item)
                
        if not valid_group:
            continue
            
        audited_groups[key] = valid_group
        
    # Process assembled standalone items for Duplicate Titles
    for title, items in standalone_titles.items():
        if len(items) > 1:
            print(f"  [DUPLICATE TITLE ERROR] Multiple standalone items share the title '{title}': {[i['sku'] for i in items]}")
            for item in items:
                update_zoho_status(item['zoho_id'], "Error uploading", "[ERROR] Duplicate Title: Please provide a 'Shopify Collection' name to group these as variants.", existing_note=item.get('notes', ''))
        else:
            item = items[0]
            key = f"STANDALONE-{item['sku']}"
            
            # Apply standard inventory/image/price checks to the valid standalone item
            if item.get('item_type') != 'inventory':
                print(f"  [INVENTORY ERROR] {item['sku']} is not an inventory type.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item must be 'Inventory' type to track on Shopify.", existing_note=item.get('notes', ''))
            elif not item.get('image_name'):
                print(f"  [IMAGE ERROR] {item['sku']} is missing an image.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item is missing an image. Shopify products must have an image.", existing_note=item.get('notes', ''))
            elif float(item.get('rate') or 0) == 0:
                print(f"  [PRICE ERROR] {item['sku']} has a price of 0.")
                update_zoho_status(item['zoho_id'], "Error uploading", "Error: Zoho item has a price of 0. Shopify products must have a valid price.", existing_note=item.get('notes', ''))
            elif not item.get('description', '').strip():
                print(f"  [DESCRIPTION ERROR] {item['sku']} is missing a description.")
                update_zoho_status(item['zoho_id'], "Error uploading", "[ERROR]: Missing Description. Items must have detailed content before syncing to Shopify.", existing_note=item.get('notes', ''))
            else:
                audited_groups[key] = [item]

    return audited_groups

# --- Phase 4: State Handshake ---
# --- Phase 4: State Handshake ---
def update_zoho_status(zoho_id, status, debug_note="", existing_note=""):
    """
    Updates item status in Zoho with a mandated audit note format.
    Mandate: [YYYY-MM-DD HH:MM] - [Success/Error Details]
    Strictly enforcing that ONLY cf_shopify_status and cf_shopify_sync_notes are modified.
    """
    url = f"{ZOHO_API_BASE}/items/{zoho_id}"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    if status == "Published":
        note = f"[{timestamp}] - [SUCCESS]: Sync Complete."
    else:
        new_err = f"[{timestamp}] - [{status}]: {debug_note}"
        note = f"{existing_note}\n\n{new_err}".strip() if existing_note else new_err
    
    custom_fields = [
        {'label': 'Shopify status', 'value': status},
        {'label': 'Shopify sync notes', 'value': note}
    ]
    
    payload = {'custom_fields': custom_fields}
    
    # --- Strict Payload Check (Immutable Source of Truth) ---
    allowed_keys = {'custom_fields'}
    if set(payload.keys()) != allowed_keys:
        print(f"  [SAFETY ABORT] Payload contains prohibited keys: {set(payload.keys()) - allowed_keys}")
        return False
        
    for cf in payload.get('custom_fields', []):
        if cf.get('label') not in ('Shopify status', 'Shopify sync notes'):
            print(f"  [SAFETY ABORT] Payload attempts to modify prohibited custom field: {cf.get('label')}")
            return False
    # --------------------------------------------------------
    
    # No retries here to avoid slowing down parallel threadpool
    resp = requests.put(url, headers=zoho_headers(), json=payload, timeout=10)
    return resp.ok

# --- Phase 3: Shopify Actions ---
# --- Phase 3 & 4: Actions & Handshake ---

# --- Phase 3 & 4: Actions & Handshake ---
def check_image_aspect_ratio(zoho_id, image_name):
    """
    Downloads original image from Zoho and verifies its aspect ratio.
    Returns (True, None) if safe (0.8 - 1.2), or (False, ErrorMsg) if invalid.
    """
    url = f"{ZOHO_API_BASE}/items/{zoho_id}/image"
    resp = requests.get(url, headers=zoho_headers(), timeout=15)
    if not resp.ok:
        return False, f"Image download failed: {resp.status_code}"
    
    try:
        img = Image.open(io.BytesIO(resp.content))
        width, height = img.size
        
        if width < 300 or height < 300:
            return False, f"Image Resolution Too Low (Size: {width}x{height}px). Minimum required is 300px for Shopify zoom functionality. Please re-sync a higher quality image."
            
        ratio = width / height
        
        if ratio < 0.8:
            return False, f"Aspect Ratio Mismatch (Ratio: {ratio:.2f}). Image is too Tall. Please crop to a 1:1 square in Zoho and re-sync."
        elif ratio > 1.2:
            return False, f"Aspect Ratio Mismatch (Ratio: {ratio:.2f}). Image is too Wide. Please crop to a 1:1 square in Zoho and re-sync."
        
        return True, None
    except Exception as e:
        return False, f"Failed to parse image data: {str(e)}"

def sync_group_to_shopify(group_name, items, dry_run=False, cache=None, active_publications=None):
    """
    Phase 3: Shopify Actions (Bulk Mutation Engine).
    Mandate: Exclusively use Bulk mutations for all updates/creates.
    Phase 4 Handshake: Parallel updates using ThreadPoolExecutor.
    """
    # Resolve the customer-facing title.
    # Collection items: use the collection name (already enriched via cf_shopify_collection).
    # Standalone items: use cf_enriched_title if set, otherwise fall back to raw Zoho name.
    if group_name.startswith("STANDALONE-"):
        shopify_title = items[0].get('enriched_title') or items[0]['name']
    else:
        shopify_title = group_name
    print(f"Group: {group_name} (Title: {shopify_title})")
    
    item_hashes = cache.get('hashes', {}) if cache else {}
    
    # 0. Anti-Gravity Mandate: SKU Collision Check
    # Verify none of the incoming SKUs belong to a DIFFERENT Product Title
    # This prevents SKUs from jumping between products and breaking order history
    check_skus = [i['sku'] for i in items]
    sku_query = " OR ".join([f"sku:{s}" for s in check_skus])
    
    col_resp = shopify_graphql("""
        query($q: String!) {
            productVariants(first: 50, query: $q) {
                edges { node { id sku inventoryItem { id } product { id title } media(first: 5) { edges { node { id alt } } } } }
            }
        }
    """, {"q": sku_query})

    collision_edges = col_resp.get('data', {}).get('productVariants', {}).get('edges', [])
    collision_info = {}  # sku → {variant_id, product_id, inv_item_id, existing_title, existing_media}
    for edge in collision_edges:
        node = edge['node']
        # If the SKU exists in Shopify under a DIFFERENT product title, it's a collision
        if node['product']['title'] != shopify_title:
            sku_val = node['sku']
            media_edges = node.get('media', {}).get('edges', [])
            existing_media = [{'id': m['node']['id'], 'alt': m['node'].get('alt')} for m in media_edges]
            print(f"  [SKU COLLISION] SKU {sku_val} is already linked to '{node['product']['title']}' on Shopify. Will update existing variant.")
            collision_info[sku_val] = {
                'variant_id':    node['id'],
                'product_id':    node['product']['id'],
                'existing_title': node['product']['title'],
                'inv_item_id':   (node.get('inventoryItem') or {}).get('id'),
                'existing_media': existing_media,
            }
            
    zoho_id_to_notes = {item['zoho_id']: item.get('notes', '') for item in items}

    # Separate colliding items from safe items.
    # Collision items are processed separately below — they update the existing Shopify
    # variant in-place and are marked Published with a collision note.
    safe_items = []
    collision_items = []
    for item in items:
        if item['sku'] in collision_info:
            collision_items.append(item)
        else:
            safe_items.append(item)

    if not safe_items and not collision_items:
        print(f"  [GROUP ABORT] All items in '{group_name}' failed validation.")
        return []

    items = safe_items  # Proceed with safe items; collision_items handled separately below

    # 0.5 Anti-Gravity Mandate: Aspect Ratio / Visual Integrity
    # If even ONE image in the group violates the 0.8 - 1.2 ratio, reject the ENTIRE group
    print(f"  [ASPECT RATIO CHECK] Validating {len(items)} images for group '{group_name}'...")
    ratio_error = None
    
    for item in items:
        # We know image_name exists due to Phase 9 check
        img_name = item.get('image_name')
        if img_name:
            is_valid, err_msg = check_image_aspect_ratio(item['zoho_id'], img_name)
            if not is_valid:
                ratio_error = err_msg
                print(f"  [ASPECT RATIO ABORT] {item['sku']} - {err_msg}")
                break # Fail fast on first bad image
                
    if ratio_error:
        print(f"  [GROUP ABORT] Group '{group_name}' failed visual integrity check (Aspect Ratio).")
        for item in items:
             zoho_updates.append((item['zoho_id'], "Error uploading", ratio_error, zoho_id_to_notes.get(item['zoho_id'], '')))
        return zoho_updates

    # 1. Search for existing Shopify Product by resolved title
    search_q = f'title:"{shopify_title}"'
    resp = shopify_graphql("""
        query($q: String!) {
            products(first: 1, query: $q) {
                edges { 
                    node { 
                        id 
                        variants(first: 100) { 
                            edges { 
                                node { 
                                    id 
                                    title
                                    sku 
                                    inventoryItem { id }
                                    media(first: 5) {
                                        edges { node { id alt } }
                                    }
                                } 
                            } 
                        } 
                    } 
                }
            }
        }
    """, {"q": search_q})
    
    product_node = None
    edges = resp.get('data', {}).get('products', {}).get('edges', [])
    if edges: product_node = edges[0]['node']
    
    shopify_skus = {}
    shopify_media = {}
    shopify_inv_items = {}
    if product_node:
        for v in product_node['variants']['edges']:
            node = v['node']
            if node['sku']: 
                shopify_skus[node['sku']] = node['id']
                if node.get('inventoryItem'):
                    shopify_inv_items[node['sku']] = node['inventoryItem']['id']
                media_edges = node.get('media', {}).get('edges', [])
                shopify_media[node['sku']] = [{'id': m['node']['id'], 'alt': m['node'].get('alt')} for m in media_edges]

    # 2. Categorize items into Bulk Operations
    to_update = [] # (z_id, v_input, sku, hash, shopify_media)
    to_create = [] # (z_id, v_input, sku, hash, shopify_media)
    to_delete = [] # (z_id, v_id, sku)
    zoho_updates = []
    
    for item in items:
        sku = item['sku']
        z_id = item['zoho_id']
        current_hash = get_item_hash(item)
        
        # Check if already synced
        if sku in item_hashes and item_hashes[sku] == current_hash and item['status'] == "Published":
            print(f"  Skip {sku}: No changes.")
            continue

        if item['status'] == "To be Archived":
            if sku in shopify_skus:
                print(f"  Action: Delete {sku}")
                to_delete.append((z_id, shopify_skus[sku], sku))
            else:
                zoho_updates.append((z_id, "Archived", "Item already removed from Shopify.", zoho_id_to_notes.get(z_id, '')))
            continue

        # Prepare variant input
        # Prepare variant input (Mandate: Nest SKU for 2024-01/04 compatibility)
        v_input = {
            "price": str(item['rate']),
            "inventoryItem": {"sku": sku, "tracked": True},
            "inventoryPolicy": "DENY",
            "inventoryQuantities": [
                {
                    "locationId": "gid://shopify/Location/96401326361",
                    "availableQuantity": int(float(item.get('stock_on_hand', 0)))
                }
            ]
        }
        
        # Map options for Bulk Creation compatibility
        opt_names = [item.get('v1_name'), item.get('v2_name'), item.get('v3_name')]
        opt_vals = [item.get('v1_val'), item.get('v2_val'), item.get('v3_val')]
        option_values = []
        for n, v in zip(opt_names, opt_vals):
            if n and v:
                option_values.append({"optionName": n, "name": v})

        existing_v_id = shopify_skus.get(sku)
        
        # If SKU not found but Product exists, AND it's a standalone item (no option values),
        # hijack the product's default variant instead of trying to add a second default variant.
        if not existing_v_id and product_node and not option_values:
            first_var = product_node['variants']['edges'][0]['node']
            existing_v_id = first_var['id']
            if first_var.get('inventoryItem'):
                shopify_inv_items[sku] = first_var['inventoryItem']['id']
            m_edges = first_var.get('media', {}).get('edges', [])
            shopify_media[sku] = [{'id': m['node']['id'], 'alt': m['node'].get('alt')} for m in m_edges]

        if existing_v_id:
            print(f"  Action: Bulk Update {sku}")
            v_input["id"] = existing_v_id
            
            # Phase 17: Shopify strict API constraint. You cannot include "inventoryQuantities" in a Bulk Update.
            # Pop it unconditionally for updates.
            v_input.pop("inventoryQuantities", None)
            
            to_update.append((z_id, v_input, sku, current_hash, shopify_media.get(sku, [])))
        elif product_node:
            print(f"  Action: Bulk Add {sku}")
            v_input["optionValues"] = option_values
            to_create.append((z_id, v_input, sku, current_hash, []))
        else:
            # Special case: Create new parent product shell if title doesn't exist
            # Mandate: Use bulk mutations for ALL variants. 
            print(f"  Action: Initial Product Shell Create for {sku}")
            if not dry_run:
                # Build productOptions with at least one value each (Mandatory in 2024-01)
                p_opts = []
                if item.get('v1_name'): p_opts.append({"name": item['v1_name'], "values": [{"name": item['v1_val']}]})
                if item.get('v2_name'): p_opts.append({"name": item['v2_name'], "values": [{"name": item['v2_val']}]})
                if item.get('v3_name'): p_opts.append({"name": item['v3_name'], "values": [{"name": item['v3_val']}]})

                # Build Product Metadata
                raw_tags = item.get('shopify_tags', '')
                if raw_tags:
                    tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
                else:
                    tags = [t.strip().title() for t in [item['category'], item['subcategory']] if t and t.strip()]
                raw_handle = shopify_title.lower().replace(' ', '-')
                seo_handle = re.sub(r'[^a-z0-9\-]', '', raw_handle)

                p_input = {
                    "title": shopify_title,
                    "vendor": item['brand'].strip() if item.get('brand') else "Generic",
                    "productType": item.get('shopify_product_type') or item['category'] or "Dental Supply",
                    "tags": tags,
                    "handle": seo_handle,
                    "descriptionHtml": item['description'],
                    "productOptions": p_opts
                }
                # Fix: Return variants edges so we can use the default variant ID
                res = shopify_graphql("""
                    mutation($in: ProductCreateInput!) { 
                      productCreate(product: $in) { 
                        product { 
                          id 
                          variants(first: 5) { edges { node { id title inventoryItem { id } } } }
                        } 
                        userErrors { message } 
                      } 
                    }""", {"in": p_input})
                
                prod_data = res.get('data', {}).get('productCreate', {}).get('product')
                if prod_data:
                    product_node = prod_data
                    p_id = product_node['id']
                    print(f"    ✓ Product shell created: {p_id}")
                    
                    # Ensure Product is published to all sales channels
                    if active_publications:
                        pub_res = shopify_graphql(
                            "mutation($id: ID!, $in: [PublicationInput!]!) { publishablePublish(id: $id, input: $in) { userErrors { message } } }",
                            {"id": p_id, "in": active_publications}
                        )
                        pub_errs = pub_res.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
                        if pub_errs:
                            print(f"    [WARNING] Failed to publish {p_id} to channels: {json.dumps(pub_errs)}")
                        else:
                            print(f"    ✓ Product {p_id} published to {len(active_publications)} active channels.")

                    # Handle Image for the initial shell if it exists
                    if item.get('image_name'):
                        sync_image_to_shopify(p_id, z_id, sku, item['image_name'])

                    # Capture default variant ID to avoid collision
                    default_v_id = None
                    vars_edges = product_node.get('variants', {}).get('edges', [])
                    if vars_edges:
                        default_v_id = vars_edges[0]['node']['id']

                    if default_v_id:
                        # Assign default Variant ID to this SKU and assign inventory item ID for later update
                        v_input["id"] = default_v_id
                        v_input.pop("inventoryQuantities", None)
                        
                        if vars_edges[0]['node'].get('inventoryItem'):
                             shopify_inv_items[sku] = vars_edges[0]['node']['inventoryItem']['id']
                             
                        v_input["optionValues"] = option_values
                        to_update.append((z_id, v_input, sku, current_hash, []))
                    else:
                        v_input["optionValues"] = option_values
                        to_create.append((z_id, v_input, sku, current_hash))
                else:
                    errors = res.get('data', {}).get('productCreate', {}).get('userErrors', [])
                    if res.get('errors'): errors += res['errors']
                    zoho_updates.append((z_id, "Error uploading", f"Product shell creation failed: {json.dumps(errors)}", zoho_id_to_notes.get(z_id, '')))
            continue

    # 3. Execute Mandatory Bulk Mutations
    if not dry_run and product_node:
        p_id = product_node['id']

        # A0. Push enrichment metadata to the existing product
        # productCreate already applies these fields for new products, but productVariantsBulkUpdate
        # only touches variant-level data. We must also call productUpdate for existing products
        # to keep tags, productType, descriptionHtml, and vendor in sync with enrichment output.
        rep_item = items[0] if items else None
        if rep_item:
            raw_tags = rep_item.get('shopify_tags', '')
            if raw_tags:
                meta_tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
            else:
                meta_tags = [t.strip().title() for t in [rep_item.get('category', ''), rep_item.get('subcategory', '')] if t and t.strip()]

            p_meta_input = {
                "id":              p_id,
                "title":           shopify_title,
                "descriptionHtml": rep_item.get('description', ''),
                "productType":     rep_item.get('shopify_product_type') or rep_item.get('category') or "Dental Supply",
                "vendor":          rep_item.get('brand', '').strip() or "Generic",
                "tags":            meta_tags,
            }
            meta_res = shopify_graphql(
                "mutation($in: ProductInput!) { productUpdate(input: $in) { product { id } userErrors { message } } }",
                {"in": p_meta_input}
            )
            meta_errs = meta_res.get('data', {}).get('productUpdate', {}).get('userErrors', [])
            if meta_res.get('errors'): meta_errs += meta_res['errors']
            if meta_errs:
                print(f"  [WARNING] productUpdate (metadata) failed: {json.dumps(meta_errs)}")
            else:
                print(f"  ✓ Product metadata updated (tags, type, description).")

        # A. Bulk Update
        if to_update:
            variants = [v[1] for v in to_update]
            res = shopify_graphql("mutation($pId: ID!, $vs: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $pId, variants: $vs) { userErrors { code message } } }", {"pId": p_id, "vs": variants})
            errs = res.get('data', {None:None}).get('productVariantsBulkUpdate', {}).get('userErrors', []) 
            errs = res.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors', [])
            if res.get('errors'): errs += res['errors'] # Capture top-level GraphQL errors
            if errs:
                print(f"  [ERROR] Bulk Update failed: {json.dumps(errs)}")
                for z_id, v_input, sku, c_hash, existing_media in to_update:
                    zoho_updates.append((z_id, "Error uploading", f"Bulk Update failed: {json.dumps(errs)}", zoho_id_to_notes.get(z_id, '')))
            else:
                for z_id, v_input, sku, c_hash, existing_media in to_update:
                    item_data = next((x for x in items if x['zoho_id'] == z_id), None)
                    success, img_hash, err_msg = True, None, None
                    if item_data and item_data.get('image_name'):
                        success, img_hash, err_msg = sync_image_to_shopify(p_id, z_id, sku, item_data['image_name'], variant_id=v_input.get('id'), existing_media=existing_media, cache_data=cache.get('hashes', {}) if cache else None)
                        
                    if success:
                        # Phase 17: Secondary execution to align inventory for updated variants
                        inv_id = shopify_inv_items.get(sku)
                        if item_data and inv_id:
                            qty = int(float(item_data.get('stock_on_hand', 0)))
                            inv_res = shopify_graphql("""
                                mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
                                  inventorySetOnHandQuantities(input: $input) {
                                    userErrors { message }
                                  }
                                }
                            """, {
                                "input": {
                                    "reason": "correction",
                                    "setQuantities": [{"inventoryItemId": inv_id, "locationId": "gid://shopify/Location/96401326361", "quantity": qty}]
                                }
                            })
                            inv_errs = inv_res.get('data', {}).get('inventorySetOnHandQuantities', {}).get('userErrors', [])
                            if inv_errs:
                                 print(f"      [WARNING] Inventory sync failed for {sku}: {json.dumps(inv_errs)}")
                                 success = False
                                 err_msg = f"Variant updated, but Inventory Sync failed: {json.dumps(inv_errs)}"
                                 
                        if success:
                            zoho_updates.append((z_id, "Published", "Successfully synced via Bulk Update.", zoho_id_to_notes.get(z_id, '')))
                            if cache: 
                                cache.setdefault('hashes', {})[sku] = c_hash
                                if img_hash: cache['hashes'][f"{sku}_img"] = img_hash
                        else:
                            zoho_updates.append((z_id, "Error uploading", err_msg, zoho_id_to_notes.get(z_id, '')))
                    else:
                        zoho_updates.append((z_id, "Error uploading", err_msg, zoho_id_to_notes.get(z_id, '')))
        
        # B. Bulk Create
        if to_create:
            v_inputs = [x[1] for x in to_create]
            payload = {"productId": p_id, "variants": v_inputs}
            res = shopify_graphql("""
                mutation($pid: ID!, $vars: [ProductVariantsBulkInput!]!) {
                  productVariantsBulkCreate(productId: $pid, variants: $vars) {
                    product { id }
                    productVariants { id sku }
                    userErrors { message }
                  }
                }
            """, {"pid": p_id, "vars": v_inputs})
            errs = res.get('data', {}).get('productVariantsBulkCreate', {}).get('userErrors', [])
            if errs:
                print(f"  [ERROR] Bulk Create failed: {json.dumps(errs)}")
                for z_id, v_input, sku, c_hash, existing_media in to_create:
                    zoho_updates.append((z_id, "Error uploading", f"Bulk Add failed: {json.dumps(errs)}", zoho_id_to_notes.get(z_id, '')))
            else:
                # Map sku back to created variant IDs to correctly attach images
                created_variants = {v.get('sku'): v.get('id') for v in res.get('data', {}).get('productVariantsBulkCreate', {}).get('productVariants', [])}
                
                for z_id, v_input, sku, c_hash, existing_media in to_create:
                    item_data = next((x for x in items if x['zoho_id'] == z_id), None)
                    created_id = created_variants.get(sku)
                    success, img_hash, err_msg = True, None, None
                    if item_data and item_data.get('image_name') and created_id:
                        success, img_hash, err_msg = sync_image_to_shopify(p_id, z_id, sku, item_data['image_name'], variant_id=created_id, existing_media=existing_media, cache_data=cache.get('hashes', {}) if cache else None)
                        
                    if success:
                        zoho_updates.append((z_id, "Published", "Successfully synced via Bulk Create.", zoho_id_to_notes.get(z_id, '')))
                        if cache: 
                            cache.setdefault('hashes', {})[sku] = c_hash
                            if img_hash: cache['hashes'][f"{sku}_img"] = img_hash
                    else:
                        zoho_updates.append((z_id, "Error uploading", err_msg, zoho_id_to_notes.get(z_id, '')))

        # C. Bulk Delete
        if to_delete:
            v_ids = [v[1] for v in to_delete]
            res = shopify_graphql("mutation($pId: ID!, $vIds: [ID!]!) { productVariantsBulkDelete(productId: $pId, variantIds: $vIds) { userErrors { code message } } }", {"pId": p_id, "vIds": v_ids})
            errs = res.get('data', {None:None}).get('productVariantsBulkDelete', {}).get('userErrors', [])
            if res.get('errors'): errs += res['errors']
            
            for z_id, _, sku in to_delete:
                if errs:
                    zoho_updates.append((z_id, "Error uploading", f"Bulk Delete failed: {json.dumps(errs)}", zoho_id_to_notes.get(z_id, '')))
                else:
                    zoho_updates.append((z_id, "Archived", "Successfully deleted variant via Bulk.", zoho_id_to_notes.get(z_id, '')))

    # --- Collision items: update existing Shopify variant on its original product ---
    # These SKUs already exist under a different product title on Shopify.
    # We update the variant in-place and mark Published with a collision note in Zoho.
    if collision_items:
        if dry_run:
            for item in collision_items:
                info = collision_info[item['sku']]
                print(f"  [DRY RUN - COLLISION UPDATE] Would update SKU {item['sku']} on existing Shopify product '{info['existing_title']}'")
        else:
            print(f"\n  [COLLISION UPDATE] Processing {len(collision_items)} collision item(s)...")
            # Group by their existing Shopify product_id for bulk mutations
            by_product = {}
            for item in collision_items:
                info = collision_info[item['sku']]
                by_product.setdefault(info['product_id'], []).append((item, info))

            for c_p_id, item_infos in by_product.items():
                c_variants = []
                for item, info in item_infos:
                    sku = item['sku']
                    v_input = {
                        "id": info['variant_id'],
                        "price": str(item['rate']),
                        "inventoryItem": {"sku": sku, "tracked": True},
                        "inventoryPolicy": "DENY",
                    }
                    c_variants.append((item, info, v_input))

                res = shopify_graphql(
                    "mutation($pId: ID!, $vs: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $pId, variants: $vs) { userErrors { code message } } }",
                    {"pId": c_p_id, "vs": [cv[2] for cv in c_variants]}
                )
                errs = res.get('data', {}).get('productVariantsBulkUpdate', {}).get('userErrors', [])
                if res.get('errors'): errs += res['errors']

                for item, info, v_input in c_variants:
                    sku  = item['sku']
                    z_id = item['zoho_id']
                    if errs:
                        zoho_updates.append((z_id, "Error uploading", f"Collision update failed: {json.dumps(errs)}", zoho_id_to_notes.get(z_id, '')))
                        continue

                    # Sync inventory for the collision variant
                    inv_id = info.get('inv_item_id')
                    if inv_id:
                        qty = int(float(item.get('stock_on_hand', 0)))
                        shopify_graphql("""
                            mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
                              inventorySetOnHandQuantities(input: $input) { userErrors { message } }
                            }
                        """, {"input": {"reason": "correction", "setQuantities": [{"inventoryItemId": inv_id, "locationId": "gid://shopify/Location/96401326361", "quantity": qty}]}})

                    # Sync image to the collision product — check return value
                    img_success = True
                    img_err = None
                    if item.get('image_name'):
                        img_success, _, img_err = sync_image_to_shopify(c_p_id, z_id, sku, item['image_name'], variant_id=info['variant_id'], existing_media=info.get('existing_media', []))

                    if not img_success:
                        zoho_updates.append((z_id, "Error uploading", f"Collision variant updated but image failed: {img_err}", zoho_id_to_notes.get(z_id, '')))
                        continue

                    collision_note = (
                        f"[COLLISION NOTE] SKU {sku} already existed on Shopify under "
                        f"'{info['existing_title']}'. Updated existing variant in-place. "
                        f"Zoho group target was '{group_name}'."
                    )
                    zoho_updates.append((z_id, "Published", collision_note, zoho_id_to_notes.get(z_id, '')))
                    if cache:
                        cache.setdefault('hashes', {})[sku] = get_item_hash(item)

    # Delegate Handshake execution to the chunk controller
    return zoho_updates


def sync_image_to_shopify(product_id, zoho_id, sku, image_name, variant_id=None, existing_media=None, cache_data=None):
    """
    Antigravity Protocol Phase 3: High-Fidelity & Quality (File API)
    Downloads the original high-resolution image from Zoho, hashes it for cache matching, 
    unlinks old corrupted media, and uploads to Shopify using GraphQL File API with polling delays.
    Returns: (success_bool, img_hash, error_msg)
    """
    existing_media = existing_media or []
    
    # 1. Download original uncompressed attachment from Zoho
    url = f"{ZOHO_API_BASE}/items/{zoho_id}/image"
    resp = requests.get(url, headers=zoho_headers(), timeout=15)
    if not resp.ok:
        print(f"      [IMAGE ERROR] Zoho download failed: {resp.status_code}")
        return False, None, f"Zoho original image download failed: {resp.status_code}"
    
    image_bytes = resp.content
    file_size = len(image_bytes)
    mime_type = "image/jpeg" if image_name.lower().endswith(('.jpg', '.jpeg')) else "image/png" if image_name.lower().endswith('.png') else "image/webp"
    
    # Generate MD5 hash of the original high-resolution file
    img_hash = hashlib.md5(image_bytes).hexdigest()
    
    # Compare against cache to skip redundant API waste
    if cache_data is not None and getattr(cache_data, "get", lambda x: None)(f"{sku}_img") == img_hash:
        print(f"      [IMAGE SKIP] MD5 Hash matches cache for {sku}. Skipping all Shopify File API calls.")
        return True, img_hash, None
                
    # If the variant has OLD media attached to it, DETACH/DELETE it to prevent "already has attached media" error.
    if existing_media and variant_id:
        old_media_ids = [m['id'] for m in existing_media]
        print(f"      [IMAGE CLEANUP] replacing legacy media for variant {variant_id} (Deleting {len(old_media_ids)} assets)...")
        # Ensure we delete the media attached to this product
        del_res = shopify_graphql("""
        mutation productDeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
          productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
            userErrors { message }
          }
        }
        """, {"productId": product_id, "mediaIds": old_media_ids})
        if del_res.get('data', {}).get('productDeleteMedia', {}).get('userErrors'):
            print(f"      [IMAGE WARNING] Failed to delete old media: {del_res}")
            
    print(f"      Syncing high-fidelity image {image_name} (MD5: {img_hash[:8]})...")
    
    # 2. Shopify File API (stagedUploadsCreate)
    staged_query = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters { name value }
        }
        userErrors { message }
      }
    }
    """
    staged_vars = {
        "input": [{
            "filename": image_name,
            "mimeType": mime_type,
            "resource": "IMAGE",
            "fileSize": str(file_size),
            "httpMethod": "POST"
        }]
    }
    
    staged_res = shopify_graphql(staged_query, staged_vars)
    targets = staged_res.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    
    if not targets:
        print(f"      [IMAGE ERROR] Failed to generate staged upload target: {staged_res}")
        return False, None, "Failed to generate staged upload target."
        
    target = targets[0]
    upload_url = target['url']
    resource_url = target['resourceUrl']
    
    # 3. HTTP POST to Staged Target
    multipart_data = {}
    for param in target['parameters']:
        multipart_data[param['name']] = param['value']
    
    files = {'file': (image_name, image_bytes, mime_type)}
    
    aws_resp = requests.post(upload_url, data=multipart_data, files=files)
    if not aws_resp.ok:
        print(f"      [IMAGE ERROR] Failed to push file to Shopify Cloud: {aws_resp.text}")
        return False, None, "Failed to push file to Shopify Cloud."
        
    # 4. Create File Record in Shopify (fileCreate)
    file_create_query = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files { 
          id 
          fileStatus 
          ... on MediaImage {
            image { url }
          }
        }
        userErrors { message }
      }
    }
    """
    file_create_vars = {
        "files": [{
            "originalSource": resource_url,
            "filename": image_name,
            "contentType": "IMAGE",
            "duplicateResolutionMode": "REPLACE"
        }]
    }
    
    fc_res = shopify_graphql(file_create_query, file_create_vars)
    file_create_data = fc_res.get('data', {}).get('fileCreate')
    
    if not file_create_data:
        print(f"      [IMAGE ERROR] fileCreate mutation empty or failed: {fc_res}")
        return False, None, "fileCreate mutation empty or failed."
        
    created_files = file_create_data.get('files', [])
    user_errors = file_create_data.get('userErrors', [])
    
    if user_errors:
        print(f"      [IMAGE ERROR] fileCreate user errors: {user_errors}")
    
    if not created_files:
        print(f"      [IMAGE ERROR] No files returned from fileCreate: {fc_res}")
        return False, None, "No files returned from fileCreate mutation."
        
    file_id = created_files[0]['id']
    
    file_url = resource_url
    if created_files[0] and isinstance(created_files[0].get('image'), dict):
        file_url = created_files[0]['image'].get('url', resource_url)
    
    # Wait briefly for Shopify to process the image asynchronously
    time.sleep(3)
    
    # 5. Link the File to the Product/Variant Media
    media_query = """
    mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
      productCreateMedia(media: $media, productId: $productId) {
        media { id }
        mediaUserErrors { code message }
      }
    }
    """
    media_vars = {
        "productId": product_id,
        "media": [{
            "originalSource": file_url,
            "mediaContentType": "IMAGE",
            "alt": image_name
        }]
    }
    
    media_res = shopify_graphql(media_query, media_vars)
    media_nodes = media_res.get('data', {}).get('productCreateMedia', {}).get('media', [])
    
    if not media_nodes:
        print(f"      [IMAGE WARNING] Failed to attach media to Product: {media_res}")
        return False, None, "Failed to attach media to Product."

    media_id = media_nodes[0]['id']
    print(f"      ✓ High-Fidelity Image attached to product (Media ID: {media_id})")

    # If variant_id is supplied, explicitly link this media to the variant
    if variant_id:
        v_media_query = """
        mutation productVariantAppendMedia($productId: ID!, $variantMedia: [ProductVariantAppendMediaInput!]!) {
          productVariantAppendMedia(productId: $productId, variantMedia: $variantMedia) {
            product { id }
            userErrors { field message }
          }
        }
        """
        v_media_vars = {
            "productId": product_id,
            "variantMedia": [{
                "variantId": variant_id,
                "mediaIds": [media_id]
            }]
        }
        
        # Shopify async processing requires media to be "READY" before linking to variant
        max_retries = 3
        for attempt in range(max_retries):
            # 4-second poll loop added before asserting link
            print(f"      [IMAGE DELAY] Waiting 4s for media readiness (Attempt {attempt+1}/{max_retries})...")
            time.sleep(4)
                
            v_res = shopify_graphql(v_media_query, v_media_vars)
            v_errs = v_res.get('data', {}).get('productVariantAppendMedia', {}).get('userErrors', [])
            
            if v_errs:
                if any('ready' in err.get('message', '').lower() for err in v_errs):
                    continue
                else:
                    print(f"      [IMAGE WARNING] Media not linked to variant explicitly: {v_res}")
                    return False, None, f"Variant Media link failed: {v_errs}"
            else:
                print(f"      ✓ Image linked directly to variant {variant_id}")
                return True, img_hash, None
                
        # If it falls through the loop, it timed out
        return False, None, "Image Processing Timeout"
            
    return True, img_hash, None

def sync_all(groups, dry_run=False, cache=None, active_publications=None):
    print(f"\nPhase 3 & 4: Processing Actions with Micro-Batching {'(DRY RUN)' if dry_run else ''}...")
    
    # Micro-Batching: Process 5 parent products at a time
    chunk_size = 5
    group_items = list(groups.items())
    total_chunks = (len(group_items) + chunk_size - 1) // chunk_size
    
    for chunk_idx in range(total_chunks):
        chunk = group_items[chunk_idx * chunk_size : (chunk_idx + 1) * chunk_size]
        print(f"\n--- Processing Micro-Batch {chunk_idx + 1}/{total_chunks} ({len(chunk)} parents) ---")
        
        batch_zoho_updates = []
        
        # 1. Execute Shopify actions for the chunk
        for key, items in chunk:
            print(f"\nGroup: {key}")
            updates = sync_group_to_shopify(key, items, dry_run, cache, active_publications=active_publications)
            if updates:
                batch_zoho_updates.extend(updates)
                
        # 2. Immediate Checkpoint Handshake (Commit chunk to Zoho)
        if batch_zoho_updates and not dry_run:
            print(f"\n  [Checkpoint] Committing {len(batch_zoho_updates)} status updates to Zoho for Micro-Batch {chunk_idx + 1}...")
            with ThreadPoolExecutor(max_workers=5) as executor:
                for z_id, status, note, existing_note in batch_zoho_updates:
                    executor.submit(update_zoho_status, z_id, status, note, existing_note)
            print("  [Checkpoint] Commit complete.")

def main():
    parser = argparse.ArgumentParser(description="Zoho to Shopify Overhaul Sync")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without sync")
    args = parser.parse_args()

    cache = load_cache()
    try:
        pending = fetch_pending_items()
        if not pending:
            print("No items pending sync. Exiting.")
            return

        groups = group_and_audit(pending)
        
        active_publications = get_active_publications() if not args.dry_run else None
        if active_publications:
            print(f"\nRetrieved {len(active_publications)} active sales channels for publishing.")
            
        sync_all(groups, dry_run=args.dry_run, cache=cache, active_publications=active_publications)

        print("\nSync Complete.")
    except Exception as e:
        import traceback
        print(f"\nFATAL ERROR: {e}")
        traceback.print_exc()
    finally:
        save_cache(cache)

if __name__ == "__main__":
    main()
