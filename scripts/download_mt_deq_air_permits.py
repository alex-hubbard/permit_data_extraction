#!/usr/bin/env python3
"""
Scrape Montana DEQ air permits from:
https://deq.mt.gov/air/assistance

The Air Quality Permits table is paginated client-side in the browser, but all
rows are present in the page HTML. This script parses all rows so permits from
every visible table page are captured.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

# Ensure repository root is importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

PAGE_URL = "https://deq.mt.gov/air/assistance"
DEFAULT_TIMEOUT = 60


def _extract_date_issued(tr) -> str:
    """
    Pull the issue date from the row, preferring the explicit h3 cell marker.
    """
    date_cell = tr.find("td", attrs={"headers": "h3"})
    if date_cell:
        return date_cell.get_text(" ", strip=True)

    tds = tr.find_all("td")
    if len(tds) >= 3:
        return tds[2].get_text(" ", strip=True)
    return ""


def _extract_permit_type(tr) -> str:
    """
    Pull the permit type from the row, preferring the explicit h4 cell marker.
    """
    type_cell = tr.find("td", attrs={"headers": "h4"})
    if type_cell:
        return type_cell.get_text(" ", strip=True)

    tds = tr.find_all("td")
    if len(tds) >= 4:
        return tds[3].get_text(" ", strip=True)
    return ""


def collect_permit_links(page_url: str) -> List[Dict[str, str]]:
    resp = requests.get(page_url, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table", class_="responsiveTablesLarge")
    if table is None:
        raise RuntimeError("Could not find Air Quality Permits table on page.")

    rows: List[Dict[str, str]] = []
    seen_urls = set()

    for row_idx, tr in enumerate(table.select("tbody tr"), start=1):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        permittee = tds[1].get_text(" ", strip=True)
        issue_date = _extract_date_issued(tr)
        permit_type = _extract_permit_type(tr)

        for link_idx, anchor in enumerate(tds[0].find_all("a", href=True), start=1):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            abs_url = urljoin(page_url, href)
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            permit_number = anchor.get_text(" ", strip=True)
            rows.append(
                {
                    "row_index": str(row_idx),
                    "permit_link_index": str(link_idx),
                    "permit_number": permit_number,
                    "permittee": permittee,
                    "issue_date": issue_date,
                    "permit_type": permit_type,
                    "pdf_url": abs_url,
                }
            )

    return rows


def _filename_for_row(item: Dict[str, str], response: requests.Response) -> str:
    cd = response.headers.get("content-disposition", "")
    if "filename=" in cd.lower():
        # Lightweight split keeps this robust enough for this source.
        filename = cd.split("filename=", 1)[-1].strip().strip("\"'")
        cleaned = clean_filename(filename)
        if cleaned:
            return cleaned

    url_name = Path(urlparse(item["pdf_url"]).path).name
    if url_name:
        return clean_filename(url_name) or "document.pdf"

    stem = clean_filename(
        f"{item.get('permit_number', '')}_{item.get('permittee', '')}_{item.get('issue_date', '')}"
    )
    stem = stem or "document"
    if not stem.lower().endswith(".pdf"):
        stem += ".pdf"
    return stem


def download_permits(
    rows: List[Dict[str, str]],
    output_dir: Path,
    *,
    skip_existing: bool,
    dry_run: bool,
) -> List[Dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    indexed_rows: List[Dict[str, str]] = []

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": PAGE_URL,
        }
    )

    for i, item in enumerate(rows, start=1):
        logger.info(
            f"[{i}/{len(rows)}] {item['permit_number']} | {item['permittee']} -> {item['pdf_url']}"
        )

        row_out = dict(item)
        if dry_run:
            row_out["status"] = "dry_run"
            row_out["local_path"] = ""
            row_out["content_type"] = ""
            indexed_rows.append(row_out)
            continue

        try:
            resp = session.get(item["pdf_url"], timeout=DEFAULT_TIMEOUT, stream=True)
            resp.raise_for_status()
        except Exception as exc:
            row_out["status"] = f"failed_request: {exc}"
            row_out["local_path"] = ""
            row_out["content_type"] = ""
            indexed_rows.append(row_out)
            continue

        content_type = (resp.headers.get("content-type") or "").lower()
        filename = _filename_for_row(item, resp)
        if "." not in Path(filename).name:
            filename += ".pdf"
        dest = output_dir / filename

        if skip_existing and dest.exists():
            row_out["status"] = "skipped_existing"
            row_out["local_path"] = str(dest)
            row_out["content_type"] = content_type
            indexed_rows.append(row_out)
            continue

        try:
            with dest.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        handle.write(chunk)
            row_out["status"] = "downloaded"
            row_out["local_path"] = str(dest)
            row_out["content_type"] = content_type
        except Exception as exc:
            row_out["status"] = f"failed_write: {exc}"
            row_out["local_path"] = ""
            row_out["content_type"] = content_type

        indexed_rows.append(row_out)

    return indexed_rows


def write_index(index_csv: Path, rows: List[Dict[str, str]]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "row_index",
        "permit_link_index",
        "permit_number",
        "permittee",
        "issue_date",
        "permit_type",
        "pdf_url",
        "status",
        "local_path",
        "content_type",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape and download Montana DEQ Air Quality Permits table PDFs."
    )
    default_output = RAW_DATA_DIR / "mt_deq_air_permits"
    default_index = default_output / "mt_deq_air_permits_index.csv"
    parser.add_argument(
        "--page-url",
        type=str,
        default=PAGE_URL,
        help=f"Montana DEQ assistance page URL (default: {PAGE_URL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for downloaded permit PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"Index CSV path (default: {default_index}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only scrape links and write index CSV; do not download PDFs.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download files even if destination already exists.",
    )
    args = parser.parse_args()

    rows = collect_permit_links(args.page_url)
    logger.info(f"Collected {len(rows)} permit PDF links from Montana DEQ table.")

    indexed_rows = download_permits(
        rows,
        output_dir=args.output_dir.expanduser().resolve(),
        skip_existing=not args.no_skip_existing,
        dry_run=args.dry_run,
    )
    index_csv = args.index_csv.expanduser().resolve()
    write_index(index_csv, indexed_rows)

    downloaded = sum(1 for r in indexed_rows if r["status"] == "downloaded")
    skipped = sum(1 for r in indexed_rows if r["status"] == "skipped_existing")
    failed = sum(1 for r in indexed_rows if r["status"].startswith("failed_"))

    logger.info("=" * 60)
    logger.info("MONTANA DEQ AIR PERMITS SCRAPE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Links collected: {len(rows)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped existing: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
