#!/usr/bin/env python3
"""Download Nevada NDEP air permit documents for data centers.

NDEP's public document portal (https://ndep-onbase.nv.gov, Hyland OnBase) has
an unauthenticated JSON API:

  POST /api/CustomQuery/KeywordSearch
       {"QueryID": 113,                          # NDEP - APC AIR PERMITS
        "Keywords": [{"ID": 197, "Value": "SWITCH", "KeywordOperator": "="}]}
  GET  /api/Document/<urlencoded row ID>         # returns the PDF

Keyword field IDs (from POST /api/Keywords {"QueryID":113}): 195 facility id,
196 permit number, 197 company name, 198 facility name, 199 address,
200 air case, 201 class, 202 document type (COMPLIANCE / ENFORCEMENT / OTHER /
PERMIT / PERMIT APPLICATION AND CORRESPONDENCE), 205 notes.
Company-name matching is prefix-style, so short/generic terms are skipped.

Jurisdiction notes: NDEP covers all of Nevada EXCEPT Clark County (Las Vegas —
Google Henderson, Switch LV etc. are at Clark County DAQ) and Washoe County
(Reno city proper). The Tahoe-Reno Industrial Center (Switch Citadel, Tesla,
etc., Storey County) IS NDEP jurisdiction. Clark/Washoe scrapers are separate
follow-ups.

Output layout
-------------
  <RAW_DATA_DIR>/nv_ndep_dc_permits/
    pdfs/<facility_id>_<permit>_<doctype>_<n>.pdf
    nv_ndep_dc_permits_index.csv
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

import dc_targets

BASE = "https://ndep-onbase.nv.gov"
QUERY_ID = 113  # NDEP - APC AIR PERMITS
KW_COMPANY, KW_FACILITY = 197, 198
OUTPUT_DIR = RAW_DATA_DIR / "nv_ndep_dc_permits"

# DisplayColumnValues order observed in the results grid.
DISPLAY_COLS = [
    "query_type", "facility_id", "doc_type", "permit_number", "air_case",
    "company_name", "facility_name", "class", "doc_category",
]

INDEX_FIELDS = ["search_term", "kw_field"] + DISPLAY_COLS + ["doc_id", "doc_name", "pdf_path"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36",
    "Content-Type": "application/json",
}


def search(session, kw_id: int, term: str, timeout: int) -> List[Dict]:
    body = {
        "QueryID": QUERY_ID,
        "Keywords": [{"ID": kw_id, "Value": term, "KeywordOperator": "="}],
        "FromDate": "", "ToDate": "",
    }
    r = session.post(f"{BASE}/api/CustomQuery/KeywordSearch", json=body, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    rows = []
    for item in payload.get("Data", []):
        vals = [c.get("Value", "") for c in item.get("DisplayColumnValues", [])]
        row = dict(zip(DISPLAY_COLS, vals + [""] * (len(DISPLAY_COLS) - len(vals))))
        row["doc_id"] = item.get("ID", "")
        row["doc_name"] = item.get("Name", "")
        rows.append(row)
    if payload.get("Truncated"):
        logger.warning(f"{term!r}: results truncated by server")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--terms", help="Comma-separated company terms (skip dc_targets)")
    ap.add_argument("--doc-types", default="PERMIT",
                    help="Document types to download (default PERMIT; "
                         "'' downloads every category)")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)

    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    else:
        # Prefix matching → require reasonably specific terms.
        terms = [t for t in dc_targets.entity_terms("NV") if len(t) >= 5]

    session = requests.Session()
    session.headers.update(HEADERS)

    seen: Dict[str, Dict] = {}
    for i, term in enumerate(terms, 1):
        for kw_id, kw_name in ((KW_COMPANY, "company"), (KW_FACILITY, "facility")):
            try:
                rows = search(session, kw_id, term, args.timeout)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}/{len(terms)}] {term!r} ({kw_name}): {e}")
                continue
            fresh = 0
            for r in rows:
                if r["doc_id"] not in seen:
                    r["search_term"], r["kw_field"] = term, kw_name
                    seen[r["doc_id"]] = r
                    fresh += 1
            if rows:
                logger.info(f"[{i}/{len(terms)}] {term!r} ({kw_name}): {len(rows)} rows, {fresh} new")
            time.sleep(args.delay)

    logger.success(f"{len(seen)} unique documents")

    wanted = {t.strip().upper() for t in args.doc_types.split(",") if t.strip()}
    rows = list(seen.values())

    if not args.no_download:
        to_get = [r for r in rows if not wanted or r["doc_type"].upper() in wanted]
        logger.info(f"Downloading {len(to_get)} documents (doc types: {sorted(wanted) or 'all'})")
        n_ok = 0
        for i, row in enumerate(to_get, 1):
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_",
                          f"{row['facility_id']}_{row['permit_number'] or row['air_case']}_{row['doc_type']}_{i}")
            dest = pdf_dir / f"{stem}.pdf"
            if dest.exists():
                row["pdf_path"] = str(dest)
                n_ok += 1
                continue
            try:
                r = session.get(f"{BASE}/api/Document/{quote(row['doc_id'], safe='')}",
                                timeout=args.timeout * 3)
                r.raise_for_status()
                if r.content.startswith(b"%PDF"):
                    dest.write_bytes(r.content)
                    row["pdf_path"] = str(dest)
                    n_ok += 1
                else:
                    logger.warning(f"[{i}] non-PDF response for {row['doc_name'][:60]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}] failed {row['doc_name'][:60]}: {e}")
            time.sleep(args.delay)
        logger.success(f"Downloaded {n_ok}/{len(to_get)} PDFs to {pdf_dir}")

    index_path = OUTPUT_DIR / "nv_ndep_dc_permits_index.csv"
    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    logger.success(f"Index written to {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
