
import os
import csv
import json
import requests
import time
import re
from dotenv import load_dotenv

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

# SKU Mapping (Inlined for simplicity or load from file if needed)
# Naive regex for SKU Categories
def get_categories_from_sku(sku):
    """
    Returns (Category, SubCategory) based on SKU prefix.
    100-XXX -> Anaesthetics
    200-XXX -> Disposables
    320-XXX -> Implants
    """
    if not sku: return None, None
    parts = sku.split('-')
    if len(parts) < 2: return None, None
    
    code = parts[0]
    sub = parts[1]
    
    cat_map = {
        '100': "Anaesthetics",
        '100-130': "Cartridges",
        '100-140': "Needles",
        '100-150': "Syringes",
        '100-160': "Topic",
        
        '200': "Disposables",
        '200-190': "Bibs",
        '200-170': "Gloves",
        
        '260': "Endodontics",
        '260-110': "Files",
        '260-150': "Points",
        
        '320': "Implants - Bicon",
        '320-170': "Universal Abutments",
        '320-140': "Permanent Abutments",
        '320-160': "Temporary Abutments",
        '320-120': "Integra CP",
        
        '340': "Instruments",
        '340-180': "Surgical",
        '340-190': "Restorative",
        
        '420': "Orthodontics",
        '420-110': "Cases",
        
        '480': "Endodontics", # Wait, 480 used by CSV?
        '480-110': "Burs",
        '480-130': "Polishers",
        '480-150': "Diamonds"
    }
    
    # Try specific first
    full_prefix = f"{code}-{sub}"
    if full_prefix in cat_map:
        return cat_map.get(code, "Dental Supply"), cat_map[full_prefix]
        
    return cat_map.get(code, "Dental Supply"), "General"

# --- Authentication ---
_zoho_access_token = None

def get_zoho_token(force_refresh=False):
    global _zoho_access_token
    if _zoho_access_token and not force_refresh:
        return _zoho_access_token
    
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
        data = resp.json()
        if 'access_token' in data:
            _zoho_access_token = data['access_token']
            return _zoho_access_token
        else:
            print(f"Auth Loop Error: {data}")
            return None
    except:
        return None

def get_zoho_headers(force_refresh=False):
    token = get_zoho_token(force_refresh)
    return {
        'Authorization': f'Zoho-oauthtoken {token}',
        'X-com-zoho-inventory-organizationid': ZOHO_ORG_ID,
        'Content-Type': 'application/json'
    }

def get_shopify_token():
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": os.getenv('SHOPIFY_CLIENT_ID'),
        "client_secret": os.getenv('SHOPIFY_CLIENT_SECRET'),
        "grant_type": "client_credentials"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json().get('access_token')
    except:
        return None

def get_shopify_headers():
    token = get_shopify_token()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

# --- Workers ---

def get_zoho_item_details(item_id):
    url = f"{ZOHO_API_BASE}/items/{item_id}"
    try:
        resp = requests.get(url, headers=get_zoho_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json().get('item')
            
        if resp.status_code == 401:
            print("    > 401 Auth Expired. Refreshing Token...")
            # Retry with force refresh
            resp = requests.get(url, headers=get_zoho_headers(force_refresh=True), timeout=10)
            if resp.status_code == 200: return resp.json().get('item')
            
        if resp.status_code == 429:
            print("    > Rate Limit (429). Sleeping 2s...")
            time.sleep(2)
            # Retry once
            resp = requests.get(url, headers=get_zoho_headers(), timeout=10)
            if resp.status_code == 200: return resp.json().get('item')
            
        print(f"    > Fetch Failed. Status: {resp.status_code}, Resp: {resp.text[:100]}")
        return None
    except Exception as e:
        print(f"    > Fetch Exception: {e}")
        return None

# Accessing global map from repair script? No, let's rebuild or use GraphQL.
# Updating Metadata is fast, we can verify via SKU search efficiently if we have valid SKUs.
# Actually, let's use the REST List again to build a map. It's robust.

SKU_TO_PRODUCT_MAP = {}

def build_shopify_map():
    print("Building Shopify SKU Map...")
    url = f"{SHOPIFY_API_BASE}/products.json?limit=250&fields=id,title,vendor,product_type,tags,variants"
    resp = requests.get(url, headers=get_shopify_headers())
    products = resp.json().get('products', [])
    
    global SKU_TO_PRODUCT_MAP
    for p in products:
        for v in p['variants']:
            if v.get('sku'):
                SKU_TO_PRODUCT_MAP[v['sku']] = p
    print(f"Mapped {len(SKU_TO_PRODUCT_MAP)} SKUs.")

def update_shopify_product(product_id, payload):
    url = f"{SHOPIFY_API_BASE}/products/{product_id}.json"
    resp = requests.put(url, headers=get_shopify_headers(), json={"product": payload})
    if resp.ok:
        print(f"  > Updated: {payload}")
        return True
    else:
        print(f"  > Update Failed: {resp.text}")
        return False

def generate_tags(z_item, cat, sub):
    tags = set()
    tags.add(cat)
    tags.add(sub)
    if z_item.get('brand'): tags.add(z_item['brand'])
    if z_item.get('manufacturer'): tags.add(z_item['manufacturer'])
    
    # Attributes parsing (simple check)
    desc = z_item.get('description', '').lower()
    name = z_item.get('name', '').lower()
    
    if 'latex free' in desc or 'latex-free' in desc: tags.add('Latex Free')
    if 'sterile' in desc or 'sterile' in name: tags.add('Sterile')
    if 'powder free' in desc: tags.add('Powder Free')
    
    return list(tags)

def repair_metadata(csv_path):
    print(f"Starting Metadata Repair (Offline Mode) for: {csv_path}")
    
    target_items = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            target_items.append(row)
            
    print(f"Processing {len(target_items)} items from CSV...")
    
    for row in target_items:
        sku = row['SKU']
        
        shopify_prod = SKU_TO_PRODUCT_MAP.get(sku)
        if not shopify_prod:
            continue
            
        # Determine New Metadata from CSV Row
        brand = row.get('Brand')
        manufacturer = row.get('Manufacturer')
        cat_name = row.get('Category Name')
        
        new_vendor = brand or manufacturer or "Dental Solutions"
        new_type = cat_name or "Dental Supply"
        
        # Helper to mimic z_item dict for tag generation
        z_item_mock = {
            'brand': brand,
            'manufacturer': manufacturer,
            'description': row.get('Sales Description', ''),
            'name': row.get('Item Name', '')
        }
        
        cat, sub = get_categories_from_sku(sku)
        new_tags = generate_tags(z_item_mock, cat, sub)
        new_tags_str = ",".join(new_tags)
        
        # Optimization: Check if update is actually needed?
        # Only if Vendor/Type/Tags differ?
        # For now, just force update to be sure.
        
        payload = {
            "vendor": new_vendor,
            "product_type": new_type,
            "tags": new_tags_str
        }
        
        print(f"[{sku}] Updating Shopify Metadata...")
        update_shopify_product(shopify_prod['id'], payload)
        time.sleep(0.5) # limit Shopify write rate

if __name__ == "__main__":
    build_shopify_map()
    repair_metadata("DS inventory Jan 31 26.csv")
