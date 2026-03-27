
import csv

def analyze_missing_info(csv_path):
    missing_items = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get('brand') and not row.get('manufacturer'):
                missing_items.append((row['sku'], row['Item Name']))
    
    # Print first 20 for research
    print(f"Total Missing Brand/Manufacturer: {len(missing_items)}")
    for i, (sku, name) in enumerate(missing_items[:20]):
        print(f"{sku}: {name}")

if __name__ == "__main__":
    analyze_missing_info("enriched-categories/280_Equipment_enriched.csv")
