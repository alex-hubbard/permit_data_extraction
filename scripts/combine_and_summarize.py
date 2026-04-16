#!/usr/bin/env python3
"""Combine per-state extracted datasets into a single file and print summary statistics."""

import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJ_ROOT = Path(__file__).resolve().parents[1]
BY_STATE_DIR = PROJ_ROOT / "data" / "processed" / "by_state"
OUTPUT_DIR = PROJ_ROOT / "data" / "processed"
COMBINED_FILE = OUTPUT_DIR / "permit_data_extracted_combined.xlsx"
COMBINED_CSV = OUTPUT_DIR / "permit_data_extracted_combined.csv"


def load_state_files() -> pd.DataFrame:
    """Read every per-state xlsx under by_state/ and concatenate."""
    frames = []
    state_dirs = sorted(
        p for p in BY_STATE_DIR.iterdir()
        if p.is_dir() and p.name != "ERROR"
    )
    for state_dir in state_dirs:
        xlsx_files = list(state_dir.glob("*.xlsx"))
        if not xlsx_files:
            continue
        for xlsx in xlsx_files:
            try:
                df = pd.read_excel(xlsx)
                # Tag with the directory name in case State Abbreviation is missing
                if "Facility State Abbreviation" not in df.columns:
                    df["Facility State Abbreviation"] = state_dir.name
                frames.append(df)
            except Exception as e:
                print(f"  WARNING: could not read {xlsx.name}: {e}", file=sys.stderr)

    if not frames:
        print("ERROR: no state files found.", file=sys.stderr)
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=True)
    return combined


def print_summary(df: pd.DataFrame) -> None:
    """Print key summary statistics."""
    separator = "=" * 60

    print(separator)
    print("PERMIT DATASET SUMMARY STATISTICS")
    print(separator)

    # --- Overall counts ---
    total_rows = len(df)
    success_mask = df["Status"].str.contains("Success", case=False, na=False)
    success_rows = success_mask.sum()
    failed_rows = total_rows - success_rows

    unique_permits = df["Filename"].nunique()

    # Emission units = rows with a non-null Unit ID among successful extractions
    has_unit = success_mask & df["Unit ID"].notna() & (df["Unit ID"].astype(str).str.strip() != "")
    emission_unit_rows = has_unit.sum()

    # Permits with no units
    no_unit_mask = df["Status"].str.contains("No Units Found", case=False, na=False)
    permits_no_units = no_unit_mask.sum()

    print(f"\nTotal records (rows):         {total_rows:,}")
    print(f"  Successful extractions:     {success_rows:,}")
    print(f"  Failed extractions:         {failed_rows:,}")
    print(f"Unique permits (files):       {unique_permits:,}")
    print(f"Emission unit records:        {emission_unit_rows:,}")
    print(f"Permits with no units found:  {permits_no_units:,}")

    # --- Geographic coverage ---
    states = (
        df.loc[success_mask, "Facility State Abbreviation"]
        .dropna()
        .str.strip()
        .str.upper()
    )
    # Filter to real 2-letter state codes (exclude UNKNOWN, blanks, etc.)
    real_states = states[states.str.len() == 2].unique()
    real_states_sorted = sorted(real_states)

    print(f"\n{separator}")
    print("GEOGRAPHIC COVERAGE")
    print(separator)
    print(f"States (+ DC) covered:        {len(real_states_sorted)}")
    print(f"  {', '.join(real_states_sorted)}")

    # Per-state breakdown
    state_counts = (
        df.loc[success_mask]
        .groupby("Facility State Abbreviation")
        .agg(
            permits=("Filename", "nunique"),
            emission_units=("Unit ID", lambda s: s.dropna().astype(str).str.strip().ne("").sum()),
            rows=("Filename", "size"),
        )
        .sort_values("rows", ascending=False)
    )
    print(f"\n{'State':<8} {'Permits':>10} {'Units':>10} {'Rows':>10}")
    print("-" * 42)
    for state, row in state_counts.iterrows():
        print(f"{state:<8} {int(row['permits']):>10,} {int(row['emission_units']):>10,} {int(row['rows']):>10,}")
    print("-" * 42)
    print(f"{'TOTAL':<8} {int(state_counts['permits'].sum()):>10,} {int(state_counts['emission_units'].sum()):>10,} {int(state_counts['rows'].sum()):>10,}")

    # --- Status breakdown ---
    print(f"\n{separator}")
    print("STATUS BREAKDOWN")
    print(separator)
    status_counts = df["Status"].value_counts()
    for status, count in status_counts.items():
        print(f"  {status:<45} {count:>8,}")

    # --- Field completeness ---
    print(f"\n{separator}")
    print("FIELD COMPLETENESS (successful rows)")
    print(separator)
    success_df = df.loc[success_mask]
    key_fields = [
        "Facility Name", "Facility Address", "Facility City",
        "Facility State Abbreviation", "Facility Zip Code", "Facility County",
        "NAICS Code", "SIC Code", "Permit Number", "Permit Type",
        "Owner/Operator Name", "Issuance Date", "Expiration Date",
        "Regulatory Authority", "Industry Description",
        "Unit ID", "Unit Description", "Unit Type", "Pollutants",
        "Emission Limits", "Opacity Limit", "Throughput/Production Limit",
        "Control Device(s)", "Capacity Value", "Fuel Type",
        "Applicable NESHAP/NSPS Subpart",
    ]
    present_fields = [f for f in key_fields if f in success_df.columns]
    missing_fields = [f for f in key_fields if f not in success_df.columns]

    print(f"\n{'Field':<55} {'Non-null':>10} {'%':>8}")
    print("-" * 76)
    for field in present_fields:
        non_null = success_df[field].notna().sum()
        # Also exclude empty strings
        non_empty = success_df[field].apply(
            lambda v: v is not None and str(v).strip() != "" and str(v).strip().lower() != "nan"
        ).sum()
        pct = 100 * non_empty / len(success_df) if len(success_df) > 0 else 0
        print(f"  {field:<53} {non_empty:>10,} {pct:>7.1f}%")

    if missing_fields:
        print(f"\n  Fields not yet in dataset (new schema):")
        for f in missing_fields:
            print(f"    - {f}")

    # --- Top industries ---
    print(f"\n{separator}")
    print("TOP 15 INDUSTRIES (by row count)")
    print(separator)
    if "Industry Description" in success_df.columns:
        ind = (
            success_df["Industry Description"]
            .dropna()
            .str.strip()
            .loc[lambda s: s != ""]
            .value_counts()
            .head(15)
        )
        for desc, count in ind.items():
            print(f"  {desc:<55} {count:>6,}")

    # --- Top unit types ---
    print(f"\n{separator}")
    print("TOP 15 UNIT TYPES (by row count)")
    print(separator)
    if "Unit Type" in success_df.columns:
        ut = (
            success_df["Unit Type"]
            .dropna()
            .str.strip()
            .loc[lambda s: s != ""]
            .value_counts()
            .head(15)
        )
        for utype, count in ut.items():
            print(f"  {utype:<55} {count:>6,}")

    print(f"\n{separator}")
    print("DONE")
    print(separator)


def main():
    print(f"Loading state files from {BY_STATE_DIR} ...")
    df = load_state_files()
    print(f"  Loaded {len(df):,} rows from {df['Facility State Abbreviation'].nunique()} state directories.\n")

    # Write combined output
    print(f"Writing combined dataset ...")
    df.to_excel(COMBINED_FILE, index=False)
    df.to_csv(COMBINED_CSV, index=False)
    print(f"  {COMBINED_FILE}  ({COMBINED_FILE.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  {COMBINED_CSV}  ({COMBINED_CSV.stat().st_size / 1024 / 1024:.1f} MB)")
    print()

    print_summary(df)


if __name__ == "__main__":
    main()
