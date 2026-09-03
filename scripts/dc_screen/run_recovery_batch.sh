#!/bin/bash
# Recovery batch: build Vertex inputs for the lost/never-extracted docs, submit,
# poll until the job finishes, then merge outputs and finalize rows CSV.
set -u
cd /home/afhubbard/permit_data_extraction
export PYTHONPATH=.
LANE=${1:-data/processed/gcp_batch/recovery_lane.csv}
NAME=$(basename "$LANE" .csv | sed 's/_lane$//')
IN=data/processed/gcp_batch/${NAME}_inputs.jsonl
RES=data/processed/gcp_batch/${NAME}_results.jsonl
echo "[$(date)] build $LANE -> $IN"
python scripts/build_vertex_batch_inputs.py build "$LANE" --out "$IN" ${TEXT_DIR:+--text-dir "$TEXT_DIR"} || exit 1
echo "[$(date)] submit"
SUB=$(python scripts/build_vertex_batch_inputs.py submit "$IN") || exit 1
echo "$SUB"
JOB=$(echo "$SUB" | sed -n 's/^submitted: //p')
OUTP=$(echo "$SUB" | sed -n 's/^  out: //p')
echo "job=$JOB out=$OUTP"
for i in $(seq 1 240); do
  sleep 120
  ST=$(python scripts/build_vertex_batch_inputs.py status "$JOB" 2>/dev/null | awk '{print $NF}')
  echo "[$(date)] $ST"
  case "$ST" in
    *SUCCEEDED*) break;;
    *FAILED*|*CANCELLED*|*EXPIRED*) echo "job ended: $ST"; exit 2;;
  esac
done
echo "[$(date)] merge outputs"
python scripts/merge_vertex_batch_outputs.py "$OUTP" --results-jsonl "$RES" || exit 3
echo "[$(date)] finalize rows"
python scripts/batch_reextract.py --finalize --results-jsonl "$RES" || exit 4
echo "[$(date)] DONE rows: ${RES%.jsonl}_rows.csv"
