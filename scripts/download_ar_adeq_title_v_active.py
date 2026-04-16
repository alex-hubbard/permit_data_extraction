#!/usr/bin/env python3
"""
Download Arkansas DEQ permits from the PDS search page.

Workflow:
1) Open https://www.adeq.state.ar.us/home/pdssql/pds.aspx#Display
2) Select Media/Type = "Air - Title V"
3) Select Permit Status = "Active"
4) Click Search
5) Walk all result pages and download permit links found in table rows
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from loguru import logger
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

PDS_URL = "https://www.adeq.state.ar.us/home/pdssql/pds.aspx#Display"
MEDIA_TYPE_TEXT = "Air - Title V"
PERMIT_STATUS_TEXT = "Active"


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _find_select_by_label_text(driver, label_text: str):
    needles = [
        f"//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label_text.lower()}')]/following::select[1]",
        f"//*[self::td or self::th or self::span or self::strong][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{label_text.lower()}')]/following::select[1]",
    ]
    for xp in needles:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems:
            try:
                if elem.tag_name.lower() == "select":
                    return elem
            except Exception:
                continue
    return None


def _find_select_with_option_text(driver, option_text: str):
    xp = (
        "//select[.//option[contains("
        "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"'{option_text.strip().lower()}')]]"
    )
    elems = driver.find_elements(By.XPATH, xp)
    return elems[0] if elems else None


def _select_option_by_text(driver, select_elem, option_text: str) -> bool:
    sel = Select(select_elem)
    wanted = option_text.strip().lower()
    for opt in sel.options:
        text = _normalize_space(opt.text)
        if text.lower() == wanted:
            sel.select_by_visible_text(opt.text)
            return True
    for opt in sel.options:
        text = _normalize_space(opt.text)
        if wanted in text.lower():
            sel.select_by_visible_text(opt.text)
            return True
    return False


def _click_search(driver) -> bool:
    candidates = driver.find_elements(
        By.XPATH,
        "//input[@type='submit' or @type='button'] | //button",
    )
    for elem in candidates:
        label = _normalize_space(
            f"{elem.get_attribute('value') or ''} {elem.text or ''} {elem.get_attribute('title') or ''}"
        ).lower()
        if "search" in label:
            driver.execute_script("arguments[0].click();", elem)
            return True
    return False


def _wait_for_results(driver, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: (
            "permit number" in (d.page_source or "").lower()
            and (
                "copy of permit online" in (d.page_source or "").lower()
                or "no records found" in (d.page_source or "").lower()
            )
        )
    )


def _extract_url_from_js(value: str, base_url: str) -> Optional[str]:
    txt = value or ""
    for pat in [
        r"https?://[^\s\"')]+",
        r"['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]",
        r"['\"]([^'\"]+download[^'\"]*)['\"]",
    ]:
        match = re.search(pat, txt, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(0) if pat.startswith("https?") else match.group(1)
        return urljoin(base_url, raw)
    return None


def _get_results_table(driver):
    xpaths = [
        "//table[.//th[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'permit number')]]",
        "//table[contains(., 'Permit Number') and contains(., 'Permit Status')]",
    ]
    for xp in xpaths:
        tables = driver.find_elements(By.XPATH, xp)
        if tables:
            return tables[0]
    return None


def _extract_rows_with_links(driver) -> List[Dict[str, str]]:
    table = _get_results_table(driver)
    if table is None:
        return []

    rows: List[Dict[str, str]] = []
    trs = table.find_elements(By.XPATH, ".//tr[td]")
    for tr in trs:
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) < 2:
            continue

        row_text = _normalize_space(tr.text)
        if not row_text:
            continue

        permit_number = _normalize_space(tds[0].text) if tds else ""
        facility_name = _normalize_space(tds[1].text) if len(tds) > 1 else ""
        permit_status = _normalize_space(tds[-2].text) if len(tds) >= 2 else ""

        anchors = tr.find_elements(By.TAG_NAME, "a")
        row_urls: Set[str] = set()
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            onclick = (a.get_attribute("onclick") or "").strip()
            url = ""
            if href and not href.lower().startswith("javascript:") and href != "#":
                url = urljoin(driver.current_url, href)
            elif onclick:
                js_url = _extract_url_from_js(onclick, driver.current_url)
                if js_url:
                    url = js_url
            if not url:
                continue

            anchor_text = _normalize_space(a.text).lower()
            low = url.lower()
            # Keep only likely file/document URLs from the "Copy of Permit Online" style column.
            # Avoid row/detail pages (e.g., p_permit_details_air.aspx) that are not documents.
            if (
                ".pdf" in low
                or "permitsonline/" in low
                or "downloads/webdatabases/permitsonline/" in low
                or ("download" in low and ".aspx" not in low)
                or ("copy" in anchor_text and ".aspx" not in low)
            ):
                row_urls.add(url)

        for url in sorted(row_urls):
            rows.append(
                {
                    "permit_number": permit_number,
                    "facility_name": facility_name,
                    "permit_status": permit_status,
                    "document_url": url,
                    "row_text": row_text,
                }
            )

    return rows


def _first_row_fingerprint(driver) -> str:
    rows = _extract_rows_with_links(driver)
    if not rows:
        return ""
    first = rows[0]
    return "|".join(
        [
            first.get("permit_number", ""),
            first.get("facility_name", ""),
            first.get("document_url", ""),
        ]
    )


def _go_to_next_page(driver, timeout: int) -> bool:
    before = _first_row_fingerprint(driver)
    xpath_candidates = [
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
        "//a[@title='Next']",
        "//a[normalize-space(.)='>']",
    ]
    for xp in xpath_candidates:
        candidates = driver.find_elements(By.XPATH, xp)
        for elem in candidates:
            if not elem.is_displayed():
                continue
            href = (elem.get_attribute("href") or "").lower()
            classes = (elem.get_attribute("class") or "").lower()
            if "void" in href or "disabled" in classes:
                continue
            try:
                driver.execute_script("arguments[0].click();", elem)
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(1.0)
                if before:
                    WebDriverWait(driver, timeout).until(
                        lambda d: _first_row_fingerprint(d) != before
                    )
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
    max_pages: Optional[int],
    max_docs: Optional[int],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: List[dict] = []
    seen_urls: Set[str] = set()

    downloaded = 0
    failed = 0
    seen = 0

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )
    driver = downloader.driver
    try:
        driver.get(PDS_URL)
        WebDriverWait(driver, max(20, wait_seconds * 6)).until(
            EC.presence_of_element_located((By.TAG_NAME, "form"))
        )

        media_select = _find_select_by_label_text(driver, "media/type")
        if media_select is None:
            media_select = _find_select_with_option_text(driver, MEDIA_TYPE_TEXT)
        if media_select is None:
            raise RuntimeError("Could not locate Media/Type select.")
        if not _select_option_by_text(driver, media_select, MEDIA_TYPE_TEXT):
            raise RuntimeError(f"Could not set Media/Type to '{MEDIA_TYPE_TEXT}'.")

        status_select = _find_select_by_label_text(driver, "permit status")
        if status_select is None:
            status_select = _find_select_with_option_text(driver, PERMIT_STATUS_TEXT)
        if status_select is None:
            raise RuntimeError("Could not locate Permit Status select.")
        if not _select_option_by_text(driver, status_select, PERMIT_STATUS_TEXT):
            raise RuntimeError(f"Could not set Permit Status to '{PERMIT_STATUS_TEXT}'.")

        if not _click_search(driver):
            raise RuntimeError("Could not find Search button.")
        _wait_for_results(driver, timeout=max(20, wait_seconds * 10))

        if "no records found" in (driver.page_source or "").lower():
            logger.warning("Search returned no records.")
            return

        page_num = 1
        while True:
            page_rows = _extract_rows_with_links(driver)
            logger.info(f"Page {page_num}: found {len(page_rows)} document link(s).")

            for row in page_rows:
                doc_url = row.get("document_url", "")
                if not doc_url or doc_url in seen_urls:
                    continue
                seen_urls.add(doc_url)
                seen += 1

                permit_no = row.get("permit_number", "")
                facility = row.get("facility_name", "")
                parsed = urlparse(doc_url)
                basename = Path(parsed.path).name or f"ar_{permit_no or seen}"
                save_as = clean_filename(f"{permit_no}_{facility}_{basename}".strip("_")) or basename

                before = set(output_dir.glob("*.pdf"))
                ok = downloader.download_document(
                    doc_url,
                    referer=driver.current_url,
                    link_text=permit_no or facility or "permit",
                    is_table_link=True,
                    save_as=save_as,
                )

                local_path = ""
                if ok:
                    downloaded += 1
                    after = set(output_dir.glob("*.pdf"))
                    new_files = sorted(after - before, key=lambda p: p.stat().st_mtime)
                    if new_files:
                        local_path = str(new_files[-1])
                else:
                    failed += 1

                index_rows.append(
                    {
                        "page": page_num,
                        "permit_number": permit_no,
                        "facility_name": facility,
                        "permit_status": row.get("permit_status", ""),
                        "document_url": doc_url,
                        "status": "downloaded" if ok else "failed_download",
                        "local_path": local_path,
                        "row_text": row.get("row_text", ""),
                    }
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                if max_docs is not None and seen >= max_docs:
                    logger.info(f"Reached --max-docs={max_docs}; stopping.")
                    break

            if max_docs is not None and seen >= max_docs:
                break
            if max_pages is not None and page_num >= max_pages:
                logger.info(f"Reached --max-pages={max_pages}; stopping.")
                break
            if not _go_to_next_page(driver, timeout=max(20, wait_seconds * 8)):
                break
            page_num += 1
    finally:
        downloader.close()

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "page",
        "permit_number",
        "facility_name",
        "permit_status",
        "document_url",
        "status",
        "local_path",
        "row_text",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(index_rows)

    logger.info("=" * 60)
    logger.info("ARKANSAS ADEQ TITLE V DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Document links seen: {seen}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Arkansas ADEQ PDS permits filtered to Media/Type=Air - Title V and Permit Status=Active."
    )
    default_output = RAW_DATA_DIR / "ar_adeq_title_v_active"
    default_index = default_output / "ar_adeq_title_v_active_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for downloaded PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"CSV index path (default: {default_index}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5,
        help="Base wait time used by Selenium waits.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.25,
        help="Pause between downloads.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit on crawled results pages.",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Optional limit on unique permit links to download.",
    )
    args = parser.parse_args()

    crawl_and_download(
        output_dir=args.output_dir.expanduser().resolve(),
        index_csv=args.index_csv.expanduser().resolve(),
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        max_pages=args.max_pages,
        max_docs=args.max_docs,
    )


if __name__ == "__main__":
    main()
