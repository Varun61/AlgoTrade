#!/bin/bash
# scheduler/options_runner.sh
#
# Runs the paper-mode NIFTY iron-condor system ONCE. Does everything itself
# (collect real data + open the weekly condor + settle expired ones + print P&L).
# Idempotent and safe to run daily. NO live orders are placed.
#
# IMPORTANT — schedule this AFTER market close, ~15:35 IST (NOT in the morning:
# before the open, option prices are stale and expiry-day settlement would be
# wrong). Weekdays only.
#
# Cron (server in IST):
#   35 15 * * 1-5 /path/to/AlgoTrade/scheduler/options_runner.sh >> /path/to/AlgoTrade/logs/cron_options.log 2>&1
# (If your server is on UTC: 15:35 IST = 10:05 UTC ->  5 10 * * 1-5 ...)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
    source "$VENV_DIR/Scripts/activate"
else
    echo "ERROR: venv not found at $VENV_DIR"; exit 1
fi

cd "$PROJECT_DIR"
echo "=== Options paper runner: $(date) ==="
python -m tools.options_daily
echo "=== Done: $(date) ==="
