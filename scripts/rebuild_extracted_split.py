"""One-off: re-split data/processed/permit_data_extracted.xlsx using the
SIC-aware manufacturing filter. Backs up the existing file first.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from permit_data_extraction.config import PROCESSED_DATA_DIR
from permit_data_extraction.dataset import (
    EXCEL_COLUMN_DISPLAY_NAMES,
    build_excel_column_order,
    read_permit_excel_full_table,
    write_permit_excel_multisheet,
)


def main() -> None:
    target = PROCESSED_DATA_DIR / "permit_data_extracted.xlsx"
    backup = target.with_name(target.stem + "_pre_sic_filter_backup.xlsx")

    if not target.exists():
        raise SystemExit(f"Missing input: {target}")

    print(f"Reading {target} ...")
    df = read_permit_excel_full_table(target)
    print(f"  combined rows: {len(df):,}")

    inverse = {v: k for k, v in EXCEL_COLUMN_DISPLAY_NAMES.items()}
    df = df.rename(columns=inverse)

    print(f"Backing up to {backup}")
    shutil.copy2(target, backup)

    excel_columns = build_excel_column_order()
    print("Rewriting workbook with SIC-aware split ...")
    write_permit_excel_multisheet(df, target, excel_columns)
    print("Done.")


if __name__ == "__main__":
    main()
