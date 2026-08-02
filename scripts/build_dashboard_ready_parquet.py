"""Build a dashboard-ready parquet: slim columns + precomputed derived fields.

The Streamlit app previously did all of its derivation at load time --- several
full-column .apply() passes, three regex scans, a string concat, and a merge ---
on every cold start. That fit in Streamlit's memory budget at ~282K rows but not
at the current ~1.1M, where the app OOMs.

This moves that work into the build. It keeps only the columns the app reads,
precomputes every derived column the app would otherwise compute, and writes
dictionary-encoded parquet. The app then loads a frame it can use directly
(see the `_DERIVED_PRESENT` short-circuit in load_data).

Derivation logic is imported from the dashboard module so the two cannot drift.

Usage:
    PYTHONPATH=. python scripts/build_dashboard_ready_parquet.py \
        --in data/processed/dashboard/permits_v5a.parquet \
        --out data/processed/dashboard/permits_v5a_ready.parquet
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "dashboards")):
    if p not in sys.path:
        sys.path.insert(0, p)

import manufacturing_subsector as dash  # noqa: E402

# Source columns the app reads, plus what the derived columns are built from.
KEEP = [
    "Facility Name", "Facility City", "Facility State Abbreviation",
    "NAICS Code", "Classified NAICS", "Industry Description",
    "Unit Description", "Unit Type", "Unit Quantity",
    "Capacity Value", "Capacity Unit", "Fuel Type", "Control Device(s)",
    "Permit Number", "Source Sheet",
]
BATCH = 100_000


def derive(df: pd.DataFrame) -> pd.DataFrame:
    classified = (df["Classified NAICS"].apply(dash._digits_only)
                  if "Classified NAICS" in df.columns
                  else pd.Series([None] * len(df), index=df.index))
    raw = df["NAICS Code"].apply(dash._digits_only)
    naics = classified.where(classified.notna(), raw)
    df["NAICS_clean"] = naics
    df["Subsector"] = naics.str[:3]

    haystack = df["Industry Description"].fillna("").str.cat(
        df["Facility Name"].fillna(""), sep=" ")
    for code, pattern in dash.KEYWORD_FILTERS.items():
        df[f"is_{code}"] = haystack.str.contains(pattern)

    df["Site Key"] = (
        df["Facility Name"].fillna("").str.strip().str.upper() + " | "
        + df["Facility City"].fillna("").str.strip().str.upper() + " | "
        + df["Facility State Abbreviation"].fillna("").str.strip().str.upper())
    df["Capacity Value (num)"] = pd.to_numeric(df["Capacity Value"], errors="coerce")
    df["Capacity Unit (norm)"] = df["Capacity Unit"].apply(dash._normalize_capacity_unit)
    df["Fuel Type (norm)"] = df["Fuel Type"].apply(dash._normalize_fuel)
    df["Unit Quantity (num)"] = pd.to_numeric(df["Unit Quantity"], errors="coerce")
    df["key_state"] = df["Facility State Abbreviation"].str.upper().str.strip()
    df["key_city"] = df["Facility City"].str.upper().str.strip()
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--centroids", type=Path,
                    default=Path("data/processed/dashboard/city_centroids.parquet"),
                    help="merge Lat/Lon in at build time so the app skips the join")
    args = ap.parse_args()

    centroids = None
    if args.centroids.exists():
        centroids = pd.read_parquet(args.centroids)
        print(f"centroids: {len(centroids):,} city keys")
    else:
        print(f"NOTE: {args.centroids} not found; Lat/Lon left unmerged")

    src = pq.ParquetFile(args.src)
    present = [c for c in KEEP if c in src.schema_arrow.names]
    missing = [c for c in KEEP if c not in src.schema_arrow.names]
    if missing:
        print(f"NOTE: source lacks {missing}; those derived columns will be empty")

    writer = None
    n = 0
    try:
        for batch in src.iter_batches(batch_size=BATCH, columns=present):
            df = derive(batch.to_pandas())
            if centroids is not None:
                df = df.merge(centroids, on=["key_state", "key_city"], how="left")
            table = pa.Table.from_pandas(df, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(
                    args.out, table.schema, compression="zstd",
                    use_dictionary=True, write_statistics=True)
            writer.write_table(table)
            n += len(df)
            print(f"  {n:,} rows", flush=True)
    finally:
        if writer is not None:
            writer.close()
    mb = args.out.stat().st_size / 1e6
    cols = pq.ParquetFile(args.out).schema_arrow.names
    print(f"-> {args.out}: {n:,} rows, {len(cols)} columns, {mb:.1f} MB")


if __name__ == "__main__":
    main()
