#!/usr/bin/env python3
"""
Download Michigan ROP permit PDFs from EGLE directory listings.

Starts at:
https://www.egle.state.mi.us/aps/downloads/ROP/Pub_ntce/

The script collects facility directories (e.g., A1234, B5678) and downloads
all PDFs from each directory.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import _create_requests_session, clean_filename

BASE_URL = "https://www.egle.state.mi.us/aps/downloads/ROP/Pub_ntce/"
FACILITY_DIR_PATTERN = re.compile(r"^[A-Z]\d", re.IGNORECASE)
EXCLUDED_DIRS = {"_vti_cnf", "include"}


@dataclass(frozen=True)
class DirectoryLink:
    name: str
    url: str


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    text: str
    row_text: str
    modified: Optional[datetime]


def fetch_html(url: str) -> str:
    session = _create_requests_session()
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return response.text
    finally:
        session.close()


def extract_directory_links(
    html: str, base_url: str, include_all_dirs: bool = False
) -> List[DirectoryLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: List[DirectoryLink] = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or not href.endswith("/"):
            continue
        if href.startswith("../"):
            continue

        name = anchor.get_text(strip=True) or href.strip("/")
        name = name.strip().strip("/")
        if not name:
            continue

        if not include_all_dirs:
            if name.lower() in EXCLUDED_DIRS:
                continue
            if not FACILITY_DIR_PATTERN.match(name):
                continue

        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        links.append(DirectoryLink(name=name, url=url))

    return sorted(links, key=lambda item: item.name)


def _parse_modified_date(text: str) -> Optional[datetime]:
    match = re.search(
        r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2).upper()}",
            "%m/%d/%Y %I:%M %p",
        )
    except ValueError:
        return None


def extract_pdf_candidates(html: str, base_url: str) -> List[PdfCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: List[PdfCandidate] = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)

        row = anchor.find_parent(["tr", "pre"]) or anchor.parent
        row_text = row.get_text(" ", strip=True) if row else ""
        modified = _parse_modified_date(row_text)
        candidates.append(
            PdfCandidate(
                url=url,
                text=anchor.get_text(strip=True),
                row_text=row_text,
                modified=modified,
            )
        )

    return candidates


def download_file(url: str, output_path: Path, sleep_seconds: float) -> bool:
    session = _create_requests_session()
    try:
        response = session.get(url, stream=True, timeout=60)
        if response.status_code != 200:
            logger.warning(f"Non-200 response {response.status_code} for {url}")
            return False

        content_type = response.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower():
            logger.warning(f"Unexpected content-type '{content_type}' for {url}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        return True
    except Exception as exc:
        logger.warning(f"Failed to download {url}: {exc}")
        return False
    finally:
        session.close()


def download_permits(
    output_dir: Path,
    limit: Optional[int],
    sleep_seconds: float,
    skip_existing: bool,
    include_all_dirs: bool,
) -> None:
    landing_html = fetch_html(BASE_URL)
    facility_dirs = extract_directory_links(landing_html, BASE_URL, include_all_dirs=include_all_dirs)

    if limit is not None:
        facility_dirs = facility_dirs[:limit]

    logger.info(f"Found {len(facility_dirs)} facility directories to process.")
    downloaded = 0
    skipped = 0
    failed = 0

    for idx, facility in enumerate(facility_dirs, start=1):
        logger.info(f"[{idx}/{len(facility_dirs)}] Processing {facility.name}")
        try:
            facility_html = fetch_html(facility.url)
        except Exception as exc:
            logger.warning(f"Failed to load directory {facility.url}: {exc}")
            failed += 1
            continue

        candidates = extract_pdf_candidates(facility_html, facility.url)
        if not candidates:
            logger.warning(f"No PDF files found in {facility.name}")
            skipped += 1
            continue

        for candidate in candidates:
            filename = clean_filename(urlparse(candidate.url).path.split("/")[-1] or "permit.pdf")
            if not filename.lower().endswith(".pdf"):
                filename = f"{filename}.pdf"
            output_path = output_dir / facility.name / filename

            if skip_existing and output_path.exists():
                skipped += 1
                logger.info(f"Skipping existing file: {output_path}")
                continue

            logger.info(f"Downloading {candidate.url} -> {output_path}")
            if download_file(candidate.url, output_path, sleep_seconds):
                downloaded += 1
            else:
                failed += 1

    logger.info("=" * 60)
    logger.info("DOWNLOAD SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Directories processed: {len(facility_dirs)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Michigan ROP permit PDFs from EGLE facility directories."
    )
    default_output = RAW_DATA_DIR / "michigan_rop_final_permits"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloaded permits (default: {default_output}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of facility directories to process.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Seconds to sleep between downloads.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Download even if the file already exists.",
    )
    parser.add_argument(
        "--include-all-dirs",
        action="store_true",
        help="Include directories that do not match the facility naming pattern.",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    download_permits(
        output_dir=output_dir,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        skip_existing=not args.no_skip_existing,
        include_all_dirs=args.include_all_dirs,
    )


if __name__ == "__main__":
    main()
