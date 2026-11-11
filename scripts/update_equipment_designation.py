#!/usr/bin/env python3
"""Recompute the Equipment Designation column of a union CSV or a dashboard
parquet in place with the current rules of scripts/classify_unit_types.py.

Used on 2026-11-11 to move the files from three values (combustion /
non-combustion / unknown) to four (combustion / electric heating / process and
storage / other).  Only the designation column changes (whatever it is named in
the file: Equipment Designation, Equipment Type or Combustion Source); the
script stops if the recomputed Equipment Category, Subcategory or Basis differ
from the stored ones, or if a row leaves "combustion" or enters it.

The original is kept as <name>_pre_designation4_<YYYYMMDD>.<ext>.

Usage: python scripts/update_equipment_designation.py FILE [FILE ...]
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_unit_types import classify_frame  # noqa: E402

NAMES = ("Equipment Designation", "Equipment Type", "Combustion Source")
NEEDED = ["Unit Type", "Unit Description", "Capacity Unit", "Fuel Type"]
KEEP = ["Equipment Category", "Equipment Subcategory", "Equipment Category Basis"]
OLD_TO_NEW = {"combustion": {"combustion"}, "non-combustion": {"electric heating", "process and storage"},
              "unknown": {"other"}}


def recompute(df: pd.DataFrame, col: str, tally: collections.Counter) -> pd.Series:
    sub = df[NEEDED + KEEP + [col]].astype("string").fillna("")
    new = classify_frame(sub[NEEDED])
    for c in KEEP:
        bad = int((sub[c].values != new[c].astype(str).values).sum())
        if bad:
            raise SystemExit(f"{bad} rows change {c}; not a designation-only update")
    out = new["Equipment Designation"].astype(str)
    old = sub[col].values
    for o, n in zip(*[pd.Series(old).values, out.values]):
        if o in OLD_TO_NEW and n not in OLD_TO_NEW[o]:
            raise SystemExit(f"row moves from {o!r} to {n!r}")
    for k, v in pd.DataFrame({"o": old, "n": out.values}).value_counts().items():
        tally[k] += int(v)
    return out


def finish(src: Path, tmp: Path) -> None:
    bk = src.with_name(f"{src.stem}_pre_designation4_{dt.date.today():%Y%m%d}{src.suffix}")
    if bk.exists():
        raise SystemExit(f"backup {bk} already exists")
    os.rename(src, bk)
    os.rename(tmp, src)


def do_csv(src: Path) -> None:
    header = pd.read_csv(src, nrows=0).columns.tolist()
    col = next(c for c in NAMES if c in header)
    tmp = src.with_suffix(".csv.tmp")
    tally: collections.Counter = collections.Counter()
    n = 0
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        for i, ch in enumerate(pd.read_csv(src, dtype=str, keep_default_na=False, chunksize=100_000)):
            ch[col] = recompute(ch, col, tally).values
            ch.to_csv(f, index=False, header=(i == 0))
            n += len(ch)
    finish(src, tmp)
    print(f"{src.name}: {n:,} rows, column {col!r}")
    for k in sorted(tally):
        print("   ", k, f"{tally[k]:,}")


def do_parquet(src: Path) -> None:
    pf = pq.ParquetFile(src)
    names = pf.schema_arrow.names
    col = next(c for c in NAMES if c in names)
    idx = names.index(col)
    tmp = src.with_suffix(".parquet.tmp")
    tally: collections.Counter = collections.Counter()
    writer = None
    n = 0
    for g in range(pf.num_row_groups):
        tbl = pf.read_row_group(g)
        new = recompute(tbl.select(NEEDED + KEEP + [col]).to_pandas(), col, tally)
        arr = pa.array(new.values, type=pa.string())
        if pa.types.is_dictionary(tbl.schema.field(col).type):
            arr = arr.dictionary_encode()
        tbl = tbl.set_column(idx, col, arr)
        if writer is None:
            writer = pq.ParquetWriter(tmp, tbl.schema, compression="zstd", use_dictionary=True, write_statistics=True)
        writer.write_table(tbl.cast(writer.schema))
        n += tbl.num_rows
    writer.close()
    finish(src, tmp)
    print(f"{src.name}: {n:,} rows, column {col!r}")
    for k in sorted(tally):
        print("   ", k, f"{tally[k]:,}")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        p = Path(a)
        (do_parquet if p.suffix == ".parquet" else do_csv)(p)
