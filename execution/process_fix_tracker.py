#!/usr/bin/env python3
"""
Fix Tracker action executor — deterministic layer for the
"DS - Zoho - Shopify - Fix" Google Sheet workflow.

The orchestrator (Claude) reads the sheet, diagnoses each Open/Reopened
issue, and translates it into an actions file. This script executes those
actions safely — with the write-safety allowlist enforced — and emits a
results file the orchestrator writes back to the sheet.

Actions file format (JSON list):
[
  {"sku": "600-150-003", "action": "diagnose"},
  {"sku": "600-150-003", "action": "reset_for_refetch"},
  {"sku": "380-120-007", "action": "set_status", "status": "Image required"},
  {"sku": "200-110-022", "action": "write_fields",
     "fields": {"cf_enriched_title": "..."}},
  {"sku": "280-120-016", "action": "queue_upload"},
  {"skus": ["A","B"],    "action": "build_batch_files"}
]

Action reference:
  diagnose            — report status, image presence, source URL, brand (read-only)
  reset_for_refetch   — delete Zoho image (mandatory before replacement), status → Image required
  set_status          — status → given value
  write_fields        — write allowlisted custom fields
  queue_upload        — status → Queue for Upload
  build_batch_files   — write enrichment_input.json / enrichment_output.json for
                        the given SKUs using live Zoho data (native brand field,
                        enriched cf_* fields) — replaces ad-hoc rebuild scripts

Usage:
    python execution/process_fix_tracker.py actions.json
    python execution/process_fix_tracker.py actions.json --results results.json
"""

import sys
import os
import json
import time
import argparse
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (PROJECT_DIR, get_zoho_token, zoho_item_by_sku,
                    zoho_write_fields, zoho_delete_image)

VALID_STATUSES = {
    'Queue for Enrichment', 'Enrichment Complete', 'Image required',
    'Image Validated', 'Queue for Upload', 'Published', 'Needs Review',
    'Error uploading', 'Update Required',
}


def act_diagnose(item, token, act):
    cf = item['cf']
    return {
        'status':     cf.get('cf_shopify_status', ''),
        'has_image':  bool(item.get('image_name') or item.get('image_document_id')),
        'source_url': cf.get('cf_source_url', ''),
        'brand':      item.get('brand', ''),
        'enriched':   cf.get('cf_enriched_title', ''),
        'collection': cf.get('cf_shopify_collection', ''),
    }


def act_reset_for_refetch(item, token, act):
    # Image replacement rule: ALWAYS delete before any new upload.
    ok, msg = zoho_delete_image(item['item_id'], token)
    if not ok:
        raise Exception(f'Image DELETE failed — aborting per replacement rule: {msg}')
    ok2, msg2 = zoho_write_fields(item['item_id'],
                                  {'cf_shopify_status': 'Image required'}, token)
    if not ok2:
        raise Exception(f'Status reset failed: {msg2}')
    return {'image': msg, 'status': 'Image required'}


def act_set_status(item, token, act):
    status = act.get('status', '')
    if status not in VALID_STATUSES:
        raise Exception(f'Invalid status {status!r}')
    ok, msg = zoho_write_fields(item['item_id'], {'cf_shopify_status': status}, token)
    if not ok:
        raise Exception(f'Status write failed: {msg}')
    return {'status': status}


def act_write_fields(item, token, act):
    fields = act.get('fields', {})
    if not fields:
        raise Exception('write_fields requires a non-empty "fields" object')
    ok, msg = zoho_write_fields(item['item_id'], fields, token)  # allowlist enforced in common
    if not ok:
        raise Exception(f'Field write failed: {msg}')
    return {'written': sorted(fields.keys())}


def act_queue_upload(item, token, act):
    return act_set_status(item, token, {'status': 'Queue for Upload'})


ACTIONS = {
    'diagnose':          act_diagnose,
    'reset_for_refetch': act_reset_for_refetch,
    'set_status':        act_set_status,
    'write_fields':      act_write_fields,
    'queue_upload':      act_queue_upload,
}


def build_batch_files(skus, token):
    """Write enrichment_input.json + enrichment_output.json from live Zoho data."""
    ei, eo, missing = [], [], []
    for sku in skus:
        item = zoho_item_by_sku(sku, token)
        if not item:
            missing.append(sku)
            continue
        cf = item['cf']
        ei.append({
            'item_id':  item['item_id'],
            'sku':      sku,
            'name':     item.get('name', ''),
            'brand':    item.get('brand', ''),          # native field — 88% populated
            'category': item.get('category_name', '') or cf.get('cf_shopify_collection', ''),
            'rate':     item.get('rate', 0),
        })
        eo.append({
            'sku':                sku,
            'enriched_title':     cf.get('cf_enriched_title', ''),
            'shopify_collection': cf.get('cf_shopify_collection', ''),
            'variant_1_name':     cf.get('cf_shopify_var_1_name', ''),
            'variant_1_value':    cf.get('cf_shopify_var_1_value', ''),
        })
        time.sleep(0.3)

    with open(os.path.join(PROJECT_DIR, 'enrichment_input.json'), 'w') as f:
        json.dump(ei, f, indent=2)
    with open(os.path.join(PROJECT_DIR, 'enrichment_output.json'), 'w') as f:
        json.dump(eo, f, indent=2)
    return {'items': len(ei), 'missing': missing}


def main():
    parser = argparse.ArgumentParser(description='Fix Tracker action executor')
    parser.add_argument('actions_file', help='JSON list of actions')
    parser.add_argument('--results', default='fix_tracker_results.json',
                        help='Where to write the results JSON')
    args = parser.parse_args()

    with open(args.actions_file) as f:
        actions = json.load(f)

    token = get_zoho_token()
    today = date.today().isoformat()
    results = []

    print('═' * 60)
    print(f'  Fix Tracker Executor — {len(actions)} action(s)')
    print('═' * 60)

    for act in actions:
        name = act.get('action', '')
        try:
            if name == 'build_batch_files':
                out = build_batch_files(act.get('skus', []), token)
                print(f'  ✓ build_batch_files  {out}')
                results.append({'action': name, 'ok': True, 'result': out})
                continue

            sku = act.get('sku', '')
            if name not in ACTIONS:
                raise Exception(f'Unknown action {name!r}')
            item = zoho_item_by_sku(sku, token)
            if not item:
                raise Exception('SKU not found in Zoho')

            out = ACTIONS[name](item, token, act)
            print(f'  ✓ {sku:<15} {name:<18} {out}')
            results.append({'sku': sku, 'action': name, 'ok': True, 'result': out})
        except Exception as e:
            sku = act.get('sku', '?')
            print(f'  ✗ {sku:<15} {name:<18} {e}')
            results.append({'sku': sku, 'action': name, 'ok': False, 'error': str(e)})
        time.sleep(0.3)

    with open(args.results, 'w') as f:
        json.dump({'date': today, 'results': results}, f, indent=2)

    ok_n = sum(1 for r in results if r['ok'])
    print()
    print('═' * 60)
    print(f'  {ok_n}/{len(results)} action(s) succeeded → {args.results}')
    print('═' * 60)
    sys.exit(0 if ok_n == len(results) else 1)


if __name__ == '__main__':
    main()
