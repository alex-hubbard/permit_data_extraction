#!/usr/bin/env python3
"""
Download the latest NH AFS TypePermit document for each facility URL.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from permit_data_extraction.config import EXTERNAL_DATA_DIR, RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


@dataclass
class PermitRow:
    row_index: int
    final_decision: str
    decision_date: Optional[datetime]
    application_number: Optional[str]


def parse_date(value: str) -> Optional[datetime]:
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def read_urls(csv_path: Path) -> List[str]:
    urls: List[str] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            url = (row.get("url") or "").strip()
            if url:
                urls.append(url)
    deduped = list(dict.fromkeys(urls))
    if len(deduped) != len(urls):
        logger.info(f"Deduped {len(urls) - len(deduped)} duplicate URLs.")
    return deduped


def find_permits_table(soup: BeautifulSoup) -> Optional[object]:
    table = soup.find("table", id="ctl00_ContentPlaceHolder1_dgPermitsApplications")
    if table:
        return table

    table = soup.find(
        "table",
        id=lambda value: value and ("dgPermitsApplications" in value or "PermitsApplications" in value),
    )
    if table:
        return table

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong"]):
        if "permits and applications" in heading.get_text(strip=True).lower():
            next_table = heading.find_next("table")
            if next_table:
                return next_table

    return soup.find("table", string=lambda value: value and "Final Decision" in value)


def locate_columns(header_cells: Iterable[object]) -> dict:
    headers = [cell.get_text(strip=True).lower() for cell in header_cells]
    indices = {}
    for idx, header in enumerate(headers):
        if "final decision" in header and "date" not in header:
            indices["final_decision"] = idx
        if "final permit issued" in header:
            indices["final_permit_issued"] = idx
        if "application" in header and "number" in header:
            indices["application_number"] = idx
        if header == "application no.":
            indices["application_number"] = idx
    return indices


def extract_candidate_rows(table) -> List[PermitRow]:
    rows = table.select("tbody tr") or table.find_all("tr")
    header_cells = table.find_all("th")
    indices = locate_columns(header_cells)
    final_idx = indices.get("final_decision")
    date_idx = indices.get("final_permit_issued")
    app_idx = indices.get("application_number")
    app_pattern = re.compile(r"\b\d{2}-\d{4}\b")

    if final_idx is None:
        logger.warning("Could not locate a Final Decision column.")
        return []

    candidates: List[PermitRow] = []
    for row_idx, row in enumerate(rows):
        cells = row.find_all("td")
        if not cells or final_idx >= len(cells):
            continue

        final_decision = cells[final_idx].get_text(strip=True)
        if "permit issued" not in final_decision.lower():
            continue

        decision_date = None
        if date_idx is not None and date_idx < len(cells):
            decision_date = parse_date(cells[date_idx].get_text(strip=True))

        application_number = None
        if app_idx is not None and app_idx < len(cells):
            application_number = cells[app_idx].get_text(strip=True) or None
        if not application_number:
            # Fallback: scan the row for an application number like 23-0049
            row_text = " ".join(cell.get_text(strip=True) for cell in cells)
            match = app_pattern.search(row_text)
            if match:
                application_number = match.group(0)

        candidates.append(
            PermitRow(
                row_index=row_idx,
                final_decision=final_decision,
                decision_date=decision_date,
                application_number=application_number,
            )
        )

    return candidates


def select_latest_row(rows: List[PermitRow]) -> Optional[PermitRow]:
    if not rows:
        return None

    dated = [row for row in rows if row.decision_date]
    if dated:
        return max(dated, key=lambda row: row.decision_date)

    return rows[0]




def _save_debug_html_text(output_dir: Path, url: str, html: str) -> None:
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("ID", ["unknown"])[0]
    debug_dir = output_dir / "debug_html"
    debug_dir.mkdir(exist_ok=True)
    debug_path = debug_dir / f"nh_afs_{query_id}.html"
    try:
        debug_path.write_text(html, encoding="utf-8")
        logger.warning(f"Saved debug HTML to {debug_path}")
    except Exception as exc:
        logger.warning(f"Failed to write debug HTML: {exc}")


def _fetch_html_with_requests(downloader: SeleniumPDFDownloader, url: str) -> Optional[str]:
    try:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if getattr(downloader, "user_agent", None):
            headers["User-Agent"] = downloader.user_agent
        response = downloader.session.get(url, headers=headers, timeout=25)
    except requests.RequestException as exc:
        logger.debug(f"Request failed for {url}: {exc}")
        return None

    if response.status_code != 200:
        logger.debug(f"Request status {response.status_code} for {url}")
        return None

    return response.text


def download_typepermit_for_url(
    downloader: SeleniumPDFDownloader,
    url: str,
    wait_seconds: int,
) -> bool:
    logger.info(f"Loading: {url}")
    html = _fetch_html_with_requests(downloader, url)

    if not html:
        driver = downloader.driver
        driver.get(url)
        time.sleep(wait_seconds)
        html = driver.page_source or ""
        if not html:
            logger.warning("Empty page source after Selenium load.")
            return False
        try:
            downloader._apply_driver_cookies_to_session()
        except Exception:
            pass

    soup = BeautifulSoup(html, "html.parser")

    table = find_permits_table(soup)
    if not table:
        logger.warning("Could not find the Permits and Applications table.")
        _save_debug_html_text(downloader.output_dir, url, html)
        return False

    rows = extract_candidate_rows(table)
    if not rows:
        logger.info("No rows with Final Decision = Permit Issued.")
        return False

    target_row = select_latest_row(rows)
    if not target_row:
        return False
    if not target_row.application_number:
        logger.warning("Missing application number for latest Permit Issued row.")
        return False

    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("ID", [""])[0]
    if not query_id:
        logger.warning("Could not parse ID from AFS URL.")
        return False

    application_number = target_row.application_number.replace(" ", "")
    permit_url = f"http://www4.des.state.nh.us/OneStopPub/Air/{query_id}{application_number}TypePermit.pdf"
    link_text = f"{query_id}-{application_number}-TypePermit"

    return downloader.download_document(permit_url, referer=url, link_text=link_text, is_table_link=True)


def main() -> None:
    default_csv = EXTERNAL_DATA_DIR / "nh_afs_links.csv"
    default_output = RAW_DATA_DIR / "nh_afs_typepermit"
    default_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )

    parser = argparse.ArgumentParser(
        description="Download latest NH AFS TypePermit permits for each facility link.",
    )
    parser.add_argument("--csv", type=Path, default=default_csv, help="CSV with NH AFS links.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Output directory (default: {default_output}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of URLs to process.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=3,
        help="Seconds to wait for dynamic content to render.",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default=default_user_agent,
        help="User-Agent string for Selenium Chrome.",
    )
    parser.add_argument(
        "--no-throttle",
        action="store_true",
        help="Disable the 1s delay between URLs.",
    )

    args = parser.parse_args()
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    urls = read_urls(args.csv.expanduser())
    if args.limit is not None:
        urls = urls[: args.limit]

    if not urls:
        logger.warning("No URLs found in CSV.")
        return

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        max_depth=0,
        use_llm=False,
        user_agent=args.user_agent,
    )

    try:
        success = 0
        failed = 0
        for idx, url in enumerate(urls, start=1):
            logger.info(f"[{idx}/{len(urls)}] Processing {url}")
            ok = download_typepermit_for_url(downloader, url, wait_seconds=args.wait_seconds)
            if ok:
                success += 1
            else:
                failed += 1
            if not args.no_throttle:
                time.sleep(1)

        logger.info("NH AFS TypePermit download complete.")
        logger.info(f"Downloaded: {success}")
        logger.info(f"Failed: {failed}")
        logger.info(f"Output dir: {output_dir}")
    finally:
        downloader.close()


if __name__ == "__main__":
    main()
