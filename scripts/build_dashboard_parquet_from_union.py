"""Build the dashboard permits.parquet directly from a union CSV.

The existing chain (build_v2_dashboard_inputs.py -> xlsx -> build_dashboard_
parquet.py) cannot be used at current scale: the union has more successful rows
than Excel's 1,048,576-row sheet limit. This reads the union CSV in batches with
pyarrow and writes parquet directly, assigning the same "Source Sheet" label the
dashboard expects (Manufacturing NAICS 31-33 vs Other NAICS) from the NAICS/SIC
columns.

Usage:
    PYTHONPATH=. python scripts/build_dashboard_parquet_from_union.py \
        --union data/processed/permit_data_union_v5a.csv \
        --out data/processed/dashboard/permits.parquet
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

csv.field_size_limit(sys.maxsize)

MFG_SHEET = "Manufacturing NAICS 31-33"
OTHER_SHEET = "Other NAICS"
BATCH = 20_000

# The dashboard reads the verbose workbook headers, not the short union names.
# Emit its exact column names and order so the published parquet is a
# drop-in replacement.
DASHBOARD_RENAME = {
    "Model Used": "Model Used (AI extraction model)",
    "Owner/Operator Name": "Owner/Operator Name (if different from Facility Name)",
    "SIC Code": "SIC Code (Standard Industrial Classification)",
    "Operating Hours": "Operating Hours (facility-wide schedule)",
    "Opacity Limit": "Opacity Limit (visible emission limit for this unit)",
    "Throughput/Production Limit": "Throughput/Production Limit (material processing limits)",
    "Rated Efficiency": ("Rated Efficiency (control/destruction/capture or "
                         "equipment thermal — as stated)"),
    "Annual Run Hours": "Annual Run Hours (this emission unit)",
    "Applicable NESHAP/NSPS Subpart": ("Applicable NESHAP/NSPS Subpart "
                                       "(federal standard per unit)"),
    "Permit Type": "Permit Type (e.g. Title V, State Only, Synthetic Minor)",
}
DASHBOARD_ORDER = [
    "Status", "Processing Date", "Model Used (AI extraction model)", "Facility Name",
    "Owner/Operator Name (if different from Facility Name)", "Facility Address",
    "Facility City", "Facility State Abbreviation", "Facility Zip Code",
    "Facility County", "NAICS Code", "SIC Code (Standard Industrial Classification)",
    "Operating Hours (facility-wide schedule)",
    "Operating Hours — numeric/value portion",
    "Operating Hours — unit or basis (e.g. hours/year)", "Industry Description",
    "Unit ID", "Unit Description", "Unit Quantity", "Unit Make", "Unit Model",
    "Year of Manufacture", "Unit Type", "Pollutants", "Emission Limits",
    "Opacity Limit (visible emission limit for this unit)",
    "Throughput/Production Limit (material processing limits)", "Control Device(s)",
    "Capacity Value", "Capacity Unit", "Fuel Type",
    "Rated Efficiency (control/destruction/capture or equipment thermal — as stated)",
    "Annual Run Hours (this emission unit)",
    "Annual Run Hours — numeric/value portion",
    "Annual Run Hours — unit or basis (e.g. hours/year)", "Generation Capacity",
    "Applicable NESHAP/NSPS Subpart (federal standard per unit)", "Permit Number",
    "Permit Type (e.g. Title V, State Only, Synthetic Minor)", "Issuance Date",
    "Expiration Date", "Regulatory Authority",
    "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)", "Filename",
    "Spec Sheet Link", "Duplicate Equipment Documents", "Latest Facility Filename",
    "Source Run", "Pieces Failed", "Classified NAICS", "Source Sheet",
]
# SIC 2000-3999 is the manufacturing division
SIC_MFG = re.compile(r"^(2\d{3}|3\d{3})")


def sheet_for(row):
    for col in ("NAICS Code", "Classified NAICS"):
        v = (row.get(col) or "").strip()
        m = re.match(r"(\d{2})", v)
        if m and m.group(1) in ("31", "32", "33"):
            return MFG_SHEET
    sic = (row.get("SIC Code") or "").strip()
    if SIC_MFG.match(sic):
        return MFG_SHEET
    return OTHER_SHEET


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--all-statuses", action="store_true",
                    help="include failure rows (default: successful rows only)")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    with args.union.open(encoding="utf-8", errors="replace", newline="") as f:
        cols = next(csv.reader(f))
    src_of = {DASHBOARD_RENAME.get(c, c): c for c in cols}
    src_of["Source Sheet"] = "Source Sheet"
    missing = [c for c in DASHBOARD_ORDER if c not in src_of]
    if missing:
        raise SystemExit(f"union is missing dashboard column(s): {missing}")
    schema = pa.schema([(c, pa.string()) for c in DASHBOARD_ORDER])

    writer = pq.ParquetWriter(args.out, schema, compression="zstd")
    batch = []
    n = kept = 0
    counts = {MFG_SHEET: 0, OTHER_SHEET: 0}

    def flush():
        if not batch:
            return
        arrays = [pa.array([r.get(src_of[c]) or "" for r in batch], type=pa.string())
                  for c in schema.names]
        writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
        batch.clear()

    with args.union.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            if not args.all_statuses and not row["Status"].startswith("Success"):
                continue
            row["Source Sheet"] = sheet_for(row)
            counts[row["Source Sheet"]] += 1
            batch.append(row)
            kept += 1
            if len(batch) >= BATCH:
                flush()
                print(f"  {kept:,} rows written", flush=True)
    flush()
    writer.close()
    mb = args.out.stat().st_size / 1e6
    print(f"read {n:,} rows -> wrote {kept:,} "
          f"({counts[MFG_SHEET]:,} manufacturing / {counts[OTHER_SHEET]:,} other)")
    print(f"-> {args.out} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
