#!/usr/bin/env python3
"""
Download Virginia DEQ Title V permits from a pre-collected CSV of links.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from permit_data_extraction.config import RAW_DATA_DIR
from scripts.download_va_deq_titlev import (
    PERMIT_LISTING_URL,
    SeleniumPDFDownloader,
    derive_filename,
)


def download_from_csv(
    csv_path: Path,
    output_dir: Path,
    headless: bool = True,
    wait_seconds: int = 4,
) -> dict:
    df = pd.read_csv(csv_path)
    if "Permit URL" not in df.columns:
        raise ValueError("CSV must contain a 'Permit URL' column.")

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    summary = {"total": 0, "downloaded": 0, "skipped": 0}

    try:
        for _, row in df.iterrows():
            permit_url = row.get("Permit URL")
            if not isinstance(permit_url, str) or not permit_url.strip():
                summary["skipped"] += 1
                continue

            summary["total"] += 1

            filename_hint = derive_filename(row.to_dict())
            success = downloader.download_document(
                permit_url.strip(),
                referer=PERMIT_LISTING_URL,
                link_text=filename_hint,
                is_table_link=True,
            )
            if success:
                summary["downloaded"] += 1
            else:
                summary["skipped"] += 1
    finally:
        downloader.close()

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Virginia DEQ Title V permit PDFs from a CSV of links."
    )
    default_csv = RAW_DATA_DIR / "virginia_title_v" / "permit_links.csv"
    default_output = RAW_DATA_DIR / "virginia_title_v"

    parser.add_argument(
        "--csv",
        type=Path,
        default=default_csv,
        help=f"Path to the permit link CSV (default: {default_csv}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloads (default: {default_output}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in a visible window.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Seconds to wait for rendering when resolving documents.",
    )

    args = parser.parse_args()
    csv_path = args.csv.expanduser()
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = download_from_csv(
        csv_path,
        output_dir,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
    )

    print("Download summary:")
    print(f"  Total URLs processed: {summary['total']}")
    print(f"  Successfully downloaded: {summary['downloaded']}")
    print(f"  Skipped/failed: {summary['skipped']}")


if __name__ == "__main__":
    main()

