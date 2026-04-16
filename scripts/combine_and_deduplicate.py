#!/usr/bin/env python3
"""
Combine all per-run Excel files in data/processed/ into a single deduplicated
dataset.  Skips the by_state/ subdirectory, the previously-generated combined
file, and any corrupt/unreadable files.

Deduplication strategy:
  - Group rows by (Filename, Unit ID).
  - Within each group keep the row with the latest Processing Date.
  - If Processing Date is missing, prefer the row from the file with the
    latest modification time.

Column harmonisation:
  - v0-era column names (e.g. "Facility State", "Capacity", "Make", "model",
    "Rated EFF", "type of boiler") are mapped to the current canonical names.
  - Display-name columns from 41-col exports (e.g. "Model Used (AI
    extraction model)") are mapped back to short canonical names.
  - All DataFrames are aligned to a single superset of columns before concat.
"""

import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJ_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJ_ROOT / "data" / "processed"
OUTPUT_XLSX = PROCESSED_DIR / "permit_data_full.xlsx"
OUTPUT_CSV = PROCESSED_DIR / "permit_data_full.csv"

# Files / patterns to skip
SKIP_NAMES = {
    "permit_data_extracted_combined.xlsx",       # by_state combo we built earlier
    "permit_data_extracted_pre_cleaning_backup.xlsx",  # subset of extracted.xlsx
}

# ---------------------------------------------------------------------------
# Column name mapping  (old / display name  ->  canonical name)
# ---------------------------------------------------------------------------
COLUMN_RENAMES = {
    # v0 names
    "Facility State": "Facility State Abbreviation",
    "Capacity": "Capacity Value",
    "Make": "Unit Make",
    "model": "Unit Model",
    "Rated EFF": "Rated Efficiency",
    "type of boiler": "Unit Type",
    # 41-col display names
    "Model Used (AI extraction model)": "Model Used",
    "Operating Hours (facility-wide schedule)": "Operating Hours",
    "Operating Hours — numeric/value portion": "Operating Hours Value",
    "Operating Hours — unit or basis (e.g. hours/year)": "Operating Hours Time Basis",
    "Annual Run Hours (this emission unit)": "Annual Run Hours",
    "Annual Run Hours — numeric/value portion": "Annual Run Hours Value",
    "Annual Run Hours — unit or basis (e.g. hours/year)": "Annual Run Hours Time Basis",
    "Rated Efficiency (control/destruction/capture or equipment thermal — as stated)": "Rated Efficiency",
    "Opacity Limit (visible emission limit for this unit)": "Opacity Limit",
    "Throughput/Production Limit (material processing limits)": "Throughput/Production Limit",
    "Applicable NESHAP/NSPS Subpart (federal standard per unit)": "Applicable NESHAP/NSPS Subpart",
    "Owner/Operator Name (if different from Facility Name)": "Owner/Operator Name",
    "SIC Code (Standard Industrial Classification)": "SIC Code",
    "Permit Type (e.g. Title V, State Only, Synthetic Minor)": "Permit Type",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename legacy / display columns to canonical names."""
    return df.rename(columns=COLUMN_RENAMES)


def _load_file(path: Path) -> pd.DataFrame | None:
    """Try to load an xlsx file; return None on failure."""
    try:
        df = pd.read_excel(path)
        if len(df) == 0:
            return None
        df = _normalize_columns(df)
        # Tag with source file mod time for tie-breaking during dedup
        df["_source_file"] = path.name
        df["_source_mtime"] = path.stat().st_mtime
        return df
    except Exception as e:
        print(f"  SKIP  {path.name}  ({e})", file=sys.stderr)
        return None


def main():
    # Gather candidate files
    candidates = sorted(PROCESSED_DIR.glob("permit_data*.xlsx"))
    candidates = [
        f for f in candidates
        if f.name not in SKIP_NAMES
        and not f.name.startswith("~$")
        and f.name != OUTPUT_XLSX.name
    ]

    print(f"Found {len(candidates)} candidate files in {PROCESSED_DIR}\n")

    # Load all files
    frames = []
    for f in candidates:
        df = _load_file(f)
        if df is not None:
            print(f"  LOADED  {f.name:<60}  rows={len(df):>8,}  cols={len(df.columns):>3}")
            frames.append(df)

    if not frames:
        print("ERROR: no files loaded.", file=sys.stderr)
        sys.exit(1)

    # Concat with union of all columns
    combined = pd.concat(frames, ignore_index=True, sort=False)
    print(f"\nCombined (before dedup): {len(combined):,} rows, {len(combined.columns)} columns")

    # ------------------------------------------------------------------
    # Deduplicate
    # ------------------------------------------------------------------
    # Coerce Processing Date to datetime for sorting
    combined["_proc_dt"] = pd.to_datetime(combined.get("Processing Date"), errors="coerce")

    # Build dedup key: Filename + Unit ID (both as strings, NaN -> "")
    combined["_dedup_fn"] = combined["Filename"].fillna("").astype(str).str.strip()
    combined["_dedup_uid"] = combined["Unit ID"].fillna("").astype(str).str.strip()

    # Sort so latest processing date (then latest file mtime) comes first
    combined.sort_values(
        ["_dedup_fn", "_dedup_uid", "_proc_dt", "_source_mtime"],
        ascending=[True, True, False, False],
        na_position="last",
        inplace=True,
    )

    before = len(combined)
    combined.drop_duplicates(subset=["_dedup_fn", "_dedup_uid"], keep="first", inplace=True)
    after = len(combined)
    print(f"Deduplicated:            {before:,}  ->  {after:,}  (removed {before - after:,} duplicates)")

    # Drop helper columns
    combined.drop(
        columns=["_source_file", "_source_mtime", "_proc_dt", "_dedup_fn", "_dedup_uid"],
        inplace=True,
    )

    # Reorder columns: put canonical columns first, extras at end
    canonical_order = [
        "Filename", "Status", "Processing Date", "Model Used",
        "Facility Name", "Owner/Operator Name",
        "Facility Address", "Facility City", "Facility State Abbreviation",
        "Facility Zip Code", "Facility County",
        "NAICS Code", "SIC Code",
        "Operating Hours", "Operating Hours Value", "Operating Hours Time Basis",
        "Industry Description",
        "Unit ID", "Unit Description", "Unit Quantity",
        "Unit Make", "Unit Model", "Year of Manufacture", "Unit Type",
        "Pollutants", "Emission Limits",
        "Opacity Limit", "Throughput/Production Limit",
        "Control Device(s)",
        "Capacity Value", "Capacity Unit",
        "Fuel Type", "Rated Efficiency",
        "Annual Run Hours", "Annual Run Hours Value", "Annual Run Hours Time Basis",
        "Generation Capacity",
        "Applicable NESHAP/NSPS Subpart",
        "Permit Number", "Permit Type",
        "Issuance Date", "Expiration Date",
        "Regulatory Authority",
        "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)",
        "Spec Sheet Link", "Duplicate Equipment Documents", "Latest Facility Filename",
    ]
    present = [c for c in canonical_order if c in combined.columns]
    extras = [c for c in combined.columns if c not in canonical_order]
    combined = combined[present + extras]

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    print(f"\nWriting output ...")
    combined.to_excel(OUTPUT_XLSX, index=False)
    combined.to_csv(OUTPUT_CSV, index=False)
    print(f"  {OUTPUT_XLSX.name}  ({OUTPUT_XLSX.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  {OUTPUT_CSV.name}   ({OUTPUT_CSV.stat().st_size / 1024 / 1024:.1f} MB)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    sep = "=" * 60
    print(f"\n{sep}")
    print("DEDUPLICATED DATASET SUMMARY")
    print(sep)

    success_mask = combined["Status"].str.contains("Success", case=False, na=False)

    total = len(combined)
    success = success_mask.sum()
    failed = total - success

    unique_files = combined["Filename"].nunique()

    has_unit = success_mask & combined["Unit ID"].notna() & (combined["Unit ID"].astype(str).str.strip() != "")
    unit_count = has_unit.sum()

    states = (
        combined.loc[success_mask, "Facility State Abbreviation"]
        .dropna().str.strip().str.upper()
    )
    real_states = sorted(states[states.str.len() == 2].unique())

    print(f"\nTotal records:                {total:,}")
    print(f"  Successful:                 {success:,}")
    print(f"  Failed:                     {failed:,}")
    print(f"Unique permits (files):       {unique_files:,}")
    print(f"Emission unit records:        {unit_count:,}")
    print(f"States + DC covered:          {len(real_states)}")
    print(f"  {', '.join(real_states)}")

    # Per-state breakdown
    state_counts = (
        combined.loc[success_mask]
        .groupby("Facility State Abbreviation")
        .agg(
            permits=("Filename", "nunique"),
            units=("Unit ID", lambda s: s.dropna().astype(str).str.strip().ne("").sum()),
            rows=("Filename", "size"),
        )
        .sort_values("rows", ascending=False)
    )
    print(f"\n{'State':<8} {'Permits':>10} {'Units':>10} {'Rows':>10}")
    print("-" * 42)
    for state, row in state_counts.iterrows():
        print(f"{state:<8} {int(row['permits']):>10,} {int(row['units']):>10,} {int(row['rows']):>10,}")
    print("-" * 42)
    print(f"{'TOTAL':<8} {int(state_counts['permits'].sum()):>10,} {int(state_counts['units'].sum()):>10,} {int(state_counts['rows'].sum()):>10,}")

    # Status breakdown
    print(f"\n{sep}")
    print("STATUS BREAKDOWN")
    print(sep)
    for status, count in combined["Status"].value_counts().items():
        print(f"  {status:<50} {count:>8,}")

    print(f"\n{sep}")
    print("DONE")
    print(sep)


if __name__ == "__main__":
    main()
