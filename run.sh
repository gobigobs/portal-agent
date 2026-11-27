#!/usr/bin/env bash
# portal-agent demo: start the demo portal, run the agent, stop the portal.
# One command:  ./run.sh
# Produces: output/records.json, output/orders.csv, output/payouts.csv,
#           output/run_log.json, screenshots/*.png
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORTAL_PORT:-8765}"
BASE_URL="http://127.0.0.1:${PORT}"

# 1. Python venv (inside the repo; never touches system Python).
if [ ! -d .venv ]; then
  echo "[run] Creating venv (.venv)..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt

# 2. Chromium for Playwright (skipped if a usable build is already there).
if ! python -c "
import glob, os
from playwright.sync_api import sync_playwright
ok = bool(glob.glob(os.path.expanduser('~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')))
print('chromium cached:', ok)
raise SystemExit(0 if ok else 1)
" 2>/dev/null; then
  echo "[run] Installing Chromium for Playwright (one-time download)..."
  python -m playwright install chromium
fi

# 3. Start the demo portal in the background.
echo "[run] Starting demo portal on ${BASE_URL} ..."
python -m portal.server --port "${PORT}" > portal.log 2>&1 &
PORTAL_PID=$!
trap 'kill ${PORTAL_PID} 2>/dev/null || true' EXIT

# Wait for the portal to answer (max ~20s).
for _ in $(seq 1 40); do
  if curl -fsS "${BASE_URL}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
curl -fsS "${BASE_URL}/api/health" >/dev/null \
  || { echo "[run] ERROR: portal did not start. See portal.log"; exit 1; }

# 4. Run the agent (login -> API orders -> API payouts 500 -> browser
#    fallback for payouts -> browser full orders list -> outputs).
echo "[run] Running agent..."
python -m agent.agent --base-url "${BASE_URL}"

# 5. Stop the portal (trap also handles this on failure).
kill "${PORTAL_PID}" 2>/dev/null || true
trap - EXIT

echo ""
echo "[run] Demo complete."
echo "  Records:    output/records.json"
echo "  CSVs:       output/orders.csv  output/payouts.csv"
echo "  Step log:   output/run_log.json"
echo "  Screenshots: screenshots/ (7 PNGs, one per step)"
