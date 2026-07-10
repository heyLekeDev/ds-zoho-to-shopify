import json, os, sys, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
org_id = os.environ["ZOHO_ORGANIZATION_ID"]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".zoho_token.json")) as f:
    token = json.load(f)["access_token"]

headers = {"Authorization": f"Zoho-oauthtoken {token}"}
base = "https://www.zohoapis.com/inventory/v1"
item_id = sys.argv[1]
status = sys.argv[2] if len(sys.argv) > 2 else "Image required"

r = requests.put(
    f"{base}/items/{item_id}",
    headers=headers,
    params={"organization_id": org_id},
    json={"custom_fields": [{"api_name": "cf_shopify_status", "value": status}]}
)
print(r.status_code, r.json().get("code"), r.json().get("message"))
