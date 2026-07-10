"""
Reset items from 'Enrichment Complete' → 'Image required'
for 380/520/480 categories only.
Fetches item IDs directly from Zoho (handles paginated list).
"""
import json, os, sys, time, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
org_id = os.environ["ZOHO_ORGANIZATION_ID"]

with open(os.path.join(os.path.dirname(__file__), ".zoho_token.json")) as f:
    token = json.load(f)["access_token"]

headers = {"Authorization": f"Zoho-oauthtoken {token}"}
base = "https://www.zohoapis.com/inventory/v1"

# Fetch all items with status = Enrichment Complete, paginated
print("Fetching items with 'Enrichment Complete' status from Zoho...")
target_prefixes = ("380-", "520-", "480-")
item_ids = []
page = 1
while True:
    r = requests.get(
        f"{base}/items",
        headers=headers,
        params={
            "organization_id": org_id,
            "cf_shopify_status": "Enrichment Complete",
            "page": page,
            "per_page": 200,
        }
    )
    data = r.json()
    if data.get("code") != 0:
        print(f"ERROR fetching page {page}: {data}")
        sys.exit(1)
    batch = data.get("items", [])
    if not batch:
        break
    for item in batch:
        sku = item.get("sku", "") or ""
        if any(sku.startswith(p) for p in target_prefixes):
            item_ids.append(item["item_id"])
    print(f"  Page {page}: {len(batch)} items fetched, {len(item_ids)} matched so far")
    if not data.get("page_context", {}).get("has_more_page"):
        break
    page += 1
    time.sleep(0.5)

print(f"\nFound {len(item_ids)} items to reset to 'Image required'")

ok = fail = 0
for i, item_id in enumerate(item_ids):
    r = requests.put(
        f"{base}/items/{item_id}",
        headers=headers,
        params={"organization_id": org_id},
        json={"custom_fields": [{"api_name": "cf_shopify_status", "value": "Image required"}]}
    )
    if r.status_code == 200 and r.json().get("code") == 0:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL {item_id}: {r.status_code} {r.text[:120]}")
    time.sleep(1.1)
    if (i + 1) % 25 == 0:
        print(f"  {i+1}/{len(item_ids)} done...")

print(f"\nDone. OK={ok}, Fail={fail}")
