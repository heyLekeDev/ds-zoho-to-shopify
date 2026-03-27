import os
import requests
import json
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
    return resp.json().get('access_token')

def shopify_graphql(query, variables=None):
    url = f"https://{SHOPIFY_SHOP_URL}/admin/api/2024-01/graphql.json"
    headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=20)
    return resp.json()

def verify():
    # Target item updated: ZOOM PROTECTIVE EYEWEAR
    q = """
    { 
        products(first: 1, query: "title:\\\"ZOOM PROTECTIVE EYEWEAR\\\"") { 
            edges { 
                node { 
                    id 
                    title 
                    vendor 
                    productType 
                    bodyHtml 
                    images(first: 1) { edges { node { id } } } 
                    variants(first: 1) { 
                        edges { 
                            node { 
                                sku 
                                image { id } 
                            } 
                        } 
                    } 
                } 
            } 
        } 
    }
    """
    res = shopify_graphql(q)
    edge = res.get('data', {}).get('products', {}).get('edges', [])
    if edge:
        node = edge[0]['node']
        print(f"Product: {node['title']}")
        print(f"  Vendor: {node['vendor']}")
        print(f"  Type: {node['productType']}")
        print(f"  Body (HTML): {node['bodyHtml'][:100]}...")
        print(f"  Images Found: {len(node['images']['edges'])}")
        v_img = node['variants']['edges'][0]['node'].get('image')
        print(f"  Variant Image Linked: {'YES' if v_img else 'NO'}")
    else:
        print("Product not found.")

if __name__ == "__main__":
    verify()
