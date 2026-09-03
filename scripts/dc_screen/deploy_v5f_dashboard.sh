#!/bin/bash
# Wait for the v5f dashboard parquet, swap it in, deploy to Cloud Run.
cd /home/afhubbard/permit_data_extraction
until grep -qE "wrote|Traceback|Error" data/processed/dashboard/build_v5f.log; do sleep 15; done
grep -qE "Traceback|Error" data/processed/dashboard/build_v5f.log && { echo "parquet build failed"; exit 1; }
cp data/processed/dashboard/permits_v5f.parquet data/processed/dashboard/permits.parquet
echo "[$(date)] permits.parquet <- v5f ($(stat -c %s data/processed/dashboard/permits.parquet) bytes); deploying"
scripts/deploy_dashboard_gcp.sh permit-dashboard
echo "[$(date)] DEPLOY EXIT $?"
