#!/usr/bin/env python3
"""Download Maricopa County AQD permit documents for data-center facilities.

Stage 2 of the Maricopa scraper. Reads facility IDs from the stage-1 index
(download_az_maricopa_dc_facilities.py) and pulls each facility's documents
from the county's OnBase public-access EDMS — same Hyland OBPA JSON API as
Nevada's NDEP portal:

  GET  /aq/api/CustomQuery                       → query 391 "AQ - Public Access Query"
  POST /aq/api/CustomQuery/KeywordSearch
       {"QueryID": 391, "Keywords": [{"ID": 3635, "Value": "<facility_id>",
        "KeywordOperator": "="}]}                → document rows
  GET  /aq/api/Document/<urlencoded row ID>      → the PDF

Row Name format: "<facility_id> - <permit_no> - <date> - <category> -
<doc type> - ...". Default download filter keeps PERMIT AND CONDITIONS
(the issued permit) and TSD (technical support docs with equipment tables);
pass --doc-filter '' to download everything.

Output layout
-------------
  <RAW_DATA_DIR>/az_maricopa_dc_permits/
    pdfs/<facility_id>_<permit_no>_<doctype>_<n>.pdf
    az_maricopa_dc_permits_index.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote

import requests
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

BASE = "https://edms.maricopa.gov/aq"
QUERY_ID = 391       # AQ - Public Access Query
KW_FACILITY_ID = 3635  # AqFacilityID
FACILITIES_CSV = RAW_DATA_DIR / "az_maricopa_dc_facilities" / "az_maricopa_dc_facilities.csv"
OUTPUT_DIR = RAW_DATA_DIR / "az_maricopa_dc_permits"

INDEX_FIELDS = [
    "facility_id", "facility_nm", "match_reason", "permit_no", "doc_date",
    "category", "doc_type", "doc_name", "doc_id", "pdf_path",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36",
    "Content-Type": "application/json",
}

NAME_RE = re.compile(
    r"^(?P<fid>F\d+)\s*-\s*(?P<permit>[^-]*)-\s*(?P<date>[\d/]*)\s*-\s*"
    r"(?P<category>[^-]*)-\s*(?P<doctype>[^-]*)"
)


def search_facility(session, facility_id: str, timeout: int) -> List[Dict]:
    body = {
        "QueryID": QUERY_ID,
        "Keywords": [{"ID": KW_FACILITY_ID, "Value": facility_id, "KeywordOperator": "="}],
        "FromDate": "", "ToDate": "",
    }
    r = session.post(f"{BASE}/api/CustomQuery/KeywordSearch", json=body, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("Truncated"):
        logger.warning(f"{facility_id}: results truncated by server")
    rows = []
    for item in payload.get("Data", []):
        name = item.get("Name", "")
        m = NAME_RE.match(name)
        rows.append({
            "facility_id": facility_id,
            "permit_no": (m.group("permit").strip() if m else ""),
            "doc_date": (m.group("date").strip() if m else ""),
            "category": (m.group("category").strip() if m else ""),
            "doc_type": (m.group("doctype").strip() if m else ""),
            "doc_name": name,
            "doc_id": item.get("ID", ""),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--facility-ids", help="Comma-separated facility IDs (skip stage-1 CSV)")
    ap.add_argument("--doc-filter", default="PERMIT AND CONDITIONS,TSD",
                    help="Comma-separated doc-type substrings to download ('' = all)")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)

    fac_meta: Dict[str, Dict[str, str]] = {}
    if args.facility_ids:
        ids = [f.strip() for f in args.facility_ids.split(",") if f.strip()]
    else:
        with open(FACILITIES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                fac_meta[row["facility_id"]] = row
        ids = list(fac_meta)

    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows: List[Dict] = []
    for i, fid in enumerate(ids, 1):
        try:
            rows = search_facility(session, fid, args.timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(ids)}] {fid}: search failed: {e}")
            continue
        meta = fac_meta.get(fid, {})
        for r in rows:
            r["facility_nm"] = meta.get("facility_nm", "")
            r["match_reason"] = meta.get("match_reason", "")
        all_rows.extend(rows)
        if rows:
            logger.info(f"[{i}/{len(ids)}] {fid} {meta.get('facility_nm','')[:40]!r}: {len(rows)} docs")
        time.sleep(args.delay)

    logger.success(f"{len(all_rows)} documents across {len(ids)} facilities")

    wanted = [t.strip().upper() for t in args.doc_filter.split(",") if t.strip()]
    if not args.no_download:
        to_get = [r for r in all_rows
                  if not wanted or any(w in r["doc_type"].upper() for w in wanted)]
        logger.info(f"Downloading {len(to_get)} documents matching {wanted or 'all'}")
        n_ok = 0
        for i, row in enumerate(to_get, 1):
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_",
                          f"{row['facility_id']}_{row['permit_no']}_{row['doc_type']}_{i}")
            dest = pdf_dir / f"{stem}.pdf"
            if dest.exists():
                row["pdf_path"] = str(dest)
                n_ok += 1
                continue
            try:
                r = session.get(f"{BASE}/api/Document/{quote(row['doc_id'], safe='')}",
                                timeout=args.timeout)
                r.raise_for_status()
                if r.content.startswith(b"%PDF"):
                    dest.write_bytes(r.content)
                    row["pdf_path"] = str(dest)
                    n_ok += 1
                else:
                    logger.warning(f"[{i}] non-PDF for {row['doc_name'][:70]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}] failed {row['doc_name'][:70]}: {e}")
            time.sleep(args.delay)
        logger.success(f"Downloaded {n_ok}/{len(to_get)} PDFs to {pdf_dir}")

    index_path = OUTPUT_DIR / "az_maricopa_dc_permits_index.csv"
    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    logger.success(f"Index written to {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
