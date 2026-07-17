#!/usr/bin/env python3
"""
Bulk-tag expiring items from the team's sheet (approved by Leke 2026-07-17).

Input: a CSV with columns  sku, expiry_date  (header row required; extra
columns ignored; dates accepted as YYYY-MM-DD or DD/MM/YYYY). The team owns
the DATA; this script is data entry on their behalf — it writes ONLY
cf_is_batch_item=true and the sheet's cf_expiry_date, nothing inferred.

Validation before any write:
  - SKU exists in Zoho (active item)
  - date parses and is in the future (past dates are flagged, not written —
    an already-expired item needs a human decision, not a silent DRAFT pull)
  - duplicate SKUs in the sheet are rejected

Usage:
  python3 execution/tag_expiring_items.py sheet.csv            # validate only
  python3 execution/tag_expiring_items.py sheet.csv --write    # tag them
"""
import os
import sys
import csv
import time
import argparse
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import get_zoho_token, zoho_find_by_sku, zoho_write_fields


def parse_date(raw):
    raw = (raw or '').strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser(description='Bulk-tag expiring items from the team sheet')
    ap.add_argument('csv_file')
    ap.add_argument('--write', action='store_true', help='Write tags (default: validate only)')
    args = ap.parse_args()

    rows, seen, problems = [], set(), []
    with open(args.csv_file, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        sku_col = next((cols[c] for c in cols if 'sku' in c), None)
        date_col = next((cols[c] for c in cols if 'expir' in c or 'date' in c), None)
        if not sku_col or not date_col:
            sys.exit(f'Cannot find sku/expiry columns in header: {reader.fieldnames}')
        for i, row in enumerate(reader, start=2):
            sku = (row.get(sku_col) or '').strip()
            if not sku:
                continue
            d = parse_date(row.get(date_col))
            if sku in seen:
                problems.append(f'row {i}: duplicate SKU {sku}')
                continue
            seen.add(sku)
            if not d:
                problems.append(f'row {i}: {sku} unparseable date {row.get(date_col)!r}')
                continue
            if d <= date.today():
                problems.append(f'row {i}: {sku} expiry {d} is already past — needs a '
                                'human decision (dispose or confirm), not tagged')
                continue
            rows.append((sku, d))

    print(f'  Sheet: {len(rows)} taggable row(s), {len(problems)} problem(s)')
    for p in problems:
        print(f'    ⚠ {p}')

    token = get_zoho_token()
    ok_rows, missing = [], []
    for sku, d in rows:
        item = zoho_find_by_sku(sku, token)
        if not item:
            missing.append(sku)
            print(f'    ✗ {sku}: not found in Zoho')
        else:
            ok_rows.append((item, sku, d))
        time.sleep(0.25)

    print(f'  Validated: {len(ok_rows)} tag-ready, {len(missing)} not in Zoho')
    if not args.write:
        for item, sku, d in ok_rows[:15]:
            print(f'    would tag {sku}: expiry {d}')
        print('  (validate-only; re-run with --write to tag)')
        return

    tagged, failed = 0, 0
    for item, sku, d in ok_rows:
        ok, msg = zoho_write_fields(item['item_id'], {
            'cf_is_batch_item': True,
            'cf_expiry_date': d.isoformat(),
        }, token)
        if ok:
            tagged += 1
        else:
            failed += 1
            print(f'    ✗ {sku}: {msg}')
        time.sleep(0.3)

    print(f'\n  Tagged {tagged}, failed {failed}.')
    if tagged:
        print('  Markdown protection is live from the next reconcile pass.')
        print('  REMINDER: first tags = cutover day (hourly ops, then uncheck '
              'Sync Stock — see ops_split.md), and run cutover_batch_items.py.')


if __name__ == '__main__':
    main()
