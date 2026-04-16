#!/usr/bin/env python3
"""
Collect the Virginia DEQ Title V permit table into a CSV file.

This uses the same Selenium flow as the downloader but stops after
rendering the page, extracting all row data, and writing the results.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import List

import pandas as pd
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from urllib.parse import urljoin

from permit_data_extraction.config import RAW_DATA_DIR
from scripts.download_va_deq_titlev import (
    BASE_URL,
    PERMIT_LISTING_URL,
    SeleniumPDFDownloader,
    find_permit_tables,
    _extract_pdf_rows_fallback,
    _find_next_button,
    _try_select_show_all,
    parse_permit_rows,
)


def collect_rows(headless: bool = True, wait_seconds: int = 4) -> List[dict]:
    downloader = SeleniumPDFDownloader(
        output_dir=RAW_DATA_DIR / "virginia_title_v" / "_temp",
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    try:
        driver = downloader.driver
        driver.get(PERMIT_LISTING_URL)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))

        used_show_all = _try_select_show_all(driver)
        rows = []
        seen = set()
        while True:
            soup = BeautifulSoup(driver.page_source, "html.parser")
            tables = find_permit_tables(soup)
            page_rows = []
            for table in tables:
                page_rows.extend(parse_permit_rows(table))
            if not page_rows:
                page_rows = _extract_pdf_rows_fallback(soup)

            for row in page_rows:
                row_key = (str(row.get("link", "")), str(row.get("link_text", "")), str(row))
                if row_key in seen:
                    continue
                seen.add(row_key)
                rows.append(row)

            if used_show_all:
                break
            next_button = _find_next_button(driver)
            if not next_button:
                break
            driver.execute_script("arguments[0].click();", next_button)
            time.sleep(1.2)
        for row in rows:
            link = row.get("link")
            if link:
                row["Permit URL"] = urljoin(BASE_URL, link)
            else:
                row["Permit URL"] = None

        return rows
    finally:
        downloader.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Virginia DEQ Title V permit links into a CSV.")
    default_output = RAW_DATA_DIR / "virginia_title_v" / "permit_links.csv"

    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help=f"Path to write permit metadata (default: {default_output}).",
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
        help="Seconds to wait for the table to render.",
    )

    args = parser.parse_args()
    rows = collect_rows(headless=not args.no_headless, wait_seconds=args.wait_seconds)

    output_path: Path = args.output.expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(output_path, index=False)
    print(f"Wrote {len(df)} rows to {output_path}")


if __name__ == "__main__":
    main()

