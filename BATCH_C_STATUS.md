# Batch C Status — 2026-03-29

## What's Done
- **enrichment_input.json** rebuilt from inventory with proper old+new SKU exclusions (30 clean items)
- **enrichment_output.json** AI-enriched (30 items, all `[INFERRED]`, no conflicts)
- **run_batch_c.sh** ready-to-run script for Stages 2–5

## The 30 Items
All from **160-Burs** parent category:
- 6x SS White Two Striper (DD) — FG/RA carbide burs
- 1x SS White Two Striper (Pearson) — 799.11VF very fine
- 14x SS White Two Striper — numbered series (283–790 + L1250)
- 1x SS White Two Striper Short-Cut Diamond (285.5)
- 2x SS White Generic Carbide (FG #558, GW2)
- 1x SS White Surgical Carbide HP (HP2)
- 1x Carbide Bur FG #47 Extra Fine
- 1x Diatech FG Diamond Bur 368 (5-pack)

## Key Finding: Old-SKU Collision Issue
11 items in the first batch candidate list were ALREADY on Shopify under OLD SKUs
(e.g., old SKU 160-140-001 = new SKU 240-250-001 for "11MM Abutment Cutting Burs").
These were correctly removed from Batch C. They may need a separate **SKU migration** 
pass to update their Shopify SKUs from old format (160-xxx) to new format.

## To Run
```bash
cd ~/Antigravity/SNL/Dental\ Solutions/Zoho
bash run_batch_c.sh
```
Each stage pauses for confirmation before proceeding.
