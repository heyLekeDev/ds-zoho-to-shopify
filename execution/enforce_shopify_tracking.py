import requests, os, json, time
from dotenv import load_dotenv

load_dotenv()

def get_shopify_token():
    url = f"https://{os.getenv('SHOPIFY_SHOP_URL')}/admin/oauth/access_token"
    payload = {
        "client_id": os.getenv('SHOPIFY_CLIENT_ID'),
        "client_secret": os.getenv('SHOPIFY_CLIENT_SECRET'),
        "grant_type": "client_credentials"
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    if 'access_token' in data:
        return data['access_token']
    raise Exception(f"Shopify Auth Failed: {data}")

def shopify_graphql(query, variables=None):
    headers = {
        "X-Shopify-Access-Token": get_shopify_token(),
        "Content-Type": "application/json"
    }
    url = f"https://{os.getenv('SHOPIFY_SHOP_URL')}/admin/api/2024-01/graphql.json"
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables})
    data = resp.json()
    if 'errors' in data:
        print("GraphQL Errors:", json.dumps(data['errors']))
    return data

def main():
    print("Fetching all variants from Shopify to enforce tracking...")
    has_next = True
    cursor = None
    processed_count = 0
    updated_count = 0
    already_tracked = 0
    
    query = """
    query getVariants($cursor: String) {
      productVariants(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            sku
            inventoryItem { id tracked }
          }
        }
      }
    }
    """
    
    mut_query = """
    mutation inventoryItemUpdate($id: ID!, $input: InventoryItemInput!) {
      inventoryItemUpdate(id: $id, input: $input) {
        inventoryItem { id tracked }
        userErrors { field message }
      }
    }
    """
    
    while has_next:
        time.sleep(0.5)
        res = shopify_graphql(query, {"cursor": cursor})
        data = res.get('data', {}).get('productVariants', {})
        if not data:
             print("No product variants data returned or error", res)
             break

        edges = data.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            processed_count += 1
            inv_item = node.get('inventoryItem', {})
            
            if not inv_item.get('tracked'):
                sku = node.get('sku', 'No SKU')
                print(f"Tracking disabled for {sku}. Enforcing tracking...")
                
                mut_res = shopify_graphql(mut_query, {
                    "id": inv_item['id'],
                    "input": { "tracked": True }
                })
                
                errs = mut_res.get('data', {}).get('inventoryItemUpdate', {}).get('userErrors', [])
                if errs:
                    print(f"  [ERROR] {sku}: {json.dumps(errs)}")
                else:
                    updated_count += 1
                    print(f"  ✓ {sku} tracking enabled.")
                    
                time.sleep(0.5) 
            else:
                already_tracked += 1
                
        page_info = data.get('pageInfo', {})
        has_next = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor')

    print(f"\nAudit Complete. Processed {processed_count} variants.")
    print(f"Already Tracked: {already_tracked}")
    print(f"Successfully Enforced Tracking On: {updated_count} variants.")

if __name__ == "__main__":
    main()
