#!/bin/bash
# scheduler/daily_runner.sh
#
# Entry point for systemd or cron.
# Activates venv, runs the algo, handles log rotation.
#
# Cron entry (run at 8:00 AM IST on weekdays, giving main.py's warm-up
# plenty of runway before the 09:15 market open):
#   0 8 * * 1-5 /path/to/angel-algo/scheduler/daily_runner.sh >> /var/log/angel-algo/cron.log 2>&1
#   (8:00 IST = 2:30 UTC, adjust for your server timezone)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

echo "=== Angel Algo Daily Runner: $(date) ==="
echo "Project: $PROJECT_DIR"

# Activate virtual environment
if [[ -f "$VENV_DIR/bin/activate" ]]; then
    source "$VENV_DIR/bin/activate"
elif [[ -f "$VENV_DIR/Scripts/activate" ]]; then
    # Windows Git Bash / WSL
    source "$VENV_DIR/Scripts/activate"
else
    echo "ERROR: Virtual environment not found at $VENV_DIR"
    echo "Create it with: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# No pre-market sleep here — main.py's own login/instrument-master download/
# warm-up (historical data fetch for the whole watchlist) should start as soon
# as cron/systemd triggers this script, so it has maximum runway to finish
# before the 09:15 market open. Actual trading is separately gated inside
# main.py (market_open / new_entry_cutoff_time in settings.yaml).

# Remove previous day's kill switch if present
rm -f "$PROJECT_DIR/.killswitch"

# Start the algo
cd "$PROJECT_DIR"
echo "Starting main.py at $(date) ..."
python main.py 2>&1 | tee -a "$LOG_DIR/algo_$(date +%Y%m%d).log"

echo "=== Daily run complete: $(date) ==="
