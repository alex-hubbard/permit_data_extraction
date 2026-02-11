#!/usr/bin/env python3
"""
Download all permit PDFs listed in data/external/Grid view.csv.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from loguru import logger

from permit_data_extraction.config import EXTERNAL_DATA_DIR, RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename, guess_filename_from_url, _create_requests_session


PERMIT_LINK_PATTERN = re.compile(r"(?P<name>[^()]*)\((?P<url>https?://[^)]+)\)")


def extract_permit_links(permit_field: str) -> List[Tuple[str, str]]:
    links: List[Tuple[str, str]] = []
    if not permit_field:
        return links

    for match in PERMIT_LINK_PATTERN.finditer(permit_field):
        name = match.group("name").strip().rstrip(",")
        url = match.group("url").strip()
        if url:
            links.append((name, url))
    return links


def build_filename(
    facility_name: str,
    permit_name: str,
    url: str,
    existing: Dict[str, int],
) -> str:
    parts: List[str] = []
    facility_clean = clean_filename(facility_name)
    if facility_clean:
        parts.append(facility_clean)

    permit_clean = clean_filename(permit_name) if permit_name else ""
    if permit_clean:
        parts.append(permit_clean)
    else:
        parts.append(clean_filename(guess_filename_from_url(url)))

    filename = " - ".join(part for part in parts if part).strip()
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    filename = filename or "permit.pdf"
    count = existing.get(filename, 0)
    if count:
        stem = filename[:-4]
        filename = f"{stem} ({count + 1}).pdf"
    existing[filename] = count + 1
    return filename


def iter_permit_records(csv_path: Path) -> Iterable[Tuple[str, str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            facility = (row.get("Facility Name") or "").strip()
            permit_field = row.get("Permit PDFs") or ""
            for permit_name, url in extract_permit_links(permit_field):
                yield facility, permit_name, url


def download_files(csv_path: Path, output_dir: Path, limit: Optional[int]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = _create_requests_session()

    total = 0
    downloaded = 0
    skipped = 0
    failed = 0
    filename_counts: Dict[str, int] = {}

    for facility, permit_name, url in iter_permit_records(csv_path):
        total += 1
        if limit and total > limit:
            logger.info(f"Reached download limit ({limit}); stopping.")
            break

        filename = build_filename(facility, permit_name, url, filename_counts)
        output_path = output_dir / filename
        if output_path.exists():
            skipped += 1
            logger.info(f"Skipping existing: {output_path.name}")
            continue

        logger.info(f"Downloading: {output_path.name}")
        try:
            response = session.get(url, stream=True, timeout=60)
            if response.status_code >= 400:
                failed += 1
                logger.warning(f"Failed ({response.status_code}) for {url}")
                continue

            tmp_path = output_path.with_suffix(output_path.suffix + ".part")
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
            tmp_path.replace(output_path)
            downloaded += 1
        except Exception as exc:
            failed += 1
            logger.warning(f"Error downloading {url}: {exc}")

    logger.info("Download summary")
    logger.info(f"Total links: {total}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDF links from Grid view.csv.")
    default_csv = EXTERNAL_DATA_DIR / "Grid view.csv"
    default_output = RAW_DATA_DIR / "grid_view_pdfs"
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=default_csv,
        help=f"Path to CSV file (default: {default_csv}).",
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

    args = parser.parse_args()
    download_files(args.csv_path.expanduser(), args.output_dir.expanduser(), args.limit)


if __name__ == "__main__":
    main()
