#!/usr/bin/env python3
"""
Download South Carolina Bureau of Air Quality operating permit PDFs from the
Air Permit Coverage search (https://apps.des.sc.gov/PermitCoverage/Home).

Selects a permit type (default AIR-TV-Regular / Title V), submits the search,
walks every results page, and downloads each row's Permit button PDF using the
shared Selenium session.
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import parse_qs, urlparse

from loguru import logger
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    TimeoutException,
    UnexpectedAlertPresentException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

HOME_URL = "https://apps.des.sc.gov/PermitCoverage/Home"
DEFAULT_PERMIT_TYPE = "AIR-TV-Regular"


def _span_text(driver, elem_id: str) -> str:
    el = driver.find_element(By.ID, elem_id)
    return (el.get_attribute("textContent") or "").strip()


def _open_permit_type_and_select(driver, permit_type_value: str) -> None:
    driver.execute_script("showPermitCheckboxes();")
    driver.execute_script("selectPermitTypeOptions(arguments[0]);", permit_type_value)
    cb = driver.find_element(
        By.CSS_SELECTOR,
        f'input[name="selectedPermitTypes"][value="{permit_type_value}"]',
    )
    if not cb.is_selected():
        raise RuntimeError(f"Could not select permit type checkbox: {permit_type_value}")


def _wait_for_search_results(driver, timeout: int = 90) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "gridTable")))


def _dismiss_alert_if_present(driver) -> Optional[str]:
    """
    The grid script uses alert('PAGING ERROR') on failed AJAX; that blocks automation
    until dismissed. Returns alert text if one was closed.
    """
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return text
    except NoAlertPresentException:
        return None


def _wait_for_jquery_idle(driver, timeout: int = 90) -> None:
    """Wait for jQuery.ajax to finish; dismiss paging alerts that block execute_script."""
    time.sleep(0.35)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _dismiss_alert_if_present(driver)
        try:
            idle = driver.execute_script(
                "return typeof jQuery !== 'undefined' && jQuery.active === 0"
            )
            if idle:
                return
        except UnexpectedAlertPresentException:
            _dismiss_alert_if_present(driver)
            continue
        time.sleep(0.12)
    raise TimeoutException("jQuery did not become idle")


def _go_to_result_page(driver, page_num: int, max_attempts: int = 6) -> None:
    if page_num < 2:
        return
    for attempt in range(1, max_attempts + 1):
        _dismiss_alert_if_present(driver)
        try:
            page_li = driver.find_element(By.ID, f"page-{page_num}")
            link = page_li.find_element(By.CSS_SELECTOR, "a.page-link")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", link)
            driver.execute_script("arguments[0].click();", link)
        except NoSuchElementException:
            driver.execute_script("pagination(arguments[0]);", page_num)
        time.sleep(0.2)
        try:
            _wait_for_jquery_idle(driver, timeout=120)
        except TimeoutException:
            logger.warning(f"jQuery idle timeout after paging to {page_num} (attempt {attempt})")

        msg = _dismiss_alert_if_present(driver)
        if msg and "PAGING" in msg.upper():
            logger.warning(
                f"Server paging error ({msg!r}) for page {page_num}, "
                f"attempt {attempt}/{max_attempts}; retrying after delay."
            )
            time.sleep(2.0 * attempt)
            continue

        try:
            WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#gridTable tbody tr"))
            )
        except TimeoutException:
            if attempt < max_attempts:
                logger.warning(f"No grid rows after paging to {page_num}; retrying.")
                time.sleep(2.0 * attempt)
                continue
            raise
        return

    raise RuntimeError(f"Could not load results page {page_num} after {max_attempts} attempts")


def _parse_permit_number_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("permitNumber") or []
    return vals[0].strip() if vals else ""


def _log_run_summary(output_dir: Path, downloaded: int, skipped: int, failed: int) -> None:
    logger.info("=" * 60)
    logger.info(
        f"Done. Downloaded: {downloaded}, skipped: {skipped}, failed: {failed}, "
        f"output: {output_dir}"
    )


def _write_index_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "permit_number",
        "facility_name",
        "pdf_url",
        "local_path",
        "status",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote index: {path}")


def _collect_rows_current_page(driver) -> List[dict]:
    rows: List[dict] = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "#gridTable tbody tr"):
        cells = tr.find_elements(By.TAG_NAME, "td")
        if len(cells) < 8:
            continue
        permit_links = cells[0].find_elements(By.CSS_SELECTOR, "a")
        permit_number = permit_links[0].text.strip() if permit_links else ""
        facility_links = cells[2].find_elements(By.CSS_SELECTOR, "a.facilityModal")
        facility_name = (
            facility_links[0].text.strip() if facility_links else cells[2].text.strip()
        )
        pdf_links = tr.find_elements(By.CSS_SELECTOR, 'a[href*="permitPDF"]')
        if not pdf_links:
            continue
        href = pdf_links[0].get_attribute("href")
        if not href:
            continue
        rows.append(
            {
                "permit_number": permit_number,
                "facility_name": facility_name,
                "pdf_url": href,
            }
        )
    return rows


def download_sc_permits(
    output_dir: Path,
    permit_type: str,
    headless: bool,
    wait_seconds: int,
    sleep_seconds: float,
    limit: Optional[int],
    skip_existing: bool,
    index_csv: Optional[Path],
) -> None:
    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    downloaded = 0
    skipped = 0
    failed = 0
    seen_urls: Set[str] = set()
    index_rows: List[dict] = []

    try:
        driver = downloader.driver
        driver.get(HOME_URL)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "permitTypeCheckboxes"))
        )
        _open_permit_type_and_select(driver, permit_type)
        submit = driver.find_element(By.ID, "submitButton")
        driver.execute_script("arguments[0].click();", submit)
        _wait_for_search_results(driver)

        total_count = int(_span_text(driver, "totalCount") or "0")
        page_size = max(1, int(_span_text(driver, "pageSize") or "15"))
        total_pages = int(_span_text(driver, "totalPages") or "0")
        if total_pages < 1 and total_count > 0:
            total_pages = max(1, (total_count + page_size - 1) // page_size)
        total_pages = max(1, total_pages)
        if total_count == 0:
            logger.warning("Search returned no rows.")
            return

        logger.info(
            f"Found {total_count} permit(s) across {total_pages} page(s) "
            f"(permit type {permit_type!r})."
        )

        for page in range(1, total_pages + 1):
            _go_to_result_page(driver, page)
            page_rows = _collect_rows_current_page(driver)
            logger.info(f"Page {page}/{total_pages}: {len(page_rows)} downloadable row(s).")

            for row in page_rows:
                pdf_url = row["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                permit_num = row["permit_number"] or _parse_permit_number_from_url(pdf_url)
                facility = row["facility_name"] or "facility"
                base_name = f"{permit_num} - {facility}"
                link_label = clean_filename(base_name) + ".pdf"

                dest = output_dir / link_label
                if skip_existing and dest.exists():
                    skipped += 1
                    logger.info(f"Skip existing: {dest.name}")
                    if index_csv is not None:
                        index_rows.append(
                            {
                                "permit_number": permit_num,
                                "facility_name": facility,
                                "pdf_url": pdf_url,
                                "local_path": str(dest),
                                "status": "skipped_existing",
                            }
                        )
                    continue

                if limit is not None and downloaded >= limit:
                    logger.info(f"Reached --limit ({limit}); stopping.")
                    if index_csv is not None:
                        _write_index_csv(index_csv, index_rows)
                    _log_run_summary(output_dir, downloaded, skipped, failed)
                    return

                before_pdfs = set(output_dir.glob("*.pdf"))
                referer = driver.current_url or HOME_URL
                ok = downloader.download_document(
                    pdf_url,
                    referer=referer,
                    link_text=link_label,
                    is_table_link=True,
                )
                if ok:
                    downloaded += 1
                    after_pdfs = set(output_dir.glob("*.pdf"))
                    new_pdfs = sorted(after_pdfs - before_pdfs, key=lambda p: p.stat().st_mtime)
                    final_path = dest
                    if new_pdfs:
                        candidate = new_pdfs[-1]
                        if candidate.resolve() != final_path.resolve():
                            candidate.replace(final_path)
                    if index_csv is not None:
                        index_rows.append(
                            {
                                "permit_number": permit_num,
                                "facility_name": facility,
                                "pdf_url": pdf_url,
                                "local_path": str(final_path),
                                "status": "downloaded",
                            }
                        )
                else:
                    failed += 1
                    if index_csv is not None:
                        index_rows.append(
                            {
                                "permit_number": permit_num,
                                "facility_name": facility,
                                "pdf_url": pdf_url,
                                "local_path": "",
                                "status": "failed",
                            }
                        )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    finally:
        downloader.close()

    _log_run_summary(output_dir, downloaded, skipped, failed)
    if index_csv is not None:
        _write_index_csv(index_csv, index_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download SC DES air permit PDFs (e.g. Title V / AIR-TV-Regular) from Permit Coverage."
    )
    default_out = RAW_DATA_DIR / "sc_des_air_permits"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_out,
        help=f"Directory for PDFs (default: {default_out}).",
    )
    parser.add_argument(
        "--permit-type",
        default=DEFAULT_PERMIT_TYPE,
        help=f"Permit type checkbox value (default: {DEFAULT_PERMIT_TYPE}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome with a visible window.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Base wait for SeleniumPDFDownloader.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.75,
        help="Pause between PDF downloads.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of PDFs to download (after skips).",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Download even when the target filename already exists.",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=None,
        help="Write a CSV index of downloads to this path (default: no index file).",
    )

    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    index_csv = args.index_csv.expanduser() if args.index_csv else None

    download_sc_permits(
        output_dir=output_dir,
        permit_type=args.permit_type,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
        skip_existing=not args.no_skip_existing,
        index_csv=index_csv,
    )


if __name__ == "__main__":
    main()
