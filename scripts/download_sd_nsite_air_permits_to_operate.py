#!/usr/bin/env python3
"""
From nSITE results, filter to Air Permit-to-Operate facilities and download
one latest in-effect Permit document per facility.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from loguru import logger

# Ensure repository root is importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

BASE_URL = "https://ceris.deq.nd.gov/ext/nsite"
RESULTS_URL = f"{BASE_URL}/DEFAULT/map/results"
DOC_PROFILE_PATH = "/ss/api/nsite-explorer/default-mode/profiles/4-documents/1-documents"

OPERATE_CATEGORIES = [
    "Air Permit to Operate - General",
    "Air Permit to Operate - Major",
    "Air Permit to Operate - Minor",
    "Air Permit to Operate - Synthetic Minor",
]

HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


@dataclass
class FacilityDoc:
    site_id: str
    site_name: str
    source_type: str
    source_status: str
    source_functional_area: str
    document_name: str
    document_description: str
    document_category: str
    document_date: Optional[datetime]
    document_url: str


def _session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "Referer": RESULTS_URL,
            "Origin": "https://ceris.deq.nd.gov",
        }
    )
    # Prime .AspNetCore.Session cookie so /ss endpoints respond with JSON payloads.
    resp = sess.get(RESULTS_URL, timeout=90)
    resp.raise_for_status()
    return sess


def _build_sites_query_payload() -> Dict[str, object]:
    filter_values = [{"attributeName": "Permit Program", "attributeValue": "Air Quality"}]
    filter_values.append({"attributeName": "Permit Status", "attributeValue": "In Effect"})
    for cat in OPERATE_CATEGORIES:
        filter_values.append({"attributeName": "Permit Category", "attributeValue": cat})

    criteria = {
        "displayHeight": 941,
        "displayWidth": 1920,
        "filterValuesJson": quote(json.dumps(filter_values, separators=(",", ":"))),
        "isIncludeUnmappable": 1,
        "latitudeMax": 49.4923,
        "latitudeMin": 45.40041,
        "longitudeMax": -94.92682,
        "longitudeMin": -105.85807,
        "modeId": "DEFAULT",
        "searchTerm": "",
    }
    return {
        "responseContentType": "application/json",
        "includeMetadataInResponse": "false",
        "loadChildren": "true",
        "queryParams": json.dumps({"filter": [criteria]}, separators=(",", ":")),
        "filterString": "",
        "top": "5000",
    }


def fetch_filtered_site_ids(sess: requests.Session) -> List[str]:
    resp = sess.get(f"{BASE_URL}/ss/explorersites", params=_build_sites_query_payload(), timeout=120)
    resp.raise_for_status()
    data = resp.json()
    ids: List[str] = []
    for row in data.get("queryResults") or []:
        site_id = str(row.get("siteId") or "").strip()
        if site_id:
            ids.append(site_id)
    deduped = sorted(set(ids))
    logger.info(f"Filtered facility count: {len(deduped)}")
    return deduped


def fetch_site_name(sess: requests.Session, site_id: str) -> str:
    params = {
        "responseContentType": "application/json",
        "includeMetadataInResponse": "false",
        "loadChildren": "true",
        "queryParams": json.dumps({"filter": [{"id": site_id}]}, separators=(",", ":")),
        "filterString": "",
        "top": "undefined",
    }
    resp = sess.get(f"{BASE_URL}/ss/explorersiteheader", params=params, timeout=90)
    resp.raise_for_status()
    rows = resp.json().get("queryResults") or []
    if not rows:
        return site_id
    return (rows[0].get("siteName") or "").strip() or site_id


def _parse_document_url(doc_url_html: str) -> str:
    match = HREF_RE.search(doc_url_html or "")
    return match.group(1).strip() if match else ""


def _parse_doc_date(token: str) -> Optional[datetime]:
    if not token:
        return None
    try:
        # Handles 2025-05-15T13:59:01.0000000-05:00
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_latest_permit_doc_for_site(sess: requests.Session, site_id: str, site_name: str) -> Optional[FacilityDoc]:
    params = {
        "responseContentType": "application/json",
        "includeMetadataInResponse": "true",
        "loadChildren": "true",
        "queryParams": json.dumps({"filter": [{"id": site_id}]}, separators=(",", ":")),
        "filterString": "",
        "top": "undefined",
    }
    resp = sess.get(f"{BASE_URL}{DOC_PROFILE_PATH}", params=params, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("queryResults") or []

    candidates: List[FacilityDoc] = []
    for row in rows:
        source_status = (row.get("docMgmtSourcestatus") or "").strip()
        source_functional_area = (row.get("docMgmtSourcefunctionalarea") or "").strip()
        source_type = (row.get("docMgmtSourcetype") or "").strip()
        if source_functional_area.lower() != "permit":
            continue
        if source_status.lower() != "in effect":
            continue
        if "air permit to operate" not in source_type.lower():
            continue

        document_url = _parse_document_url(row.get("docMgmtDocurl") or "")
        if not document_url:
            continue
        candidates.append(
            FacilityDoc(
                site_id=site_id,
                site_name=site_name,
                source_type=source_type,
                source_status=source_status,
                source_functional_area=source_functional_area,
                document_name=(row.get("docMgmtDocName") or "").strip(),
                document_description=(row.get("docMgmtDocDescr") or "").strip(),
                document_category=(row.get("docMgmtCategory") or "").strip(),
                document_date=_parse_doc_date(row.get("docMgmtDocRvcdCreatedDate") or ""),
                document_url=document_url,
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda d: d.document_date or datetime.min, reverse=True)
    return candidates[0]


def _download_one(sess: requests.Session, out_dir: Path, doc: FacilityDoc, skip_existing: bool) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_tag = doc.document_date.strftime("%Y%m%d") if doc.document_date else "undated"
    stem = clean_filename(f"sd_{doc.site_id}_{date_tag}_{doc.document_name}") or f"sd_{doc.site_id}_{date_tag}"
    if "." in Path(stem).name:
        filename = stem
    else:
        filename = f"{stem}.pdf"
    dest = out_dir / filename

    row = {
        "site_id": doc.site_id,
        "site_name": doc.site_name,
        "source_type": doc.source_type,
        "source_status": doc.source_status,
        "source_functional_area": doc.source_functional_area,
        "document_name": doc.document_name,
        "document_description": doc.document_description,
        "document_category": doc.document_category,
        "document_date": doc.document_date.isoformat() if doc.document_date else "",
        "document_url": doc.document_url,
        "status": "",
        "local_path": "",
    }

    if skip_existing and dest.exists():
        row["status"] = "skipped_existing"
        row["local_path"] = str(dest)
        return row

    try:
        r = sess.get(doc.document_url, timeout=120)
        r.raise_for_status()
        with dest.open("wb") as handle:
            handle.write(r.content)
        row["status"] = "downloaded"
        row["local_path"] = str(dest)
    except Exception as exc:
        row["status"] = f"failed_download: {exc}"
    return row


def write_index(index_csv: Path, rows: List[Dict[str, str]]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "site_id",
        "site_name",
        "source_type",
        "source_status",
        "source_functional_area",
        "document_name",
        "document_description",
        "document_category",
        "document_date",
        "document_url",
        "status",
        "local_path",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Start from nSITE results, filter to Air Permit-to-Operate facilities, "
            "and download one latest in-effect Permit file per facility."
        )
    )
    default_out = RAW_DATA_DIR / "sd_nsite_air_permits_to_operate"
    default_index = default_out / "sd_nsite_air_permits_to_operate_index.csv"
    parser.add_argument("--output-dir", type=Path, default=default_out)
    parser.add_argument("--index-csv", type=Path, default=default_index)
    parser.add_argument("--max-facilities", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8, help="Parallel worker count.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-skip-existing", action="store_true")
    args = parser.parse_args()

    sess = _session()
    site_ids = fetch_filtered_site_ids(sess)
    if args.max_facilities is not None:
        site_ids = site_ids[: args.max_facilities]
    logger.info(f"Processing facilities: {len(site_ids)}")

    # Resolve facility names sequentially (lightweight and stable endpoint).
    site_name_map: Dict[str, str] = {}
    for sid in site_ids:
        try:
            site_name_map[sid] = fetch_site_name(sess, sid)
        except Exception:
            site_name_map[sid] = sid

    matched_docs: List[FacilityDoc] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(fetch_latest_permit_doc_for_site, sess, sid, site_name_map.get(sid, sid)): sid
            for sid in site_ids
        }
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                doc = fut.result()
            except Exception as exc:
                logger.warning(f"{sid}: failed to collect docs ({exc})")
                continue
            if doc is not None:
                matched_docs.append(doc)

    logger.info(f"Facilities with matching latest permit doc: {len(matched_docs)}")

    rows: List[Dict[str, str]] = []
    if args.dry_run:
        for doc in matched_docs:
            rows.append(
                {
                    "site_id": doc.site_id,
                    "site_name": doc.site_name,
                    "source_type": doc.source_type,
                    "source_status": doc.source_status,
                    "source_functional_area": doc.source_functional_area,
                    "document_name": doc.document_name,
                    "document_description": doc.document_description,
                    "document_category": doc.document_category,
                    "document_date": doc.document_date.isoformat() if doc.document_date else "",
                    "document_url": doc.document_url,
                    "status": "dry_run",
                    "local_path": "",
                }
            )
    else:
        out_dir = args.output_dir.expanduser().resolve()
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    _download_one,
                    sess,
                    out_dir,
                    doc,
                    skip_existing=not args.no_skip_existing,
                ): doc.site_id
                for doc in matched_docs
            }
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    rows.append(fut.result())
                except Exception as exc:
                    rows.append(
                        {
                            "site_id": sid,
                            "site_name": site_name_map.get(sid, sid),
                            "source_type": "",
                            "source_status": "",
                            "source_functional_area": "",
                            "document_name": "",
                            "document_description": "",
                            "document_category": "",
                            "document_date": "",
                            "document_url": "",
                            "status": f"failed_download: {exc}",
                            "local_path": "",
                        }
                    )

    index_csv = args.index_csv.expanduser().resolve()
    write_index(index_csv, rows)

    downloaded = sum(1 for r in rows if r["status"] == "downloaded")
    skipped = sum(1 for r in rows if r["status"] == "skipped_existing")
    failed = sum(1 for r in rows if r["status"].startswith("failed_"))
    logger.info("=" * 70)
    logger.info("SD nSITE AIR PERMIT-TO-OPERATE DOWNLOAD COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Facilities considered: {len(site_ids)}")
    logger.info(f"Facilities matched: {len(matched_docs)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped existing: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
