"""Reshape the data-center generator extraction into union-appendable rows.

`extract_dc_generators.py --finalize` writes dc_generator_units.csv, which is
the analysis view: Success rows only, no state backfill. The union wants the
same convention as every other lane -- "Success (No Units Found)" placeholders
kept (they still document a facility, which is what the ECHO coverage join
counts), failures dropped, and `Facility State Abbreviation` always populated
so the row lands in the right state.

The DC lane's job keys (`tx_4878594_Technical_Review`) share no filenames with
the union, so the rows append rather than merge; feed the output to
append_new_files_to_union.py.

Usage:
    python scripts/build_dc_generator_union_rows.py \
        [--out data/processed/dc_generators/dc_generator_union_rows.csv]
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)

RESULTS_JSONL = Path("data/processed/dc_generators/extraction_results.jsonl")
OUT_CSV = Path("data/processed/dc_generators/dc_generator_union_rows.csv")
KEEP_STATUSES = ("Success", "Success (No Units Found)")
STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR".split()
)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results-jsonl", type=Path, default=RESULTS_JSONL)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    args = ap.parse_args()

    rows, cols = [], []
    dropped, fixed_state = Counter(), Counter()
    seen_files = set()
    for line in args.results_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        lane = rec.get("state", "")
        for r in rec.get("rows", []):
            if r.get("Status") not in KEEP_STATUSES:
                dropped[r.get("Status")] += 1
                continue
            if not cols:
                cols = list(r.keys())
            st = (r.get("Facility State Abbreviation") or "").strip().upper()
            if st not in STATES:
                r["Facility State Abbreviation"] = lane.upper()
                fixed_state[lane] += 1
            r["source_state"] = lane
            r["source_job"] = rec.get("job", "")
            rows.append(r)
            seen_files.add(r.get("Filename"))

    out_cols = cols + ["source_state", "source_job"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    by_state = Counter(r["Facility State Abbreviation"] for r in rows)
    docs_by_state = Counter()
    for fn, st in {(r["Filename"], r["Facility State Abbreviation"]) for r in rows}:
        docs_by_state[st] += 1
    print(f"{len(rows):,} rows / {len(seen_files):,} documents -> {args.out}")
    print(f"dropped: {dict(dropped)}")
    print(f"state backfilled from lane: {dict(fixed_state)}")
    for st, n in by_state.most_common():
        print(f"  {st}: {n:,} rows / {docs_by_state[st]:,} docs")


if __name__ == "__main__":
    main()
