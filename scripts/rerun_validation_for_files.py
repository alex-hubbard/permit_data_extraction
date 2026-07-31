"""Re-run multi-model validation for a specific list of permit text files.

Used to recover permits lost to infrastructure failures (e.g., expired IP
authorization) in a prior validation run, keeping the same panel via env vars:

    LLM_MODEL=gemma-4 VALIDATION_MODEL_OPENAI=gemini-2.5-flash \
    VALIDATION_MODEL_ANTHROPIC=google/glm-5 LLM_TIMEOUT_SECONDS=300 \
    python scripts/rerun_validation_for_files.py data/processed/validation/gemma4_rerun_list.json

Writes a normal timestamped validation_run_*.jsonl/.md/.xlsx set.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from permit_data_extraction.dataset import (
    TEXT_INPUT_DIR,
    VALIDATION_MODELS,
    _ensure_validation_output_dir,
    append_validation_record,
    configure_llm,
    get_validation_run_path,
    validate_permit,
    write_validation_report_excel,
    write_validation_report_md,
    write_validation_run_header,
)


def main():
    list_path = Path(sys.argv[1])
    filenames = json.loads(list_path.read_text())
    paths = []
    for fn in filenames:
        p = TEXT_INPUT_DIR / f"{fn}.txt"
        if p.exists():
            paths.append(p)
        else:
            print(f"WARNING: missing text file, skipping: {fn}")

    client = configure_llm()
    if not client:
        sys.exit("LLM configuration failed")

    _ensure_validation_output_dir()
    run_path = get_validation_run_path()
    run_id = run_path.stem
    timestamp = datetime.now().isoformat()
    print(f"Re-validating {len(paths)} permits with panel {VALIDATION_MODELS}")

    success = review = 0
    records = []
    with open(run_path, "w", encoding="utf-8") as f:
        write_validation_run_header(f, run_id, timestamp, len(filenames), len(paths), VALIDATION_MODELS, TEXT_INPUT_DIR)
        for i, p in enumerate(paths, 1):
            print(f"[{i}/{len(paths)}] {p.name}")
            rec = validate_permit(p, client, VALIDATION_MODELS)
            records.append(rec)
            append_validation_record(f, rec, run_id, timestamp)
            success += rec.get("status") == "SUCCESS"
            review += rec.get("status") != "SUCCESS"

    write_validation_report_md(run_path, records, run_id, timestamp, len(filenames), len(paths), VALIDATION_MODELS, success, review)
    write_validation_report_excel(run_path, records, run_id, timestamp, len(filenames), len(paths), VALIDATION_MODELS, success, review)
    print(f"Done: SUCCESS={success}, REVIEW_REQUIRED={review}; output: {run_path}")


if __name__ == "__main__":
    main()
