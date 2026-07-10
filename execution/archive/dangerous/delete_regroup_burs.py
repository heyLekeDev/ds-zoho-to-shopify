"""
delete_regroup_burs.py
Deletes incorrectly-published standalone and mixed-grouping diamond bur products
from Shopify, then resets their Zoho statuses so they can be re-published
correctly through the pipeline.

Products to delete:
  - gid://shopify/Product/10277547442457  Diatech Diamond Burs (wrong: 368+830+859 mixed)
  - gid://shopify/Product/10311358972185  Diatech FG Diamond Bur 835 — 1.0mm (standalone)
  - gid://shopify/Product/10311359004953  Diatech FG Diamond Bur 835 — 1.4mm (standalone)
  - gid://shopify/Product/10311359070489  Diatech FG Diamond Bur 859 — 1.0mm Fine (standalone)
  - gid://shopify/Product/10311363592473  Diatech FG Diamond Bur 850 — 1.6mm (standalone)
  - gid://shopify/Product/10311359889689  Edenta FG Diamond Bur 830 — 1.4mm Coarse (standalone)
  - gid://shopify/Product/10311360282905  Edenta FG Diamond Bur 835 — 1.4mm Coarse (standalone)
  - gid://shopify/Product/10311360839961  Hi-Di FG Diamond Bur 650 — Extra-Fine (standalone)
  - gid://shopify/Product/10311361397017  Hi-Di FG Diamond Bur 675 — Extra Coarse (standalone)
"""

import os, sys, requests, json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

SHOP_URL = os.environ['SHOPIFY_SHOP_URL']
API_VERSION = '2025-01'
GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"

_token = None

def get_token():
    global _token
    if _token: return _token
    r = requests.post(f"https://{SHOP_URL}/admin/oauth/access_token", json={
        'client_id': os.environ['SHOPIFY_CLIENT_ID'],
        'client_secret': os.environ['SHOPIFY_CLIENT_SECRET'],
        'grant_type': 'client_credentials'
    })
    _token = r.json()['access_token']
    return _token

def gql(query, variables=None):
    headers = {'X-Shopify-Access-Token': get_token(), 'Content-Type': 'application/json'}
    r = requests.post(GRAPHQL_URL, headers=headers, json={'query': query, 'variables': variables})
    return r.json()

# Products to delete: (shopify_product_id_int, description)
PRODUCTS_TO_DELETE = [
    (10277547442457, 'Diatech Diamond Burs (mixed grouping — 368/830/859 shapes)'),
    (10311358972185, 'Diatech FG Diamond Bur 835 — 1.0mm (standalone)'),
    (10311359004953, 'Diatech FG Diamond Bur 835 — 1.4mm (standalone)'),
    (10311359070489, 'Diatech FG Diamond Bur 859 — 1.0mm Fine (standalone)'),
    (10311363592473, 'Diatech FG Diamond Bur 850 — 1.6mm (standalone)'),
    (10311359889689, 'Edenta FG Diamond Bur 830 — 1.4mm Coarse (standalone)'),
    (10311360282905, 'Edenta FG Diamond Bur 835 — 1.4mm Coarse (standalone)'),
    (10311360839961, 'Hi-Di FG Diamond Bur 650 — Extra-Fine (standalone)'),
    (10311361397017, 'Hi-Di FG Diamond Bur 675 — Extra Coarse (standalone)'),
]

DELETE_MUTATION = """
mutation deleteProduct($id: ID!) {
  productDelete(input: {id: $id}) {
    deletedProductId
    userErrors { field message }
  }
}
"""

# Zoho items to reset (will be re-enriched and re-published)
# item_id → sku mapping for status reset
ZOHO_ITEMS_TO_RESET = [
    ('5583220000000947802', '160-130-008', 'DIATECH FG 368-314-020-5-XF'),
    ('5583220000000945437', '160-130-009', 'DIATECH FG 368-314-023-5-F'),
    ('5583220000000946856', '160-130-010', 'DIATECH FG 830-314-012-2.7-ML'),
    ('5583220000000945910', '160-130-011', 'DIATECH FG 835-314-010-4-ML'),
    ('5583220000000946383', '160-130-012', 'DIATECH FG 835-314-014-4-ML'),
    ('5583220000000948275', '160-130-013', 'DIATECH FG 850-314-016-10-ML'),
    ('5583220000000944964', '160-130-014', 'DIATECH FG 859-314-010-10-F'),
    ('5583220000000947329', '160-130-015', 'DIATECH FG 859-314-010-10UF'),
    ('5583220000000944018', '160-130-003', 'EDENTA F.G DIAMOND 830/014 CRS'),
    ('5583220000000944491', '160-130-004', 'EDENTA F.G DIAMOND 835/014 CRS'),
    ('5583220000000942597', '160-130-006', 'HI-DI DIAMOND BUR FG 650 XF'),
    ('5583220000000943072', '160-130-007', 'HI-DI DIAMOND BUR FG 675 EXTRA'),
]

def get_zoho_token():
    r = requests.post('https://accounts.zoho.com/oauth/v2/token', params={
        'refresh_token': os.environ['ZOHO_REFRESH_TOKEN'],
        'client_id': os.environ['ZOHO_CLIENT_ID'],
        'client_secret': os.environ['ZOHO_CLIENT_SECRET'],
        'grant_type': 'refresh_token'
    })
    token = r.json()['access_token']
    with open(os.path.join(os.path.dirname(__file__), '..', '.zoho_token.json'), 'w') as f:
        json.dump({'access_token': token}, f)
    return token

def reset_zoho_status(item_id, name, zoho_token):
    org_id = os.environ['ZOHO_ORGANIZATION_ID']
    r = requests.put(
        f'https://www.zohoapis.com/inventory/v1/items/{item_id}',
        headers={'Authorization': f'Zoho-oauthtoken {zoho_token}'},
        params={'organization_id': org_id},
        json={'custom_fields': [
            {'api_name': 'cf_shopify_status', 'value': ''},
            {'api_name': 'cf_sync_result', 'value': 'Deleted — awaiting re-group'},
            {'api_name': 'cf_shopify_sync_notes', 'value': 'Deleted standalone product. Will be re-published as grouped variant.'},
            {'api_name': 'cf_enriched_title', 'value': ''},
            {'api_name': 'cf_shopify_collection', 'value': ''},
            {'api_name': 'cf_shopify_var_1_name', 'value': ''},
            {'api_name': 'cf_shopify_var_1_value', 'value': ''},
            {'api_name': 'cf_shopify_var_2_name', 'value': ''},
            {'api_name': 'cf_shopify_var_2_value', 'value': ''},
        ]}
    )
    return r.json().get('code') == 0

def main():
    print('=' * 60)
    print('  Delete & Reset — Diamond Bur Regroup')
    print('=' * 60)

    # Step 1: Delete Shopify products
    print('\n  Step 1: Deleting Shopify products...\n')
    deleted = 0
    for prod_id_int, desc in PRODUCTS_TO_DELETE:
        gid = f'gid://shopify/Product/{prod_id_int}'
        result = gql(DELETE_MUTATION, {'id': gid})
        errors = result.get('data', {}).get('productDelete', {}).get('userErrors', [])
        if errors:
            print(f'  ✗ [{prod_id_int}] {desc}')
            for e in errors:
                print(f'      Error: {e["message"]}')
        else:
            deleted_id = result.get('data', {}).get('productDelete', {}).get('deletedProductId')
            if deleted_id:
                print(f'  ✓ Deleted [{prod_id_int}] {desc}')
                deleted += 1
            else:
                print(f'  ? [{prod_id_int}] {desc} — may already be deleted')

    # Step 2: Reset Zoho statuses
    print('\n  Step 2: Resetting Zoho custom fields...\n')
    zoho_token = get_zoho_token()
    reset = 0
    for item_id, sku, name in ZOHO_ITEMS_TO_RESET:
        ok = reset_zoho_status(item_id, name, zoho_token)
        if ok:
            print(f'  ✓ Reset {sku}  {name[:40]}')
            reset += 1
        else:
            print(f'  ✗ Failed to reset {sku}  {name[:40]}')

    print(f'\n  Done. Deleted {deleted}/{len(PRODUCTS_TO_DELETE)} Shopify products.')
    print(f'  Reset {reset}/{len(ZOHO_ITEMS_TO_RESET)} Zoho items.')
    print()
    print('  Next steps:')
    print('    1. cp enrichment_input_160130.json enrichment_input.json')
    print('    2. cp enrichment_output_160130.json enrichment_output.json')
    print('    3. python3 execution/enrich_items.py --write')
    print('    4. python3 execution/validate_images.py')
    print('    5. python3 execution/fetch_images.py  (if any Image required)')
    print('    6. python3 execution/validate_enrichment.py')
    print('    7. python3 execution/sync_zoho_to_shopify.py')

if __name__ == '__main__':
    main()
