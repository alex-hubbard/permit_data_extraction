#!/usr/bin/env python3
"""Append the four equipment-taxonomy columns to the release parquet and CSV.

Streams both files (the box has 7 GB of RAM; the union CSV is 1.3 GB) and runs
scripts/classify_unit_types.classify_frame on each row group / chunk, so the
new columns are a pure function of each file's own Unit Type, Unit Description,
Capacity Unit and Fuel Type fields.  Columns added, at the end:

    Equipment Category, Equipment Subcategory, Combustion Source,
    Equipment Category Basis

Existing columns and row order are untouched.  Output goes to a .tmp path and
is renamed into place only after the row count matches the input; the original
is kept as <name>_pre_equipcols_<YYYYMMDD>.<ext> unless --no-backup.

Usage (from the repo root):
    python scripts/add_equipment_columns.py \
        --parquet data/processed/dashboard/permits_v5f.parquet \
        --csv data/processed/permit_data_union_v5f.csv

Then rebuild the dashboard-ready file:
    PYTHONPATH=. python scripts/build_dashboard_ready_parquet.py \
        --in data/processed/dashboard/permits_v5f.parquet \
        --out data/processed/dashboard/permits_v5f_ready.parquet
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_unit_types import OUT_COLS, classify_frame  # noqa: E402

NEEDED = ["Unit Type", "Unit Description", "Capacity Unit", "Fuel Type"]


def _backup_path(p: Path) -> Path:
    stamp = dt.date.today().strftime("%Y%m%d")
    return p.with_name(f"{p.stem}_pre_equipcols_{stamp}{p.suffix}")


def _finish(src: Path, tmp: Path, n_in: int, n_out: int, backup: bool) -> None:
    if n_in != n_out:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"row count mismatch for {src.name}: in={n_in:,} out={n_out:,}; output discarded")
    if backup:
        bk = _backup_path(src)
        if bk.exists():
            raise SystemExit(f"backup {bk} already exists; remove it or pass --no-backup")
        os.rename(src, bk)
        print(f"  original kept as {bk.name}")
    else:
        src.unlink()
    os.rename(tmp, src)
    print(f"  -> {src} ({n_out:,} rows)")


def add_to_parquet(src: Path, backup: bool) -> None:
    pf = pq.ParquetFile(src)
    names = pf.schema_arrow.names
    already = [c for c in OUT_COLS if c in names]
    if already:
        raise SystemExit(f"{src.name} already has {already}; refusing to append twice")
    missing = [c for c in NEEDED if c not in names]
    if missing:
        raise SystemExit(f"{src.name} lacks {missing}")
    tmp = src.with_suffix(".parquet.tmp")
    writer = None
    n = 0
    tally = {}
    try:
        for i in range(pf.num_row_groups):
            tbl = pf.read_row_group(i)
            sub = tbl.select(NEEDED).to_pandas()
            cat = classify_frame(sub)
            for c in OUT_COLS:
                arr = pa.array(cat[c].astype(str), type=pa.string()).dictionary_encode()
                tbl = tbl.append_column(c, arr)
            if writer is None:
                writer = pq.ParquetWriter(tmp, tbl.schema, compression="zstd", use_dictionary=True, write_statistics=True)
            writer.write_table(tbl)
            n += tbl.num_rows
            for k, v in cat["Equipment Category"].value_counts().items():
                tally[k] = tally.get(k, 0) + int(v)
            print(f"  parquet row group {i + 1}/{pf.num_row_groups}: {n:,} rows", flush=True)
    finally:
        if writer is not None:
            writer.close()
    _finish(src, tmp, pf.metadata.num_rows, n, backup)
    print(pd.Series(tally).sort_values(ascending=False).to_string())


def add_to_csv(src: Path, backup: bool, chunksize: int) -> None:
    with src.open("rb") as f:
        head = f.read(1 << 20)
    first_line_end = head.find(b"\n")
    terminator = "\r\n" if first_line_end > 0 and head[first_line_end - 1:first_line_end] == b"\r" else "\n"
    header = pd.read_csv(src, nrows=0, dtype=str).columns.tolist()
    already = [c for c in OUT_COLS if c in header]
    if already:
        raise SystemExit(f"{src.name} already has {already}; refusing to append twice")
    missing = [c for c in NEEDED if c not in header]
    if missing:
        raise SystemExit(f"{src.name} lacks {missing}")
    tmp = src.with_suffix(".csv.tmp")
    n = 0
    first = True
    reader = pd.read_csv(src, dtype=str, keep_default_na=False, na_filter=False, chunksize=chunksize)
    with tmp.open("w", encoding="utf-8", newline="") as out:
        for chunk in reader:
            cat = classify_frame(chunk)
            for c in OUT_COLS:
                chunk[c] = cat[c].astype(str)
            chunk.to_csv(out, index=False, header=first, lineterminator=terminator)
            first = False
            n += len(chunk)
            print(f"  csv: {n:,} rows", flush=True)
    # count input rows the same way (quoted newlines make wc -l unreliable)
    n_in = sum(len(c) for c in pd.read_csv(src, dtype=str, keep_default_na=False, na_filter=False,
                                           usecols=[header[0]], chunksize=500_000))
    _finish(src, tmp, n_in, n, backup)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet", type=Path, help="release parquet to extend in place")
    ap.add_argument("--csv", type=Path, help="release union CSV to extend in place")
    ap.add_argument("--chunksize", type=int, default=100_000)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    if not (args.parquet or args.csv):
        ap.error("pass --parquet and/or --csv")
    if args.parquet:
        print(f"== {args.parquet}")
        add_to_parquet(args.parquet, backup=not args.no_backup)
    if args.csv:
        print(f"== {args.csv}")
        add_to_csv(args.csv, backup=not args.no_backup, chunksize=args.chunksize)


if __name__ == "__main__":
    main()
