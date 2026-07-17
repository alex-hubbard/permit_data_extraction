#!/usr/bin/env bash
# Free-model (lbl/gemma-4-thinking) re-extraction of union files >=100K chars
# that were never covered by the 2026-07 gemini recall batch.
#
# Resumable: progress checkpoints to freemodel_results.jsonl; rerun this script
# after any interruption and it picks up where it left off. A lockfile prevents
# accidental double-launch.
#
#   nohup bash scripts/run_freemodel_reextract.sh >> data/processed/reextraction/freemodel_run.log 2>&1 &
#
# Stops on its own when the list is complete, or after 3 consecutive attempts
# with zero new completions (dead endpoint / expired auth — fix, then rerun).
set -u
cd "$(dirname "$0")/.."

LIST=data/processed/reextraction/freemodel_list.json
RESULTS=data/processed/reextraction/freemodel_results.jsonl
LOCK=data/processed/reextraction/freemodel.lock

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "Another run is already active (lock: $LOCK). Exiting."
    exit 0
fi

export PYTHONPATH=.
export PYTHONUNBUFFERED=1  # completion lines reach the log immediately
export LLM_MODEL=lbl/gemma-4-thinking
export LLM_LARGE_MODEL=lbl/gemma-4-thinking   # never fall back to a paid model
export LLM_MAX_OUTPUT_TOKENS=65536
export LLM_TIMEOUT_SECONDS=480
export LLM_HARD_TIMEOUT_SECONDS=540
WORKERS="${WORKERS:-8}"

remaining() {
    python3 - "$LIST" "$RESULTS" <<'EOF'
import json, sys
listed = set(json.load(open(sys.argv[1])))
done = set()
try:
    for line in open(sys.argv[2]):
        try:
            rec = json.loads(line)
            if "extraction" in rec:
                done.add(rec["filename"])
        except json.JSONDecodeError:
            pass
except FileNotFoundError:
    pass
print(len(listed - done))
EOF
}

# Exit codes: 0 = list complete (or another instance holds the lock);
# 1 = gave up after consecutive no-progress attempts (systemd on-failure
# restarts us after a cool-down, resuming when the outage clears).
gave_up=0
no_progress=0
while true; do
    before=$(remaining)
    if [ "$before" -eq 0 ]; then
        echo "=== list complete ==="
        break
    fi
    echo "=== $(date -Is) attempt start: $before files remaining, workers=$WORKERS ==="
    python3 scripts/batch_reextract.py "$LIST" --workers "$WORKERS" --results-jsonl "$RESULTS"
    after=$(remaining)
    echo "=== $(date -Is) attempt end: $after files remaining ==="
    if [ "$after" -eq 0 ]; then
        break
    fi
    if [ "$after" -ge "$before" ]; then
        no_progress=$((no_progress + 1))
        if [ "$no_progress" -ge 3 ]; then
            echo "=== no progress in 3 consecutive attempts ($after remaining) — giving up; fix and rerun ==="
            gave_up=1
            break
        fi
    else
        no_progress=0
    fi
    sleep 120
done

echo "=== finalizing rows CSV ==="
python3 scripts/batch_reextract.py --finalize --results-jsonl "$RESULTS"
echo "=== done: $(remaining) files without a success record ==="
exit "$gave_up"
