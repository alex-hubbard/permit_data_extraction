#!/usr/bin/env bash
# Deploy the permit dashboard to Cloud Run.
#
# Assembles a minimal build context (app module + the two parquet files baked
# into the image, so boot needs no bucket access), then builds and deploys via
# Cloud Build. Cold starts are accepted (min-instances=0): idle cost is ~$0
# and the first visitor waits ~20-30 s while the frame loads.
#
# Usage: scripts/deploy_dashboard_gcp.sh [service-name]   (default permit-dashboard)
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${1:-permit-dashboard}"
REGION="us-central1"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/dashboards" "$STAGE/data/processed/dashboard" "$STAGE/.streamlit"
cp dashboards/manufacturing_subsector.py "$STAGE/dashboards/"
cp data/processed/dashboard/permits.parquet "$STAGE/data/processed/dashboard/"
cp data/processed/dashboard/city_centroids.parquet "$STAGE/data/processed/dashboard/"

cat > "$STAGE/requirements.txt" <<'EOF'
streamlit==1.43.0
pandas==2.1.4
pyarrow==14.0.2
plotly==6.0.0
EOF

cat > "$STAGE/.streamlit/config.toml" <<'EOF'
[browser]
gatherUsageStats = false
[server]
headless = true
enableXsrfProtection = true
EOF
# empty secrets file: silences Streamlit's "No secrets found" probe error;
# the app then falls back to the baked-in local parquet.
: > "$STAGE/.streamlit/secrets.toml"

cat > "$STAGE/Dockerfile" <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# shell form so $PORT expands (Cloud Run sets it)
CMD streamlit run dashboards/manufacturing_subsector.py \
    --server.port=$PORT --server.address=0.0.0.0
EOF

gcloud run deploy "$SERVICE" \
    --source "$STAGE" \
    --region "$REGION" \
    --memory 4Gi --cpu 2 \
    --timeout 3600 \
    --session-affinity \
    --allow-unauthenticated \
    --max-instances 2 \
    --quiet

gcloud run services describe "$SERVICE" --region "$REGION" \
    --format='value(status.url)'
