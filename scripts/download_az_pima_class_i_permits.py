#!/usr/bin/env python3
"""
Download Pima County, AZ Class I air quality permit PDFs.

The script reads the permit table at:
https://www.pima.gov/531/Class-I-Air-Quality-Permit-Search

It extracts links from the "Permits" column, downloads each PDF,
and writes an index CSV describing results.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from permit_data_extraction.config import RAW_DATA_DIR

INDEX_URL = "https://www.pima.gov/531/Class-I-Air-Quality-Permit-Search"
DEFAULT_TIMEOUT = 60
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def clean_filename(filename: str) -> str:
    """Return a filesystem-safe filename."""
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = re.sub(r"\s+", " ", filename).strip()
    return filename


def guess_extension_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return ".pdf"
    return ".pdf"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def extract_permit_rows(html: str, base_url: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("Could not find permit table on the page.")

    header_cells = table.select("thead tr th")
    if not header_cells:
        first_row = table.find("tr")
        if first_row is None:
            raise RuntimeError("Could not find a table header row.")
        header_cells = first_row.find_all(["th", "td"])

    headers = [normalize_header(cell.get_text(" ", strip=True)) for cell in header_cells]
    if not headers:
        raise RuntimeError("Could not parse table headers.")

    permit_col_idx = next((i for i, h in enumerate(headers) if h == "permits"), None)
    permit_number_idx = next((i for i, h in enumerate(headers) if h == "permit number"), None)
    business_name_idx = next((i for i, h in enumerate(headers) if h == "business name"), None)
    plant_name_idx = next((i for i, h in enumerate(headers) if h == "plant name"), None)
    address_idx = next((i for i, h in enumerate(headers) if h == "address"), None)

    if permit_col_idx is None:
        raise RuntimeError("Could not find a 'Permits' column in the table.")

    rows_data: List[dict] = []
    tbody_rows = table.select("tbody tr")
    if not tbody_rows:
        all_rows = table.find_all("tr")
        tbody_rows = all_rows[1:] if len(all_rows) > 1 else []

    for row in tbody_rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue

        def _cell_text(index: Optional[int]) -> str:
            if index is None or index >= len(cells):
                return ""
            return cells[index].get_text(" ", strip=True)

        permit_number = _cell_text(permit_number_idx)
        business_name = _cell_text(business_name_idx)
        plant_name = _cell_text(plant_name_idx)
        address = _cell_text(address_idx)

        permit_cell = cells[permit_col_idx] if permit_col_idx < len(cells) else None
        if permit_cell is None:
            continue

        anchors = permit_cell.find_all("a", href=True)
        for anchor in anchors:
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            full_url = urljoin(base_url, href)
            link_label = anchor.get_text(" ", strip=True) or "Permit"
            rows_data.append(
                {
                    "permit_number": permit_number,
                    "business_name": business_name,
                    "plant_name": plant_name,
                    "address": address,
                    "link_label": link_label,
                    "pdf_url": full_url,
                }
            )

    if not rows_data:
        raise RuntimeError("No permit links found in the permit table.")
    return rows_data


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    idx = 2
    while True:
        candidate = path.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def download_file(session: requests.Session, url: str, target_path: Path, referer: str) -> None:
    headers = {"Referer": referer}
    with session.get(url, stream=True, timeout=DEFAULT_TIMEOUT, headers=headers) as response:
        response.raise_for_status()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)


def write_index_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "permit_number",
        "business_name",
        "plant_name",
        "address",
        "link_label",
        "pdf_url",
        "local_path",
        "status",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote index CSV: {path}")


def download_pima_class_i_permits(
    output_dir: Path,
    index_csv: Optional[Path],
    max_permits: Optional[int],
    sleep_seconds: float,
    skip_existing: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = create_session()

    logger.info(f"Loading index page: {INDEX_URL}")
    response = session.get(INDEX_URL, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()

    permits = extract_permit_rows(response.text, INDEX_URL)
    if max_permits is not None:
        permits = permits[:max_permits]
    logger.info(f"Found {len(permits)} permit link(s).")

    downloaded = 0
    skipped = 0
    failed = 0
    csv_rows: List[dict] = []

    for idx, row in enumerate(permits, start=1):
        permit_number = row["permit_number"] or "unknown"
        business_name = row["business_name"] or "Unknown Business"
        link_label = row["link_label"] or "Permit"
        pdf_url = row["pdf_url"]

        base_name = clean_filename(f"{permit_number}_{business_name}_{link_label}")
        ext = guess_extension_from_url(pdf_url)
        filename = base_name if base_name.lower().endswith(ext) else f"{base_name}{ext}"
        target_path = output_dir / filename
        if target_path.exists() and skip_existing:
            skipped += 1
            logger.info(f"[{idx}/{len(permits)}] Skipping existing file: {target_path.name}")
            csv_rows.append(
                {
                    **row,
                    "local_path": str(target_path),
                    "status": "skipped_existing",
                    "error": "",
                }
            )
            continue

        target_path = unique_path(target_path) if target_path.exists() else target_path
        logger.info(f"[{idx}/{len(permits)}] Downloading {pdf_url} -> {target_path.name}")
        try:
            download_file(session, pdf_url, target_path, referer=INDEX_URL)
            downloaded += 1
            csv_rows.append(
                {
                    **row,
                    "local_path": str(target_path),
                    "status": "downloaded",
                    "error": "",
                }
            )
        except Exception as exc:
            failed += 1
            logger.warning(f"Download failed for {pdf_url}: {exc}")
            csv_rows.append(
                {
                    **row,
                    "local_path": "",
                    "status": "failed_download",
                    "error": str(exc),
                }
            )

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    logger.info(
        f"Done. Downloaded: {downloaded}, skipped: {skipped}, failed: {failed}, output: {output_dir}"
    )
    if index_csv is not None:
        write_index_csv(index_csv, csv_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Pima County, AZ Class I permit PDFs from the public permit table."
    )
    default_output_dir = RAW_DATA_DIR / "az_pima_class_i_permits"
    default_index_csv = default_output_dir / "az_pima_class_i_permits_index.csv"

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir,
        help=f"Directory to save downloaded permits (default: {default_output_dir}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index_csv,
        help=f"Path for index CSV (default: {default_index_csv}).",
    )
    parser.add_argument(
        "--max-permits",
        type=int,
        default=None,
        help="Optional limit on number of permits to process.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.25,
        help="Delay between downloads to reduce request bursts.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Redownload files even when a destination file already exists.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    index_csv = args.index_csv.expanduser().resolve() if args.index_csv else None

    download_pima_class_i_permits(
        output_dir=output_dir,
        index_csv=index_csv,
        max_permits=args.max_permits,
        sleep_seconds=args.sleep_seconds,
        skip_existing=not args.no_skip_existing,
    )


if __name__ == "__main__":
    main()
