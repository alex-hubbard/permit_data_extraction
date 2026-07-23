#!/usr/bin/env python3
"""
Download Ohio EPA eDocument records filtered to Program = AIR PERMIT.

Portal:
https://edocpub.epa.ohio.gov/publicportal/edochome.aspx
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

SEARCH_URL = "https://edocpub.epa.ohio.gov/publicportal/edochome.aspx"
RESULTS_TABLE_ID = "ctl00_results_DocHitList_tblDocuments"
PROGRAM_SELECT_ID = "ctl00_search_KeywordPanel1_ddlValue_-1_1_109_1"
SEARCH_BUTTON_ID = "ctl00_search_btnSearch"
AIR_PERMIT_VALUE = "AIR PERMIT"


def wait_for_search_form(driver: webdriver.Chrome, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, SEARCH_BUTTON_ID)))
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, PROGRAM_SELECT_ID)))


def set_air_permit_filter(driver: webdriver.Chrome) -> None:
    select = Select(driver.find_element(By.ID, PROGRAM_SELECT_ID))
    select.select_by_value(AIR_PERMIT_VALUE)


def submit_search(driver: webdriver.Chrome, timeout: int) -> None:
    button = driver.find_element(By.ID, SEARCH_BUTTON_ID)
    button.click()
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, RESULTS_TABLE_ID)))
    time.sleep(1.0)


def set_items_per_page(driver: webdriver.Chrome, page_size: int, timeout: int) -> None:
    if page_size <= 0:
        return
    candidates = driver.find_elements(By.CSS_SELECTOR, f"#{RESULTS_TABLE_ID} select")
    for element in candidates:
        try:
            select = Select(element)
            values = {o.get_attribute("value") or o.text.strip() for o in select.options}
            if str(page_size) in values:
                select.select_by_value(str(page_size))
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, RESULTS_TABLE_ID)))
                time.sleep(1.0)
                logger.info(f"Set items-per-page to {page_size}.")
                return
        except Exception:
            continue


def extract_result_rows(page_html: str, current_url: str) -> List[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.find(id=RESULTS_TABLE_ID)
    if not table:
        return []

    rows: List[dict] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        link = tr.find("a", href=True)
        if not link:
            continue
        href = (link.get("href") or "").strip()
        text = link.get_text(" ", strip=True)
        row_text = tr.get_text(" ", strip=True)
        if not href:
            continue

        doc_url = ""
        onclick = link.get("onclick") or ""
        m_onclick = re.search(r"ViewDocument\.aspx\?docid=\d+", onclick, flags=re.IGNORECASE)
        if m_onclick:
            doc_url = urljoin(current_url, m_onclick.group(0))
        if not doc_url:
            m_text = re.search(
                r"https?://[^\s\"']*ViewDocument\.aspx\?docid=\d+",
                tr.get_text(" ", strip=True),
                flags=re.IGNORECASE,
            )
            if m_text:
                doc_url = m_text.group(0)
        if not doc_url and "javascript:" not in href.lower():
            doc_url = urljoin(current_url, href)
        if not doc_url or "ViewDocument.aspx?docid=" not in doc_url:
            continue

        docid_match = re.search(r"docid=(\d+)", doc_url, flags=re.IGNORECASE)
        docid = docid_match.group(1) if docid_match else ""
        rows.append(
            {
                "title": text,
                "document_url": doc_url,
                "docid": docid,
                "row_text": row_text,
            }
        )
    return rows


def _wait_for_table_refresh(driver: webdriver.Chrome, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, RESULTS_TABLE_ID)))
    time.sleep(1.0)


def goto_next_page(driver: webdriver.Chrome, timeout: int) -> bool:
    xpath_candidates = [
        "//*[@id='ctl00_results_DocHitList_tblDocuments']//a[contains(@title, 'Next')]",
        "//*[@id='ctl00_results_DocHitList_tblDocuments']//a[normalize-space(text())='>']",
        "//*[@id='ctl00_results_DocHitList_tblDocuments']//a[contains(normalize-space(text()), 'Next')]",
    ]
    for xpath in xpath_candidates:
        for link in driver.find_elements(By.XPATH, xpath):
            if not link.is_displayed():
                continue
            href = (link.get_attribute("href") or "").lower()
            classes = (link.get_attribute("class") or "").lower()
            if "javascript:void" in href or "disabled" in classes:
                continue
            try:
                driver.execute_script("arguments[0].click();", link)
                _wait_for_table_refresh(driver, timeout)
                return True
            except TimeoutException:
                return False
            except Exception:
                continue
    return False


def crawl_and_download(
    output_dir: Path,
    index_csv: Path,
    headless: bool,
    wait_seconds: int,
    sleep_seconds: float,
    page_size: int,
    max_pages: Optional[int],
    max_docs: Optional[int],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_index: List[dict] = []
    seen_doc_urls = set()
    total_downloaded = 0
    total_failed = 0
    total_seen = 0

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )
    driver = downloader.driver
    try:
        driver.get(SEARCH_URL)
        wait_for_search_form(driver, timeout=max(20, wait_seconds * 6))
        set_air_permit_filter(driver)
        submit_search(driver, timeout=max(20, wait_seconds * 6))
        set_items_per_page(driver, page_size=page_size, timeout=max(20, wait_seconds * 6))

        page_num = 1
        while True:
            page_rows = extract_result_rows(driver.page_source, current_url=driver.current_url)
            if not page_rows:
                no_docs = "There are no documents to display." in driver.page_source
                if no_docs:
                    logger.warning("Search returned no rows for Program = AIR PERMIT.")
                else:
                    logger.warning("No downloadable links were parsed on this page.")
                break

            logger.info(f"Page {page_num}: parsed {len(page_rows)} rows.")
            for row in page_rows:
                doc_url = row["document_url"]
                if doc_url in seen_doc_urls:
                    continue
                seen_doc_urls.add(doc_url)
                total_seen += 1

                file_hint = clean_filename(
                    f"{row.get('docid', '')}_{row.get('title', '')}".strip("_")
                )
                ok = downloader.download_document(
                    doc_url,
                    referer=driver.current_url,
                    link_text=row.get("title", ""),
                    is_table_link=True,
                    save_as=file_hint or row.get("docid") or "ohio_air_permit",
                )
                if ok:
                    status = "downloaded"
                    total_downloaded += 1
                else:
                    status = "failed_download"
                    total_failed += 1

                rows_index.append(
                    {
                        "page": page_num,
                        "title": row.get("title", ""),
                        "document_url": doc_url,
                        "docid": row.get("docid", ""),
                        "status": status,
                        "local_path": "",
                        "row_text": row.get("row_text", ""),
                    }
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                if max_docs is not None and total_seen >= max_docs:
                    logger.info(f"Reached --max-docs={max_docs}; stopping crawl.")
                    break

            if max_docs is not None and total_seen >= max_docs:
                break
            if max_pages is not None and page_num >= max_pages:
                logger.info(f"Reached --max-pages={max_pages}; stopping crawl.")
                break
            if not goto_next_page(driver, timeout=max(20, wait_seconds * 6)):
                break
            page_num += 1
    finally:
        downloader.close()

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["page", "title", "document_url", "docid", "status", "local_path", "row_text"]
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_index)

    logger.info("=" * 60)
    logger.info("OHIO AIR PERMIT DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Rows seen: {total_seen}")
    logger.info(f"Downloaded: {total_downloaded}")
    logger.info(f"Failed: {total_failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Ohio EPA eDocuments where Program = AIR PERMIT."
    )
    default_output = RAW_DATA_DIR / "ohio_epa_air_permits"
    default_index = default_output / "ohio_epa_air_permits_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloaded files (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"Path for index CSV (default: {default_index}).",
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
        help="Base wait time for Selenium waits.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
        help="Pause between downloads.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Items per result page when available (e.g., 10,20,30,50,100,250,600).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap on number of pages to crawl.",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Optional cap on number of unique document links to download.",
    )
    args = parser.parse_args()

    crawl_and_download(
        output_dir=args.output_dir.expanduser().resolve(),
        index_csv=args.index_csv.expanduser().resolve(),
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        page_size=args.page_size,
        max_pages=args.max_pages,
        max_docs=args.max_docs,
    )


if __name__ == "__main__":
    main()
