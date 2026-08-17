#!/usr/bin/env bash
# Launch the permit dashboard locally on the standard port.
#
# PERMIT_DASHBOARD_S3_URI is cleared so the app reads the local parquet
# (data/processed/dashboard/permits.parquet) even if an S3 URI is configured.
# First load takes ~15 s; the first visit to each subsector builds its cache
# (a few seconds) and everything after is sub-second — pre-warm the views you
# plan to demo.
#
# Usage: scripts/run_dashboard.sh [port]   (default 8501)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8501}"
exec env PERMIT_DASHBOARD_S3_URI= streamlit run dashboards/manufacturing_subsector.py \
    --server.port "$PORT" \
    --browser.gatherUsageStats false
