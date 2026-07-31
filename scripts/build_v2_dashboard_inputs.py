"""Build the dashboard xlsx from permit_data_union_v2.csv.

Takes the success rows of the v2 union (which includes the 2026-07 forced-chunk
re-extraction), splits them into the Manufacturing/Other sheets using the same
SIC-aware logic as the main pipeline (write_permit_excel_multisheet), and writes
data/processed/permit_data_extracted_v2.xlsx. The existing
permit_data_extracted.xlsx (May snapshot) is left untouched.

Extra union columns (Source Run, Pieces Failed) are appended to the column
order so provenance survives into the parquet.

Then build the parquet with:
    PYTHONPATH=. python scripts/build_dashboard_parquet.py \
        --xlsx data/processed/permit_data_extracted_v2.xlsx --skip-centroids
"""

from pathlib import Path

import pandas as pd

from permit_data_extraction.dataset import (
    EXCEL_COLUMN_DISPLAY_NAMES,
    build_excel_column_order,
    write_permit_excel_multisheet,
)

UNION_V2 = Path("data/processed/permit_data_union_v2.csv")
OUT_XLSX = Path("data/processed/permit_data_extracted_v2.xlsx")


def main() -> None:
    df = pd.read_csv(UNION_V2, dtype=str, keep_default_na=False, na_values=[""])
    print(f"union v2: {len(df):,} rows / {df['Filename'].nunique():,} files")

    success = df["Status"].fillna("").str.startswith("Success")
    df = df[success].copy()
    print(f"success rows: {len(df):,} / {df['Filename'].nunique():,} files")

    inverse = {v: k for k, v in EXCEL_COLUMN_DISPLAY_NAMES.items()}
    df = df.rename(columns=inverse)

    column_order = build_excel_column_order()
    extras = [c for c in df.columns if c not in column_order and c != "Classified NAICS"]
    if extras:
        print(f"appending extra columns to order: {extras}")
        column_order = column_order + extras

    write_permit_excel_multisheet(df, OUT_XLSX, column_order)
    print(f"wrote {OUT_XLSX}")


if __name__ == "__main__":
    main()
