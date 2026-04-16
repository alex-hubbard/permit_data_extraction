#!/usr/bin/env python3
"""
Download latest South Dakota Title V permit-related documents from an nSITE
detail page (JS-rendered), e.g.:
https://ceris.deq.nd.gov/ext/nsite/DEFAULT/map/results/detail/212070/455
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# Ensure repository root is importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

DEFAULT_DETAIL_URL = "https://ceris.deq.nd.gov/ext/nsite/DEFAULT/map/results/detail/212070/455"
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
TITLE_V_HINT_RE = re.compile(
    r"(title\s*v|air permit to operate\s*-\s*major|\baop\b|operating permit)",
    re.IGNORECASE,
)


@dataclass
class DocRow:
    file_url: str
    context_text: str
    file_hint: str
    record_date: Optional[datetime]


def _setup_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=options)


def _extract_context(anchor) -> str:
    node = anchor
    for _ in range(10):
        node = node.parent
        if node is None:
            break
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and text.lower() != "link":
            return text
    return ""


def _extract_date(text: str) -> Optional[datetime]:
    matches = DATE_RE.findall(text)
    for token in reversed(matches):
        try:
            return datetime.strptime(token, "%m/%d/%Y")
        except ValueError:
            continue
    return None


def _extract_file_hint(context: str, url: str) -> str:
    prefix = context.split(" Link ", 1)[0].strip()
    if prefix:
        return prefix[:180]
    parsed_name = Path(urlparse(url).path).name
    return parsed_name or "document"


def collect_title_v_rows(detail_url: str, headless: bool, wait_seconds: int) -> List[DocRow]:
    driver = _setup_driver(headless=headless)
    try:
        logger.info(f"Opening nSITE detail page: {detail_url}")
        driver.get(detail_url)
        time.sleep(wait_seconds)
        html = driver.page_source
    finally:
        driver.quit()

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select('a[href*="/ext/ncore/downloadfile/"]')
    logger.info(f"Found {len(anchors)} downloadable document link(s) on page.")

    rows: List[DocRow] = []
    seen_urls = set()
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        context = _extract_context(anchor)
        if not TITLE_V_HINT_RE.search(context):
            continue

        rows.append(
            DocRow(
                file_url=href,
                context_text=context,
                file_hint=_extract_file_hint(context, href),
                record_date=_extract_date(context),
            )
        )

    rows.sort(key=lambda r: r.record_date or datetime.min, reverse=True)
    logger.info(f"Filtered to {len(rows)} Title V-related row(s).")
    return rows


def _copy_driver_cookies_to_session(detail_url: str, headless: bool) -> requests.Session:
    driver = _setup_driver(headless=headless)
    session = requests.Session()
    try:
        driver.get(detail_url)
        time.sleep(3)
        cookies = driver.get_cookies()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))
    finally:
        driver.quit()
    return session


def download_rows(
    rows: List[DocRow],
    output_dir: Path,
    detail_url: str,
    *,
    latest_only: bool,
    max_results: Optional[int],
    skip_existing: bool,
    dry_run: bool,
    headless: bool,
) -> List[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    indexed: List[dict] = []

    candidates = rows
    if latest_only and rows:
        latest_dt = rows[0].record_date
        if latest_dt is not None:
            candidates = [r for r in rows if r.record_date == latest_dt]
    if max_results is not None:
        candidates = candidates[:max_results]

    logger.info(f"Downloading {len(candidates)} selected row(s).")
    session = _copy_driver_cookies_to_session(detail_url, headless=headless)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": detail_url,
        }
    )

    for row in candidates:
        date_tag = row.record_date.strftime("%Y%m%d") if row.record_date else "undated"
        stem = clean_filename(f"sd_titlev_{date_tag}_{row.file_hint}") or "sd_titlev_document"
        filename = f"{stem}.pdf"
        dest = output_dir / filename

        result = {
            "record_date": row.record_date.strftime("%Y-%m-%d") if row.record_date else "",
            "file_url": row.file_url,
            "file_hint": row.file_hint,
            "context_text": row.context_text,
            "status": "",
            "local_path": "",
        }

        if dry_run:
            result["status"] = "dry_run"
            indexed.append(result)
            continue

        if skip_existing and dest.exists():
            result["status"] = "skipped_existing"
            result["local_path"] = str(dest)
            indexed.append(result)
            continue

        try:
            resp = session.get(row.file_url, timeout=90)
            resp.raise_for_status()
            with dest.open("wb") as handle:
                handle.write(resp.content)
            result["status"] = "downloaded"
            result["local_path"] = str(dest)
        except Exception as exc:
            result["status"] = f"failed: {exc}"

        indexed.append(result)

    return indexed


def write_index(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["record_date", "file_url", "file_hint", "context_text", "status", "local_path"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download latest South Dakota Title V documents from an nSITE detail URL."
    )
    default_out = RAW_DATA_DIR / "sd_denr_title_v_nsite"
    default_index = default_out / "sd_denr_title_v_nsite_index.csv"
    parser.add_argument(
        "--detail-url",
        type=str,
        default=DEFAULT_DETAIL_URL,
        help=f"nSITE detail URL to scrape (default: {DEFAULT_DETAIL_URL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_out,
        help=f"Directory to save downloaded documents (default: {default_out}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"CSV index path (default: {default_index}).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=8,
        help="Seconds to wait for JS-rendered content (default: 8).",
    )
    parser.add_argument(
        "--all-title-v",
        action="store_true",
        help="Download all matched Title V docs instead of only newest-dated docs.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Optional cap on number of selected documents.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and index document links without downloading files.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download files even if destination exists.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome with visible window.",
    )
    args = parser.parse_args()

    rows = collect_title_v_rows(
        detail_url=args.detail_url,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
    )
    indexed = download_rows(
        rows=rows,
        output_dir=args.output_dir.expanduser().resolve(),
        detail_url=args.detail_url,
        latest_only=not args.all_title_v,
        max_results=args.max_results,
        skip_existing=not args.no_skip_existing,
        dry_run=args.dry_run,
        headless=not args.no_headless,
    )
    index_path = args.index_csv.expanduser().resolve()
    write_index(index_path, indexed)

    downloaded = sum(1 for r in indexed if r["status"] == "downloaded")
    skipped = sum(1 for r in indexed if r["status"] == "skipped_existing")
    failed = sum(1 for r in indexed if r["status"].startswith("failed:"))
    logger.info("=" * 60)
    logger.info("SD nSITE TITLE V DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Matched Title V rows: {len(rows)}")
    logger.info(f"Selected rows: {len(indexed)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped existing: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Index CSV: {index_path}")


if __name__ == "__main__":
    main()
