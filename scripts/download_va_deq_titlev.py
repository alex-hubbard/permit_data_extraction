#!/usr/bin/env python3
"""
Specialized downloader for Virginia DEQ Title V permits.

This script uses Selenium to render the main permit listing table,
extracts all permit document links, and downloads the PDFs using the
shared SeleniumPDFDownloader utilities.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

BASE_URL = "https://www.deq.virginia.gov"
PERMIT_LISTING_URL = (
    "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-air-permits-for-data-centers"                        # "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-title-v-permits"
    # "https://www.deq.virginia.gov/news-info/shortcuts/permits/air/title-v-permits"
)


def find_permit_tables(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """
    Locate permit-related tables by scanning headers for permit/document keywords.
    """
    matches: List[BeautifulSoup] = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if headers and any(
            any(token in header for token in ("permit", "document", "facility", "issued"))
            for header in headers
        ):
            matches.append(table)
    return matches


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

        row_data = dict(zip(headers, cells))
        link_els = tr.find_all("a", href=True)
        if link_els:
            for link_el in link_els:
                row_copy = dict(row_data)
                row_copy["link"] = link_el.get("href")
                row_copy["link_text"] = link_el.get_text(strip=True)
                rows.append(row_copy)
        else:
            row_data["link"] = None
            row_data["link_text"] = ""
            rows.append(row_data)

    return rows


def _extract_pdf_rows_fallback(soup: BeautifulSoup) -> List[dict]:
    """
    Fallback parser: capture all PDF/document links on the page.
    """
    rows: List[dict] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        if ".pdf" not in href.lower() and "FileLeafRef=" not in href:
            continue
        text = a.get_text(" ", strip=True)
        rows.append({"Facility Name": text, "link": href, "link_text": text})
    return rows


def derive_filename(row: dict) -> str:
    name_parts = []
    for key in ["Facility Name", "Facility", "Permit Number", "Permit", "link_text"]:
        value = row.get(key)
        if value:
            name_parts.append(value)

    if not name_parts:
        return "virginia_permit"

    return clean_filename(" - ".join(name_parts))


def _try_select_show_all(driver) -> bool:
    selectors = ["select[name$='_length']", "select[name*='length']", "label select"]
    for css in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, css):
            if not element.is_displayed():
                continue
            selector = Select(element)
            for option in selector.options:
                text = (option.text or "").strip().lower()
                value = (option.get_attribute("value") or "").strip().lower()
                if "all" in text or value == "-1":
                    selector.select_by_visible_text(option.text)
                    time.sleep(1.0)
                    return True
    return False


def _find_next_button(driver):
    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        "a.paginate_button.next, a[id$='_next'], a[title='Next'], a[aria-label*='Next']",
    )
    for btn in candidates:
        classes = (btn.get_attribute("class") or "").lower()
        if "disabled" in classes:
            continue
        if btn.is_displayed():
            return btn
    return None


def _wait_for_listing_content(driver, timeout: int = 30) -> None:
    """
    Wait for the page to contain either a table or at least one PDF-like link.
    """
    wait = WebDriverWait(driver, timeout)
    wait.until(
        lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, "table")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "a[href*='.pdf'], a[href*='FileLeafRef=']")) > 0
            or "issued-air-permits-for-data-centers" in (d.current_url or "")
        )
    )
    time.sleep(1.2)


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
        try:
            _wait_for_listing_content(driver, timeout=max(20, wait_seconds * 5))
        except Exception:
            # One retry helps with intermittent first-load rendering/cookie-banner delays.
            driver.get(PERMIT_LISTING_URL)
            _wait_for_listing_content(driver, timeout=max(20, wait_seconds * 5))

        used_show_all = _try_select_show_all(driver)
        seen_rows = set()
        rows: List[dict] = []

        while True:
            soup = BeautifulSoup(driver.page_source, "html.parser")
            tables = find_permit_tables(soup)
            page_rows: List[dict] = []
            for table in tables:
                page_rows.extend(parse_permit_rows(table))
            if not page_rows:
                page_rows = _extract_pdf_rows_fallback(soup)

            for row in page_rows:
                row_key = (str(row.get("link", "")), str(row.get("link_text", "")), str(row))
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                rows.append(row)

            if used_show_all:
                break
            next_button = _find_next_button(driver)
            if not next_button:
                break
            driver.execute_script("arguments[0].click();", next_button)
            time.sleep(1.2)

        if not rows:
            raise RuntimeError("No permit rows were parsed from the Virginia DEQ page.")

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

