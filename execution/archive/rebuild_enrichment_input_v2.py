"""
Rebuild enrichment_input.json for the 380/520/480 batch from CSV + enrichment_output.json.
Uses CSV for item_id/sku/name/rate, output.json for brand/category context.
"""
import csv, json, os

ROOT = os.path.dirname(os.path.abspath(__file__))

def _parse_rate(s):
    """Convert price string like 'NGN 1900.00' or '1900.00' to float."""
    import re
    m = re.search(r"[\d,]+\.?\d*", str(s).replace(",", ""))
    return float(m.group().replace(",", "")) if m else 0.0

# Load CSV
csv_path = os.path.join(ROOT, "DS inventory Jan 31 26.csv")
csv_by_sku = {}
with open(csv_path, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        sku = (row.get("SKU") or "").strip()
        if sku:
            csv_by_sku[sku] = row

# Load enrichment_output.json (source of truth for our 231 items)
output_path = os.path.join(ROOT, "enrichment_output.json")
with open(output_path) as f:
    output_items = json.load(f)
print(f"enrichment_output.json: {len(output_items)} items")

TARGET_PREFIXES = ("380-", "520-", "480-")

# Also need item_id for the nil-SKU item
NIL_SKU_ITEM_ID = "5583220000001500662"  # 520-190-004 (nil SKU in Zoho)

result = []
missing_ids = []

for out in output_items:
    sku = out.get("sku", "")

    # Look up in CSV by SKU
    # For "nil" SKU, find it by item ID directly
    if sku == "nil":
        item_id = NIL_SKU_ITEM_ID
        # Find in CSV by item ID
        csv_row = next(
            (row for row in csv_by_sku.values() if str(row.get("Item ID", "")).strip() == NIL_SKU_ITEM_ID),
            {}
        )
        name = csv_row.get("Item Name", "(PELLA) CROWN FORM REFILL #111")
        rate = _parse_rate(csv_row.get("Selling Price", "") or csv_row.get("Rate", "") or "0")
    else:
        csv_row = csv_by_sku.get(sku, {})
        item_id = str(csv_row.get("Item ID", "")).strip()
        name = csv_row.get("Item Name", "")
        rate = _parse_rate(csv_row.get("Selling Price", "") or csv_row.get("Rate", "") or "0")
        if not item_id:
            missing_ids.append(sku)

    # Derive brand/category from output tags + product type
    tags = out.get("shopify_tags", "")
    product_type = out.get("shopify_product_type", "")
    collection = out.get("shopify_collection", "")

    # Category from SKU prefix
    if sku.startswith("380-") or sku == "nil" and False:
        parent_cat = "Lab Products"
        category = "Lab"
    elif sku.startswith("520-"):
        parent_cat = "Restorative"
        category = "Restorative"
    elif sku.startswith("480-"):
        parent_cat = "Preventive"
        category = "Preventive"
    else:
        parent_cat = "Lab Products"
        category = "Lab"

    # Brand from tags (first tag is usually brand)
    brand = ""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    # Common brand names in tags
    brand_names = {"medesy","vita","deprag","pearson","biocryl","president","unodent","vivid",
                   "filpin","filpost","parapost","panavia","tepe","plak-smacker","oral-b","colgate",
                   "pella","getz","wizard","geistlich","ethoss","bicon","coltene","komet"}
    for tag in tag_list:
        if tag.lower() in brand_names:
            brand = tag.title()
            break

    result.append({
        "item_id": item_id,
        "sku": sku,
        "name": name or out.get("enriched_title", ""),
        "brand": brand,
        "category": category,
        "parent_category": parent_cat,
        "rate": rate,
    })

print(f"Built {len(result)} entries")
if missing_ids:
    print(f"WARNING: {len(missing_ids)} items missing item_id: {missing_ids[:5]}")

# Check nil-SKU handled
nil_items = [x for x in result if x["sku"] == "nil"]
print(f"nil-SKU items: {len(nil_items)} — item_id={nil_items[0]['item_id'] if nil_items else 'N/A'}")

out_path = os.path.join(ROOT, "enrichment_input.json")
with open(out_path, "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
print(f"Saved to {out_path}")
