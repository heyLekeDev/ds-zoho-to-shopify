"""
Rebuild enrichment_input.json from Zoho items at 'Image required' status
for 380/520/480 SKU prefixes. Uses the same format as enrich_items.py --prepare.
"""
import csv, json, os, sys, time, requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(ROOT, ".env"))
org_id = os.environ["ZOHO_ORGANIZATION_ID"]

with open(os.path.join(ROOT, ".zoho_token.json")) as f:
    token_data = json.load(f)

# Refresh token
r = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
    "grant_type": "refresh_token",
    "client_id": os.environ["ZOHO_CLIENT_ID"],
    "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
    "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
}, timeout=15)
token_data = r.json()
if "access_token" not in token_data:
    print(f"Token refresh failed: {token_data}")
    sys.exit(1)
token = token_data["access_token"]
# Cache it
with open(os.path.join(ROOT, ".zoho_token.json"), "w") as f:
    import time as t
    token_data["fetched_at"] = t.time()
    json.dump(token_data, f)
print(f"Token OK: {token[:20]}...")

headers = {"Authorization": f"Zoho-oauthtoken {token}"}
base = "https://www.zohoapis.com/inventory/v1"

TARGET_PREFIXES = ("380-", "520-", "480-")

# Load CSV for brand/category context
csv_path = os.path.join(ROOT, "DS inventory Jan 31 26.csv")
csv_data = {}  # sku -> row
with open(csv_path, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        sku = (row.get("SKU") or "").strip()
        if sku:
            csv_data[sku] = row

print("Fetching all items from Zoho (paginated)...")
all_items = []
page = 1
while True:
    r = requests.get(f"{base}/items", headers=headers, params={
        "organization_id": org_id,
        "page": page,
        "per_page": 200,
        "status": "active",
    })
    data = r.json()
    if data.get("code") != 0:
        print(f"ERROR page {page}: {data}")
        sys.exit(1)
    batch = data.get("items", [])
    if not batch:
        break
    all_items.extend(batch)
    print(f"  Page {page}: {len(batch)} items (total {len(all_items)})")
    if not data.get("page_context", {}).get("has_more_page"):
        break
    page += 1
    time.sleep(0.3)

print(f"Total fetched: {len(all_items)}")

# Filter: 380/520/480 prefix AND status = Image required
target_items = []
for item in all_items:
    sku = (item.get("sku") or "").strip()
    # Check SKU prefix OR the item name implies our category (for nil-SKU items)
    is_target_sku = any(sku.startswith(p) for p in TARGET_PREFIXES)
    # For nil-SKU: check custom fields
    cf_status = ""
    for cf in item.get("custom_fields", []):
        if cf.get("api_name") == "cf_shopify_status":
            cf_status = cf.get("value", "")
            break

    if cf_status != "Image required":
        continue

    # Include SKU-prefixed items + nil-SKU items that we know are ours
    if is_target_sku or sku in ("nil", ""):
        # For nil-SKU: only include if it's item 520-190-004
        if sku in ("nil", "") and item["item_id"] != "5583220000001500662":
            continue
        target_items.append(item)

print(f"Matched {len(target_items)} items with 'Image required' in 380/520/480 range")

# Build enrichment_input.json format
# Fields: item_id, sku, name, brand, category, parent_category, rate
# Load enrichment_output.json for brand/category data
output_path = os.path.join(ROOT, "enrichment_output.json")
output_by_sku = {}
if os.path.exists(output_path):
    with open(output_path) as f:
        for it in json.load(f):
            s = it.get("sku", "")
            if s:
                output_by_sku[s] = it

result = []
for item in target_items:
    sku = (item.get("sku") or "").strip()
    item_id = item["item_id"]
    name = item.get("name", "")
    rate = float(item.get("rate", 0) or 0)

    # Get brand/category from CSV
    csv_row = csv_data.get(sku, {})
    category = (csv_row.get("Category") or csv_row.get("Product Type") or "").strip()
    brand = (csv_row.get("Brand") or csv_row.get("Manufacturer") or "").strip()

    # Fallback: derive category from SKU prefix
    if not category:
        if sku.startswith("380-"):
            category = "Lab Products"
        elif sku.startswith("520-"):
            category = "Restorative"
        elif sku.startswith("480-"):
            category = "Preventive"

    result.append({
        "item_id": item_id,
        "sku": sku or "nil",
        "name": name,
        "brand": brand,
        "category": category,
        "parent_category": category,
        "rate": rate,
    })

print(f"Built {len(result)} entries for enrichment_input.json")

out_path = os.path.join(ROOT, "enrichment_input.json")
with open(out_path, "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print(f"Saved to {out_path}")
