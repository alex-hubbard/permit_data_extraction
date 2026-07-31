"""Build the stratified manual-audit sample and reviewer workbook (paper Section 4.1).

Samples N permits from the consolidated dataset, stratified by state (proportional,
minimum one permit per state) and document-length tercile (extracted-text size).
Writes to data/processed/validation/manual_audit/:
  - audit_sample_manifest.csv  (one row per sampled permit, with strata and file paths)
  - audit_workbook.xlsx        (General Fields + Unit Fields sheets with blank
                                Reference Value / Correct / Notes columns)

Usage: python scripts/build_audit_sample.py [n_permits] [seed]
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed" / "permit_data_full.csv"
TEXT_DIR = ROOT / "data" / "interim" / "extracted_text"
OUT_DIR = ROOT / "data" / "processed" / "validation" / "manual_audit"

GENERAL_FIELDS = [
    "Facility Name", "Facility Address", "Facility City", "Facility State Abbreviation",
    "Facility Zip Code", "Facility County", "NAICS Code", "Operating Hours",
    "Industry Description", "Permit Number", "Issuance Date", "Expiration Date",
    "Regulatory Authority", "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)",
]
UNIT_FIELDS = [
    "Unit ID", "Unit Description", "Unit Quantity", "Unit Make", "Unit Model",
    "Year of Manufacture", "Unit Type", "Pollutants", "Emission Limits",
    "Control Device(s)", "Capacity Value", "Capacity Unit", "Fuel Type",
    "Rated Efficiency", "Annual Run Hours", "Generation Capacity",
]


def main():
    n_target = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    rng = np.random.default_rng(seed)

    df = pd.read_csv(DATA, low_memory=False)
    ok = df[df["Status"].astype(str).str.startswith("Success")].copy()

    # One row per permit with state and row count
    permits = (
        ok.groupby("Filename")
        .agg(state=("Facility State Abbreviation", lambda s: s.dropna().astype(str).str.strip().mode().iat[0]
                    if not s.dropna().empty else "UNKNOWN"),
             n_rows=("Filename", "size"))
        .reset_index()
    )

    # Attach text size (document length proxy); drop permits whose text file is gone
    permits["txt_path"] = permits["Filename"].map(lambda f: TEXT_DIR / f"{f}.txt")
    permits["txt_size"] = permits["txt_path"].map(lambda p: p.stat().st_size if p.exists() else np.nan)
    missing_txt = permits["txt_size"].isna().sum()
    permits = permits.dropna(subset=["txt_size"])
    permits["size_tercile"] = pd.qcut(permits["txt_size"], 3, labels=["short", "medium", "long"])

    # Proportional allocation by state with a minimum of 1
    counts = permits["state"].value_counts()
    alloc = (counts / counts.sum() * n_target).round().astype(int).clip(lower=1)
    # trim back to target by shaving the largest strata
    while alloc.sum() > n_target:
        alloc[alloc.idxmax()] -= 1
    while alloc.sum() < n_target:
        alloc[counts.idxmax()] += 1

    sampled_parts = []
    for state, k in alloc.items():
        pool = permits[permits["state"] == state]
        k = min(k, len(pool))
        # spread across size terciles within the state
        per_terc = max(k // 3, 1)
        chosen = []
        for terc in ("short", "medium", "long"):
            tp = pool[pool["size_tercile"] == terc]
            take = min(per_terc, len(tp))
            if take:
                chosen.append(tp.sample(take, random_state=int(rng.integers(0, 2**31))))
        got = pd.concat(chosen) if chosen else pool.iloc[:0]
        if len(got) < k:
            rest = pool.drop(got.index)
            extra = rest.sample(min(k - len(got), len(rest)), random_state=int(rng.integers(0, 2**31)))
            got = pd.concat([got, extra])
        sampled_parts.append(got.iloc[:k])
    sample = pd.concat(sampled_parts).drop_duplicates(subset="Filename")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = sample[["Filename", "state", "size_tercile", "n_rows", "txt_size", "txt_path"]].copy()
    manifest["txt_path"] = manifest["txt_path"].astype(str)
    manifest.to_csv(OUT_DIR / "audit_sample_manifest.csv", index=False)

    # Build the audit workbook
    rows = ok[ok["Filename"].isin(sample["Filename"])]
    gen_records, unit_records = [], []
    for fname, grp in rows.groupby("Filename"):
        first = grp.iloc[0]
        for f in GENERAL_FIELDS:
            gen_records.append({
                "Filename": fname, "Field Name": f,
                "Extracted Value": first.get(f),
                "Reference Value": "", "Correct (Y/N)": "", "Notes": "",
            })
        for _, r in grp.iterrows():
            uid = r.get("Unit ID")
            if pd.isna(uid) or not str(uid).strip():
                continue
            for f in UNIT_FIELDS:
                unit_records.append({
                    "Filename": fname, "Unit ID": uid, "Field Name": f,
                    "Extracted Value": r.get(f),
                    "Reference Value": "", "Correct (Y/N)": "", "Notes": "",
                })
        # Row for missed units: reviewer lists units present in the PDF but absent here
        unit_records.append({
            "Filename": fname, "Unit ID": "(MISSED UNITS — list any units in the PDF not extracted)",
            "Field Name": "", "Extracted Value": "", "Reference Value": "",
            "Correct (Y/N)": "", "Notes": "",
        })

    with pd.ExcelWriter(OUT_DIR / "audit_workbook.xlsx", engine="openpyxl") as xl:
        manifest.to_excel(xl, sheet_name="Sample Manifest", index=False)
        pd.DataFrame(gen_records).to_excel(xl, sheet_name="General Fields", index=False)
        pd.DataFrame(unit_records).to_excel(xl, sheet_name="Unit Fields", index=False)

    print(f"Sampled {len(sample)} permits (target {n_target}, seed {seed}); "
          f"{missing_txt} permits skipped (no text file).")
    print(f"States covered: {sample['state'].nunique()}; terciles: {sample['size_tercile'].value_counts().to_dict()}")
    print(f"General audit lines: {len(gen_records)}; unit audit lines: {len(unit_records)}")
    print(f"Wrote {OUT_DIR / 'audit_sample_manifest.csv'} and {OUT_DIR / 'audit_workbook.xlsx'}")


if __name__ == "__main__":
    main()
