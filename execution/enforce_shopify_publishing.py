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
    print("Fetching active Publication IDs...")
    pub_query = """
    query {
      publications(first: 10) {
        edges { node { id name } }
      }
    }
    """
    pub_res = shopify_graphql(pub_query)
    publications = [edge['node'] for edge in pub_res.get('data', {}).get('publications', {}).get('edges', [])]
    
    if not publications:
        print("No active publications found.")
        return
        
    print(f"Found {len(publications)} channels: {', '.join([p['name'] for p in publications])}")

    print("\nFetching all Products from Shopify to enforce publishing...")
    has_next = True
    cursor = None
    processed_count = 0
    updated_count = 0
    
    query = """
    query getProducts($cursor: String) {
      products(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node { id title }
        }
      }
    }
    """
    
    mut_query = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    
    # Needs a list of PublicationInput: { publicationId: ID }
    pub_inputs = [{"publicationId": p['id']} for p in publications]
    
    while has_next:
        res = shopify_graphql(query, {"cursor": cursor})
        data = res.get('data', {}).get('products', {})
        if not data:
             print("No products data returned or error", res)
             break

        edges = data.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            processed_count += 1
            p_id = node['id']
            title = node['title']
            
            mut_res = shopify_graphql(mut_query, {
                "id": p_id,
                "input": pub_inputs
            })
            
            errs = mut_res.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
            if errs:
                print(f"  [ERROR] {title}: {json.dumps(errs)}")
            else:
                updated_count += 1
                print(f"  ✓ {title} published to channels.")
                
            time.sleep(0.5) 
                
        page_info = data.get('pageInfo', {})
        has_next = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor')

    print(f"\nAudit Complete. Processed {processed_count} products.")
    print(f"Successfully Enforced Channel Publishing On: {updated_count} products.")

if __name__ == "__main__":
    main()
