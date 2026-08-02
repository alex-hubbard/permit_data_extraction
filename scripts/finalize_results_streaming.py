"""Stream a results JSONL into a per-unit rows CSV.

Same output as batch_reextract.py --finalize, but never holds the rows in
memory: that version builds a list of every unit row and hands it to
pandas.DataFrame, which OOM-kills on large runs (the 28.5K-doc / 475K-unit
new-state batch died silently on a 7GB box).

Two passes: probe the first --probe records to fix the column set, then
stream every record straight to csv.writer. Last success record per filename
wins, matching the original's semantics.

Usage:
    PYTHONPATH=. python scripts/finalize_results_streaming.py RESULTS.jsonl [--out ROWS.csv]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.dataset import (  # noqa: E402
    GENERAL_TARGET_FIELDS,
    LLM_MODEL,
    UNIT_DETAIL_FIELDS,
    postprocess_extraction_row,
)


def rows_for(rec):
    extracted = rec["extraction"]
    general = {f: extracted.get(f) for f in GENERAL_TARGET_FIELDS}
    model_used = f"{rec.get('model', LLM_MODEL)} (forced-chunked)"
    units = [u for u in (extracted.get("Emission Units") or []) if isinstance(u, dict)]
    base = {"Filename": rec["filename"], "Model Used": model_used,
            "Pieces Failed": rec.get("pieces_failed", 0)}
    if not units:
        row = {**base, "Status": "Success (No Units Found)", **general}
        yield postprocess_extraction_row(row)
        return
    for unit in units:
        row = {**base, "Status": "Success", **general}
        for f in UNIT_DETAIL_FIELDS:
            row[f] = unit.get(f)
        yield postprocess_extraction_row(row)


def last_success_offsets(path):
    """filename -> byte offset of its last success record (single pass, no payloads held)."""
    offsets = {}
    with path.open("rb") as f:
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("extraction") is not None:
                offsets[rec["filename"]] = pos
    return offsets


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results_jsonl", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--probe", type=int, default=300)
    args = ap.parse_args()
    out = args.out or args.results_jsonl.with_name(args.results_jsonl.stem + "_rows.csv")

    offsets = last_success_offsets(args.results_jsonl)
    print(f"{len(offsets):,} files with successful extractions", flush=True)

    fields = []
    seen_fields = set()
    with args.results_jsonl.open("rb") as f:
        for i, pos in enumerate(list(offsets.values())[: args.probe]):
            f.seek(pos)
            for row in rows_for(json.loads(f.readline())):
                for k in row:
                    if k not in seen_fields:
                        seen_fields.add(k)
                        fields.append(k)
    print(f"{len(fields)} columns from {args.probe}-record probe", flush=True)

    n_rows = n_files = 0
    extra = set()
    with args.results_jsonl.open("rb") as f, out.open("w", newline="", encoding="utf-8") as o:
        w = csv.DictWriter(o, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for pos in offsets.values():
            f.seek(pos)
            rec = json.loads(f.readline())
            for row in rows_for(rec):
                extra |= set(row) - seen_fields
                w.writerow(row)
                n_rows += 1
            n_files += 1
            if n_files % 2000 == 0:
                print(f"  {n_files:,} files / {n_rows:,} rows", flush=True)
    if extra:
        print(f"WARNING: {len(extra)} column(s) seen after the probe were dropped: "
              f"{sorted(extra)[:8]}")
    print(f"Finalized {n_files:,} files -> {n_rows:,} rows -> {out}")


if __name__ == "__main__":
    main()
