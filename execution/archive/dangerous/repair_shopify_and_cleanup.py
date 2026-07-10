
import os
import csv
import json
import base64
import requests
import time
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

# Configuration
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_ORG_ID = os.getenv('ZOHO_ORG_ID')
SHOPIFY_SHOP_URL = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_API_VERSION = '2024-01'
SHOPIFY_ACCESS_TOKEN = os.getenv('SHOPIFY_ACCESS_TOKEN')

ZOHO_API_BASE = "https://www.zohoapis.com/inventory/v1"
SHOPIFY_API_BASE = f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}"

# --- Zoho Auth ---
_zoho_access_token = None

def get_zoho_token():
    global _zoho_access_token
    if _zoho_access_token: return _zoho_access_token
    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'redirect_uri': 'http://localhost:8080',
        'grant_type': 'refresh_token'
    }
    try:
        resp = requests.post(url, params=params, timeout=10)
        _zoho_access_token = resp.json().get('access_token')
        return _zoho_access_token
    except Exception as e:
        print(f"Zoho Auth Error: {e}")
        return None

def get_zoho_headers():
    return {
        'Authorization': f'Zoho-oauthtoken {get_zoho_token()}',
        'X-com-zoho-inventory-organizationid': ZOHO_ORG_ID,
        'Content-Type': 'application/json'
    }

# --- Shopify Auth ---
def get_shopify_token():
    # Always fetch fresh logic like debug script
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": os.getenv('SHOPIFY_CLIENT_ID'),
        "client_secret": os.getenv('SHOPIFY_CLIENT_SECRET'),
        "grant_type": "client_credentials"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('access_token')
        else:
            print(f"Auth Failed: {res.text}")
            return None
    except Exception as e:
        print(f"Auth Exception: {e}")
        return None

def get_shopify_headers():
    token = get_shopify_token()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

# --- Helpers ---

def get_shopify_product_by_sku(sku):
    """GraphQL to find product ID by variant SKU."""
    query = """
    {
      products(first: 5, query: "sku:\\"%s\\"") {
        edges {
          node {
            id
            legacyResourceId
            title
            images(first: 1) { edges { node { id } } }
            variants(first: 10) {
              edges {
                node {
                  id
                  sku
                  image { id }
                }
              }
            }
          }
        }
      }
    }
    """ % sku
    resp = requests.post(f"https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json", 
                         headers=get_shopify_headers(), json={'query': query})
    
    nodes = resp.json().get('data', {}).get('products', {}).get('edges', [])
    return nodes

def get_zoho_item_details(item_id):
    url = f"{ZOHO_API_BASE}/items/{item_id}"
    resp = requests.get(url, headers=get_zoho_headers())
    return resp.json().get('item')

def get_zoho_image_b64(item_id, image_name):
    # Fixed URL: relying on header for Org ID
    url = f"{ZOHO_API_BASE}/items/{item_id}/image"
    resp = requests.get(url, headers=get_zoho_headers())
    if resp.ok:
        return base64.b64encode(resp.content).decode('utf-8')
    return None

def upload_image_to_shopify_variant(product_id, variant_id, b64_data):
    """Uploads image to product and links to variant."""
    # 1. Upload to Product
    url_prod = f"{SHOPIFY_API_BASE}/products/{product_id}/images.json"
    payload = {
        "image": {
            "attachment": b64_data
        }
    }
    resp = requests.post(url_prod, headers=get_shopify_headers(), json=payload)
    if not resp.ok:
        print(f"  > Product Img Upload Failed: {resp.text}")
        return False
    
    image_id = resp.json()['image']['id']
    
    # 2. Link to Variant
    url_var = f"{SHOPIFY_API_BASE}/variants/{variant_id}.json"
    payload_var = {
        "variant": {
            "id": variant_id,
            "image_id": image_id
        }
    }
    requests.put(url_var, headers=get_shopify_headers(), json=payload_var)
    print(f"  > Image Attached to Variant {variant_id}")
    return True

def delete_shopify_product(product_id):
    url = f"{SHOPIFY_API_BASE}/products/{product_id}.json"
    requests.delete(url, headers=get_shopify_headers())

# Global Map for SKU Lookup (created during cleanup)
SKU_TO_ID_MAP = {}

def cleanup_and_build_map():
    print("Scanning for Duplicate Products & Building SKU Map...")
    global SKU_TO_ID_MAP
    
    # Fetch all products (REST API for reliability and immediate availability)
    url = f"{SHOPIFY_API_BASE}/products.json?limit=250&fields=id,title,created_at,variants,images"
    resp = requests.get(url, headers=get_shopify_headers())
    products = resp.json().get('products', [])
    
    # 1. Group by Title for Duplicate Cleanup
    by_title = defaultdict(list)
    for p in products:
        by_title[p['title']].append(p)
        
    duplicates_removed = 0
    
    # 2. Iterate and Cleanup
    final_products = []
    
    for title, prods in by_title.items():
        if len(prods) > 1:
            print(f"Duplicate Found: '{title}' ({len(prods)} copies)")
            # Sort: Most variants first, then newest (by ID desc) logic
            # Actually, sort by ID ASC (oldest first) to be stable? 
            # Or ID DESC (newest)?
            # Let's keep the one with MOST VARIANTS.
            prods.sort(key=lambda x: (len(x.get('variants', [])), len(x.get('images', []))), reverse=True)
            
            keeper = prods[0]
            to_delete = prods[1:]
            
            print(f"  Keeping: ID {keeper['id']} (Vars: {len(keeper['variants'])})")
            for d in to_delete:
                print(f"  Deleting: ID {d['id']} (Vars: {len(d['variants'])})")
                delete_shopify_product(d['id'])
                duplicates_removed += 1
                time.sleep(0.5)
            
            final_products.append(keeper)
        else:
            final_products.append(prods[0])

    print(f"Cleanup Complete. Removed {duplicates_removed} duplicates.")
    
    # 3. Build SKU Map from the Final List
    print("Building SKU Map from Remaining Products...")
    for p in final_products:
        p_id = p['id']
        for v in p['variants']:
            if v.get('sku'):
                # Map SKU to (ParentID, VariantID, ImageID_if_exists)
                sku = v['sku']
                has_image = True if v.get('image_id') else False
                SKU_TO_ID_MAP[sku] = {
                    'parent_id': p_id,
                    'variant_id': v['id'],
                    'has_image': has_image
                }
    print(f"Map Built. {len(SKU_TO_ID_MAP)} SKUs indexed.")

def repair_images_from_csv(csv_path):
    print(f"Starting Image Repair for items in: {csv_path}")
    
    # Read CSV
    target_items = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('CF.Shopify status') == 'Queue for Upload': 
                target_items.append(row)
                
    print(f"Found {len(target_items)} target items.")
    
    for item in target_items:
        sku = item['SKU']
        zoho_id = item['Item ID']
        
        # LOOKUP IN MAP
        match = SKU_TO_ID_MAP.get(sku)
        if not match:
            print(f"[{sku}] Not found in Shopify (via Map).")
            continue
            
        if match['has_image']:
            # Optional: Check if Parent has image too?
            # For now, if variant has image, we are good.
            print(f"[{sku}] Image already exists. Updating Status...")
            update_zoho_status(zoho_id, "Published")
            continue

        print(f"[{sku}] Fetching Image for Repair...")
        
        # 2. Get Zoho Image
        z_item = get_zoho_item_details(zoho_id)
        if not z_item or not z_item.get('image_name'):
            print("  > No image in Zoho to sync.")
            continue
            
        b64 = get_zoho_image_b64(zoho_id, z_item['image_name'])
        if b64:
            if upload_image_to_shopify_variant(match['parent_id'], match['variant_id'], b64):
                print("  > Success. Updating Zoho Status...")
                update_zoho_status(zoho_id, "Published")
            else:
                print("  > Upload Failed.")
        else:
            print("  > Download from Zoho Failed.")
            
        time.sleep(0.5)

def update_zoho_status(item_id, status):
    """Updates the CF.Shopify status in Zoho."""
    # Fixed URL: relying on header for Org ID
    url = f"{ZOHO_API_BASE}/items/{item_id}"
    
    # Payload: Update custom field
    payload = {
        "custom_fields": [
            {
                "label": "Shopify status",
                "value": status
            }
        ]
    }
    
    try:
        resp = requests.put(url, headers=get_zoho_headers(), json=payload)
        if resp.status_code == 200:
            print(f"  > Zoho Status Updated to '{status}'")
            return True
        else:
            print(f"  > Zoho Update Failed: {resp.text}")
            return False
    except Exception as e:
        print(f"  > Zoho Update Error: {e}")
        return False


if __name__ == "__main__":
    # 1. Cleanup and Build Index
    cleanup_and_build_map()
    
    # 2. Repair Images
    repair_images_from_csv("DS inventory Jan 31 26.csv")
