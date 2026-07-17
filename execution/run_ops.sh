#!/bin/bash
# Stage 6 (ops half) — headless reconcile: price + stock drift, expiry markdowns,
# batch caps, short-dated tags, expired pulls, cutover flags.
#
# The commerce EXECUTOR. Called from three places (2026-07-17 design):
#   - launchd com.snl.ds-ops, 13:07 daily, NO app and NO model
#   - "DS maintenance - Catalogue" (07:07) as its closing pass, because its syncs
#     can wipe markdowns/caps and this restores them
#   - "DS maintenance - Commerce" (13:37) as a fallback when the 13:07 run is dead
# Deterministic work only; judgment categories stay in the report for the
# Commerce routine to analyze. Reports accumulate in ops_reports/; the reconciler
# refuses mass drift (--max-fixes, exit 2).

set -uo pipefail

PROJECT_DIR="/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho"
PYTHON="/usr/local/bin/python3"
STAMP="$(date +%Y-%m-%d_%H%M)"
TODAY="$(date +%Y-%m-%d)"

REPORT_DIR="$PROJECT_DIR/ops_reports"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$REPORT_DIR" "$LOG_DIR"

REPORT="$REPORT_DIR/ops_${STAMP}.json"
LOG="$LOG_DIR/ops_${TODAY}.log"

{
  echo "════════════════════════════════════════════════════════════"
  echo "  ds-ops run — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "════════════════════════════════════════════════════════════"
} >> "$LOG"

# --fix is approved standing (Leke, 2026-07-17): Zoho is the source of truth for
# price and stock. The reconciler self-limits — it refuses to mass-fix when the
# drift count looks like an upstream break.
"$PYTHON" -u "$PROJECT_DIR/execution/reconcile_published.py" --fix --json "$REPORT" >> "$LOG" 2>&1
EXIT=$?

if [ $EXIT -ne 0 ]; then
  echo "  ✗ reconciler exited $EXIT — report may be absent or partial" >> "$LOG"
fi

# Canonical "latest" copy, same path the interactive tooling already reads.
if [ -f "$REPORT" ]; then
  cp "$REPORT" "$PROJECT_DIR/.reconcile_report.json"
fi

# Keep a month of history; these are small JSON files but they accumulate daily.
find "$REPORT_DIR" -name 'ops_*.json' -mtime +30 -delete 2>/dev/null
find "$LOG_DIR" -name 'ops_*.log' -mtime +30 -delete 2>/dev/null

echo "  ds-ops done (exit $EXIT) — report: $REPORT" >> "$LOG"
exit $EXIT
