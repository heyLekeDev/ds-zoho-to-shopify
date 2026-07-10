import csv
import json
import re
import os
import argparse
from typing import Dict, List, Tuple, Optional

# --- Constants & Patterns ---
INPUT_FILE = "DS inventory Jan 31 26.csv"
SKU_MAP_FILE = "sku_category_map.json"

# Size Standardization Map
SIZE_MAP = {
    'S': 'Small', 'SMALL': 'Small', 'SM': 'Small',
    'M': 'Medium', 'MEDIUM': 'Medium', 'MED': 'Medium',
    'L': 'Large', 'LARGE': 'Large', 'LG': 'Large',
    'XS': 'Extra Small', 'X-SMALL': 'Extra Small',
    'XL': 'Extra Large', 'X-LARGE': 'Extra Large',
    'XXL': '2X-Large', '2XL': '2X-Large', '2X': '2X-Large',
    '3XL': '3X-Large', '3X': '3X-Large',
    '4XL': '4X-Large', '4X': '4X-Large',
    'JUNIOR': 'Junior', 'ADULT': 'Adult'
}

# Color Standardization Map (Expanded for Scrubs)
COLOR_MAP = {
    'GAL': 'Galaxy', 'GALAXY': 'Galaxy',
    'JANAV': 'Jane Navy',
    'ELPIN': 'Electric Pink',
    'PEWTE': 'Pewter', 'PEWTER': 'Pewter',
    'GRPW': 'Grape Wine', 
    'WINE': 'Wine',
    'NAVY': 'Navy',
    'BLACK': 'Black',
    'WHITE': 'White',
    'GREY': 'Grey', 'GRAY': 'Grey',
    'JADE': 'Jade',
    'GRANI': 'Granite', 'GRANITE': 'Granite',
    'RED': 'Red', 'BLUE': 'Blue', 'GREEN': 'Green', 'PINK': 'Pink',
    'PURPLE': 'Purple', 'ORANGE': 'Orange', 'YELLOW': 'Yellow',
    'TEAL': 'Teal', 'ROYAL': 'Royal', 'CEIL': 'Ceil',
    'KHAKI': 'Khaki', 'OLIVE': 'Olive', 'HUNTER': 'Hunter',
    'CARIBBEAN': 'Caribbean', 'EGGPLANT': 'Eggplant', 
    'TURQUOISE': 'Turquoise', 'INDIGO': 'Indigo', 
    'TAN': 'Tan', 'BEIGE': 'Beige', 
    'CHOCOLATE': 'Chocolate', 'COCOA': 'Cocoa', 
    'BURGUNDY': 'Burgundy', 'RUBY': 'Ruby', 'RASPBERRY': 'Raspberry'
}

# Regex Construction
# Sort keys by length desc to ensure "EXTRA LARGE" matches before "LARGE", etc.
size_keys = sorted(list(SIZE_MAP.keys()), key=len, reverse=True)
SIZE_PATTERN = r'\b(' + "|".join(re.escape(k) for k in size_keys) + r')\b'

# Sort color keys by length matches "GALAXY" before "GAL"
color_keys = sorted(list(COLOR_MAP.keys()), key=len, reverse=True)
COLOR_PATTERN = r'\b(' + "|".join(re.escape(k) for k in color_keys) + r')\b'

SHOE_SIZE_PATTERN = r'\bSIZE\s*(\d+(?:\.\d+)?)\b'
VOLUME_PATTERN = r'\b(\d+(?:\.\d+)?)\s*(ML|G|L|OZ|CC)\b'
DIM_PATTERN = r'(\d+(?:\.\d+)?)\s*[X]\s*(\d+(?:\.\d+)?)\s*(MM|CM|IN)?'
EQUIPMENT_POWER_PATTERN = r'\b(\d+(?:\.\d+)?)\s*(V|W|KW|HP|MICRON)\b'
INTEGRA_PATTERN = r'INTEGRA\s*-?\s*CP'

# Case Correction Map
CASING_MAP = {
    'Ua': 'UA', 'Dsa': 'DSA', 'Mhs': 'MHS', 'Eds': 'EDS', 
    'Jr': 'Jr.', 'Pkt': 'Pkt', 'Pks': 'Pks',
    "Mens": "Men's", "Womens": "Women's",
    "Men'S": "Men's", "Women'S": "Women's"
}

# Number Word Map
NUMBER_WORD_MAP = {
    'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 'FIVE': '5',
    'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9', 'TEN': '10'
}

def load_sku_map():
    if not os.path.exists(SKU_MAP_FILE):
        return {}
    with open(SKU_MAP_FILE, 'r') as f:
        return json.load(f)

def load_external_descriptions(filepath):
    if not filepath or not os.path.exists(filepath):
        return {}
    with open(filepath, 'r') as f:
        return json.load(f)

def fix_casing(text):
    words = text.split()
    fixed_words = [CASING_MAP.get(w, w) for w in words]
    return " ".join(fixed_words)

def normalize_title(name):
    # 1. Dimensions: 4.5X5.0MM -> 4.5 x 5.0mm
    def dim_repl(m):
        unit = m.group(3) if m.group(3) else ""
        return f"{m.group(1)} x {m.group(2)}{unit}"
    name = re.sub(DIM_PATTERN, dim_repl, name, flags=re.IGNORECASE)
    
    # Trademarks & Brands
    name = re.sub(INTEGRA_PATTERN, "Integra-CP™", name, flags=re.IGNORECASE)
    name = re.sub(r'\bBICON\b', "Bicon", name, flags=re.IGNORECASE)
    
    # Men's/Women's protection (remove apostrophe to avoid S matching)
    name = re.sub(r"\b(Men|Women)'s\b", r"\1s", name, flags=re.IGNORECASE)
    
    # Protect "Two Striper" from Number Word Map
    # Replace "Two Striper" with temporary token
    name = re.sub(r'\bTwo Striper\b', 'TWO_STRIPER_TOKEN', name, flags=re.IGNORECASE)

    # Number Words -> Digits (One -> 1)
    for word, digit in NUMBER_WORD_MAP.items():
        name = re.sub(r'\b' + re.escape(word) + r'\b', digit, name, flags=re.IGNORECASE)
    
    # Restore "Two Striper" (Keep original casing preference or enforce Title)
    name = name.replace('TWO_STRIPER_TOKEN', 'Two Striper')
        
    # Degrees -> °
    name = re.sub(r'\bDEGREES?\b', '°', name, flags=re.IGNORECASE)
    # Handle "30D" or "30 D" if relevant? User said "somthing in degrees".
    # Assuming literal word "degree".
    
    # Strip dangling "SIZE" at end (e.g., SUNVILLE... SIZE)
    name = re.sub(r'\s+SIZE\s*$', '', name, flags=re.IGNORECASE)

    # 5. Fix Units casing (Mm -> mm, Ft -> ft, Gm -> gm)
    # Use word boundaries. Be careful with "IN" (Inches) vs "in" (preposition).
    # We only lowercase clear units.
    units_map = {
        'MM': 'mm', 'CM': 'cm', 'M': 'm', 
        'KG': 'kg', 'GM': 'gm', 'G': 'g',
        'L': 'l', 'ML': 'ml',
        'FT': 'ft', 'OZ': 'oz'
    }
    for u, low in units_map.items():
         name = re.sub(r'\b' + u + r'\b', low, name, flags=re.IGNORECASE)

    # 6. Specific Fixes (Bio-Oss spacing)
    # Fix "Bio-Oss-0.5Gm" -> "Bio-Oss 0.5gm"
    # Replace Hyphen-Digit with Space-Digit if preceded by letters
    name = re.sub(r'([a-zA-Z])\s*-\s*(\d)', r'\1 \2', name)

    return name.strip()

def infer_variants(name):
    """Infers variants (Size, Volume, Color) from Item Name.
       Returns: (extracted_attributes, list_of_raw_matches_to_strip)
    """
    attrs = {}
    matches = []
    
    # Check Shoe Size (Number)
    shoe_match = re.search(SHOE_SIZE_PATTERN, name, re.IGNORECASE)
    if shoe_match:
        attrs['Size'] = shoe_match.group(1)
        matches.append(shoe_match.group(0)) # Strip "SIZE 12"
    else:
        # Check Apparel/Standard Size
        size_match = re.search(SIZE_PATTERN, name, re.IGNORECASE)
        if size_match:
            raw = size_match.group(1).upper()
            norm = SIZE_MAP.get(raw, raw.title())
            attrs['Size'] = norm
            matches.append(size_match.group(0))

    # Volume
    vol_match = re.search(VOLUME_PATTERN, name, re.IGNORECASE)
    if vol_match:
        attrs['Volume'] = f"{vol_match.group(1)}{vol_match.group(2).lower()}"
        matches.append(vol_match.group(0))

    # Color - Find ALL matches
    color_matches = list(re.finditer(COLOR_PATTERN, name, re.IGNORECASE))
    found_colors = []
    for m in color_matches:
        raw = m.group(1).upper()
        
        # EXCEPTION: Skip "White" if it is part of "Nite White" or "Day White"
        # Check if the match is "WHITE" and preceded by "NITE" or "DAY"
        if raw == 'WHITE':
            start_idx = m.start()
            preceding_text = name[:start_idx].strip()
            if preceding_text.upper().endswith("NITE") or preceding_text.upper().endswith("DAY"):
                continue

        norm = COLOR_MAP.get(raw, raw.title())
        if norm not in found_colors:
            found_colors.append(norm)
        matches.append(m.group(0))
        
    if found_colors:
        attrs['Color'] = " ".join(found_colors)
        
    return attrs, matches

def extract_attributes(name, sku, category_data):
    attrs = {}
    matches = []
    
    # Bicon Implants (Integra-CP)
    if "INTEGRA-CP" in name.upper() or "INTEGRA CP" in name.upper():
        dim_match = re.search(r'(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)mm', name, re.IGNORECASE)
        if dim_match:
            attrs['Diameter'] = f"{dim_match.group(1)}mm"
            attrs['Length'] = f"{dim_match.group(2)}mm"
            # We don't necessarily strip dimensions for Bicon, keeping logic simple or stripping if desired.
            # matches.append(dim_match.group(0)) # Optional: Strip dimensions from name?
            
            attrs['Well'] = f"{float(well_match.group(1)):.1f}mm"
            matches.append(well_match.group(0))

    # Equipment (280-...)
    if "280-" in sku:
        # 1. Power/Spec (24V, 50W, 25 MICRON)
        pwr_matches = list(re.finditer(EQUIPMENT_POWER_PATTERN, name, re.IGNORECASE))
        specs = []
        for m in pwr_matches:
            val = m.group(1)
            unit = m.group(2).upper()
            if unit == "MICRON":
                attrs['Filter Size'] = f"{val} Micron"
            else:
                specs.append(f"{val}{unit}")
            matches.append(m.group(0))
        
        if specs:
            attrs['Power/Spec'] = ", ".join(specs)

        # 2. Extract Model (Naively: Any term starting with '#' or alphanumeric code at end?)
        # For now, let's look for "D 17-..." pattern seen in Castellini
        part_match = re.search(r'\b([A-Z]{1,2}\s*\d+-[A-Z0-9]+)\b', name)
        if part_match:
             attrs['Part Number'] = part_match.group(1)
             matches.append(part_match.group(0))

    # Burs Specific Logic (160-Burs)
    if any(k in name.upper() for k in ["BUR", "DIAMOND", "CARBIDE", "TWO STRIPER", "FG", "RA", "HP"]):
        # Extract Shape/Model Code (e.g. 1557, 558, 514.4C, 830/014)
        
        # --- DIATECH PARSING LOGIC ---
        # Code Format: Shape(3)-Shank(3)-Size(3)-Len(opt)-Grit(opt)
        # Handles:
        # 859-314-010-10-F  (Dash separated)
        # 859-314-010-10UF  (Merged Length/Grit)
        # 368-314-020-5-XF  (Dash separated)
        
        # Regex Explanation:
        # Group 1: Shape (3 digits)
        # Group 2: Size (3 digits)
        # Group 3: Optional Length (digits) + Optional Grit (Letters)
        diatech_match = re.search(r'\b(\d{3})-314-(\d{3})(?:-([\d]+)([A-Z]+)?)?(?:-([A-Z]+))?\b', name, re.IGNORECASE)
        
        if diatech_match:
            shape_code = diatech_match.group(1)
            size_code = diatech_match.group(2)
            
            # Parsing the optional suffix parts
            part3_num = diatech_match.group(3) # Length if present
            part3_alpha = diatech_match.group(4) # Grit if merged with length (e.g. 10UF -> UF)
            part4_alpha = diatech_match.group(5) # Grit if separate (e.g. -F)
            
            # Determine Grit string
            grit_code = ""
            if part3_alpha: 
                grit_code = part3_alpha
            elif part4_alpha:
                grit_code = part4_alpha
            
            # Store Attributes
            attrs['Shape'] = shape_code
            attrs['Head Size'] = f"0.{size_code}mm"
            
            # Map Shape Codes
            shape_names = {
                '859': 'Needle / Flame',
                '368': 'Football / Egg',
                '835': 'Cylinder Flat End',
                '830': 'Pear',
                '850': 'Round End Taper',
                '801': 'Round',
                '379': 'Egg',
                '862': 'Flame'
            }
            if shape_code in shape_names:
                attrs['Shape Name'] = shape_names[shape_code]

            # Map Grit Codes
             # Map Grit Codes
            grit_map = {
                'XF': 'Extra Fine (Yellow)',
                'F': 'Fine (Red)',
                'ML': 'Medium (Blue)', 
                'C': 'Coarse (Green)',
                'XC': 'Super Coarse (Black)',
                'UF': 'Ultra Fine (White)'
            }
            norm_grit = grit_code.upper()
            if norm_grit in grit_map:
                attrs['Grit'] = grit_map[norm_grit]
            elif 'ML' in name.upper() and not attrs.get('Grit'):
                 attrs['Grit'] = 'Multilayer Medium (Blue)'
            
            matches.append(diatech_match.group(0))
            
        else: 
            # Fallback to existing logic for non-Diatech complex codes
            # 1. ISO-ish patterns (digits/digits)
            iso_match = re.search(r'\b(\d{3}/\d{3})\b', name)
            if iso_match:
                attrs['Shape'] = iso_match.group(1)
                matches.append(iso_match.group(0))
            else:
                # 2. Complex Alphanumeric Codes (Two Striper style: 514.4C, 799.11VF)
                ts_code_match = re.search(r'\b(\d{3,}\.\d+[A-Z]+)\b', name)
                if ts_code_match:
                    attrs['Shape'] = ts_code_match.group(1)
                    matches.append(ts_code_match.group(0))
                else:
                    # 3. Simple Number Codes
                    simple_code_match = re.search(r'\b(\d{3,5})\b(?!\s*(?:mm|cm|gm|ml|pk|pcs))', name, re.IGNORECASE)
                    if simple_code_match:
                        attrs['Shape'] = simple_code_match.group(1)
                        matches.append(simple_code_match.group(0))

    # --- ENDODONTICS SPECIFIC LOGIC ---
    if any(k in name.upper() for k in ["FILE", "REAMER", "BROACH", "GUTTA", "PAPER POINT", "DRILL", "PLUGGER", "SPREADER", "STOP"]):
        
        # 1. Length/Size/Diameter Extraction (e.g. 21mm, 25mm, 4.0MM)
        # Often appears as "21MM", "25 MM", "4.0MM"
        # Support decimals like 4.0MM
        len_match = re.search(r'\b(\d+(?:\.\d+)?)\s*MM\b', name, re.IGNORECASE)
        if len_match:
            # If value is small (< 10), it's likely a Diameter/Size, not Length
            # But we can store it as Size or Length depending on context?
            # Reamers "4.0MM" is size. "25MM" is length.
            # Let's simple fix: Extract it.
            val = len_match.group(1)
            # If float(val) < 10, maybe it's size? 
            # Actually, let's just use "Size" if the keyword "Size" isn't present, 
            # OR we can treat all MM as potential Size/Length. 
            # Existing logic mapped MM to "Length". 
            # For 4.0MM Latch Reamer, "Length": "4.0mm" is weird but acceptable variant?
            # Or better, check value. 
            if float(val) < 10:
                 attrs['Size'] = f"{val}mm"
            else:
                 attrs['Length'] = f"{val}mm"
            
            matches.append(len_match.group(0))

        # 2. Size Extraction
        # Patterns: "#20", "Size 20", "ISO 20", "020", "X1", "F1", "Color 15"
        
        # ProTaper / WaveOne Specific (X1-X5, F1-F5, S1, S2)
        # Use word boundaries or strict formatting
        pt_match = re.search(r'\b(X[1-5]|F[1-5]|S[1-2])\b', name, re.IGNORECASE)
        if pt_match:
            attrs['Size'] = pt_match.group(1).upper()
            matches.append(pt_match.group(0))
        else:
            # Standard ISO Sizes (#06 to #140)
            # Look for "#20", "Size 20", "ISO 20", "Color 15"
            iso_match = re.search(r'(?:#|Size\s*|ISO\s*|Color\s*)(\d{1,3})\b', name, re.IGNORECASE)
            if iso_match:
                attrs['Size'] = iso_match.group(1)
                matches.append(iso_match.group(0))
            else:
                 # Look for loose number if context implies size (e.g. "K-FILE 20")
                 # This is risky, but common in "020" or "030" formats
                 loose_iso = re.search(r'\b0(\d{2})\b', name)
                 if loose_iso:
                     attrs['Size'] = loose_iso.group(1) # Convert 020 -> 20
                     matches.append(loose_iso.group(0))

        # 3. Taper Extraction (e.g. .04, .06, 04 Taper)
        taper_match = re.search(r'\b(?:\.|0)(\d{2})\s*Taper\b', name, re.IGNORECASE) or \
                      re.search(r'\.0(\d)\b', name) # .04, .06
        if taper_match:
            attrs['Taper'] = f".0{taper_match.group(1)}"
            matches.append(taper_match.group(0))

        # 4. Range handling (e.g. 15-40, 45-80)
        range_match = re.search(r'\b(\d{2})\s*-\s*(\d{2})\b', name)
        if range_match:
             attrs['Size'] = f"{range_match.group(1)}-{range_match.group(2)}"
             matches.append(range_match.group(0))

    # Generic Variant Inference
    inferred_attrs, inferred_matches = infer_variants(name)
    attrs.update(inferred_attrs)
    matches.extend(inferred_matches)
            
    return attrs, matches

def generate_description(name, attrs, category_data, existing_desc, external_data, sku):
    """Generates a technical description. Priorities: External > Existing > Template. Plain Text."""
    
    # 1. External Lookup
    if external_data and 'description' in external_data:
        return external_data['description'], True

    # 2. Existing (if sufficient)
    if existing_desc and len(existing_desc) > 10:
        clean_desc = re.sub(r'<[^>]+>', '', existing_desc).strip()
        return clean_desc, False 
        
    # 3. Templates (Plain Text)
    cat_code = category_data.get('category', '')
    sub_cat = category_data.get('subcategory', 'Dental Supply')
    
    desc_text = ""
    is_generated = True
    
    # 360 - Janitorial / Apparel
    if "Apparel" in cat_code or "Apparel" in sub_cat:
        desc_text = f"Professional {sub_cat.lower()} designed for clinical comfort and durability. Meets professional standards for healthcare environments.\n\n"
        if attrs:
             desc_text += "Specifications:\n"
             for k, v in attrs.items():
                 desc_text += f"- {k}: {v}\n"
                 
    elif "360" in cat_code or "Janitorial" in cat_code:
        desc_text = f"Professional medical-grade {sub_cat.lower()} designed for rigorous dental clinic sanitation standards.\n\nCategory: {sub_cat}"

    elif "INTEGRA-CP" in name.upper():
        well = attrs.get('Well', 'Unknown')
        desc_text = f"The Integra-CP™ is a root-form dental implant featuring a hydroxylapatite (HA) surface treatment. {well} internal well connection."

    elif "Equipment" in category_data.get('category', '') or "280" in cat_code:
        # Defaults
        title_c = name.title()
        brand_n = attrs.get('Brand', 'the manufacturer')
        
        # Keyword-based Templates
        u_name = name.upper()
        
        if "ARTICULATOR" in u_name:
            desc_text = f"Precision {title_c} designed for accurate occlusion analysis and prosthetic fabrication. Features robust construction for reliable laboratory use."
        
        elif "CURING LIGHT" in u_name or "LED LIGHT" in u_name:
            desc_text = f"High-intensity {title_c} for rapid and effective polymerization of dental composites. Ergonomic design ensures clinical efficiency."
            
        elif any(x in u_name for x in ["BULB", "LAMP", "ILLUMINATOR"]):
            volts = attrs.get('Power/Spec', 'standard')
            desc_text = f"Replacement {title_c} ({volts}) for dental units. Engineered to provide optimal illumination and color accuracy for clinical procedures."
            
        elif any(x in u_name for x in ["HANDPIECE", "HAND PIECE", "CONTRA ANGLE", "DRILL"]):
            desc_text = f"Professional {title_c} engineered for smooth operation and high torque. Durable construction suitable for various clinical applications."
            
        elif any(x in u_name for x in ["O RING", "GASKET", "SEAL", "WASHER"]):
             desc_text = f"Genuine replacement {title_c} ensuring proper sealing and functionality for dental equipment units."
             
        elif "SUCTION" in u_name or "HOSE" in u_name or "TUBING" in u_name:
             desc_text = f"Durable {title_c} designed for high-flow suction and fluid management systems. Flexible and sterilization-compatible material."
             
        elif "FILTER" in u_name:
             spec = attrs.get('Filter Size', 'high-efficiency')
             desc_text = f"Premium {title_c} ({spec}) designed to protect dental unit components from debris and contaminants."
             
        elif "BOARD" in u_name or "PCB" in u_name:
             desc_text = f"OEM replacement {title_c} for dental unit electronics. Ensures reliable system performance and compatibility."
             
        elif "TANK" in u_name:
             desc_text = f"Replacement {title_c} for dental hygiene water systems. Designed for easy refilling and durable service."

        # --- New Categories Phase 2 ---
        
        elif any(x in u_name for x in ["WRENCH", "TORQUE", "SCREW", "KEY"]):
             desc_text = f"Precision {title_c} designed for accurate adjustments and maintenance of dental instruments."

        elif any(x in u_name for x in ["MACHINE", "FURNACE", "FORMER", "CLEANER", "SPA", "PUMP", "SCANNER", "ITERO", "ELECTROSURGE", "COMPRESSOR", "SCALER", "PIEZO", "SONIC"]):
             desc_text = f"Advanced {title_c} for efficient laboratory or clinical workflows. Features durable construction and high-performance specifications."

        elif any(x in u_name for x in ["CABLE", "WIRE", "SWITCH", "PCB", "BOARD", "ELECTRODE", "SENSOR", "DETECTOR", "GENERATOR", "INTERFACE", "RELAY", "SOFTWARE", "CAM", "MONITOR", "TERMINAL"]):
             desc_text = f"Replacement {title_c} for dental unit electronics. Ensures reliable connectivity and system integration."

        elif any(x in u_name for x in ["SPRAY", "LUBRICANT", "OIL", "CLEANER"]):
             desc_text = f"High-quality {title_c} for maintenance and care of dental handpieces and instruments."

        elif any(x in u_name for x in ["HEADREST", "ARMREST", "CUSPIDOR", "UPHOLSTERY", "CHAIR"]):
             desc_text = f"Genuine replacement {title_c} for dental chairs, ensuring patient comfort and unit functionality."

        elif any(x in u_name for x in ["SLEEVE", "APRON", "COVER", "BARRIER", "EJECTOR"]):
             desc_text = f"Protective {title_c} designed for optimal infection control and equipment protection."

        elif any(x in u_name for x in ["VALVE", "COUPLING", "ADAPTER", "NOZZLE", "CONNECTOR", "REGULATOR", "DISTRIBUTOR"]):
             desc_text = f"Precision {title_c} component for fluid or air control in dental units."
             
        elif any(x in u_name for x in ["O RING", "O-RING", "GASKET", "SEAL", "WASHER"]):
             desc_text = f"Genuine replacement {title_c} ensuring proper sealing and functionality for dental equipment units."
             
        elif any(x in u_name for x in ["SUCTION", "HOSE", "TUBING", "TUB.", "AIR GR."]):
             desc_text = f"Durable {title_c} designed for high-flow suction and fluid management systems."
             
        elif "OPTIGUARD" in u_name:
             desc_text = f"Protective {title_c} surface sealant for composite restorations. Extends longevity and finish."

        else:
            # Fallback for unclassified equipment
            desc_text = f"Professional-grade {title_c} designed for clinical durability and performance.\n\n"
            desc_text += "Features:\n- High-reliability component for dental equipment.\n- Precision engineered for compatibility.\n"

        # Append common Specs if available
        if attrs:
            desc_text += "\n\nSpecifications:\n"
            for k, v in attrs.items():
                desc_text += f"- {k}: {v}\n"

    else:
        desc_text = f"High-quality {sub_cat} item for professional clinical use. Manufactured to meet strict dental industry standards."
         
    return desc_text, is_generated

def enrich_item(row, sku_map, external_descs):
    sku = row.get('SKU', '') or row.get('CF.SKU - new', '')
    name = row.get('Item Name', '')
    brand = "" # Initialize usage to prevent UnboundLocalError
    
    # Get Category from SKU prefix
    prefix = "-".join(sku.split('-')[:2]) if sku else ""
    cat_data = sku_map.get(prefix, {})
    if not cat_data:
        zoho_cat = row.get('Category Name', '')
        if "Equipment" in zoho_cat or sku.startswith("280-"):
            cat_data = {'category': '280-Equipment', 'subcategory': zoho_cat}
    
    # External Data Lookup
    external_data = None
    if sku in external_descs:
        val = external_descs[sku]
        external_data = val if isinstance(val, dict) else {'description': val}
    elif name in external_descs:
        val = external_descs[name]
        external_data = val if isinstance(val, dict) else {'description': val}
    else:
        # Fuzzy / Partial Match (e.g. key "Crosstex Bibs" matches "Crosstex Bibs - Blue")
        # Sort keys by length descending to match longest specific key first
        for key in sorted(external_descs.keys(), key=len, reverse=True):
            if key.lower() in name.lower():
                val = external_descs[key]
                external_data = val if isinstance(val, dict) else {'description': val}
                break

    # 1. Normalize
    enriched_title = normalize_title(name)
    
    # 2. Attributes & Matches
    attrs, matches = extract_attributes(enriched_title, sku, cat_data)
    
    # 3. Grouping & Variants
    collection = ""
    variants = []
    
    # Identify Variants available
    if attrs.get('Size'): variants.append(('Size', attrs['Size']))
    if attrs.get('Volume'): variants.append(('Volume', attrs['Volume']))
    if attrs.get('Color'): variants.append(('Color', attrs['Color']))
    if attrs.get('Diameter'): variants.append(('Diameter', attrs['Diameter']))
    if attrs.get('Length'): variants.append(('Length', attrs['Length']))
    if attrs.get('Well'): variants.append(('Well', attrs['Well']))
    if attrs.get('Shape'): variants.append(('Shape', attrs['Shape']))
    if attrs.get('Taper'): variants.append(('Taper', attrs['Taper']))
    if attrs.get('Grit'): variants.append(('Grit', attrs['Grit']))
    if attrs.get('Head Size'): variants.append(('Head Size', attrs['Head Size']))
    if attrs.get('Shape Name'): variants.append(('Shape Name', attrs['Shape Name']))

    # Determine Product Name (Root Name)
    root_name = enriched_title
    
    # Strip variant values from root name to create Product Name
    # Sort matches by length desc to avoid partial replacements issues
    sorted_matches = sorted(matches, key=len, reverse=True)
    for m in sorted_matches:
        # Use word boundaries to prevent stripping partial words (e.g. S from Elastic)
        # Note: We assume matches are word-like (alphanumeric/unit).
        pattern = r'\b' + re.escape(m) + r'\b'
        root_name = re.sub(pattern, '', root_name, flags=re.IGNORECASE)
    
    # Clean up formatting of root name
    root_name = re.sub(r'(\s*-\s*)+', '-', root_name) # Handle multiple hyphens
    # Aggressive stripping removed closing parens. Using simpler stripping.
    root_name = root_name.strip(' -.,;') 
    root_name = re.sub(r'(\s{2,})', ' ', root_name) # double spaces
    root_name = re.sub(r'\(\s*\)', '', root_name) # Empty parens
    
    # --- Aggressive Cleaning for Grouping ---
    # Fixes issues where boundaries failed to strip matches like "#20" or "No.4"
    # Remove "#" followed by digits
    root_name = re.sub(r'#\s*\d+', '', root_name)
    # Remove "No." followed by digits
    root_name = re.sub(r'No\.\s*\d+', '', root_name, flags=re.IGNORECASE)
    # Remove "Size" followed by digits (if missed)
    root_name = re.sub(r'Size\s*\d+', '', root_name, flags=re.IGNORECASE)
    
    # Remove parenthesized codes like (XXF), (200PK), (1-6) that are variants
    # Only remove if content is short (variant codes) to avoid removing valid name parts?
    # Let's try removing parens with digits or short caps 
    root_name = re.sub(r'\(\s*[A-Z0-9-]{1,6}\s*\)', '', root_name, flags=re.IGNORECASE) 

    # Normalize Plurals in Group Name
    root_name = re.sub(r'\bPOINTS\b', 'POINT', root_name, flags=re.IGNORECASE)
    root_name = re.sub(r'\bFILES\b', 'FILE', root_name, flags=re.IGNORECASE) # K-FILES -> K-FILE
    root_name = re.sub(r'\bDRILLS\b', 'DRILL', root_name, flags=re.IGNORECASE)

    root_name = root_name.strip(' -.,;') 
    root_name = re.sub(r'(\s{2,})', ' ', root_name) # double spaces again
    root_name = root_name.strip()
    
    # UPPERCASE the Root Name (Product Name) as requested by USER
    # if "Integra-CP" not in root_name and "Bicon" not in root_name:
    #      root_name = root_name.title() 
    #      root_name = fix_casing(root_name)
    root_name = root_name.upper()

    # RE-RUN Unit Normalization (because .upper() messed them up: 5.0mm -> 5.0MM)
    # Actually, user wants ALL UPPERCASE for suggested name? 
    # "I think all uppercase for sugested name might be better thatn camel casing used."
    # If user wants ALL UPPER, then "5.0MM" is actually correct/desired?
    # BUT user previously asked to standardize "Mm" to "mm". 
    # "The 15 X 20Mm Resorbable Membrane still has "Mn" uper and lower case which is not the same as "mm" because it is a unit of lentgh. lets standerdize these"
    # This implies they want the units lowercase even if the text is uppercase? 
    # "BIO-OSS - 0.5GM" -> "Bio-Oss 0.5gm" (Title Case)
    # If I made it "BIO-OSS 0.5GM", is that better?
    # Let's assume Upper Text + Lower Units for readability, OR pure Upper.
    # Given the previous instruction "Mm" -> "mm", I should probably keep units lowercase even in Uppercase title to be scientifically correct.
    # e.g. "DIATECH FG 368 0.020mm" looks better than "0.020MM".
    
    # Let's Apply Unit Lowercasing *after* Uppercasing
    units_map = {
        'MM': 'mm', 'CM': 'cm', 'M': 'm', 
        'KG': 'kg', 'GM': 'gm', 'G': 'g',
        'L': 'l', 'ML': 'ml',
        'FT': 'ft', 'OZ': 'oz'
    }
    for u, low in units_map.items():
         # 1. Standalone words (e.g. " 15 Mm ")
         root_name = re.sub(r'\b' + u + r'\b', low, root_name, flags=re.IGNORECASE)
         # 2. Attached to numbers (e.g. "20Mm")
         root_name = re.sub(r'(\d)\s*' + u + r'\b', r'\1' + low, root_name, flags=re.IGNORECASE)
         
    # --- COLLECTION / GROUPING LOGIC ---
    
    # Determine Brand (if not already set from external)
    if not brand:
         brand = row.get('Brand', '') 

    # SS White Specific Grouping
    if "SSW" in root_name.upper() or "SS WHITE" in root_name.upper() or "SSW" in str(brand).upper():
        brand = "SS White" # Enforce Brand
        if "RA" in root_name.upper() or "LATCH" in root_name.upper():
            collection = "SS White Carbide Burs - RA (Latch)"
            # Clean up Root Name
            root_name = root_name.replace("Ssw Ra", "").replace("Burs-Ssw", "").strip(" -")
            if not root_name.startswith("SS White"): root_name = f"SS White RA - {root_name}"
        elif "FG" in root_name.upper() or "FRICTION" in root_name.upper():
            collection = "SS White Carbide Burs - FG"
             # Clean up Root Name
            root_name = root_name.replace("Ssw Fg", "").replace("Burs-Ssw", "").strip(" -#")
            if not root_name.startswith("SS White"): root_name = f"SS White FG - {root_name}"
        else:
            collection = "SS White Carbide Burs"
    
    # Only assign collection (Group Name) if there are variants to group
    elif variants:
        collection = root_name 
    else:
        collection = ""

    # Map Variants to Output Columns
    variant_1_name = variants[0][0] if len(variants) > 0 else None
    variant_1_value = variants[0][1] if len(variants) > 0 else None
    variant_2_name = variants[1][0] if len(variants) > 1 else None
    variant_2_value = variants[1][1] if len(variants) > 1 else None
    variant_3_name = variants[2][0] if len(variants) > 2 else None
    variant_3_value = variants[2][1] if len(variants) > 2 else None

    # Construct Suggested Title
    variant_str = " / ".join([v[1] for v in variants])
    if variant_str:
        suggested_title = f"{root_name} - {variant_str}"
    else:
        suggested_title = root_name

    # 4. Description
    existing_desc = row.get('Sales Description', '').strip()
    desc, was_generated = generate_description(enriched_title, attrs, cat_data, existing_desc, external_data, sku)
    
    # 5. Additional Fields
    brand = row.get('Brand', '').strip()
    if not brand and external_data and external_data.get('brand'):
        brand = external_data.get('brand')
    
    if brand.isupper(): brand = brand.title()
    
    manufacturer = row.get('Manufacturer', '').strip()
    if not manufacturer and external_data and external_data.get('manufacturer'):
        manufacturer = external_data.get('manufacturer')

    if manufacturer.isupper(): manufacturer = manufacturer.title()
    if not manufacturer and brand: manufacturer = brand

    # --- BRAND / MANUFACTURER OVERRIDES ---
    # Fix "Generic" assignments if Name contains specific hints (e.g. Unodent, H/S)
    if brand in ["Generic", ""] or manufacturer in ["Generic", ""]:
        u_name = name.upper()
        if "H/S" in u_name or "HENRY SCHEIN" in u_name:
             brand = "Henry Schein"
             manufacturer = "Henry Schein"
        elif "UNODENT" in u_name:
             brand = "UnoDent"
             manufacturer = "UnoDent"
        elif "COMFIT" in u_name or "COM-FIT" in u_name:
             brand = "Dentsply Sirona"
             manufacturer = "Dentsply Sirona"
        elif "BRIGHTWAY" in u_name:
             brand = "Brightway"
             manufacturer = "Brightway Holdings"
        elif "PLASDENT" in u_name:
             brand = "Plasdent"
             manufacturer = "Plasdent"
        elif "ZIRC" in u_name:
             brand = "Zirc"
             manufacturer = "Zirc"
        elif "VIVID" in u_name:
             brand = "Vivid"
             manufacturer = "Vivid"
        elif "(UN" in u_name: # Catch truncated (UNODENT) or (UN...
             brand = "UnoDent"
             manufacturer = "UnoDent"

    # JSON Rules for Missing Brands
    if not brand or not manufacturer:
        try:
            with open('enrichment_brand_map.json', 'r') as f:
                rules = json.load(f).get('rules', [])
                for rule in rules:
                    # Simple regex search for pattern in Name
                    if re.search(rule['pattern'], u_name, re.IGNORECASE):
                        if not brand: brand = rule.get('brand')
                        if not manufacturer: manufacturer = rule.get('manufacturer')
                        break
        except FileNotFoundError:
            pass
    
    # Package Details
    pkg_length = row.get('Package Length', '')
    pkg_width = row.get('Package Width', '')
    pkg_height = row.get('Package Height', '')
    pkg_weight = row.get('Package Weight', '')
    dim_unit = row.get('Dimension Unit', 'cm')
    if not dim_unit: dim_unit = 'cm'

    # 6. Comments
    tags = []
    if was_generated: tags.append("[GENERATED]")
    else: tags.append("[MATCH]")
    
    # Leave suggested title blank if it's identical to the original Name
    final_suggested_title = suggested_title
    if final_suggested_title.strip() == name.strip():
        final_suggested_title = ""

    return {
        "sku": sku,
        "shopify_collection": collection,
        "Item Name": name, # Explicitly "Item Name" = Original Source Value
        "item-name-suggested": final_suggested_title, # Renamed from item_name_new
        "enriched_title": enriched_title,
        "variant_1_name": variant_1_name,
        "variant_1_value": variant_1_value,
        "variant_2_name": variant_2_name,
        "variant_2_value": variant_2_value,
        "variant_3_name": variant_3_name,
        "variant_3_value": variant_3_value,
        "description_html": desc,
        "sales_description": desc,
        "brand": brand,
        "manufacturer": manufacturer,
        "package_length": pkg_length,
        "package_width": pkg_width,
        "package_height": pkg_height,
        "package_weight": pkg_weight,
        "dimension_unit": dim_unit,
        "enrichment_comments": " ".join(tags),
        "processing_status": "Done"
    }

def main():
    parser = argparse.ArgumentParser(description="Enrich Inventory Data")
    parser.add_argument("--category", help="Filter by Category Name (e.g. '100-Anaesthetics')")
    parser.add_argument("--descriptions", help="Path to external descriptions JSON")
    args = parser.parse_args()

    sku_map = load_sku_map()
    external_descs = load_external_descriptions(args.descriptions) if args.descriptions else {}

    print(f"Batch Mode: Filtering for '{args.category}'" if args.category else "Processing ALL")
    
    # Create output filename
    cat_name = args.category.replace(' ', '_').replace('-', '_').replace('/', '_') if args.category else "ALL"
    output_filename = f"enriched-categories/{cat_name}_enriched.csv"
    
    os.makedirs(os.path.dirname(output_filename), exist_ok=True)

    with open(INPUT_FILE, mode='r', encoding='utf-8-sig') as infile, \
         open(output_filename, mode='w', newline='', encoding='utf-8') as outfile:
        
        reader = csv.DictReader(infile)
        fieldnames = [
            'sku', 'Item Name', 'item-name-suggested', 'sales_description', 
            'brand', 'manufacturer', 'shopify_collection',
            'variant_1_name', 'variant_1_value', 
            'variant_2_name', 'variant_2_value', 
            'variant_3_name', 'variant_3_value',
            'package_length', 'package_width', 'package_height', 'package_weight', 'dimension_unit',
            'enrichment_comments'
        ]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()

        count = 0
        for row in reader:
            if row.get('Status') != 'Active': continue

            # Filter by Category (Parent Category or Category Name)
            if args.category:
                cat_col = row.get('Category Name', '')
                parent_col = row.get('Parent Category', '')
                if args.category not in cat_col and args.category not in parent_col:
                    continue

            enriched = enrich_item(row, sku_map, external_descs)
            writer.writerow({k: enriched.get(k, '') for k in fieldnames})
            count += 1
            
        print(f"Processed {count} items. Saved to {output_filename}")

if __name__ == "__main__":
    main()
