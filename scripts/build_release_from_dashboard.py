"""Build the canonical release CSV from the dashboard's extraction workbook.

The Streamlit dashboard reads `data/processed/permit_data_extracted.xlsx`
(sheets "Manufacturing NAICS 31-33" + "Other NAICS"). As of 2026-06-16 this is
the dataset of record for the paper. This script concatenates both sheets and
normalizes the verbose extraction column headers to the short canonical names
used by the manuscript schema, the automated-QC script, and the validation
harness, writing `data/processed/permit_data_release.csv`.

Re-run after the dashboard workbook is regenerated.
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "data" / "processed" / "permit_data_extracted.xlsx"
OUT = ROOT / "data" / "processed" / "permit_data_release.csv"
SHEETS = ("Manufacturing NAICS 31-33", "Other NAICS")

# Verbose extraction header -> canonical short name.
RENAME = {
    "Model Used (AI extraction model)": "Model Used",
    "Owner/Operator Name (if different from Facility Name)": "Owner/Operator Name",
    "SIC Code (Standard Industrial Classification)": "SIC Code",
    "Operating Hours (facility-wide schedule)": "Operating Hours",
    "Opacity Limit (visible emission limit for this unit)": "Opacity Limit",
    "Throughput/Production Limit (material processing limits)": "Throughput/Production Limit",
    "Rated Efficiency (control/destruction/capture or equipment thermal — as stated)": "Rated Efficiency",
    "Annual Run Hours (this emission unit)": "Annual Run Hours",
    "Applicable NESHAP/NSPS Subpart (federal standard per unit)": "Applicable NESHAP/NSPS Subpart",
    "Permit Type (e.g. Title V, State Only, Synthetic Minor)": "Permit Type",
}


def main() -> None:
    frames = []
    for sheet in SHEETS:
        df = pd.read_excel(XLSX, sheet_name=sheet, dtype=str)
        df["Source Sheet"] = sheet
        frames.append(df)
        print(f"  [load] {sheet}: {len(df):,} rows")
    combined = pd.concat(frames, ignore_index=True).rename(columns=RENAME)
    combined.to_csv(OUT, index=False)
    n_permits = combined["Filename"].nunique()
    print(f"  [write] {OUT}  ({len(combined):,} rows, {n_permits:,} permits, {combined.shape[1]} cols)")


if __name__ == "__main__":
    main()
