#!/usr/bin/env python3
import os
import requests
import json
import time
from dotenv import load_dotenv

load_dotenv()

ZOHO_ORG_ID = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')

def get_zoho_token():
    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'grant_type': 'refresh_token'
    }
    resp = requests.post(url, params=params, timeout=10)
    return resp.json()['access_token']

def reset_zoho_status(skus):
    token = get_zoho_token()
    headers = {
        'Authorization': f'Zoho-oauthtoken {token}',
        'X-com-zoho-inventory-organizationid': ZOHO_ORG_ID,
        'Content-Type': 'application/json'
    }
    
    for sku in skus:
        print(f"Searching for SKU {sku}...")
        r = requests.get('https://www.zohoapis.com/inventory/v1/items', headers=headers, params={'sku': sku})
        items = r.json().get('items', [])
        if not items:
            print(f"  SKU {sku} not found.")
            continue
            
        z_id = items[0]['item_id']
        url = f"https://www.zohoapis.com/inventory/v1/items/{z_id}"
        
        payload = {
            'custom_fields': [
                {'label': 'Shopify status', 'value': 'Update Required'}
            ]
        }
        
        upd = requests.put(url, headers=headers, json=payload, timeout=10)
        if upd.ok:
            print(f"  ✓ {sku} reset to 'Update Required'")
        else:
            print(f"  FAILED {sku}: {upd.status_code} {upd.content}")
        time.sleep(0.5)

if __name__ == "__main__":
    skus = [
        "160-140-003", "160-140-004", "160-140-005", "160-140-006",
        "180-110-001", "200-120-001", "200-120-002", "200-120-003",
        "200-120-004", "200-120-005", "200-190-001", "260-150-006",
        "260-150-008", "340-190-011", "340-190-020", "340-190-025",
        "420-110-002", "420-110-012", "480-150-049", "480-150-057"
    ]
    reset_zoho_status(skus)
