#!/usr/bin/env python3
"""
Batch 1 Enrichment — 94 previously-Published items
Reads enrichment_input_batch1.json → writes enrichment_output.json
"""
import json, re

def fmt_mm(val):
    """Format a float as a clean mm string: 4.0 → '4.0mm', 5 → '5.0mm'"""
    v = float(val)
    return f"{v:.1f}mm" if v != int(v) or '.' in str(val) else f"{int(v)}.0mm"

def desc(body, bullets):
    items = ''.join(f'<li>{b}</li>' for b in bullets)
    return f'<p>{body}</p>\n<ul>\n  {chr(10).join("<li>" + b + "</li>" for b in bullets)}\n</ul>'

def make_desc(body, bullets):
    items_html = "\n  ".join(f"<li>{b}</li>" for b in bullets)
    return f"<p>{body}</p>\n<ul>\n  {items_html}\n</ul>"

RESULTS = []

# ── 1. Barbed Broaches (260-110-xxx) ──────────────────────────────────────────
broach_sizes = {
    '260-110-001': ('Assorted', 'Assorted'),
    '260-110-002': ('Size 0 (XXXXF)', 'Size 0 (XXXXF)'),
    '260-110-003': ('Size 1 (XXXF)',  'Size 1 (XXXF)'),
    '260-110-004': ('Size 2 (XXF)',   'Size 2 (XXF)'),
    '260-110-005': ('Size 3 (XF)',    'Size 3 (XF)'),
    '260-110-006': ('Size 4 (F)',     'Size 4 (F)'),
    '260-110-007': ('Size 5 (M)',     'Size 5 (M)'),
    '260-110-008': ('Size 6 (ST)',    'Size 6 (ST)'),
}
for sku, (size_label, size_val) in broach_sizes.items():
    standalone = size_label == 'Assorted'
    RESULTS.append({
        "sku": sku,
        "shopify_collection": None if standalone else "Barbed Broaches",
        "enriched_title": f"Barbed Broaches — {size_label}" if not standalone else "Barbed Broaches — Assorted",
        "brand": None,
        "shopify_product_type": "Endodontic Instrument",
        "shopify_tags": "Barbed Broaches, Endodontics, Pulp Extirpation, Root Canal",
        "variant_1_name": None if standalone else "Size",
        "variant_1_value": None if standalone else size_val,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            "Stainless steel barbed broaches for mechanical removal of vital and necrotic pulp tissue from root canals. Designed for single-use to prevent cross-contamination.",
            ["Material: Stainless steel", "Use: Pulp extirpation and debris removal", "Application: Root canal preparation", "Recommended: Single-use"]
        ),
        "enrichment_comments": "[MATCH] Barbed broaches grouped by size. Assorted pack is standalone.",
        "processing_status": "complete"
    })

# ── 2. Floss & Interdental (480-110-xxx) ────────────────────────────────────
RESULTS.append({
    "sku": "480-110-002",
    "shopify_collection": "OraTek 2-in-1 Floss & Interdental Brush",
    "enriched_title": "2-in-1 Floss & Interdental Brush — Blue (15/pk)",
    "brand": "OraTek",
    "shopify_product_type": "Interdental Aid",
    "shopify_tags": "OraTek, Floss, Interdental Brush, 2-in-1, Oral Hygiene",
    "variant_1_name": "Colour",
    "variant_1_value": "Blue",
    "variant_2_name": None,
    "variant_2_value": None,
    "description_html": make_desc(
        "Dual-function oral hygiene tool combining dental floss and an interdental brush in a single disposable pick. Cleans between teeth and along the gum line simultaneously.",
        ["Brand: OraTek", "Pack Size: 15 per pack", "Function: Flossing + interdental cleaning", "Colour: Blue"]
    ),
    "enrichment_comments": "[MATCH] OraTek 2-in-1, Blue variant.",
    "processing_status": "complete"
})
RESULTS.append({
    "sku": "480-110-003",
    "shopify_collection": "OraTek 2-in-1 Floss & Interdental Brush",
    "enriched_title": "2-in-1 Floss & Interdental Brush — White (15/pk)",
    "brand": "OraTek",
    "shopify_product_type": "Interdental Aid",
    "shopify_tags": "OraTek, Floss, Interdental Brush, 2-in-1, Oral Hygiene",
    "variant_1_name": "Colour",
    "variant_1_value": "White",
    "variant_2_name": None,
    "variant_2_value": None,
    "description_html": make_desc(
        "Dual-function oral hygiene tool combining dental floss and an interdental brush in a single disposable pick. Cleans between teeth and along the gum line simultaneously.",
        ["Brand: OraTek", "Pack Size: 15 per pack", "Function: Flossing + interdental cleaning", "Colour: White"]
    ),
    "enrichment_comments": "[MATCH] OraTek 2-in-1, White variant.",
    "processing_status": "complete"
})
RESULTS.append({
    "sku": "480-110-005",
    "shopify_collection": None,
    "enriched_title": "Bridge Aid Floss Threader",
    "brand": None,
    "shopify_product_type": "Interdental Aid",
    "shopify_tags": "Floss Threader, Bridge Aid, Oral Hygiene, Bridges, Braces",
    "variant_1_name": None,
    "variant_1_value": None,
    "variant_2_name": None,
    "variant_2_value": None,
    "description_html": make_desc(
        "Rigid-tip floss threader designed to guide dental floss under fixed bridges, orthodontic appliances, and implant restorations.",
        ["Use: Threading floss under bridges and appliances", "Application: Fixed bridges, implants, orthodontic wires", "Design: Rigid loop tip for easy threading"]
    ),
    "enrichment_comments": "[MATCH] Standalone floss threader.",
    "processing_status": "complete"
})

# ── 3. Integra-CP Implants (320-120-xxx) ────────────────────────────────────
integra_cp_items = [
    ("320-120-002", "3.0X6.0MM INTEGRA CP 2MM WELL",   "3.0", "6.0", "2.0"),
    ("320-120-003", "3.0X8.0MM INTEGRA CP 2MM WELL",   "3.0", "8.0", "2.0"),
    ("320-120-004", "3.5X8.0MM INTEGRA CP 2MM WELL",   "3.5", "8.0", "2.0"),
    ("320-120-005", "4.0X6.0MM INTEGRA CP 2.5MM WEL",  "4.0", "6.0", "2.5"),
    ("320-120-006", "4.0X8MM INTEGRA CP 2.5MM WELL",   "4.0", "8.0", "2.5"),
    ("320-120-008", "4.5X11MM INTEGRA CP 2.5MM WELL",  "4.5","11.0", "2.5"),
    ("320-120-009", "4.5X11MM INTEGRA CP 3MM WELL",    "4.5","11.0", "3.0"),
    ("320-120-010", "4.5X5.0MM INTEGRA CP 3MM WELL",   "4.5", "5.0", "3.0"),
    ("320-120-011", "4.5X6MM INTEGRA CP 3MM WELL",     "4.5", "6.0", "3.0"),
    ("320-120-012", "4.5X8MM INTEGRA CP 2.5MM WELL",   "4.5", "8.0", "2.5"),
    ("320-120-013", "4.5X8MM INTEGRA CP 3MM WELL",     "4.5", "8.0", "3.0"),
    ("320-120-014", "4X11MM INTEGRA CP 2.5MM WELL",    "4.0","11.0", "2.5"),
    ("320-120-015", "4X5MM INTEGRA CP 2.5MM WELL",     "4.0", "5.0", "2.5"),
    ("320-120-016", "5.0X11MM INTEGRA CP 3MM WELL",    "5.0","11.0", "3.0"),
    ("320-120-017", "5.0X5MM INTEGRA CP 3MM WELL",     "5.0", "5.0", "3.0"),
    ("320-120-018", "5.0X6MM INTEGRA CP 3MM WELL",     "5.0", "6.0", "3.0"),
    ("320-120-019", "5.0X8MM INTEGRA CP 3MM WELL",     "5.0", "8.0", "3.0"),
    ("320-120-020", "6.0X5.0MM INTEGRA CP 3MM WELL",   "6.0", "5.0", "3.0"),
    ("320-120-021", "6.0X6.0MM INTEGRA CP 3MM WELL",   "6.0", "6.0", "3.0"),
    ("320-120-022", "6.0X8MM INTEGRA CP 3MM WELL",     "6.0", "8.0", "3.0"),
]
for sku, name, d, l, w in integra_cp_items:
    d_f, l_f, w_f = float(d), float(l), float(w)
    d_str = f"{d_f:.1f}mm"
    l_str = f"{l_f:.0f}mm" if l_f == int(l_f) else f"{l_f}mm"
    w_str = f"{w_f:.1f}mm" if w_f != int(w_f) else f"{int(w_f)}mm"
    config = f"{l_str} — {w_str} Well"
    title = f"{d_str} x {l_str} Integra-CP™ Implant ({w_str} Well)"
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Integra-CP Implants",
        "enriched_title": title,
        "brand": "Bicon",
        "shopify_product_type": "Dental Implant",
        "shopify_tags": f"Bicon, Integra-CP, Dental Implant, {d_str}, Osseointegration, HA Coating, Implantology",
        "variant_1_name": "Diameter",
        "variant_1_value": d_str,
        "variant_2_name": "Length & Well",
        "variant_2_value": config,
        "description_html": make_desc(
            f"The Integra-CP™ is a root-form dental implant featuring a hydroxylapatite (HA) surface treatment for enhanced osseointegration. Designed for the Bicon system with a {w_str} internal well connection.",
            [f"Diameter: {d_str}", f"Length: {l_str}", f"Internal Well: {w_str}", "Surface: Hydroxylapatite (HA) coating", "System: Bicon Short Implant System", "Material: Surgical titanium"]
        ),
        "enrichment_comments": f"[MATCH] Integra-CP implant. Diameter {d_str}, Length {l_str}, Well {w_str}.",
        "processing_status": "complete"
    })

# ── 4. Permanent Abutments (320-140-xxx) ────────────────────────────────────
perm_abut_items = [
    ("320-140-001", "4.0", "10.0",  "0",  "2.0"),
    ("320-140-002", "4.0", "10.0", "15",  "2.0"),
    ("320-140-003", "4.0", "10.0",  "0",  "2.5"),
    ("320-140-004", "4.0", "10.0",  "0",  "3.0"),
    ("320-140-005", "4.0", "10.0", "15",  "2.5"),
    ("320-140-006", "4.0", "10.0", "15",  "3.0"),
    ("320-140-007", "4.0",  "6.5",  "0",  "2.0"),
    ("320-140-008", "4.0",  "6.5",  "0",  "3.0"),
    ("320-140-009", "4.0",  "6.5", "15",  "2.5"),
    ("320-140-010", "4.0",  "6.5", "15",  "2.0"),
    ("320-140-011", "4.0",  "6.5", "15",  "3.0"),
    ("320-140-012", "4.0",  "6.5", "25",  "2.5"),
    ("320-140-013", "5.0", "10.0",  "0",  "2.5"),
    ("320-140-014", "5.0", "10.0",  "0",  "3.0"),
    ("320-140-015", "5.0", "10.0", "15",  "2.5"),
    ("320-140-016", "5.0", "10.0", "15",  "3.0"),
    ("320-140-017", "5.0", "12.0",  "0",  "3.0"),
    ("320-140-018", "5.0", "12.0", "15",  "3.0"),
    ("320-140-019", "5.0",  "5.0",  "0",  "3.0"),
    ("320-140-020", "5.0",  "5.0", "15",  "3.0"),
    ("320-140-021", "5.0",  "6.5",  "0",  "2.5"),
    ("320-140-022", "5.0",  "6.5",  "0",  "3.0"),
    ("320-140-023", "5.0",  "6.5", "15",  "2.5"),
    ("320-140-024", "5.0",  "6.5", "15",  "3.0"),
    ("320-140-025", "5.0",  "6.5", "25",  "2.5"),
    ("320-140-026", "5.0",  "6.5", "25",  "3.0"),
    ("320-140-027", "6.5",  "5.0",  "0",  "3.0"),
    ("320-140-028", "6.5",  "5.0", "15",  "3.0"),
    ("320-140-029", "6.5",  "6.5",  "0",  "3.0"),
    ("320-140-030", "6.5",  "6.5", "15",  "3.0"),
    ("320-140-031", "7.5",  "8.0",  "0",  "3.0"),
    ("320-140-032", "7.5",  "8.0", "15",  "3.0"),
]
for sku, d, l, ang, w in perm_abut_items:
    d_f, l_f, w_f = float(d), float(l), float(w)
    d_str = f"{d_f:.1f}mm"
    l_str = f"{l_f:.0f}mm" if l_f == int(l_f) else f"{l_f}mm"
    w_str = f"{w_f:.1f}mm" if w_f != int(w_f) else f"{int(w_f)}mm"
    ang_str = f"{ang}°"
    config = f"{l_str} — {ang_str} — {w_str} Well"
    title = f"{d_str} x {l_str} Bicon Permanent Abutment — {ang_str} ({w_str} Well)"
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Bicon Permanent Abutments",
        "enriched_title": title,
        "brand": "Bicon",
        "shopify_product_type": "Implant Abutment",
        "shopify_tags": f"Bicon, Permanent Abutment, Implant Abutment, {d_str}, {ang_str}, Prosthetic, Implantology",
        "variant_1_name": "Diameter",
        "variant_1_value": d_str,
        "variant_2_name": "Config",
        "variant_2_value": config,
        "description_html": make_desc(
            f"Bicon permanent abutment for use with the Bicon Short Implant System. Features a {ang_str} angulation and {w_str} internal well connection for prosthetic restoration.",
            [f"Diameter: {d_str}", f"Height: {l_str}", f"Angulation: {ang_str}", f"Internal Well: {w_str}", "System: Bicon Short Implant", "Material: Titanium"]
        ),
        "enrichment_comments": f"[MATCH] Bicon permanent abutment. D={d_str}, L={l_str}, Angle={ang_str}, Well={w_str}.",
        "processing_status": "complete"
    })

# ── 5. BeeSure Floral Face Masks (200-150-xxx) ────────────────────────────────
mask_designs = {
    "200-150-001": "Daisy",
    "200-150-002": "Fern",
    "200-150-003": "Hibiscus",
    "200-150-004": "Orchid",
    "200-150-005": "Plume",
}
for sku, design in mask_designs.items():
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "BeeSure Floral Face Masks",
        "enriched_title": f"BeeSure Floral Face Mask — {design}",
        "brand": "BeeSure",
        "shopify_product_type": "Disposable Face Mask",
        "shopify_tags": "BeeSure, Face Mask, Floral, Disposable, Infection Control, PPE",
        "variant_1_name": "Design",
        "variant_1_value": design,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            f"BeeSure 3-ply disposable face mask with a decorative {design} floral print. Provides bacterial filtration for infection control in clinical settings while maintaining a professional aesthetic.",
            ["Brand: BeeSure", "Layers: 3-ply", "Type: Disposable, earloop", f"Design: {design} floral print", "Application: Infection control, clinical use"]
        ),
        "enrichment_comments": f"[MATCH] BeeSure floral mask, {design} design.",
        "processing_status": "complete"
    })

# ── 6. Brilliance Composites (520-160-xxx) ────────────────────────────────────
brilliance_ng = {
    "520-160-002": ("A1/B1",      "4g"),
    "520-160-003": ("A3.5/B3",    "4g"),
    "520-160-004": ("A3/D3",      "4g"),
    "520-160-005": ("Translucent","4g"),
    "520-160-010": ("A2/B2",      "4g"),
    "520-160-011": ("C2/C3",      "4g"),
}
brilliance_flow = {
    "520-160-006": ("A2/B2",   "2.3g"),
    "520-160-007": ("A3.5/B3", "2.3g"),
    "520-160-008": ("A3/D3",   "2.3g"),
    "520-160-009": ("A1/B1",   "2.3g"),
}
for sku, (shade, size) in brilliance_ng.items():
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Brilliance NG Enamel Composite",
        "enriched_title": f"Brilliance NG Enamel Composite — Shade {shade} ({size} Syringe)",
        "brand": "Coltene",
        "shopify_product_type": "Composite Resin",
        "shopify_tags": f"Coltene, Brilliance NG, Composite, Nano-glass, Shade {shade}, Restorative",
        "variant_1_name": "Shade",
        "variant_1_value": shade,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            "Brilliance NG (Nano-Glass) is a light-cured nano-glass hybrid composite resin by Coltene/Whaledent. Delivers excellent optical properties with high polishability for aesthetic restorations.",
            [f"Shade: {shade}", f"Syringe Size: {size}", "Type: Nano-glass hybrid composite", "Cure: Light-cured", "Brand: Coltene/Whaledent", "Application: Anterior and posterior restorations"]
        ),
        "enrichment_comments": f"[MATCH] Coltene Brilliance NG Enamel, shade {shade}, 4g syringe.",
        "processing_status": "complete"
    })
for sku, (shade, size) in brilliance_flow.items():
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Brilliance Flow Composite",
        "enriched_title": f"Brilliance Flow Composite — Shade {shade} ({size} Syringe)",
        "brand": "Coltene",
        "shopify_product_type": "Flowable Composite",
        "shopify_tags": f"Coltene, Brilliance Flow, Flowable Composite, Shade {shade}, Restorative",
        "variant_1_name": "Shade",
        "variant_1_value": shade,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            "Brilliance Flow is a light-cured flowable composite resin by Coltene/Whaledent. Low viscosity for easy placement in small cavities, pits, fissures, and as a liner under composite restorations.",
            [f"Shade: {shade}", f"Syringe Size: {size}", "Type: Flowable composite resin", "Cure: Light-cured", "Brand: Coltene/Whaledent", "Application: Small cavities, pit and fissure sealant liner"]
        ),
        "enrichment_comments": f"[MATCH] Coltene Brilliance Flow, shade {shade}, 2.3g syringe.",
        "processing_status": "complete"
    })

# ── 7. Temporary Abutments (320-160-xxx) ────────────────────────────────────
temp_abut_items = [
    ("320-160-001", "4.0", "4.5", "2.5"),
    ("320-160-002", "4.0", "4.5", "3.0"),
    ("320-160-003", "4.0", "6.5", "2.5"),
    ("320-160-007", "5.0", "4.5", "2.5"),
    ("320-160-008", "5.0", "4.5", "3.0"),
    ("320-160-009", "5.0", "6.5", "2.5"),
    ("320-160-010", "5.0", "6.5", "3.0"),
    ("320-160-011", "6.5", "4.5", "3.0"),
    ("320-160-012", "6.5", "6.5", "3.0"),
]
for sku, d, h, p in temp_abut_items:
    d_f, h_f, p_f = float(d), float(h), float(p)
    d_str = f"{d_f:.1f}mm"
    h_str = f"{h_f:.1f}mm"
    p_str = f"{p_f:.1f}mm" if p_f != int(p_f) else f"{int(p_f)}mm"
    config = f"{h_str} — {p_str} Post"
    title = f"{d_str} x {h_str} Bicon Temporary Abutment ({p_str} Post)"
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Bicon Temporary Abutments",
        "enriched_title": title,
        "brand": "Bicon",
        "shopify_product_type": "Implant Abutment",
        "shopify_tags": f"Bicon, Temporary Abutment, Implant, {d_str}, Provisional, Implantology",
        "variant_1_name": "Diameter",
        "variant_1_value": d_str,
        "variant_2_name": "Config",
        "variant_2_value": config,
        "description_html": make_desc(
            f"Bicon temporary abutment for provisional restorations during the healing phase. Compatible with the Bicon Short Implant System. Features a {p_str} post for chairside fabrication of provisional crowns.",
            [f"Implant Diameter: {d_str}", f"Abutment Height: {h_str}", f"Post Diameter: {p_str}", "Use: Provisional restoration", "System: Bicon Short Implant", "Material: Titanium"]
        ),
        "enrichment_comments": f"[MATCH] Bicon temp abutment. D={d_str}, H={h_str}, Post={p_str}.",
        "processing_status": "complete"
    })

# ── 8. BeeSure Latex Gloves (200-170-xxx) ────────────────────────────────────
glove_sizes = {
    "200-170-001": "Large",
    "200-170-002": "Medium",
}
for sku, size in glove_sizes.items():
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "BeeSure Latex Powder-Free Gloves",
        "enriched_title": f"BeeSure Latex Powder-Free Examination Gloves — {size} (100/box)",
        "brand": "BeeSure",
        "shopify_product_type": "Examination Gloves",
        "shopify_tags": "BeeSure, Latex Gloves, Powder-Free, Examination Gloves, PPE, Infection Control",
        "variant_1_name": "Size",
        "variant_1_value": size,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            f"BeeSure latex powder-free examination gloves in {size.lower()} size. Textured fingertips for enhanced grip and tactile sensitivity during clinical procedures.",
            ["Brand: BeeSure", "Material: Natural latex", "Powder: Powder-free", f"Size: {size}", "Pack Size: 100 gloves per box", "Application: Clinical examination and procedures"]
        ),
        "enrichment_comments": f"[MATCH] BeeSure latex powder-free gloves, size {size}.",
        "processing_status": "complete"
    })

# ── 9. Amalgam Carver (340-180-001) ─────────────────────────────────────────
RESULTS.append({
    "sku": "340-180-001",
    "shopify_collection": None,
    "enriched_title": "Amalgam Carver — Hollenback 3S",
    "brand": None,
    "shopify_product_type": "Restorative Instrument",
    "shopify_tags": "Amalgam Carver, Hollenback 3S, Restorative, Dental Instrument, Carver",
    "variant_1_name": None,
    "variant_1_value": None,
    "variant_2_name": None,
    "variant_2_value": None,
    "description_html": make_desc(
        "The Hollenback 3S is a double-ended amalgam carver used for contouring and carving amalgam restorations to anatomical form before setting. Features paired angled blades for mesial and distal surfaces.",
        ["Pattern: Hollenback 3S", "Ends: Double-ended", "Material: Stainless steel", "Application: Amalgam carving and contouring", "Use: Restorative dentistry"]
    ),
    "enrichment_comments": "[MATCH] Standalone Hollenback 3S amalgam carver. No size variants.",
    "processing_status": "complete"
})

# ── 10. Crosstex Patient Bibs (200-190-xxx) ──────────────────────────────────
bib_colors = {
    "200-190-002": "Blue",
    "200-190-003": "Lavender",
    "200-190-004": "Peach",
    "200-190-005": "Yellow",
}
for sku, color in bib_colors.items():
    RESULTS.append({
        "sku": sku,
        "shopify_collection": "Crosstex Patient Bibs",
        "enriched_title": f"Crosstex Patient Bibs — {color} (500/box)",
        "brand": "Crosstex",
        "shopify_product_type": "Patient Bib",
        "shopify_tags": "Crosstex, Patient Bibs, Disposable, Infection Control, PPE",
        "variant_1_name": "Colour",
        "variant_1_value": color,
        "variant_2_name": None,
        "variant_2_value": None,
        "description_html": make_desc(
            f"Crosstex 3-ply disposable patient bibs in {color.lower()}. Fluid-resistant barrier for patient protection during dental procedures. Tissue-poly-tissue construction.",
            ["Brand: Crosstex", f"Colour: {color}", "Layers: 3-ply (tissue-poly-tissue)", "Pack Size: 500 per box", "Application: Patient protection during procedures"]
        ),
        "enrichment_comments": f"[MATCH] Crosstex patient bibs, {color}, 500/box.",
        "processing_status": "complete"
    })

# ── Final: merge with existing output & write ────────────────────────────────
import os
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), '..', 'enrichment_output.json')
OUTPUT_FILE = os.path.normpath(OUTPUT_FILE)

existing = []
if os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE) as f:
        existing = json.load(f)

# Only keep existing items NOT in this batch (avoid duplicates)
new_skus = {r['sku'] for r in RESULTS}
kept_existing = [r for r in existing if r['sku'] not in new_skus]

final = kept_existing + RESULTS

with open(OUTPUT_FILE, 'w') as f:
    json.dump(final, f, indent=2)

print(f"Written {len(final)} items to enrichment_output.json")
print(f"  Batch 1 (new): {len(RESULTS)}")
print(f"  Kept existing: {len(kept_existing)}")
print()
print("SKU counts by collection:")
from collections import Counter
colls = Counter(r.get('shopify_collection') or 'STANDALONE' for r in RESULTS)
for coll, count in sorted(colls.items()):
    print(f"  {coll}: {count}")
