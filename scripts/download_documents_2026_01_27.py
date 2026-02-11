#!/usr/bin/env python3
"""
Download permit PDFs listed in data/external/Documents_2026_01_27.xlsx.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import EXTERNAL_DATA_DIR, RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import _create_requests_session, clean_filename


DOWNLOAD_LINK_PATTERN = re.compile(r"/doc/download\?docid=\d+", re.I)


@dataclass
class DocumentRow:
    row_index: int
    document_id: str
    ai: str
    document_subtype: str
    description: str
    date: Optional[str]
    view_url: str


def _normalize_date(value: object) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None


def iter_document_rows(xlsx_path: Path) -> Iterable[DocumentRow]:
    df = pd.read_excel(xlsx_path)
    for idx, row in df.iterrows():
        view_url = str(row.get("Document Link") or "").strip()
        if not view_url:
            continue
        yield DocumentRow(
            row_index=int(idx),
            document_id=str(row.get("Document ID") or "").strip(),
            ai=str(row.get("AI") or "").strip(),
            document_subtype=str(row.get("Document Subtype") or "").strip(),
            description=str(row.get("Description") or "").strip(),
            date=_normalize_date(row.get("Date")),
            view_url=view_url,
        )


def build_filename(row: DocumentRow, existing: Dict[str, int]) -> str:
    parts = []
    if row.ai:
        parts.append(f"AI {row.ai}")
    if row.document_id:
        parts.append(f"Doc {row.document_id}")
    if row.document_subtype:
        parts.append(row.document_subtype)
    if row.date:
        parts.append(row.date)
    if row.description:
        parts.append(row.description)

    filename = clean_filename(" - ".join(part for part in parts if part).strip()) or "permit"
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    count = existing.get(filename, 0)
    if count:
        stem = filename[:-4]
        filename = f"{stem} ({count + 1}).pdf"
    existing[filename] = count + 1
    return filename


def _extract_download_url(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    for anchor in soup.find_all("a", href=True):
        link_text = anchor.get_text(" ", strip=True).lower()
        if "download document" in link_text:
            return urljoin(base_url, anchor.get("href", ""))

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if DOWNLOAD_LINK_PATTERN.search(href):
            return urljoin(base_url, href)

    match = DOWNLOAD_LINK_PATTERN.search(html)
    if match:
        return urljoin(base_url, match.group(0))
    return None


def resolve_download_url(session, view_url: str) -> Tuple[Optional[str], Optional[str]]:
    response = session.get(view_url, timeout=60)
    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" in content_type or view_url.lower().endswith(".pdf"):
        return response.url, None

    download_url = _extract_download_url(response.text, response.url)
    if download_url:
        return download_url, None

    return None, "No download link found"


def download_pdf(session, url: str, output_path: Path) -> Optional[str]:
    response = session.get(url, stream=True, timeout=90)
    if response.status_code >= 400:
        return f"HTTP {response.status_code}"

    content_type = (response.headers.get("content-type") or "").lower()
    if "application/pdf" not in content_type and "application/octet-stream" not in content_type:
        return f"Unexpected content-type: {content_type or 'unknown'}"

    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    with tmp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                handle.write(chunk)
    tmp_path.replace(output_path)
    return None


def _setup_driver(download_dir: Path, headless: bool) -> webdriver.Chrome:
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    prefs = {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": False,
        "safebrowsing.disable_download_protection": True,
        "plugins.always_open_pdf_externally": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    return webdriver.Chrome(options=chrome_options)


def _wait_for_download(download_dir: Path, start_time: float, timeout: int = 120) -> Optional[Path]:
    elapsed = 0
    while elapsed < timeout:
        pdfs = [
            path
            for path in download_dir.glob("*")
            if path.is_file()
            and not path.name.endswith(".crdownload")
            and path.stat().st_mtime >= start_time
        ]
        if pdfs:
            return max(pdfs, key=lambda path: path.stat().st_mtime)
        time.sleep(1)
        elapsed += 1
    return None


def download_via_browser(
    view_url: str,
    output_path: Path,
    download_dir: Path,
    headless: bool,
    wait_seconds: int = 6,
) -> Optional[str]:
    driver = _setup_driver(download_dir, headless=headless)
    try:
        driver.get(view_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        try:
            download_link = driver.find_element(
                By.XPATH,
                "//a[contains(., 'Download Document') or .//span[contains(., 'Download Document')]]",
            )
            download_link.click()
        except Exception as exc:
            return f"Could not click Download Document link: {exc}"

        time.sleep(1)
        if len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])

        logger.info("Solve the reCAPTCHA checkbox in the browser.")
        logger.info("Then click the final Download button and press Enter here.")
        input()

        try:
            buttons = driver.find_elements(
                By.XPATH,
                "//a[contains(., 'Download')]|//button[contains(., 'Download')]",
            )
            for button in buttons:
                try:
                    button.click()
                    break
                except Exception:
                    continue
        except Exception:
            pass

        start_time = time.time()
        downloaded = _wait_for_download(download_dir, start_time=start_time, timeout=180)
        if not downloaded:
            return "Timed out waiting for download."

        output_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded.replace(output_path)
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def download_files(
    xlsx_path: Path,
    output_dir: Path,
    limit: Optional[int],
    sleep_seconds: float,
    use_selenium: bool,
    headless: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = _create_requests_session()
    download_dir = output_dir / "_browser_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    totals = {"rows": 0, "resolved": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    filename_counts: Dict[str, int] = {}

    for row in iter_document_rows(xlsx_path):
        totals["rows"] += 1
        if limit and totals["rows"] > limit:
            logger.info(f"Reached download limit ({limit}); stopping.")
            break

        filename = build_filename(row, filename_counts)
        output_path = output_dir / filename
        if output_path.exists():
            totals["skipped"] += 1
            logger.info(f"Skipping existing: {output_path.name}")
            continue

        logger.info(f"Resolving download URL for doc {row.document_id or row.row_index}")
        download_url, error = resolve_download_url(session, row.view_url)
        if error or not download_url:
            if use_selenium:
                logger.warning(
                    f"Failed to resolve download URL for {row.view_url}; trying browser flow."
                )
                error = download_via_browser(
                    row.view_url,
                    output_path,
                    download_dir,
                    headless=headless,
                )
                if error:
                    totals["failed"] += 1
                    logger.warning(f"Browser download failed for {row.view_url}: {error}")
                else:
                    totals["downloaded"] += 1
                continue

            totals["failed"] += 1
            logger.warning(f"Failed to resolve download URL for {row.view_url}: {error}")
            continue

        totals["resolved"] += 1
        logger.info(f"Downloading: {output_path.name}")
        error = download_pdf(session, download_url, output_path)
        if error:
            if use_selenium:
                logger.warning(f"Direct download failed; trying browser flow: {error}")
                error = download_via_browser(
                    row.view_url,
                    output_path,
                    download_dir,
                    headless=headless,
                )
                if error:
                    totals["failed"] += 1
                    logger.warning(f"Browser download failed for {row.view_url}: {error}")
                else:
                    totals["downloaded"] += 1
                continue

            totals["failed"] += 1
            logger.warning(f"Failed download for {download_url}: {error}")
        else:
            totals["downloaded"] += 1

        if sleep_seconds:
            time.sleep(sleep_seconds)

    logger.info("Download summary")
    logger.info(f"Total rows processed: {totals['rows']}")
    logger.info(f"Download URLs resolved: {totals['resolved']}")
    logger.info(f"Downloaded: {totals['downloaded']}")
    logger.info(f"Skipped: {totals['skipped']}")
    logger.info(f"Failed: {totals['failed']}")
    logger.info(f"Output directory: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download permit PDFs from Documents_2026_01_27.xlsx.")
    default_xlsx = EXTERNAL_DATA_DIR / "Documents_2026_01_27.xlsx"
    default_output = RAW_DATA_DIR / "documents_2026_01_27"
    parser.add_argument(
        "--xlsx-path",
        type=Path,
        default=default_xlsx,
        help=f"Path to Excel file (default: {default_xlsx}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of downloads.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.25,
        help="Seconds to sleep between downloads (default: 0.25).",
    )
    parser.add_argument(
        "--use-selenium",
        action="store_true",
        help="Use a browser flow for reCAPTCHA-protected downloads.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode (required for reCAPTCHA).",
    )

    args = parser.parse_args()
    headless = not args.no_headless
    if args.use_selenium and headless:
        logger.warning("Forcing visible browser because reCAPTCHA requires it.")
        headless = False

    download_files(
        args.xlsx_path.expanduser(),
        args.output_dir.expanduser(),
        args.limit,
        args.sleep_seconds,
        args.use_selenium,
        headless,
    )


if __name__ == "__main__":
    main()
