#!/usr/bin/env python3
"""
Download South Dakota permit PDFs from facility IDs.

Builds permit links using the format:
https://apps.sd.gov/NR34AirQuality/ExportPDFs.ashx?AirFacilityID=SD0000004600500002
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

from permit_data_extraction.config import EXTERNAL_DATA_DIR, RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import _create_requests_session, clean_filename

BASE_URL = "https://apps.sd.gov/NR34AirQuality/ExportPDFs.ashx"


def build_permit_url(facility_id: str) -> str:
    return f"{BASE_URL}?AirFacilityID={facility_id}"


def iter_facilities(csv_path: Path) -> Iterable[dict]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield row


def derive_filename(facility_id: str, name: str) -> str:
    name_part = clean_filename(name) if name else "facility"
    filename = f"{facility_id} - {name_part}.pdf"
    return clean_filename(filename)


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
    csv_path: Path,
    output_dir: Path,
    limit: Optional[int],
    sleep_seconds: float,
    skip_existing: bool,
) -> None:
    total = 0
    downloaded = 0
    skipped = 0
    failed = 0

    for row in iter_facilities(csv_path):
        facility_id = (row.get("Facility ID") or "").strip()
        name = (row.get("Name") or "").strip()
        if not facility_id:
            skipped += 1
            continue

        total += 1
        if limit and total > limit:
            break

        permit_url = build_permit_url(facility_id)
        filename = derive_filename(facility_id, name)
        output_path = output_dir / filename

        if skip_existing and output_path.exists():
            skipped += 1
            logger.info(f"Skipping existing file: {output_path.name}")
            continue

        logger.info(f"Downloading {facility_id} -> {output_path.name}")
        if download_file(permit_url, output_path, sleep_seconds):
            downloaded += 1
        else:
            failed += 1

    logger.info("=" * 60)
    logger.info("DOWNLOAD SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total rows processed: {total}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download South Dakota permit PDFs from Facilities_SD.csv."
    )
    default_csv = EXTERNAL_DATA_DIR / "Facilities_SD.csv"
    default_output = RAW_DATA_DIR / "south_dakota_permits"
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=default_csv,
        help=f"Path to Facilities_SD.csv (default: {default_csv}).",
    )
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
        help="Optional limit on number of facilities to process.",
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

    args = parser.parse_args()
    csv_path: Path = args.csv_path.expanduser()
    output_dir: Path = args.output_dir.expanduser()

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    download_permits(
        csv_path=csv_path,
        output_dir=output_dir,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        skip_existing=not args.no_skip_existing,
    )


if __name__ == "__main__":
    main()
