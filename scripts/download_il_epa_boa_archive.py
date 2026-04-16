#!/usr/bin/env python3
"""
Download Illinois EPA Bureau of Air archived public-notice permit PDFs.

Portal (search UI):
https://epa.illinois.gov/public-notices/boa-notices/archive.html

The page uses the Illinois WebSiteApi (see epa-air-permit-search.js):
- POST .../GetArchivedAirPublicNotices/ — DataTables search (Permit Type dropdown)
- GET  .../GetPermitDocumentsList/ — documents for a facility + permit
- GET  .../GetAirPermitDocument/{id} — PDF bytes

Default permit type is the dropdown value "Title V" (matches Operating - Title V,
Title V Construction, etc., per server filtering).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

API_BASE = "https://webapps.illinois.gov/EPA/WebSiteApi/api/PublicNotices/"
ARCHIVE_URL = f"{API_BASE}GetArchivedAirPublicNotices/"
DOCUMENTS_URL = f"{API_BASE}GetPermitDocumentsList/"
PDF_URL = f"{API_BASE}GetAirPermitDocument/"
PERMIT_TYPES_URL = f"{API_BASE}GetPermitTypes"
PORTAL_PAGE = "https://epa.illinois.gov/public-notices/boa-notices/archive.html"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://epa.illinois.gov",
    "Referer": PORTAL_PAGE,
}


def _session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def fetch_permit_types(session: requests.Session, timeout: int) -> List[str]:
    r = session.get(PERMIT_TYPES_URL, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected GetPermitTypes response")
    return [str(x) for x in data]


def post_archived_page(
    session: requests.Session,
    *,
    start: int,
    length: int,
    permit_type: str,
    timeout: int,
) -> Dict[str, Any]:
    """Mirror DataTables POST from epa-air-permit-search.js (custom filter fields)."""
    body: Dict[str, str] = {
        "draw": "1",
        "start": str(start),
        "length": str(length),
        "facilityName": "",
        "airIdNumber": "",
        "permitId": "",
        "city": "",
        "permitType": permit_type,
        "publicNoticeDateRangeStart": "",
        "publicNoticeDateRangeEnd": "",
        "usepaNoticeDateRangeStart": "",
        "usepaNoticeDateRangeEnd": "",
        "county": "",
        "sicCode": "",
        "order[0][column]": "6",
        "order[0][dir]": "desc",
    }
    r = session.post(ARCHIVE_URL, data=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_permit_documents(
    session: requests.Session,
    facility_id: str,
    permit_id: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    params = {
        "facilityId": facility_id,
        "permitId": permit_id,
        "draw": "1",
        "start": "0",
        "length": "500",
        "order[0][column]": "3",
        "order[0][dir]": "desc",
    }
    r = session.get(DOCUMENTS_URL, params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []
    return rows


def download_pdf(
    session: requests.Session,
    public_notice_id: int,
    dest: Path,
    timeout: int,
) -> bool:
    url = f"{PDF_URL}{public_notice_id}"
    r = session.get(url, timeout=timeout, stream=True)
    if r.status_code != 200:
        logger.warning(f"HTTP {r.status_code} for {url}")
        return False
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "pdf" not in ctype:
        logger.warning(f"Unexpected Content-Type {ctype!r} for {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            if chunk:
                f.write(chunk)
    return True


def crawl(
    *,
    output_dir: Path,
    index_csv: Path,
    permit_type: str,
    page_size: int,
    timeout: int,
    sleep_seconds: float,
    list_only: bool,
    max_permits: Optional[int],
    max_pdfs: Optional[int],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = _session()

    types = fetch_permit_types(session, timeout)
    if permit_type and permit_type not in types:
        logger.warning(
            f"Permit type {permit_type!r} not in GetPermitTypes list; "
            f"server may still accept it. Known: {types}"
        )

    archive_rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        payload = post_archived_page(
            session, start=start, length=page_size, permit_type=permit_type, timeout=timeout
        )
        batch = payload.get("data") or []
        if not batch:
            break
        archive_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    logger.info(f"Archive rows for permitType={permit_type!r}: {len(archive_rows)}")

    seen_pairs: Set[Tuple[str, str]] = set()
    unique_pairs: List[Tuple[str, str, str]] = []
    for row in archive_rows:
        fid = str(row.get("FacilityId") or "").strip()
        pid = str(row.get("PermitId") or "").strip()
        name = str(row.get("FacilityName") or "").strip()
        if not fid or not pid:
            continue
        key = (fid, pid)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        unique_pairs.append((fid, pid, name))
        if max_permits is not None and len(unique_pairs) >= max_permits:
            break

    logger.info(f"Unique facility+permit pairs: {len(unique_pairs)}")

    doc_index: List[Dict[str, str]] = []
    downloaded = 0
    failed = 0
    seen_pdf_ids: Set[int] = set()
    scheduled_pdfs = 0
    stop_all = False

    for facility_id, permit_id, facility_name in unique_pairs:
        if stop_all:
            break
        docs = fetch_permit_documents(session, facility_id, permit_id, timeout)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        for doc in docs:
            pnid = doc.get("PublicNoticeId")
            if pnid is None:
                continue
            try:
                pnid_int = int(pnid)
            except (TypeError, ValueError):
                continue
            if pnid_int in seen_pdf_ids:
                continue
            if max_pdfs is not None and scheduled_pdfs >= max_pdfs:
                stop_all = True
                break

            doc_type = str(doc.get("PublicNoticeType") or "document").strip()
            sub_type = str(doc.get("SubmittalType") or "").strip()
            safe_type = clean_filename(doc_type)[:80] or "document"
            fname = clean_filename(
                f"il_{facility_id}_{permit_id}_{pnid_int}_{safe_type}.pdf"
            )
            dest = output_dir / fname
            url = f"{PDF_URL}{pnid_int}"

            if list_only:
                doc_index.append(
                    {
                        "facility_id": facility_id,
                        "permit_id": permit_id,
                        "facility_name": facility_name,
                        "public_notice_id": str(pnid_int),
                        "public_notice_type": doc_type,
                        "submittal_type": sub_type,
                        "document_url": url,
                        "local_path": "",
                        "status": "listed",
                    }
                )
                seen_pdf_ids.add(pnid_int)
                scheduled_pdfs += 1
                continue

            ok = download_pdf(session, pnid_int, dest, timeout)
            if ok:
                downloaded += 1
                status = "downloaded"
            else:
                failed += 1
                status = "failed_download"
            doc_index.append(
                {
                    "facility_id": facility_id,
                    "permit_id": permit_id,
                    "facility_name": facility_name,
                    "public_notice_id": str(pnid_int),
                    "public_notice_type": doc_type,
                    "submittal_type": sub_type,
                    "document_url": url,
                    "local_path": str(dest) if ok else "",
                    "status": status,
                }
            )
            seen_pdf_ids.add(pnid_int)
            scheduled_pdfs += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "facility_id",
        "permit_id",
        "facility_name",
        "public_notice_id",
        "public_notice_type",
        "submittal_type",
        "document_url",
        "local_path",
        "status",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(doc_index)

    logger.info("=" * 60)
    logger.info("ILLINOIS BOA ARCHIVE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Archive rows: {len(archive_rows)}, unique permits: {len(unique_pairs)}")
    logger.info(f"Indexed document rows: {len(doc_index)}")
    if not list_only:
        logger.info(f"Downloaded PDFs: {downloaded}, failed: {failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download Illinois EPA archived air permit PDFs (default: Permit Type = Title V)."
        )
    )
    default_out = RAW_DATA_DIR / "il_epa_boa_archive"
    default_index = default_out / "il_epa_boa_archive_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_out,
        help=f"Directory for PDFs and summary (default: {default_out}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"CSV index path (default: {default_index}).",
    )
    parser.add_argument(
        "--permit-type",
        type=str,
        default="Title V",
        help='Permit Type dropdown value (e.g. "Title V", "Operating - Title V"). Default: Title V.',
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="Rows per GetArchivedAirPublicNotices request.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout seconds.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.15,
        help="Pause between API calls / downloads.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Build index CSV without downloading PDFs.",
    )
    parser.add_argument(
        "--max-permits",
        type=int,
        default=None,
        help="Stop after this many unique facility+permit pairs (for testing).",
    )
    parser.add_argument(
        "--max-pdfs",
        type=int,
        default=None,
        help="Stop after scheduling this many PDF downloads (for testing).",
    )
    args = parser.parse_args()

    crawl(
        output_dir=args.output_dir.expanduser().resolve(),
        index_csv=args.index_csv.expanduser().resolve(),
        permit_type=args.permit_type.strip(),
        page_size=max(1, args.page_size),
        timeout=max(10, args.timeout),
        sleep_seconds=max(0.0, args.sleep_seconds),
        list_only=args.list_only,
        max_permits=args.max_permits,
        max_pdfs=args.max_pdfs,
    )


if __name__ == "__main__":
    main()
