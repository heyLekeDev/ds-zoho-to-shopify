#!/usr/bin/env python3
import os
import requests
import json
import time
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_SHOP_URL = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')

def get_shopify_token():
    url = f"https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token"
    payload = {
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    if 'access_token' in data:
        return data['access_token']
    raise Exception(f"Shopify Auth Failed: {data}")

def shopify_graphql(query, variables=None):
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/2024-01/graphql.json"
    headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=20)
    return resp.json()

def cleanup_standalone():
    print("Searching for legacy products titled 'STANDALONE-...'")
    
    query = """
    query($q: String!, $cursor: String) {
      products(first: 50, query: $q, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            title
          }
        }
      }
    }
    """
    
    legacy_ids = []
    cursor = None
    
    while True:
        resp = shopify_graphql(query, {"q": "title:STANDALONE-*", "cursor": cursor})
        data = resp.get('data', {}).get('products', {})
        edges = data.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            if node['title'].startswith("STANDALONE-"):
                legacy_ids.append((node['id'], node['title']))
        
        if not data.get('pageInfo', {}).get('hasNextPage'):
            break
        cursor = data['pageInfo']['endCursor']

    if not legacy_ids:
        print("No legacy STANDALONE products found.")
        return

    print(f"Found {len(legacy_ids)} legacy products to delete:")
    for _, title in legacy_ids:
        print(f"  - {title}")
    
    confirm = input("\nProceed with deletion? (y/n): ")
    if confirm.lower() != 'y':
        print("Cleanup aborted.")
        return

    delete_mutation = """
    mutation($id: ID!) {
      productDelete(input: {id: $id}) {
        deletedProductId
        userErrors { field message }
      }
    }
    """
    
    for p_id, title in legacy_ids:
        print(f"Deleting {title} ({p_id})...")
        res = shopify_graphql(delete_mutation, {"id": p_id})
        errors = res.get('data', {}).get('productDelete', {}).get('userErrors', [])
        if errors:
            print(f"  ERROR: {errors}")
        else:
            print(f"  ✓ Deleted.")
        time.sleep(0.5)

    print("\nCleanup Complete.")

if __name__ == "__main__":
    cleanup_standalone()
