
import csv
import openpyxl
import re
import os

# --- Configuration ---
SKU_MAP_FILE = 'DS SKU map.xlsx'
ZOHO_FILE = 'Zoho items Jan 2026.csv'
OUTPUT_FILE = '.tmp/Zoho_items_updated.csv'
UNMAPPED_FILE = '.tmp/Zoho_items_unmapped.csv'

EXCLUDE_KEYWORDS = {
    'ACCESSORIES', 'MISCELLANEOUS', 'OTHER', 'OTHERS', '(DIRECT ITEM)', 
    'GENERAL', 'STANDARD', 'EQUIPMENT', 'DIRECT ITEM', 'PRODUCTS'
}

# Map Old SKU Prefix parts to New Map Categories
CATEGORY_ALIASES = {
    'BUR': 'Burs',
    'BURS': 'Burs',
    'PREVENTIVES': 'Preventive Products',
    'DRUGS': 'Anaesthetics', 
    'CASTELLINI': 'Equipment',
    'UNIFORM': 'Surgical Products', # Best fit for Gowns/Aprons matches
    'DSA SHOES': 'Surgical Products',
    'INFECTN CONTROL': 'Sterilisation',
    'IMPRESSION': 'Restorative', 
    'CROWN & BRIDGE': 'Restorative',
    'MATRIX SYSTEMS': 'Restorative',
    'ARTICULATIN PAPER': 'Restorative',
    'CEMENT & LINERS': 'Restorative',
    'COMPOSITE': 'Restorative',
    'COSMETIC ACCESORY': 'Cosmetics',
    'COSMETIC WHITENIN': 'Cosmetics',
    'DISP-BIBS': 'Disposable Products',
    'DISP-GLOVES': 'Disposable Products',
    'DISPOSABLES': 'Disposable Products',
    'EDUCATIONAL': 'Education',
    'ENDO-HYFLEX': 'Endodontics',
    'ENDO-K FILES': 'Endodontics',
    'ENDO-PROTAPER': 'Endodontics',
    'ENDO-GUTTA PERCHA': 'Endodontics',
    'ENDO-WAVEONE GOLD': 'Endodontics',
    'EQUIP BULBS': 'Equipment',
    'EQUIP CAPITAL': 'Equipment',
    'EQUIP SMALL': 'Equipment',
    'EQUIP SPARE PARTS': 'Equipment',
    'EVACUATION PROD': 'Disposable Products', 
    'FINISH&POLISH': 'Restorative',
    'HANDPIECE': 'Equipment',
    'INSTR COLOR CODE': 'Instruments',
    'INSTR DIAG': 'Instruments',
    'INSTR OPERATIVE': 'Instruments',
    'INSTR PERIO': 'Instruments',
    'INSTR SURGICAL': 'Instruments',
    'INSTR MISC': 'Instruments',
    'LAB': 'Lab Products',
    'LAB BUR': 'Lab Products', # "Lab Rotary Instruments"
    'MISCELANEOUS': 'Office Products', # Fallback
    'OFFICE PROD': 'Office Products',
    'PINS & POSTS': 'Restorative',
    'PRACTICE PROD': 'Office Products',
    'RESTORATIVE ACC': 'Restorative',
    'RESTORATIVE CORE': 'Restorative',
    'RUBBER DAM': 'Endodontics',
    'VITA': 'Lab Products',
    'X-RAY': 'X-ray',
    'ACRYLICS': 'Lab Products', # Best guess
    'ACCESSORIES': 'Burs', # If Bicon, handled by vendor override
    'UNIVERSAL ABUT': 'Implantology',
    'INTEGRA-CP': 'Implantology',
    'PERM ABUT': 'Implantology',
}

MANUAL_OVERRIDES = {
    'BICON': 'Implantology',
    'CASTELLINI': 'Equipment'
}

def load_sku_rules(filepath):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    sheet = wb.active
    
    keyword_rules = [] # List of (Keyword, Prefix)
    category_map = {} # {Category_Name_Upper: Prefix} (Direct Item)
    
    for row in sheet.iter_rows(min_row=2, values_only=True):
        # Category, Cat Code, Sub-Category, Sub Code, Prefix
        cat_raw = row[0]
        sub_raw = row[2]
        prefix = str(row[4]).strip() if row[4] else None
        
        if not prefix:
            continue
            
        category = str(cat_raw).strip().upper() if cat_raw else None
        sub_category = str(sub_raw).strip().upper() if sub_raw else None
        
        # 1. Broad Category Direct Map
        if category:
            # If sub_category is empty or (Direct Item), map category -> prefix
            if not sub_category or sub_category == '(DIRECT ITEM)':
                category_map[category] = prefix
                
        # 2. Keyword Rules from SubCategory
        if sub_category and sub_category not in EXCLUDE_KEYWORDS:
            keyword_rules.append((sub_category, prefix))
            # Singular/Plural
            if sub_category.endswith('S') and not sub_category.endswith('SS'):
                keyword_rules.append((sub_category[:-1], prefix))

    # Sort keywords by length (Longest match first)
    keyword_rules.sort(key=lambda x: len(x[0]), reverse=True)
    
    return keyword_rules, category_map

def get_next_sku(prefix_template, counter):
    seq_str = f"{counter:03d}"
    if 'xxx' in prefix_template.lower():
        return re.sub(r'xxx', seq_str, prefix_template, flags=re.IGNORECASE)
    else:
        return f"{prefix_template}-{seq_str}"

# --- Specific Refinements from Phase 2 Research ---
SPECIAL_RULES = [
    # (Keyword, Target Prefix, Reason)
    ('LIGMAJECT', '100-160-xxx', 'Specific Anaesthetic Syringe'),
    ('DURALAY', '380-160-xxx', 'Lab Pattern Resin'),
    ('SNAP LIQUID', '480-150-xxx', 'Temporary C&B Material'),
    ('SNAP POWDER', '480-150-xxx', 'Temporary C&B Material'),
    ('LATCH REAMER', '320-130-xxx', 'Bicon Implant Osteotome'), # Bicon specific
    ('ABUTMENT CUTTING', '320-110-xxx', 'Implant Accessory'),
    ('EXTENDED LATCH REAMER', '320-130-xxx', 'Bicon Implant Osteotome'),
    ('OSTEOTOME', '320-130-xxx', 'Implant Osteotome'), # Generic Osteotome
    
    # Anaesthetics
    ('LIGNOSPAN', '100-120-xxx', 'Local Anaesthetic'),
    ('SEPTOCAINE', '100-120-xxx', 'Local Anaesthetic'),
    ('LIDOCAINE', '100-120-xxx', 'Local Anaesthetic'),
    ('ARTICAINE', '100-120-xxx', 'Local Anaesthetic'),
    ('MEPIVACAINE', '100-120-xxx', 'Local Anaesthetic'),
    ('TOPICAL', '100-170-xxx', 'Topical Anaesthetic'),
    
    # Restorative
    ('ARTICULATING', '480-110-xxx', 'Articulating Materials'),
    ('HANEL', '480-110-xxx', 'Articulating Materials'),
    ('ARTICULATOR', '380-120-xxx', 'Lab Equipment'), # Articulators are Lab Equipment usually
    
    # Biomaterials
    ('BIO-OSS', '120-110-xxx', 'Bone substitutes'),
    ('SYNTHOGRAFT', '120-110-xxx', 'Bone substitutes'),
    ('ETHOSS', '120-110-xxx', 'Bone substitutes'),
    ('BIO-GIDE', '120-120-xxx', 'Resorbable Membranes'),
    ('MEMBRANE', '120-120-xxx', 'Resorbable Membranes'),
    
    # Burs
    ('1557', '140-120-xxx', 'Carbide Bur'),
    ('557', '140-120-xxx', 'Carbide Bur'),
    ('330', '140-120-xxx', 'Carbide Bur'),
    ('702', '140-120-xxx', 'Carbide Bur'), # Surgical/Carbide
    ('CARBIDE', '140-120-xxx', 'Carbide Bur'),
    ('DIAMOND', '140-130-xxx', 'Diamond Bur'),
    ('BUR BLOCK', '140-110-xxx', 'Bur Accessories'),
    ('BUR HOLDER', '140-110-xxx', 'Bur Accessories'),
    ('BUR CLEANER', '140-110-xxx', 'Bur Accessories'),
    ('BUR BRUSH', '140-110-xxx', 'Bur Accessories'),
    
    # Infection Control
    ('STERILE SURGERY', '180-170-xxx', 'Disposable Instruments'), # Bur-Surgical? Map likely puts surgical burs in Burs>Surgical
    
    # Final Pattern Matches
    ('TWO STRIPER', '140-170-xxx', 'Two-Striper Burs'), # Fix casing mismatch
    ('SSW', '140-120-xxx', 'Carbide Burs'), # Standard White Burs are Carbide
    ('TRI HAWK', '140-120-xxx', 'Carbide Burs'),
    ('BUR CLEANING', '140-110-xxx', 'Bur Accessories'),
    ('DISINFECTING BOX', '140-110-xxx', 'Bur Accessories'),
    ('CLIP CLAP', '140-110-xxx', 'Bur Accessories'),
    ('CASTELLINI', '260-140-xxx', 'Castellini Spare Parts'),
    ('CATTANI', '260-140-xxx', 'Equipment Spare Parts'),
    
    # Spare Parts (Equipment)
    ('BULB', '260-140-xxx', 'Equipment Misc'),
    ('LAMP', '260-140-xxx', 'Equipment Misc'),
    ('VALVE', '260-140-xxx', 'Equipment Misc'),
    ('HOSE', '260-140-xxx', 'Equipment Misc'),
    ('FILTER', '260-140-xxx', 'Equipment Misc'),
    ('O-RING', '260-140-xxx', 'Equipment Misc'),
    ('GASKET', '260-140-xxx', 'Equipment Misc'),
    ('PCB', '260-140-xxx', 'Equipment Misc'),
    ('BOARD', '260-140-xxx', 'Equipment Misc'),
    ('SWITCH', '260-140-xxx', 'Equipment Misc'),
    ('FUSE', '260-140-xxx', 'Equipment Misc'),
    
    # Endodontics Refinement
    ('GUTTA PERCHA', '240-110-xxx', 'Gutta-Percha'),
    ('GATES GLIDDEN', '240-170-xxx', 'Endo Rotary'),
    ('PROTAPER', '240-170-xxx', 'Endo Rotary'),
    ('WAVEONE', '240-170-xxx', 'Endo Rotary'),
    ('HYFLEX', '240-170-xxx', 'Endo Rotary'),
    ('HYFL', '240-170-xxx', 'Endo Rotary'), # Abbreviation match
    ('X - FILE', '240-170-xxx', 'Endo Rotary'), # Normalized space
    ('X-FILE', '240-170-xxx', 'Endo Rotary'),
    ('PTN ASS', '240-170-xxx', 'Endo Rotary'),
    ('AH PLUS', '240-160-xxx', 'Root Canal Sealer'),
    ('BROACH', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - FILE', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-FILE', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - TYPE', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-TYPE', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - FLEX', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-FLEX', '240-120-xxx', 'Endo Hand Instrument'),
    ('PTN ASS', '240-170-xxx', 'Endo Rotary'),
    ('AH PLUS', '240-160-xxx', 'Root Canal Sealer'),
    ('BROACH', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - FILE', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-FILE', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - TYPE', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-TYPE', '240-120-xxx', 'Endo Hand Instrument'),
    ('K - FLEX', '240-120-xxx', 'Endo Hand Instrument'), # Normalized space
    ('K-FLEX', '240-120-xxx', 'Endo Hand Instrument'),
    ('SPREADER', '240-120-xxx', 'Endo Hand Instrument'),
    ('SPIRO', '240-120-xxx', 'Endo Hand Instrument'),
    ('LUBRICANT', '240-130-xxx', 'Intra-canal Medicament'),
    ('ENDO STOP', '240-140-xxx', 'Endo Misc'), # Accessories/Misc
    ('DENTAL DAM', '240-180-xxx', 'Rubber Dam'),
    
    # Infection Control & Disposables Refinement
    ('POUCH', '540-120-xxx', 'Heat Sterilisation Pouch'),
    ('AUTOCLAVE', '540-120-xxx', 'Heat Sterilisation'),
    ('AUTOCLAV', '540-120-xxx', 'Heat Sterilisation'), # Typo
    ('STERI', '540-120-xxx', 'Heat Sterilisation'), # Catch-all for STERI-xxx
    ('INDICATOR', '540-120-xxx', 'Heat Sterilisation'),
    
    ('WIPE', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('CLEANER', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('SOLVENT', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('SANITIZER', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('SCRUB', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('DUALZYME', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    
    ('MASK', '180-150-xxx', 'Disposable Face Masks'),
    ('GLOVE', '180-160-xxx', 'Disposable Gloves'),
    ('SHIELD', '180-110-xxx', 'Barrier/Protective'),
    ('WRAP', '180-110-xxx', 'Barrier/Protective'),
    ('FILM', '180-110-xxx', 'Barrier/Protective'),
    ('COVER', '180-110-xxx', 'Barrier/Protective'),
    ('SLEEVE', '180-110-xxx', 'Barrier/Protective'),
    ('BIB', '180-220-xxx', 'Disposable Patient Bibs'),
    ('CAP', '180-110-xxx', 'Barrier/Protective'), # Or Misc? Map to Barrier
    ('GOWN', '180-110-xxx', 'Barrier/Protective'),
    ('VISOR', '180-110-xxx', 'Barrier/Protective'),
    ('GLASSES', '180-110-xxx', 'Barrier/Protective'),
    ('GOGGLE', '180-110-xxx', 'Barrier/Protective'),
    ('GOOGLE', '180-110-xxx', 'Barrier/Protective'), # Typos
    ('NOTE', '180-110-xxx', 'Barrier/Protective'), # HEAD COVERS (D NOTEC) -> Head Note? No, just catch Head Covers
    ('NET', '180-110-xxx', 'Barrier/Protective'), # Head Net
    ('SAFEMARK', '180-150-xxx', 'Disposable Face Masks'), # Specific Brand
    ('SHARP', '180-140-xxx', 'Evacuation/Sharps'), # Map to Evacuation or similar
    ('SOAP', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('SURFLETS', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('ALPROJET', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    ('CLEAN WATER', '540-110-xxx', 'Cold Sterilisation/Disinfection'),
    
    # Instrument Accessories (Color Codes)
    ('CODE RING', '340-110-xxx', 'Instrument Accessories'),
    ('COLOR CODE', '340-110-xxx', 'Instrument Accessories'),
    ('COLOUR CODE', '340-110-xxx', 'Instrument Accessories'),
    ('COLOUR CODED', '340-110-xxx', 'Instrument Accessories'),
    ('ID RING', '340-110-xxx', 'Instrument Accessories'),
    ('INSTRUMENT TAPE', '340-110-xxx', 'Instrument Accessories'),
    ('ID TAPE', '340-110-xxx', 'Instrument Accessories'),
    ('INSTR COLOR CODE', '340-110-xxx', 'Instrument Accessories'), # Matches partial old SKU or name
    
    # Preventive Correction
    
    # Preventive Correction
    ('PIT & FISSURE', '460-170-xxx', 'Preventive Prophylaxis'),
    ('TOOTHFAIRY', '460-170-xxx', 'Preventive Prophylaxis'),
    ('DURAPHAT', '460-120-xxx', 'Preventive Fluorides'),
]

def normalize_text(text):
    """
    Normalizes text by adding spaces around special characters.
    Example: "TOOTHBRUSH-RUBBER" -> "TOOTHBRUSH - RUBBER"
    """
    if not text:
        return ""
    # Add space around -, /, &, (, )
    # Using regex replace. \1 refers to the captured group.
    # We replace any instance of these chars with ' <char> '
    normalized = re.sub(r'([-&/()])', r' \1 ', text)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized

def determine_new_sku(item_row, keyword_rules, category_map):
    raw_item_name = item_row.get('Item Name', '').upper()
    current_sku = item_row.get('SKU', '').upper()
    vendor = item_row.get('Vendor', '').upper()
    raw_description = item_row.get('Description', '').upper()
    
    # Normalize for matching purposes
    item_name = normalize_text(raw_item_name)
    description = normalize_text(raw_description)
    
    # -1. Check Special Refinement Rules First (Highest Priority)
    for keyword, prefix, _ in SPECIAL_RULES:
        if keyword in item_name:
            return prefix

    # 0. Clean Old SKU Prefix
    old_sku_prefix = None
    if '-' in current_sku:
        parts = current_sku.split('-')
        for alias_key in CATEGORY_ALIASES:
            if current_sku.startswith(alias_key):
                old_sku_prefix = alias_key
                break
        
        if not old_sku_prefix:
            old_sku_prefix = parts[0].strip()

    # 1. Keyword Matching (Specific Subcats)
    matched_prefix = None
    for keyword, prefix in keyword_rules:
        if keyword in item_name:
            matched_prefix = prefix
            break
            
    if matched_prefix:
        return matched_prefix

    # 2. Vendor Logic (Before Aliases to allow specific overrides)
    if 'BICON' in vendor:
        return '320-100-xxx' # Implantology
    if 'CASTELLINI' in vendor:
        return '260-140-xxx' # Equipment > Miscellaneous (Code 140) - BETTER than 260-100
        
    # 3. Alias / Category Mapping
    mapped_category = None
    if old_sku_prefix:
        mapped_category = CATEGORY_ALIASES.get(old_sku_prefix, old_sku_prefix)
    
    if mapped_category:
        mapped_category = mapped_category.upper()
        if mapped_category in category_map:
            return category_map[mapped_category]
                
    # 4. Description Search
    if description:
        for keyword, prefix in keyword_rules:
            if keyword in description:
                return prefix
                
    return None

def process_items():
    print("Loading SKU Map...")
    keyword_rules, category_map = load_sku_rules(SKU_MAP_FILE)
    print(f"Loaded {len(keyword_rules)} keyword rules.")
    
    counters = {} 
    
    print("Processing Zoho Items...")
    with open(ZOHO_FILE, mode='r', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames
        if 'CF.SKU - new' not in fieldnames:
            fieldnames.append('CF.SKU - new')
        if 'Notes' not in fieldnames:
            fieldnames.append('Notes')
            
        rows = list(reader)
        
    updated_rows = []
    unmapped_rows = []
    mapped_count = 0
    
    for row in rows:
        prefix = determine_new_sku(row, keyword_rules, category_map)
        categories_tried = [] # Trace for debugging if needed, or just simple note
        
        if prefix:
            # Check if this is a "Direct Item" (Code 100)
            # Pattern: xxx-100-xxx
            is_code_100 = '-100-' in prefix
            
            if prefix not in counters:
                counters[prefix] = 0
            counters[prefix] += 1
            new_sku = get_next_sku(prefix, counters[prefix])
            row['CF.SKU - new'] = new_sku
            
            if is_code_100:
                row['Notes'] = "Manual Review: Assigned to General Category (Code 100) after automated matching."
            else:
                row['Notes'] = ""
                
            mapped_count += 1
            updated_rows.append(row)
        else:
            row['CF.SKU - new'] = ''
            row['Notes'] = "Unmapped: No matching category found."
            updated_rows.append(row)
            unmapped_rows.append(row)
            
    print(f"Mapped {mapped_count} out of {len(rows)} items.")
    
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, mode='w', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)
        
    if unmapped_rows:
        with open(UNMAPPED_FILE, mode='w', encoding='utf-8') as ufile:
            uwriter = csv.DictWriter(ufile, fieldnames=['Item Name', 'SKU', 'Vendor', 'Description'])
            uwriter.writeheader()
            for r in unmapped_rows:
                uwriter.writerow({k: r.get(k,'') for k in ['Item Name', 'SKU', 'Vendor', 'Description']})
        print(f"Unmapped items log saved to {UNMAPPED_FILE}")
        
    print("Done.")

if __name__ == "__main__":
    process_items()
