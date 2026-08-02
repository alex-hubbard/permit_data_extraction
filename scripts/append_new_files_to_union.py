"""Append rows for brand-new files to a union CSV, streaming.

merge_freemodel_into_union.py does row-level merge-not-replace and needs both
sides in memory (pandas) — that OOMs once the union passes ~600K rows on a
small box. When every incoming file is NEW to the union (a fresh-corpus
extraction rather than a re-extraction), no row-level merge is required: the
rows just append. This does that with csv streaming, constant memory.

Files already present in the union are SKIPPED and reported — send those
through merge_freemodel_into_union.py instead, where the unit-key merge logic
protects existing rows.

Usage:
    PYTHONPATH=. python scripts/append_new_files_to_union.py \
        --union-in data/processed/permit_data_union_v4.csv \
        --union-out data/processed/permit_data_union_v5.csv \
        --rows-csv data/processed/gcp_batch/newstate_results_rows.csv \
        --source-run gcp_newstate_2026-08 --processing-date 2026-08-01
"""
import argparse
import csv
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

RENAME = {
    "Operating Hours Value": "Operating Hours — numeric/value portion",
    "Operating Hours Time Basis": "Operating Hours — unit or basis (e.g. hours/year)",
    "Annual Run Hours Value": "Annual Run Hours — numeric/value portion",
    "Annual Run Hours Time Basis": "Annual Run Hours — unit or basis (e.g. hours/year)",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union-in", type=Path, required=True)
    ap.add_argument("--union-out", type=Path, required=True)
    ap.add_argument("--rows-csv", type=Path, required=True)
    ap.add_argument("--source-run", required=True)
    ap.add_argument("--processing-date", required=True)
    args = ap.parse_args()

    with args.union_in.open(encoding="utf-8", errors="replace", newline="") as f:
        union_cols = next(csv.reader(f))
        fn_i = union_cols.index("Filename")
        existing = set()
        n_union = 0
        for row in csv.reader(f):
            n_union += 1
            if len(row) > fn_i:
                existing.add(row[fn_i])
    print(f"union in: {n_union:,} rows / {len(existing):,} files, "
          f"{len(union_cols)} columns", flush=True)

    with args.rows_csv.open(encoding="utf-8", errors="replace", newline="") as f:
        new_cols = [RENAME.get(c, c) for c in next(csv.reader(f))]
    unknown = [c for c in new_cols if c not in union_cols]
    if unknown:
        print(f"NOTE: {len(unknown)} incoming column(s) not in the union, dropped: "
              f"{unknown}")

    n_new = n_skip = 0
    skipped_files = set()
    seen_new = set()
    with args.union_in.open(encoding="utf-8", errors="replace", newline="") as fin, \
            args.rows_csv.open(encoding="utf-8", errors="replace", newline="") as fnew, \
            args.union_out.open("w", encoding="utf-8", newline="") as fout:
        w = csv.writer(fout)
        r_union = csv.reader(fin)
        next(r_union)
        w.writerow(union_cols)
        for row in r_union:
            w.writerow(row)
        r_new = csv.DictReader(fnew, fieldnames=new_cols)
        next(r_new)  # header
        for rec in r_new:
            fname = rec.get("Filename")
            if fname in existing:
                n_skip += 1
                skipped_files.add(fname)
                continue
            rec["Source Run"] = args.source_run
            rec["Processing Date"] = args.processing_date
            w.writerow([rec.get(c, "") for c in union_cols])
            seen_new.add(fname)
            n_new += 1
            if n_new % 100000 == 0:
                print(f"  appended {n_new:,} rows", flush=True)
    print(f"appended {n_new:,} rows / {len(seen_new):,} new files; "
          f"skipped {n_skip:,} rows from {len(skipped_files):,} already-in-union files")
    print(f"-> {args.union_out}: {n_union + n_new:,} rows / "
          f"{len(existing) + len(seen_new):,} files")


if __name__ == "__main__":
    main()
