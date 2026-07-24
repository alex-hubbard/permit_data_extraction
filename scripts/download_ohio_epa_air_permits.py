#!/usr/bin/env python3
"""
Download Ohio EPA eDocument records filtered to Program = AIR PERMIT.

Portal:
https://edocpub.epa.ohio.gov/publicportal/edochome.aspx

The portal silently stops advancing its pager after ~18 pages (~1,800 rows at
100/page), so a single unfiltered search only ever yields the newest slice.
This script sweeps the Document Date range (From/To Date fields) in buckets,
recursively halving any bucket that looks truncated, until every bucket pages
out naturally. Progress is resumable: docids are recovered from the index CSV
and from filenames already in the output directory, and the index CSV is
appended row-by-row as documents are processed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Set, Tuple
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
FROM_DATE_INPUT_ID = "ctl00_search_KeywordPanel1_txtFrom"
TO_DATE_INPUT_ID = "ctl00_search_KeywordPanel1_txtTo"
SEARCH_BUTTON_ID = "ctl00_search_btnSearch"
AIR_PERMIT_VALUE = "AIR PERMIT"

INDEX_FIELDS = ["bucket", "page", "title", "document_url", "docid", "status", "local_path", "row_text"]


def wait_for_search_form(driver: webdriver.Chrome, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, SEARCH_BUTTON_ID)))
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, PROGRAM_SELECT_ID)))


def set_air_permit_filter(driver: webdriver.Chrome) -> None:
    select = Select(driver.find_element(By.ID, PROGRAM_SELECT_ID))
    select.select_by_value(AIR_PERMIT_VALUE)


def set_date_filter(driver: webdriver.Chrome, from_date: Optional[date], to_date: Optional[date]) -> None:
    for element_id, value in ((FROM_DATE_INPUT_ID, from_date), (TO_DATE_INPUT_ID, to_date)):
        field = driver.find_element(By.ID, element_id)
        field.clear()
        if value is not None:
            field.send_keys(value.strftime("%m/%d/%Y"))


def submit_search(driver: webdriver.Chrome, timeout: int) -> bool:
    """Click search; return False when the search yields no result table."""
    button = driver.find_element(By.ID, SEARCH_BUTTON_ID)
    button.click()
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, RESULTS_TABLE_ID)))
    except TimeoutException:
        if "There are no documents to display." in driver.page_source:
            return False
        raise
    time.sleep(1.0)
    return True


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
    # The grid re-renders under us, so any element handle can go stale between
    # find and use; treat staleness as "rescan the candidates", never crash.
    for attempt in range(3):
        for xpath in xpath_candidates:
            for link in driver.find_elements(By.XPATH, xpath):
                try:
                    if not link.is_displayed():
                        continue
                    href = (link.get_attribute("href") or "").lower()
                    classes = (link.get_attribute("class") or "").lower()
                    if "javascript:void" in href or "disabled" in classes:
                        continue
                    driver.execute_script("arguments[0].click();", link)
                    _wait_for_table_refresh(driver, timeout)
                    return True
                except TimeoutException:
                    return False
                except Exception:
                    continue
        time.sleep(1.0)
    return False


def migrate_index_csv(index_csv: Path) -> None:
    """Rewrite a pre-sweep index (7 cols, no bucket) to the current schema."""
    if not index_csv.exists():
        return
    with index_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] == INDEX_FIELDS:
        return
    logger.info(f"Migrating {index_csv.name} to {len(INDEX_FIELDS)}-column schema.")
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(INDEX_FIELDS)
        for row in rows[1:]:
            if len(row) == len(INDEX_FIELDS) - 1:
                row = [""] + row
            writer.writerow(row[: len(INDEX_FIELDS)])


def load_seen_docids(index_csv: Path, output_dir: Path) -> Set[str]:
    """Resume set: docids from the index CSV plus docid-prefixed filenames on disk.

    Rows with failed_download status are excluded so failures retry on resume.
    """
    seen: Set[str] = set()
    if index_csv.exists():
        with index_csv.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                docid = (row.get("docid") or "").strip()
                if docid.isdigit() and row.get("status") != "failed_download":
                    seen.add(docid)
    if output_dir.exists():
        for path in output_dir.iterdir():
            m = re.match(r"(\d+)_", path.name)
            if m:
                seen.add(m.group(1))
    return seen


class IndexWriter:
    """Append-as-you-go index CSV so interruptions lose nothing."""

    def __init__(self, index_csv: Path):
        index_csv.parent.mkdir(parents=True, exist_ok=True)
        new_file = not index_csv.exists() or index_csv.stat().st_size == 0
        self._handle = index_csv.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if new_file:
            self._writer.writeheader()

    def write(self, row: dict) -> None:
        self._writer.writerow(row)
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class Crawler:
    def __init__(
        self,
        output_dir: Path,
        index_writer: IndexWriter,
        seen_docids: Set[str],
        headless: bool,
        wait_seconds: int,
        sleep_seconds: float,
        page_size: int,
        stale_pages: int,
    ):
        self.output_dir = output_dir
        self.index = index_writer
        self.seen_docids = seen_docids
        self.wait_seconds = wait_seconds
        self.sleep_seconds = sleep_seconds
        self.page_size = page_size
        self.stale_pages = stale_pages
        self.total_downloaded = 0
        self.total_failed = 0
        self.downloader = SeleniumPDFDownloader(
            output_dir=output_dir,
            headless=headless,
            wait_seconds=wait_seconds,
            max_depth=0,
            use_llm=False,
        )
        self.driver = self.downloader.driver

    def close(self) -> None:
        self.downloader.close()

    def crawl_bucket(self, from_date: Optional[date], to_date: Optional[date]) -> Tuple[int, int]:
        """Search one date bucket and download every new doc.

        Returns (unique_rows_in_bucket, new_downloads). The pager silently stops
        advancing (same rows re-served) both at the portal's result cap and on
        the true last page, so "page contributed no unseen docid" is the only
        reliable stop signal; the caller decides cap-vs-complete from the count.
        """
        bucket_label = f"{from_date or 'begin'}..{to_date or 'today'}"
        timeout = max(20, self.wait_seconds * 6)
        driver = self.driver

        driver.get(SEARCH_URL)
        wait_for_search_form(driver, timeout=timeout)
        set_air_permit_filter(driver)
        set_date_filter(driver, from_date, to_date)
        if not submit_search(driver, timeout=timeout):
            logger.info(f"[{bucket_label}] no documents.")
            return 0, 0
        set_items_per_page(driver, page_size=self.page_size, timeout=timeout)

        bucket_docids: Set[str] = set()
        new_downloads = 0
        page_num = 1
        stale_streak = 0
        while True:
            page_rows = extract_result_rows(driver.page_source, current_url=driver.current_url)
            if not page_rows:
                break

            fresh = [r for r in page_rows if r["docid"] not in bucket_docids]
            bucket_docids.update(r["docid"] for r in page_rows)
            if not fresh:
                stale_streak += 1
                if stale_streak >= self.stale_pages:
                    logger.info(
                        f"[{bucket_label}] pager stopped advancing at page {page_num} "
                        f"({len(bucket_docids)} unique rows)."
                    )
                    break
            else:
                stale_streak = 0
                logger.info(f"[{bucket_label}] page {page_num}: {len(fresh)} new of {len(page_rows)} rows.")

            for row in fresh:
                if row["docid"] in self.seen_docids:
                    continue
                self.seen_docids.add(row["docid"])
                file_hint = clean_filename(
                    f"{row.get('docid', '')}_{row.get('title', '')}".strip("_")
                )
                ok = self.downloader.download_document(
                    row["document_url"],
                    referer=driver.current_url,
                    link_text=row.get("title", ""),
                    is_table_link=True,
                    save_as=file_hint or row.get("docid") or "ohio_air_permit",
                )
                if ok:
                    self.total_downloaded += 1
                    new_downloads += 1
                    status = "downloaded"
                else:
                    self.total_failed += 1
                    status = "failed_download"
                self.index.write(
                    {
                        "bucket": bucket_label,
                        "page": page_num,
                        "title": row.get("title", ""),
                        "document_url": row["document_url"],
                        "docid": row.get("docid", ""),
                        "status": status,
                        "local_path": "",
                        "row_text": row.get("row_text", ""),
                    }
                )
                if self.sleep_seconds > 0:
                    time.sleep(self.sleep_seconds)

            if not goto_next_page(driver, timeout=timeout):
                break
            page_num += 1

        return len(bucket_docids), new_downloads


def sweep(crawler: Crawler, from_date: date, to_date: date, cap_threshold: int) -> None:
    """Crawl [from_date, to_date]; recursively halve ranges that hit the result cap."""
    unique_rows, new_downloads = crawler.crawl_bucket(from_date, to_date)
    label = f"{from_date}..{to_date}"
    if unique_rows < cap_threshold:
        logger.info(f"[{label}] complete: {unique_rows} rows, {new_downloads} new downloads.")
        return
    if from_date == to_date:
        logger.warning(
            f"[{label}] single day still returns {unique_rows} rows (>= cap {cap_threshold}); "
            "some documents on this date may be unreachable."
        )
        return
    mid = from_date + (to_date - from_date) / 2
    logger.info(f"[{label}] hit result cap ({unique_rows} rows) — splitting at {mid}.")
    sweep(crawler, from_date, mid, cap_threshold)
    sweep(crawler, mid + timedelta(days=1), to_date, cap_threshold)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Ohio EPA eDocuments where Program = AIR PERMIT."
    )
    default_output = RAW_DATA_DIR / "ohio_epa_air_permits"
    default_index = default_output / "ohio_epa_air_permits_index.csv"
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--index-csv", type=Path, default=default_index)
    parser.add_argument("--no-headless", action="store_true", help="Run Chrome in visible mode.")
    parser.add_argument("--wait-seconds", type=int, default=4)
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Pause between downloads.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--from-date", type=date.fromisoformat, default=date(1970, 1, 1),
        help="Sweep start (ISO date, default 1970-01-01).",
    )
    parser.add_argument(
        "--to-date", type=date.fromisoformat, default=None,
        help="Sweep end (ISO date, default today).",
    )
    parser.add_argument(
        "--year-buckets", type=int, default=2,
        help="Initial bucket width in years; capped buckets are halved recursively.",
    )
    parser.add_argument(
        "--cap-threshold", type=int, default=1700,
        help="Unique rows at/above which a bucket is assumed truncated by the portal cap.",
    )
    parser.add_argument(
        "--stale-pages", type=int, default=2,
        help="Consecutive pages with no unseen docid before declaring the pager done.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    index_csv = args.index_csv.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    migrate_index_csv(index_csv)
    seen = load_seen_docids(index_csv, output_dir)
    logger.info(f"Resume set: {len(seen)} docids already downloaded/indexed.")

    to_date = args.to_date or date.today()
    state_path = output_dir / "sweep_state.json"
    done_buckets: Set[str] = set()
    if state_path.exists():
        done_buckets = set(json.loads(state_path.read_text()))

    index_writer = IndexWriter(index_csv)

    def make_crawler() -> Crawler:
        return Crawler(
            output_dir=output_dir,
            index_writer=index_writer,
            seen_docids=seen,
            headless=not args.no_headless,
            wait_seconds=args.wait_seconds,
            sleep_seconds=args.sleep_seconds,
            page_size=args.page_size,
            stale_pages=args.stale_pages,
        )

    crawler = make_crawler()
    try:
        bucket_start = args.from_date
        while bucket_start <= to_date:
            bucket_end = min(
                date(bucket_start.year + args.year_buckets, bucket_start.month, 1) - timedelta(days=1),
                to_date,
            )
            key = f"{bucket_start}..{bucket_end}"
            # Never checkpoint a bucket ending at the sweep edge — new docs keep
            # arriving there, so it must be re-swept on every run.
            checkpointable = bucket_end < to_date
            if key in done_buckets:
                bucket_start = bucket_end + timedelta(days=1)
                continue
            try:
                sweep(crawler, bucket_start, bucket_end, cap_threshold=args.cap_threshold)
            except Exception as exc:
                logger.warning(f"[{key}] bucket failed ({type(exc).__name__}: {exc}); restarting browser.")
                try:
                    crawler.close()
                except Exception:
                    pass
                crawler = make_crawler()
                sweep(crawler, bucket_start, bucket_end, cap_threshold=args.cap_threshold)
            if checkpointable:
                done_buckets.add(key)
                state_path.write_text(json.dumps(sorted(done_buckets)))
            bucket_start = bucket_end + timedelta(days=1)
    finally:
        crawler.close()
        index_writer.close()

    logger.info("=" * 60)
    logger.info("OHIO AIR PERMIT DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Downloaded: {crawler.total_downloaded}")
    logger.info(f"Failed: {crawler.total_failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
