#!/bin/bash
# scheduler/daily_runner.sh
#
# Entry point for systemd or cron.
# Activates venv, runs the algo, handles log rotation.
#
# Cron entry (run at 8:55 AM IST on weekdays):
#   55 3 * * 1-5 /path/to/angel-algo/scheduler/daily_runner.sh >> /var/log/angel-algo/cron.log 2>&1
#   (8:55 IST = 3:25 UTC, adjust for your server timezone)

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

# Wait until close to market open (safety buffer)
echo "Waiting for 9:00 AM IST ..."
python - <<'PYEOF'
from datetime import datetime
import time, pytz

ist = pytz.timezone("Asia/Kolkata")
now = datetime.now(ist)
target = now.replace(hour=9, minute=0, second=0, microsecond=0)
if now < target:
    wait = (target - now).total_seconds()
    print(f"  Sleeping {wait:.0f}s until market open ...")
    time.sleep(max(0, wait - 5))
PYEOF

# Remove previous day's kill switch if present
rm -f "$PROJECT_DIR/.killswitch"

# Start the algo
cd "$PROJECT_DIR"
echo "Starting main.py at $(date) ..."
python main.py 2>&1 | tee -a "$LOG_DIR/algo_$(date +%Y%m%d).log"

echo "=== Daily run complete: $(date) ==="
