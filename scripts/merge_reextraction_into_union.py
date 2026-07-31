"""Merge the forced-chunk re-extraction rows into the union dataset.

Replaces ALL union rows for the 1,212 re-extracted files (recall suspects +
hard failures) with the rows from batch_reextract_rows.csv, tagged
Source Run = reextract_2026-07. Per-permit source selection is preserved:
a file's rows come entirely from one run, never interleaved.

Column reconciliation:
- The re-extraction's split hours columns are renamed to the union's names
  ("Operating Hours Value" -> "Operating Hours — numeric/value portion", etc.).
- Permit-level derived columns the re-extraction lacks (Classified NAICS,
  Latest Facility Filename, Duplicate Equipment Documents) are carried over
  per-Filename from the old union rows (facility identity is unchanged).
- Spec Sheet Link is unit-level and cannot be carried over -> left empty.
- "Pieces Failed" is kept as a new provenance column (0 = fully covered doc;
  >0 = some chunks still failed, third pass pending). Old rows get NA.

Usage:
    PYTHONPATH=. python scripts/merge_reextraction_into_union.py

Writes data/processed/permit_data_union_v2.csv (does not touch the original).
"""

from pathlib import Path

import pandas as pd

UNION_CSV = Path("data/processed/permit_data_union.csv")
REEXTRACT_CSV = Path("data/processed/reextraction/batch_reextract_rows.csv")
OUT_CSV = Path("data/processed/permit_data_union_v2.csv")

SOURCE_RUN_TAG = "reextract_2026-07"
PROCESSING_DATE = "2026-07-08"

RENAME = {
    "Operating Hours Value": "Operating Hours — numeric/value portion",
    "Operating Hours Time Basis": "Operating Hours — unit or basis (e.g. hours/year)",
    "Annual Run Hours Value": "Annual Run Hours — numeric/value portion",
    "Annual Run Hours Time Basis": "Annual Run Hours — unit or basis (e.g. hours/year)",
}
CARRY_OVER = ["Classified NAICS", "Latest Facility Filename", "Duplicate Equipment Documents"]


def main():
    union = pd.read_csv(UNION_CSV, dtype=str, keep_default_na=False, na_values=[""])
    re_rows = pd.read_csv(REEXTRACT_CSV, dtype=str, keep_default_na=False, na_values=[""])
    re_files = set(re_rows["Filename"])

    old = union[union["Filename"].isin(re_files)]
    print(f"union: {len(union):,} rows / {union['Filename'].nunique():,} files")
    print(f"replacing {len(old):,} rows for {old['Filename'].nunique():,} re-extracted files "
          f"with {len(re_rows):,} rows")

    re_rows = re_rows.rename(columns=RENAME)
    re_rows["Source Run"] = SOURCE_RUN_TAG
    re_rows["Processing Date"] = PROCESSING_DATE

    # carry permit-level derived columns over from the old union rows
    carry = (old.groupby("Filename")[CARRY_OVER].first())
    for col in CARRY_OVER:
        re_rows[col] = re_rows["Filename"].map(carry[col])

    # align to union schema (+ Pieces Failed provenance column at the end)
    out_cols = list(union.columns) + ["Pieces Failed"]
    for col in out_cols:
        if col not in re_rows.columns:
            re_rows[col] = pd.NA
    re_rows = re_rows[out_cols]

    kept = union[~union["Filename"].isin(re_files)].copy()
    kept["Pieces Failed"] = pd.NA
    merged = pd.concat([kept[out_cols], re_rows], ignore_index=True)

    merged.to_csv(OUT_CSV, index=False)
    print(f"-> {OUT_CSV}: {len(merged):,} rows / {merged['Filename'].nunique():,} files")
    print(merged["Source Run"].value_counts().to_string())


if __name__ == "__main__":
    main()
