#!/usr/bin/env python3
"""
Specialized downloader for Virginia DEQ Title V permits.

This script uses Selenium to render the main permit listing table,
extracts all permit document links, and downloads the PDFs using the
shared SeleniumPDFDownloader utilities.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

BASE_URL = "https://www.deq.virginia.gov"
PERMIT_LISTING_URL = (
    "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-air-permits-for-data-centers"                        # "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-title-v-permits"
    # "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/title-v-permits"
)


def find_permit_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    """
    Locate the primary Title V permit table by scanning headers for the word 'permit'.
    """
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if headers and any("permit" in header for header in headers):
            return table
    return None


def parse_permit_rows(table: BeautifulSoup) -> List[dict]:
    """
    Extract row data from the permit table, capturing facility metadata and the PDF link.
    """
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    rows = []
    tbody = table.find("tbody") or table

    for tr in tbody.find_all("tr"):
        cells = [cell.get_text(strip=True) for cell in tr.find_all(["td", "th"])]
        if not cells:
            continue

        # Normalize row length to headers
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))

        link_el = tr.find("a")
        link = link_el.get("href") if link_el else None

        row_data = dict(zip(headers, cells))
        row_data["link"] = link
        rows.append(row_data)

    return rows


def derive_filename(row: dict) -> str:
    name_parts = []
    for key in ["Facility Name", "Facility", "Permit Number", "Permit"]:
        value = row.get(key)
        if value:
            name_parts.append(value)

    if not name_parts:
        return "virginia_permit"

    return clean_filename(" - ".join(name_parts))


def download_permits(output_dir: Path, headless: bool = True, wait_seconds: int = 4) -> None:
    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,  # We supply explicit document URLs, so no recursion needed
        use_llm=False,
    )

    try:
        driver = downloader.driver
        driver.get(PERMIT_LISTING_URL)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))

        soup = BeautifulSoup(driver.page_source, "html.parser")
        permit_table = find_permit_table(soup)
        if not permit_table:
            raise RuntimeError("Could not locate permit table on Virginia DEQ page.")

        rows = parse_permit_rows(permit_table)
        if not rows:
            raise RuntimeError("No permit rows were parsed from the table.")

        downloaded = 0
        skipped = 0

        for row in rows:
            link = row.get("link")
            if not link:
                skipped += 1
                continue

            permit_url = urljoin(BASE_URL, link)
            filename = derive_filename(row)

            success = downloader.download_document(
                permit_url,
                referer=PERMIT_LISTING_URL,
                link_text=filename,
                is_table_link=True,
            )
            if success:
                downloaded += 1
            else:
                skipped += 1

        print(f"Downloaded {downloaded} permit documents.")
        if skipped:
            print(f"Skipped {skipped} rows without documents or failed downloads.")

    finally:
        downloader.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Virginia DEQ Title V permit PDFs.")
    default_output = Path(RAW_DATA_DIR) / "virginia_title_v"
    parser.add_argument(
        "--output-dir",
        default=default_output,
        type=Path,
        help=f"Directory to store downloaded permits (default: {default_output}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Seconds to wait for page rendering.",
    )

    args = parser.parse_args()
    output_dir: Path = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    download_permits(output_dir, headless=not args.no_headless, wait_seconds=args.wait_seconds)


if __name__ == "__main__":
    main()

