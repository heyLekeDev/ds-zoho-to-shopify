import json, requests, time, os, sys
sys.path.insert(0, '/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho')
from dotenv import load_dotenv
from pathlib import Path

load_dotenv('/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho/.env')
org_id = os.environ['ZOHO_ORGANIZATION_ID']
client_id = os.environ['ZOHO_CLIENT_ID']
client_secret = os.environ['ZOHO_CLIENT_SECRET']
refresh_token = os.environ['ZOHO_REFRESH_TOKEN']

r = requests.post('https://accounts.zoho.com/oauth/v2/token', params={
    'refresh_token': refresh_token,
    'client_id': client_id,
    'client_secret': client_secret,
    'grant_type': 'refresh_token'
})
token = r.json()['access_token']
Path('/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho/.zoho_token.json').write_text(json.dumps({'access_token': token}))
print('Token refreshed')

headers = {'Authorization': f'Zoho-oauthtoken {token}'}
base = 'https://www.zohoapis.com/inventory/v1'
params = {'organization_id': org_id}

for sku in ['340-190-038', '340-190-121', '340-190-122']:
    r = requests.get(f'{base}/items', params={**params, 'search_text': sku}, headers=headers)
    items = r.json().get('items', [])
    item = next((i for i in items if i.get('sku') == sku), None)
    if not item:
        print(f'{sku}: NOT FOUND')
        continue
    item_id = item['item_id']
    r2 = requests.get(f'{base}/items/{item_id}', params=params, headers=headers)
    detail = r2.json().get('item', {})
    cf = {f['api_name']: f.get('value','') for f in detail.get('custom_fields', [])}
    status = cf.get('cf_shopify_status','')
    coll = cf.get('cf_shopify_collection','')
    v1n = cf.get('cf_shopify_var_1_name','')
    v1v = cf.get('cf_shopify_var_1_value','')
    print(f'{sku}: status={repr(status)} collection={repr(coll)} v1_name={repr(v1n)} v1_value={repr(v1v)}')
    time.sleep(0.5)
