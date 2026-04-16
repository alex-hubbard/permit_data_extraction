#!/usr/bin/env python3
"""
Recovery script for the corrupted run of 2026-04-10 (14:46 - 22:50).

The run file (permit_data_2026-04-10_14-46-20_v0-0-1.xlsx) is corrupted —
the Excel write was interrupted, leaving a 2.3KB file with no worksheet data.
10,230 files were successfully extracted by the LLM but the results were lost.

This script removes those entries from the processing log so they can be
reprocessed on the next run.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "interim" / "processed_files_log.jsonl"
CORRUPTED_RUN_START = "2026-04-10T14:46"
CORRUPTED_RUN_END = "2026-04-10T22:51"


def main():
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f]

    total_before = len(entries)

    # Identify entries from the corrupted run
    corrupted = [
        e for e in entries
        if CORRUPTED_RUN_START <= e["timestamp"] <= CORRUPTED_RUN_END
    ]
    corrupted_success = [e for e in corrupted if e["status"] == "success"]

    print(f"Total log entries: {total_before}")
    print(f"Corrupted run entries: {len(corrupted)} ({len(corrupted_success)} success, {len(corrupted) - len(corrupted_success)} failed)")

    if not corrupted:
        print("No entries found for the corrupted run. Nothing to do.")
        return

    # Back up the log before modifying
    backup_path = LOG_PATH.with_suffix(f".jsonl.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(LOG_PATH, backup_path)
    print(f"Backed up log to: {backup_path}")

    # Remove only the corrupted run's SUCCESS entries (keep failed ones so they
    # aren't retried unless --retry-failed is used)
    corrupted_success_keys = {
        (e["filename"], e["timestamp"]) for e in corrupted_success
    }
    kept = [
        e for e in entries
        if (e["filename"], e["timestamp"]) not in corrupted_success_keys
    ]

    with open(LOG_PATH, "w", encoding="utf-8") as f:
        for entry in kept:
            f.write(json.dumps(entry) + "\n")

    print(f"Removed {len(corrupted_success)} success entries from corrupted run")
    print(f"Log now has {len(kept)} entries (was {total_before})")
    print(f"\nThose {len(corrupted_success)} files will be reprocessed on the next run.")


if __name__ == "__main__":
    main()
