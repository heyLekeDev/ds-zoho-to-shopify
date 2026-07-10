
import csv
import re
import os

INPUT_FILE = '.tmp/Zoho_items_updated.csv'
OUTPUT_FILE = '.tmp/Zoho_items_all_refined.csv'

# Counter for serial numbers: key="Cat-Sub", value=int (max serial)
counters = {}

def normalize_text(text):
    if not text:
        return ""
    # Add space around special chars: -, /, &, ( ) if touching text
    s = re.sub(r'([-/&()])', r' \1 ', text)
    # The "X" Rule: Place single space before and after 'X' when used as dimension separator (e.g. 4.0X4.5MM)
    s = re.sub(r'(\d)X(\d)', r'\1 X \2', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def get_brand_override(combined_text):
    # TePe -> Preventive Brush/Floss
    if "TEPE" in combined_text:
        if "FLOSS" in combined_text or "TAPE" in combined_text: return "460", "120", "Preventive: Floss (TePe)"
        return "460", "180", "Preventive: Toothbrushes (TePe)"
    # Geistlich -> Biomaterials (120)
    # Check for Brand OR Specific Products (BIO-GIDE, BIO-OSS) which are Geistlich
    # Regex to handle "BIO - GIDE" or "BIO-GIDE" or "BIO GIDE" due to normalization
    if "GEISTLICH" in combined_text or re.search(r'BIO\s*-\s*GIDE', combined_text) or re.search(r'BIO\s*-\s*OSS', combined_text):
        if re.search(r'BIO\s*-\s*GIDE', combined_text) or "MEMBRANE" in combined_text: return "120", "120", "Biomaterials: Membranes (Geistlich)"
        return "120", "110", "Biomaterials: Bone Substitutes (Geistlich)"
    # Vivid / Zirconia -> Lab (380) if not Gowns
    if "VIVID" in combined_text or "ZIRCONIA" in combined_text:
         if "GOWN" not in combined_text and "APRON" not in combined_text:
             return "380", "170", "Lab: Miscellaneous (Vivid/Zirconia)"
    return None, None, None

def expand_acronyms(text):
    if not text: return ""
    # Expansions
    replacements = {
        r'\bTEMP ABUT\b': 'TEMPORARY ABUTMENT',
        r'\bPERM ABUT\b': 'PERMANENT ABUTMENT',
        r'\bIMP\b': 'IMPLANT',
        r'\bIMPL\b': 'IMPLANT',
        r'\bRA\b': 'RIGHT ANGLE',
        r'\bFG\b': 'FRICTION GRIP',
        r'\b5\s*PKT\b': '5 POCKET',
        r'\b3\s*PKT\b': '3 POCKET',
        r'\bTOP\b': 'SCRUB TOP',
        r'\bTROUSER': 'SCRUB PANTS', # Match start of word or whole word
        r'\bGRANI\b': 'GRANITE',
        r'\bC-BLUE\b': 'CEIL BLUE',
        # Handle POST in context later, but if "POST" appears with ABUTMENT/IMPLANT it is handled by category logic
    }
    s = text.upper()
    for pattern, repl in replacements.items():
        s = re.sub(pattern, repl, s)
    return s

    expanded_name = expand_acronyms(text)
    
    # Brand Overrides (Hard-coded) - "Brand-Category Hard-Coding"
    # TePe -> Preventive Brush/Floss
    if "TEPE" in expanded_name:
        if "FLOSS" in expanded_name or "TAPE" in expanded_name: return "460", "120", "Preventive: Floss (TePe)"
        return "460", "180", "Preventive: Toothbrushes (TePe)"
    # Geistlich -> Biomaterials
    if "GEISTLICH" in expanded_name:
        if "BIO-GIDE" in expanded_name or "MEMBRANE" in expanded_name: return "120", "120", "Biomaterials: Membranes (Geistlich)"
        return "120", "110", "Biomaterials: Bone Substitutes (Geistlich)"
    # Vivid / Zirconia -> Lab
    if "VIVID" in expanded_name or "ZIRCONIA" in expanded_name:
         # Need to be careful not to grab Vivid items that are something else?
         # "VIVID RITE GOWN" -> Surgical Gown.
         # The "Brand-Category Hard-Coding" said "Vivid/Zirconia: Categorize as Lab Products (380)".
         # But "VIVID RITE GOWN" is clearly Surgical.
         # So maybe check if "Lab" keywords exist or default to Lab if not Gown?
         # User said "Override existing logic". But Gown logic is also "existing logic".
         # I will stick to "Vivid/Zirconia" as Lab overrides IF NOT clearly specific like Gowns.
         if "GOWN" not in expanded_name and "APRON" not in expanded_name:
             return "380", "170", "Lab: Miscellaneous (Vivid/Zirconia)" # Using Misc Lab subcat for general Vivid lab items
    
    return None, None, None

def get_clinical_sweep_category(combined_text):
    # Equipment & Parts (260)
    # Parts -> 260-130
    if any(k in combined_text for k in ["SEAT", "BACKREST", "TUBING", "CABLE", " ROTOR", " UNIT"]): # Space before ROTOR/UNIT to avoid false positives like "Monitor"?
         if "UNIT" in combined_text and "DISPENSER" in combined_text: pass # Defer to Dispenser
         else: return "260", "130", "Equipment: Large Equipment & Accessories (Clinical Sweep)"
    
    # Small Equipment -> 260-150
    if any(k in combined_text for k in ["VACUUM FORMER", "ELECTROSURGE", "SURGE", "AMALGAMATOR"]):
         return "260", "150", "Equipment: Small Equipment (Clinical Sweep)"

    # Restorative (480)
    # Impressions -> 480-200
    if any(k in combined_text for k in ["PUTTY", "BODY", "AFFINIS", "PRESIDENT", "ALGINATE", "RETRACTION", "MIXING TIP"]):
         return "480", "200", "Restorative: Impression Materials (Clinical Sweep)"
    # Varnishes -> 480-240
    if any(k in combined_text for k in ["COPAL", "VARNISH", "LINER", "CAVITY"]):
         return "480", "240", "Restorative: Varnishes & Liners (Clinical Sweep)"

    # Disposables (180)
    # Evacuation -> 180-140
    if any(k in combined_text for k in ["SALIVA", "SUCTION", "ASPIRATOR", "EJECTOR"]):
         return "180", "140", "Disposables: Evacuation Products (Clinical Sweep)"
    # Dispensers -> 180-130
    if any(k in combined_text for k in ["DISPENSER", "HOLDER", "BRACKET"]):
         return "180", "130", "Disposables: Dispensers (Clinical Sweep)"

    # Instrument Overrides (340)
    # Surgical -> 340-190
    if any(k in combined_text for k in ["PERITOME", "BONE FILE", "HEMOSTAT", "SCALPEL", "BLADE"]):
         return "340", "190", "Instruments: Surgical (Clinical Sweep)"
    # Restorative -> 340-180
    if any(k in combined_text for k in ["FORCEP", "CARVER", "BURNISHER"]): # Forcep usually surgical but "Forcep for crown/bridge" might be restorative? User said Restorative 340-180 for Forcep here.
         return "340", "180", "Instruments: Restorative (Clinical Sweep)"
    
    return None, None, None

def get_final_cleanup_category(combined_text):
    # Equipment Parts (260) -> 260-130
    # Note: Added leading spaces to some to avoid partial word matches if needed, but 'combined_text' is space-separated norm text.
    if any(k in combined_text for k in ["O-RING", " TANK", "VALVE", "TUBING", "CABLE", "ELECTRODE", "WRENCH", " ROTOR", " CUP", "HEADREST"]):
         return "260", "130", "Equipment: Large Equipment & Accessories (Final Cleanup)"
    
    # Medicaments (100) -> 100-130
    if any(k in combined_text for k in ["DALACIN", "CATAFLAM", "RELEV", "GALCIPRO", "ANTIBIOTIC", "PILL", "TABLET"]):
         return "100", "130", "Medicaments (Final Cleanup)"

    # Restorative (480)
    # Bonding/Mixing -> 480-130
    if any(k in combined_text for k in ["MICRO APPLICATOR", "INTRAORAL TIP", "MIXING BOWL", "DISPENSING GUN"]):
         return "480", "130", "Restorative: Bonding & Mixing Accessories (Final Cleanup)"
    # Pins & Posts -> 480-220
    if any(k in combined_text for k in ["FILPIN", "FILPOST", "PIN BENDER"]):
         return "480", "220", "Restorative: Pins & Posts (Final Cleanup)"
    # Filling -> 480-180
    if any(k in combined_text for k in ["UNOLUX", "FLOW COMP"]):
         return "480", "180", "Restorative: Filling Materials (Final Cleanup)"

    # Lab (380)
    # 380-110 (Accessories) 
    if any(k in combined_text for k in ["THERMOFORMING", "BIOCRYL", "CASTING RING", "ALUMINUM OXIDE", "DIE SPACER"]):
         return "380", "110", "Lab: Accessories (Final Cleanup)"

    # Preventive (460)
    # 460-150 (Oral Hygiene)
    if any(k in combined_text for k in ["MOUTH RINSE", "RETAINER CASE", "TONGUE SCRAPPER", "BRUSHING TIMER", "REFILL HEAD"]):
         return "460", "150", "Preventive: Oral Hygiene (Final Cleanup)"

    # X-Ray (580)
    # 580-110
    if any(k in combined_text for k in ["DEVELOPER", "FIXER"]):
         return "580", "110", "X-Ray: Accessories (Final Cleanup)"

    # Apparel Check (560)
    # 560-150
    if any(k in combined_text for k in ["CHEROKEE", "BUTTERSOFT", "SUNVILLE", "V-NECK", "CLOGS"]):
         return "560", "150", "Surgical: Gowns (Apparel Override)"

    return None, None, None

def clean_name_for_mapping(name):
    # Strip "Secondary" / "secondary" from the end (or anywhere?) - User said "Strip from the name"
    # "ending in the word 'Secondary'" -> implies suffix, but safer to remove it if present as standalone word?
    # User instruction: "Identify any item name ending in the word 'Secondary'... Strip it..."
    # Also "Treat this word as an archival tag."
    cleaned = re.sub(r'\bSECONDARY\b', '', name, flags=re.IGNORECASE).strip()
    return cleaned

def get_targeted_update_category(combined_text):
    # Lab Ceramics (380-110)
    if any(k in combined_text for k in ["VITA", "VMK95", "TRANSLUS", "PORCELAIN POWDER", "CASTING RING", "THERMOFORMING MAT"]):
         return "380", "110", "Lab: Accessories (Targeted Update)"
    
    # Diagnostic Tools (340-110)
    if any(k in combined_text for k in ["IWANSON", "CALIPER", "GAUGE", "BENDER", "DAPPEN DISH"]):
         return "340", "110", "Instruments: Diagnostic (Targeted Update)"

    # Restorative (480-190) - Polishing/Finishing
    if any(k in combined_text for k in ["POLISHER", "ACRYPOL", "SILICONE POINT", "FELT WHEEL"]):
         return "480", "190", "Restorative: Finishing & Polishing (Targeted Update)"
    
    # Protective Wear (180-160) - Gloves
    # Only if NOT already Glove category? 180-160 is "Protective Wear (Gloves/Masks)"?
    # Map Check: 180-160 is "Disposables : Protective Wear".
    if any(k in combined_text for k in ["NITRILE", "LATEX", "GLOVES"]):
         return "180", "160", "Disposables: Protective Wear (Targeted Update)"

    # Equipment Maint (260-130) - Repeats parts
    if any(k in combined_text for k in ["O-RING", " TANK", "VALVE", "TUBING", "CABLE", "ELECTRODE", "WRENCH", " ROTOR"]):
         return "260", "130", "Equipment: Large Equipment & Accessories (Targeted Update)"
         
    return None, None, None

def get_final_sweep_category(combined_text):
    # Equipment Accessories (260)
    # 260-110 (Handpieces)
    if any(k in combined_text for k in ["ROTOR", " SPRAY"]):
         return "260", "110", "Equipment: Handpieces (Final Sweep)"
    # 260-130 (Large Eq Acc)
    if any(k in combined_text for k in ["NOZZLE", "O-RING", " TANK", "INTERFACE CAB", "ELECTRODE"]):
         return "260", "130", "Equipment: Large Equipment & Accessories (Final Sweep)"

    # Dental Consumables
    # Whitening (160)
    if any(k in combined_text for k in ["NITE WHITE", "OPTIGUARD"]):
         return "160", "130", "Cosmetics: Whitening (Final Sweep)" 

    # Bonding (480)
    if any(k in combined_text for k in ["ONE COAT", "APPLICATOR TIPS"]):
         return "480", "130", "Restorative: Bonding Agents (Final Sweep)"

    # Disposable Misc (180)
    if "EVA BAGS" in combined_text:
         # 180-240 is Plastics
         return "180", "240", "Disposables: Plastics (Final Sweep)"
    if "DISPOSABLE CUPS" in combined_text:
         # 180-240 plastics or 180-130 dispensers or 180-180 misc.
         # User said 180-240 or 180-180. Cups are plastics logic.
         return "180", "240", "Disposables: Plastics (Final Sweep)"

    return None, None, None

def get_final_polish_category(combined_text):
    # 1. Bicon/Integra-CP Rescue (Priority)
    if "INTEGRA-CP" in combined_text or "INTEGRA CP" in combined_text or "BICON" in combined_text:
         return "320", "120", "Implantology: Implants (Bicon Rescue)"

    # 2. Equipment Maintenance Clean-up (260) -> 260-130
    if any(k in combined_text for k in ["COIL", "SCREWS", "NOZZLE", "O-RING", " TANK", " CABLE"]):
         return "260", "130", "Equipment: Large Equipment & Accessories (Final Polish)"

    # 3. Restorative & Surgical Accessories
    # Tips (480-100 -> 480-130)
    if any(k in combined_text for k in [" TIPS", "INTRAORAL", "MIXING TIP", "COLTENE"]):
         return "480", "130", "Restorative: Bonding & Accessories (Final Polish)"
    
    # Dishes (340-100 -> 340-110)
    if any(k in combined_text for k in ["KIDNEY DISH", "EMESIS BASIN"]):
         return "340", "110", "Instruments: Accessories (Final Polish)"

    return None, None, None

def get_force_map_category(combined_text):
    """Phase 8: Force-mapping to eliminate all unmapped and Sub-100 items."""
    
    # 1. Bicon/Integra-CP Hard Override (HIGHEST PRIORITY)
    # This overrides ALL other logic
    if "INTEGRA-CP" in combined_text or "INTEGRA CP" in combined_text or "BICON" in combined_text:
         return "320", "120", "Implantology: Implants (Bicon Force-Map)"
    
    # 2. Surgical Instruments (340)
    # 340-190 (Surgical)
    if any(k in combined_text for k in ["ELEVATOR", "CUTTER", "FORCEP", "PERITOME", "SCALPEL", "HEMOSTAT", " DRILL", " PIN", "PLUG REMOVAL"]):
         return "340", "190", "Instruments: Surgical (Force-Map)"
    # 340-110 (Accessories)
    if any(k in combined_text for k in ["LIDS", "MINI LATCH HEAD", " LID", "PINS"]):
         return "340", "110", "Instruments: Accessories (Force-Map)"
    
    # 3. Equipment Accessories (260)
    if any(k in combined_text for k in ["O-RING", "O RING", " TANK", " CABLE", "DETECTOR", " COIL", "SCREWS", " SEAL", " SCREW"]):
         return "260", "130", "Equipment: Large Equipment & Accessories (Force-Map)"
    
    # 4. Restoration & Lab
    if any(k in combined_text for k in ["INTRA ORAL TIPS", "INTRAORAL TIPS"]):
         return "480", "130", "Restorative: Bonding & Accessories (Force-Map)"
    if "DENTAL STONE" in combined_text:
         return "380", "160", "Lab: Materials (Force-Map)"
    
    # 5. Lab & Preventive Generic Cleanup (catch remaining 380-100 and 460-100)
    # This will be checked in the main loop based on current category
    
    return None, None, None

def categorize_item(name, desc):
    # 1. Clean Name (Archival Tag Strip)
    cleaned_name_raw = clean_name_for_mapping(name)
    was_cleaned = (cleaned_name_raw != name)
    
    expanded_name = expand_acronyms(cleaned_name_raw)
    combined = (expanded_name + " " + (desc or "")).upper()
    
    # PHASE 8: Force-Map Check (HIGHEST PRIORITY)
    # This runs FIRST to override all other logic
    f_cat, f_sub, f_reas = get_force_map_category(combined)
    if f_cat: return f_cat, f_sub, f_reas
    
    # 2. "Top" Context Override (Bench Top vs Gowns)
    # The Rule: If "BENCH TOP" -> Equipment 260-150.
    # Note: expand_acronyms might convert "TOP" to "SCRUB TOP", turning "BENCH TOP" into "BENCH SCRUB TOP".
    if "BENCH TOP" in combined or "BENCH SCRUB TOP" in combined:
         return "260", "150", "Equipment: Small Equipment (Bench Top Correction)"

    # 3. Integra-CP Check (after strip)
    # Bicon / Integra-CP Rescue (Priority)
    if "INTEGRA-CP" in combined or "INTEGRA CP" in combined: # Normalized usually has space?
         return "320", "120", "Implantology: Implants (Bicon Rescue)"

    # Check Brand Overrides First

    b_cat, b_sub, b_reas = get_brand_override(combined) # Pass combined text to catch brands in description text
    if b_cat: return b_cat, b_sub, b_reas
    
    # Check Clinical Sweep Rules (High Priority for clearing 100s)
    c_cat, c_sub, c_reas = get_clinical_sweep_category(combined)
    if c_cat: return c_cat, c_sub, c_reas
    
    # Check Final Cleanup Rules (Highest Priority for remaining 100s)
    f_cat, f_sub, f_reas = get_final_cleanup_category(combined)
    if f_cat: return f_cat, f_sub, f_reas
    
    # Check Targeted Update Rules (Deep Clean Phase 5)
    t_cat, t_sub, t_reas = get_targeted_update_category(combined)
    if t_cat:
         reason_suffix = ""
         if was_cleaned: reason_suffix = " (Archival Suffix Cleaned)"
         return t_cat, t_sub, t_reas + reason_suffix
    
    # Check Final Sweep Rules (Last Resort for 100/Unmapped cleanup)
    s_cat, s_sub, s_reas = get_final_sweep_category(combined)
    if s_cat: return s_cat, s_sub, s_reas

    # Check Final Polish Rules (Ultimate Rescue/Polish)
    p_cat, p_sub, p_reas = get_final_polish_category(combined)
    if p_cat: return p_cat, p_sub, p_reas

    # Bicon Override Logic (Moved to function or top here)
    if "BICON" in combined:
         # Categorize as Implantology (320)
         if "TEMPORARY ABUTMENT" in combined or "TEMP ABUT" in combined:
            return "320", "110", "Implantology: Accessories (Temporary Abutment)"
         # ... existing Bicon logic logic ...
         if "UNIVERSAL ABUTMENT" in combined:
              return "320", "110", "Implantology: Accessories (Universal Abutment)"
         if "INTEGRA CP" in combined:
              return "320", "110", "Implantology: Accessories (Integra CP)"
         if "IMPLANT" in combined or "FIXTURE" in combined:
             return "320", "120", "Implantology: Implants"
         return "320", "110", "Implantology: Accessories (Bicon)"


    # === SPECIALTY CONFLICT RESOLUTION (ABUTMENTS / IMPLANTS) ===
    # Strict Prohibition: Do NOT map these to Education (200) or Office, even if "POST" is present.
    # Hierarchy Verification: BICON or ABUTMENT -> Check Implantology (320)
    
    # === The "Post" Correction ===
    # If "POST" is part of a dimension (e.g., 3MM POST), map to Implantology (320).
    # If "POST" refers to a dental pin (e.g., SCREW POST), map to Restorative > Pins & Posts (480-220).
    # If "POST" is followed by "ERIOR" (Posterior), Ignore.
    
    # Check for "POST" usage
    if "POST" in combined and "POSTERIOR" not in combined:
         # Check for dimension preceding "POST" (e.g. 3MM POST, 3.0MM POST, 3.0 MM POST)
         if re.search(r'\d\s*MM\s+POST', combined):
              return "320", "110", "Implantology: Accessories (Abutment Post)" # Or 320-120? Usually post is accessory/abutment component.
         
         if "SCREW POST" in combined or "FIBER POST" in combined or "PARA POST" in combined or "TITANIUM POST" in combined:
              return "480", "220", "Restorative: Pins & Posts"

    
    # Priority Check: If it is explicitly an Abutment or Implant, it should NOT be Cements/Restorative mainly.
    # Only if it's strictly an accessory or material might it differ, but "Universal Abutment" is definitely 320.
    
    if "ABUTMENT" in combined or "ABUT" in combined or "IMPLANT" in combined:
        # Bicon Specifics handled above or here if missed


        if "UNIVERSAL ABUTMENT" in combined:
             return "320", "110", "Implantology: Accessories (Universal Abutment)"
        if "ABUTMENT" in combined:
             return "320", "110", "Implantology: Accessories (Abutment)"
        if "IMPLANT" in combined:
             if "FLOSS" in combined: return "460", "120", "Preventive: Floss" # Implant Floss exception
             return "320", "120", "Implantology: Implants"
    
    # === 1. ANAESTHETICS (100) ===
    # 100-110 Haemostats
    # 100-120 Local Anaesthetic
    # 100-130 Medicaments
    # 100-150 Needles
    # 100-160 Syringes
    # 100-170 Topical
    if any(k in combined for k in ["LIGNOSPAN", "SEPTOCAINE", "LIDOCAINE", "MEPIVACAINE", "ARTICAINE", "CITANEST", "SCANDONEST"]):
        return "100", "120", "Anaesthetics: Local"
    if any(k in combined for k in ["NEEDLE", "MONOJECT", "TRANSCOJECT", "SEPTOJECT"]):
        if "HOLDER" not in combined: # Avoid Needle Holder
            return "100", "150", "Anaesthetics: Needles"
    if "SYRINGE" in combined:
        if "ETCH" not in combined and "COMPOSITE" not in combined and "IMPRESSION" not in combined:
             return "100", "160", "Anaesthetics: Syringes" # Assuming anaesthetic syringe if not material delivery
    if "TOPICAL" in combined or "BENZOCAINE" in combined or "GELATO" in combined:
        return "100", "170", "Anaesthetics: Topical"

    # === 2. BIOMATERIALS (120) ===
    # 120-110 Bone subst
    # 120-120 Membranes
    if "BONE" in combined and ("SUBSTITUTE" in combined or "GRAFT" in combined or "SYNTHETIC" in combined or "BOVINE" in combined or "PORCINE" in combined or "BIO-OSS" in combined or "ETHOSS" in combined or "SYNTHOGRAFT" in combined):
        return "120", "110", "Biomaterials: Bone Substitutes"
    if "MEMBRANE" in combined or "BIO-GIDE" in combined:
        return "120", "120", "Biomaterials: Membranes"

    # === 3. BURS (140) ===
    # 140-110 Accessories (Blocks, Holders)
    # 140-120 Carbide
    # 140-130 Diamond
    # 140-160 Surgical
    # Avoid false positive "BURNER"
    if "BURNER" in combined:
        return "380", "120", "Lab Equipment (Burner)"
    if re.search(r'\bBURS?\b', combined):
        if any(k in combined for k in ["BLOCK", "HOLDER", "CLEANING BRUSH", "STAND"]):
            return "140", "110", "Burs: Accessories"
        if "SURGICAL" in combined or "LINDEMANN" in combined or "TREPHINE" in combined or "ZEX" in combined:
            return "140", "160", "Burs: Surgical"
        if "DIAMOND" in combined:
            return "140", "130", "Burs: Diamond"
        if "CARBIDE" in combined or "FISSURE" in combined or "ROSE HEAD" in combined or "ROUND BUR" in combined:
            return "140", "120", "Burs: Carbide"
        if "DIATECH" in combined:
            return "140", "140", "Burs: Diatech"
        if "TWO STRIPER" in combined:
            return "140", "170", "Burs: Two-Striper"
        return "140", "150", "Burs: Others"

    # === 4. COSMETICS (160) ===
    # 160-130 Whitening
    if "WHITENING" in combined or "BLEACHING" in combined or "OPALESCENCE" in combined or "POLA" in combined or "ZOOM" in combined:
        return "160", "130", "Cosmetics: Whitening"

    # === 5. DISPOSABLES (180) ===
    # 180-110 Barrier/Protective (Tray covers, sleeves)
    # 180-120 Cotton (Rolls, Pellets, Gauze)
    # 180-140 Evacuation (Tips, Ejectors)
    # 180-150 Face Masks
    # 180-160 Gloves
    # 180-220 Bibs
    # 180-230 Cups
    # 180-210 Paper (Towels)
    if "GLOVE" in combined:
        return "180", "160", "Disposables: Gloves"
    if "MASK" in combined and "FACE" in combined:
        return "180", "150", "Disposables: Face Masks"
    if "BIB" in combined:
        return "180", "220", "Disposables: Patient Bibs"
    if "COTTON" in combined or "GAUZE" in combined or "SPONGE" in combined or "ROLL" in combined:
        # Check if dispenser
        if "DISPENSER" in combined:
             return "180", "130", "Disposables: Dispensers"
        return "180", "120", "Disposables: Cotton Products"
    if "CUP" in combined and "PLASTIC" in combined:
        if "DISPENSER" in combined:
             return "180", "130", "Disposables: Dispensers"
        return "180", "230", "Disposables: Plastic Cups"
    if "EJECTOR" in combined or "ASPIRATOR TIP" in combined or "SUCTION TIP" in combined or "EVACUATOR" in combined:
        return "180", "140", "Disposables: Evacuation"
    if "TOWEL" in combined or "TISSUE" in combined or "WIPE" in combined:
        return "180", "210", "Disposables: Paper Products"
    if "TRAY COVER" in combined or "SLEEVE" in combined or "BARRIER" in combined:
        return "180", "110", "Disposables: Barrier Products"

    # === 6. ENDODONTICS (240) ===
    # 240-110 Gutta Percha
    # 240-120 Hand Instruments
    # 240-130 Intra-canal Medicaments
    # 240-150 Paper points
    # 240-160 Sealers
    # 240-170 Rotary
    if any(k in combined for k in ["K-FILE", "H-FILE", "HEDSTROEM", "REAMER", "BROACH", "BARBED BROACH", "FILE"]):
        # Rotary check
        if "ROTARY" in combined or "PROTAPER" in combined or "WAVEONE" in combined or "RECIPROC" in combined or "HYFLEX" in combined or "GATES GLIDDEN" in combined or "PEESO" in combined:
            return "240", "170", "Endodontics: Rotary Instruments"
        # Hand check
        if "HAND" in combined or "K-FILE" in combined or "H-FILE" in combined:
             return "240", "120", "Endodontics: Hand Instruments"
        # Generic File check -> Hand if small size or unspecified? But user wants strict.
        # If "FILE" and Endo keywords present or implied by context.
        # Let's assume K/H/Hedstroem are Hand.
        # If just "FILE" and it's "SCHLUGER" -> Surgical (Caught later by Instrument logic or here if we prioritize)
        if "SCHLUGER" in combined or "BONE FILE" in combined:
             return "340", "190", "Surgical: Bone File"
        return "240", "120", "Endodontics: Hand Instruments" # Fallback for endo files
    if "GUTTA PERCHA" in combined: # Includes Gutta Percha Points
        return "240", "110", "Endodontics: Gutta Percha"
    if "PAPER POINT" in combined:
        return "240", "150", "Endodontics: Paper Points"
    if "SEALER" in combined or "AH PLUS" in combined:
        return "240", "160", "Endodontics: Sealers"
    if "EDTA" in combined or "HYPOCHLORITE" in combined or "CHLORHEXIDINE" in combined or "LUBRICANT" in combined or "GLYDE" in combined or "RC PREP" in combined:
        return "240", "130", "Endodontics: Medicaments"


    # === 7. EQUIPMENT (260) ===
    # 260-110 Handpieces
    # 260-120 Lab Equip (also 380-120?) Map has both.
    # 260-150 Small Equip (Curing light, apex locator)
    # 260-140 Misc (Bulbs, spare parts)
    if "HANDPIECE" in combined or "TURBINE" in combined or "CONTRA ANGLE" in combined or "STRAIGHT HANDPIECE" in combined:
        if "LUBRICANT" in combined or "OIL" in combined or "SPRAY" in combined:
             return "520", "110", "Services: Repairs/Maintenance" # Or Equipment Misc? Map has 260-140 Misc. Let's use 260-140 for parts.
             # Actually there's no Lubricant category. Let's put in 260-140 Equipment Misc or 260-110 Handpieces.
             # Map has 260-110 Handpieces.
             return "260", "110", "Equipment: Handpieces (Maintenance)"
        return "260", "110", "Equipment: Handpieces"
    if "CURING LIGHT" in combined or "AMALGAMATOR" in combined or "APEX LOCATOR" in combined or "SCALER UNIT" in combined:
        return "260", "150", "Equipment: Small"
    if "BULB" in combined or "LAMP" in combined:
        return "260", "140", "Equipment: Misc (Bulbs)"

    # === 8. INSTRUMENTS (340) ===
    # 340-120 Diagnostic
    # 340-160 Operative
    # 340-170 Perio
    # 340-190 Surgical
    # Previously implemented logic
    if any(k in combined for k in ["EXTRACTION FORCEPS", "ROOT ELEVATOR", "LUXATOR", "PERIOTOME", "SCALPEL", "BONE FILE", "HEMOSTAT", "NEEDLE HOLDER", "RONGEUR", "OSTEOTOME", "CHISEL", "SUTURE SCISSOR", "SURGICAL SCISSOR", "TISSUE FORCEPS", "MOSQUITO", "SCISSOR", "ELEVATOR", "SCHLUGER"]):
         return "340", "190", "Surgical Instruments"
    if "FORCEPS" in combined:
        if "ARTICULATING" in combined: return "340", "120", "Diagnostic Instruments"
        return "340", "190", "Surgical (Forceps)"
    if "SCALER" in combined or "CURETTE" in combined or "GRACEY" in combined:
        if "UNIT" in combined or "INSERT" in combined: # Scaler Unit or Insert
             if "INSERT" in combined: return "340", "170", "Perio Instruments (Scaler Insert)"
             return "260", "150", "Equipment: Small (Scaler Unit)"
        return "340", "170", "Perio Instruments"
    if any(k in combined for k in ["EXCAVATOR", "BURNISHER", "CONDENSER", "PLUGGER", "CARVER", "COMPOSITE INSTRUMENT", "PLASTIC INSTRUMENT", "AMALGAM CARRIER", "SPATULA", "CEMENT SPATULA"]):
         if "WAX" in combined: return "380", "130", "Lab Instruments"
         return "340", "160", "Restorative Instruments"
    if any(k in combined for k in ["MIRROR", "PROBE", "EXPLORER", "TWEEZER", "COTTON PLIER", "DRESSING PLIER"]):
         return "340", "120", "Diagnostic Instruments"

    if any(k in combined for k in ["MIRROR", "PROBE", "EXPLORER", "TWEEZER", "COTTON PLIER", "DRESSING PLIER"]):
          return "340", "120", "Diagnostic Instruments"

    # === APPAREL & PROTECTIVE WEAR ===
    # 560-150 Gowns
    # 560-120 Aprons
    # User Directive: "Rational: In a clinical inventory, scrubs and gowns both fall under 'Clinical Protective Apparel.' Resulting SKU Prefix: 560-150-xxx."
    
    if "GOWN" in combined and "LAB" not in combined:
         return "560", "150", "Surgical: Gowns"
    if "APRON" in combined:
         return "560", "120", "Surgical: Apron"
    
    if any(k in combined for k in ["TROUSER", "PANT", "SCRUB", "UNIFORM", "TUNIC", "JACKET", "V-NECK"]):
        # Map to Gowns (150) per directive
        return "560", "150", "Surgical: Gowns (Apparel/Scrubs)"
             
    if "CAP" in combined and ("BARREL" not in combined and "WATER" not in combined and "AMALGAM" not in combined): 
         return "560", "150", "Surgical: Gowns (Caps)"

    # === 9. PREVENTIVE (460) ===
    # 460-120 Floss
    # 460-180 Toothbrushes
    # 460-190 Toothpaste
    # Consumer-Brand Rules
    if any(k in combined for k in ["ORAL-B", "COLGATE", "SENSODYNE", "AQUAFRESH", "CREST", "REACH", "JORDAN"]):
        # Toothbrushes
        if any(k in combined for k in ["TOOTHBRUSH", "BRUSH", "STAGES", "CROSSACTION", "PRO-HEALTH", "PULSAR", "COMPLETE", "INDICATOR", "ADVANTAGE", "ALL ROUNDER"]):
             return "460", "180", "Preventive: Toothbrushes (Brand Search)"
        # Toothpastes
        if any(k in combined for k in ["TOOTHPASTE", "PASTE", "GEL", "PRO-RELIEF", "REPAIR", "TOTAL", "OPTIC WHITE", "MAX FRESH", "SENSITIVE"]):
             return "460", "190", "Preventive: Toothpaste (Brand Search)"
        # Floss
        if "FLOSS" in combined or "TAPE" in combined:
             return "460", "120", "Preventive: Floss"
        # Mouthwash
        if "MOUTHWASH" in combined or "RINSE" in combined or "PLAX" in combined:
             return "460", "150", "Preventive: Mouthwash"

    if "TOOTHBRUSH" in combined or "STAGES " in combined:
        return "460", "180", "Preventive: Toothbrushes"
    if "TOOTHPASTE" in combined:
        return "460", "190", "Preventive: Toothpaste"
    if "FLOSS" in combined:
        return "460", "120", "Preventive: Floss"

    # === 10. RESTORATIVE (480) ===
    # 480-110 Articulating
    # 480-120 Bite Reg
    # 480-130 Bonding
    # 480-140 Cements
    # 480-150 Crown & Bridge
    # 480-170 Etchants
    # 480-180 Filling (Composite, Amalgam)
    # 480-190 Finish/Polish
    # 480-200 Impressions
    # 480-210 Matrix
    # 480-220 Pins/Posts
    # 480-230 Trays
    if "COMPOSITE" in combined or "FILTEK" in combined or "TETRIC" in combined or "AMALGAM" in combined or "CAPSULE" in combined or "FLOWABLE" in combined or "DENTIN" in combined or "ENAMEL" in combined:
         return "480", "180", "Restorative: Filling Materials"
    if "ETCH" in combined:
         return "480", "170", "Restorative: Etchants"
    if "BOND" in combined and "AGENT" in combined or "ADHESIVE" in combined or "PRIME" in combined:
         return "480", "130", "Restorative: Bonding"
    if "CEMENT" in combined or "IRM" in combined or "TEMP BOND" in combined or "DYCAL" in combined or "LUTING" in combined:
         return "480", "140", "Restorative: Cements"
    if "IMPRESSION" in combined and ("MATERIAL" in combined or "PUTTY" in combined or "LIGHT BODY" in combined or "HEAVY BODY" in combined or "ALGINATE" in combined):
         return "480", "200", "Restorative: Impression Materials"
    if "IMPRESSION TRAY" in combined:
         return "480", "230", "Restorative: Trays"
    if "ARTICULATING PAPER" in combined:
         return "480", "110", "Restorative: Articulating"
    if "MATRIX" in combined or "WEDGE" in combined or "BAND" in combined:
         return "480", "210", "Restorative: Matrix Systems"
    if "FINISHING" in combined or "POLISHING" in combined or "STRIP" in combined or "DISC" in combined or "POINT" in combined and "PAPER" not in combined and "GUTTA" not in combined:
         return "480", "190", "Restorative: Finish/Polish"
    if "CROWN" in combined and ("FORM" in combined or "TEMPORARY" in combined):
         return "480", "150", "Restorative: Crown & Bridge"

    # === 11. LAB (380) ===
    # 380-160 Materials (Acrylics, Stone, Plaster)
    if "ACRYLIC" in combined or "MONOMER" in combined or "POLYMER" in combined or "PLASTER" in combined or "STONE" in combined or "WAX" in combined:
         return "380", "160", "Lab: Materials"

    # === 12. ORTHODONTICS (420) ===
    # 420-120 Brackets
    # 420-180 Wires
    # 420-130 Elastics
    if "BRACKET" in combined: return "420", "120", "Ortho: Brackets"
    if "WIRE" in combined and "KIRSCHNER" not in combined: return "420", "180", "Ortho: Wires" # Kirschner wire is surgical
    if "ELASTIC" in combined and "CHAIN" in combined or "LIGATURE" in combined: return "420", "130", "Ortho: Elastics"

    return None, None, None

def main():
    # 1. Load counters
    try:
        with open(INPUT_FILE, 'r') as f:
            reader = csv.DictReader(f)
            data = list(reader)
    except FileNotFoundError:
        print(f"File {INPUT_FILE} not found.")
        return

    # Initialize counters from existing valid SKUs
    for row in data:
        sku = row.get('CF.SKU - new', '')
        if sku and re.match(r'\d{3}-\d{3}-\d{3}', sku):
            parts = sku.split('-')
            # Only count if subcategory is NOT 100? No, we need to respect existing seq if we use them.
            # But we are reclassifying potential 100s. 
            # If an item is already 480-180-001, we want to know 001 exists.
            key = f"{parts[0]}-{parts[1]}"
            try:
                serial = int(parts[2])
                if key not in counters:
                    counters[key] = 0
                if serial > counters[key]:
                    counters[key] = serial
            except ValueError:
                pass

    print("Initialized counters.")
    
    fieldnames = reader.fieldnames
    updated_rows = []
    
    reclassified_count = 0
    
    for row in data:
        name = row['Item Name']
        desc = row['Description']
        current_sku = row.get('CF.SKU - new', '')
        
        # Determine if candidate for reclassification
        # 1. Has '100' subcode (xxx-100-xxx)
        # 2. Or is unassigned
        # 3. Or matches specific user overrides (Zoom, Yearly Labels)
        
        is_generic = False
        force_reclassify = False
        current_cat_sub = ""
        
        if current_sku:
            parts = current_sku.split('-')
            if len(parts) >= 2:
                current_cat_sub = f"{parts[0]}-{parts[1]}"
                if parts[1] == '100' or parts[1] in ['140', '150', '160', '170', '180']: # Target Generic (100) AND Miscellaneous (140-180)
                    is_generic = True
        else:
            is_generic = True # Treat unassigned as generic candidate
            
        norm_name = normalize_text(name)
        combined_upper = (norm_name + " " + (desc or "")).upper()
        
        # User specified override checks
        if "ZOOM" in combined_upper and current_cat_sub != "160-130": force_reclassify = True
        if "YEARLY LABEL" in combined_upper and current_cat_sub != "400-160": force_reclassify = True

        # Force reclassify if it's an Abutment/Bicon incorrectly assigned
        if "ABUTMENT" in combined_upper or "BICON" in combined_upper or "IMPLANT" in combined_upper:
            if not current_cat_sub.startswith("320"):
                 force_reclassify = True
        
        # Force reclassify Brands (TePe, Geistlich)
        if "TEPE" in combined_upper or "GEISTLICH" in combined_upper:
             force_reclassify = True
        
        # Force reclassify Bicon Rescue Keywords (Final Polish verification)
        if "INTEGRA" in combined_upper or "BICON" in combined_upper:
             force_reclassify = True

        # Force reclassify if matching Apparel keywords and in 560-100
        if any(k in combined_upper for k in ["TROUSER", "PANT", "SCRUB", "UNIFORM", "V-NECK", "JACKET", "TOP"]):
             # Special Check: If it is "BENCH TOP", it is EQUIPMENT 260-150.
             if "BENCH TOP" in combined_upper:
                  if current_cat_sub != "260-150": force_reclassify = True
             elif current_cat_sub == "560-100":
                  force_reclassify = True
        
        # Force reclassify Lab & Preventive generic items (Phase 9)
        if current_cat_sub == "380-100" or current_cat_sub == "460-100":
             force_reclassify = True

        if is_generic or force_reclassify:
            cat, sub, reason = categorize_item(norm_name, desc)
            
            # Explicit logic for forced items (Zoom, Yearly Label) override
            if "ZOOM" in combined_upper: cat, sub, reason = "160", "130", "Cosmetics: Whitening (User Request)"
            if "YEARLY LABEL" in combined_upper: cat, sub, reason = "400", "160", "Office Products: Stationery (User Request)"
            
            if cat and sub:
                new_key = f"{cat}-{sub}"
                # Update if different OR if re-verifying a Misc/Generic item
                # We want to proceed if:
                # 1. New key is different (Change)
                # 2. OR current is '100' or 'Misc' and we want to serialize it properly in new logic?
                # Actually if new key is SAME as old key, and it's 140/Misc, we might not want to change it unless we are "Deep Cleaning" into a BETTER category.
                # If categorize_item returns same Misc category, do nothing?
                # But categorization logic usually returns SPECIFIC categories.
                # So if new_key != current_cat_sub, update.
                
                if new_key != current_cat_sub:
                    # Valid update
                    
                    # Generate new SKU
                    key = f"{cat}-{sub}"
                    if key not in counters:
                        counters[key] = 0
                    counters[key] += 1
                    new_serial = counters[key]
                    new_sku = f"{cat}-{sub}-{new_serial:03d}"
                    
                    row['CF.SKU - new'] = new_sku
                    
                    # Update Notes Logic
                    row['Notes'] = f"Verified: {reason} via Brand/Keyword Search (Deep Clean)."
                    
                    if "Clinical Sweep" in reason:
                         row['Notes'] = "Re-categorized from 100 via Clinical Keyword Mapping."
                    
                    if "Final Cleanup" in reason or "Apparel Override" in reason:
                         row['Notes'] = "Final Refinement: Re-categorized via Professional Clinical Mapping."
                    
                    if "Bench Top Correction" in reason:
                         row['Notes'] = "Corrected: Bench Top classified as Equipment, not Gown."
                    elif "Bicon Force-Map" in reason:
                         row['Notes'] = "Bicon Force-Map: Archival suffix ignored; assigned to Implants (320-120)."
                    elif "Force-Map" in reason:
                         row['Notes'] = "Force-Mapped: Applied Clinical logic to clear Sub-category 100."
                    elif "Bicon Rescue" in reason:
                         row['Notes'] = "Bicon Rescue: Mapped to Implants; Archival suffix ignored."
                    elif "Integra-CP Correction" in reason or "Archival Suffix Cleaned" in reason:
                         row['Notes'] = "Archival suffix 'Secondary' ignored; mapped to Clinical Category."
                    elif "Final Sweep" in reason:
                         row['Notes'] = "Final Sweep: Re-mapped via Clinical Accessory Logic."
                    elif "Final Polish" in reason:
                         row['Notes'] = "Final Polish: Applied Clinical Accessory logic to clear Sub-category 100."
                    elif "Targeted Update" in reason:
                         row['Notes'] = "Re-mapped from 100 via Clinical Industry Logic."

                    # Specific Note Override for Apparel 560-100
                    if sub == '100' and "Apparel" in reason:
                        row['Notes'] = "Apparel - no specific sub-category for trousers in Map."
                        
                    reclassified_count += 1
        
        updated_rows.append(row)

    # 3. Write
    with open(OUTPUT_FILE, 'w') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)
        
    print(f"Processed {len(updated_rows)} rows. Reclassified {reclassified_count} items. Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
