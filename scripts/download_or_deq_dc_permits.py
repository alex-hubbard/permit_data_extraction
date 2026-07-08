#!/usr/bin/env python3
"""Download Oregon DEQ air permits (ACDPs) for data centers.

Oregon DEQ's legacy AQ permits database (classic ASP, plain GET requests —
https://deqonline.deq.state.or.us/aq/aqpermitsonline/SearchResult.asp) lists
every permit document (permit, review report, annual report) with direct PDF
links. Data centers hold Standard or Simple ACDPs (and some General Type 18
"Electric Power Generators" ACDPs), so this script:

  1. Enumerates ALL documents for the selected permit types (full index kept —
     useful beyond data centers).
  2. Flags data-center rows by matching source/plant-site names against
     dc_targets terms plus a "data center" pattern.
  3. Downloads PDFs for flagged rows only (default: permit + review report
     doc types; annual reports on request).

Pagination: SearchResult.asp?...&pagenumber=N&socx=0, 50 rows/page.

Output layout
-------------
  <RAW_DATA_DIR>/or_deq_dc_permits/
    pdfs/<source>_<permit>_<doctype>_<year>.pdf
    or_deq_acdp_full_index.csv     every document row scraped
    or_deq_dc_permits_index.csv    data-center subset
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from bs4 import BeautifulSoup
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

import dc_targets

BASE = "https://deqonline.deq.state.or.us/aq/aqpermitsonline/SearchResult.asp"
OUTPUT_DIR = RAW_DATA_DIR / "or_deq_dc_permits"

# SearchFilter.asp option values: 1=Title V, 2=Standard ACDP, 3=Simple ACDP,
# 21=General ACDP Type 18 (Electric Power Generators)
DEFAULT_PERMIT_TYPES = ["2", "3", "21"]

COLS = [
    "source_number", "source_name", "plant_site_name", "address", "city",
    "zip", "county", "region", "permit_number", "permit_type",
    "doc_type", "doc_year", "unused", "download_url",
]

DC_NAME_PATTERN = re.compile(r"data ?cent|server|colocation|cloud|hyperscale", re.IGNORECASE)

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36"}


def fetch_page(session, permit_type: str, page: int, timeout: int) -> str:
    params = {
        "sourcenumber": "", "sourcename": "", "plantsitename": "",
        "streetaddress": "", "city": "", "zip": "", "county": "",
        "deqregion": "", "permitnumber": "", "permittype": permit_type,
        "pagenumber": str(page), "socx": "0",
    }
    r = session.get(BASE, params=params, timeout=timeout, verify=False)
    r.raise_for_status()
    return r.text


def parse_rows(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", class_="copyrowshade")
        if len(tds) < 10:
            continue
        vals = [td.get_text(" ", strip=True) for td in tds]
        link = tr.find("a", href=re.compile(r"\.pdf$", re.IGNORECASE))
        row = dict(zip(COLS, vals + [""] * (len(COLS) - len(vals))))
        row["download_url"] = link["href"] if link else ""
        out.append(row)
    return out


def has_next(html: str) -> bool:
    return "Go to Next Page" in html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--permit-types", default=",".join(DEFAULT_PERMIT_TYPES))
    ap.add_argument("--doc-types", default="permit,review report,rr",
                    help="Doc types to download for DC rows (comma-separated, "
                         "case-insensitive substring match)")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    import urllib3
    urllib3.disable_warnings()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows: List[Dict[str, str]] = []
    for ptype in [t.strip() for t in args.permit_types.split(",") if t.strip()]:
        page = 1
        while True:
            html = fetch_page(session, ptype, page, args.timeout)
            rows = parse_rows(html)
            all_rows.extend(rows)
            logger.info(f"permit type {ptype} page {page}: {len(rows)} rows")
            if not rows or not has_next(html):
                break
            page += 1
            time.sleep(args.delay)

    logger.success(f"{len(all_rows)} document rows scraped")

    # Flag data-center rows: name pattern or known-operator terms.
    terms = [t.lower() for t in dc_targets.entity_terms("OR")]
    def is_dc(row) -> bool:
        # Normalize punctuation the same way dc_targets cleans its terms, so
        # "DESIGN, LLC" matches the "Design LLC" alias.
        text = f"{row['source_name']} {row['plant_site_name']}".lower()
        text = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 &-]", " ", text))
        return bool(DC_NAME_PATTERN.search(text)) or any(t in text for t in terms)

    dc_rows = [r for r in all_rows if is_dc(r)]
    dc_sources = {r["source_number"] for r in dc_rows}
    # Include all docs for a flagged source (a source's permit may not repeat
    # the operator name on every document row).
    dc_rows = [r for r in all_rows if r["source_number"] in dc_sources]
    logger.success(f"{len(dc_sources)} data-center sources, {len(dc_rows)} document rows")

    for path, rows in (
        (OUTPUT_DIR / "or_deq_acdp_full_index.csv", all_rows),
        (OUTPUT_DIR / "or_deq_dc_permits_index.csv", dc_rows),
    ):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS + ["pdf_path"], extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    logger.success(f"Indexes written to {OUTPUT_DIR}")

    if args.no_download:
        return 0

    wanted = [t.strip().lower() for t in args.doc_types.split(",") if t.strip()]
    to_get = [r for r in dc_rows if r["download_url"]
              and any(w in r["doc_type"].lower() for w in wanted)]
    logger.info(f"Downloading {len(to_get)} PDFs (doc types: {args.doc_types})")
    n_ok = 0
    for i, row in enumerate(to_get, 1):
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_",
                      f"{row['source_number']}_{row['permit_number']}_{row['doc_type']}_{row['doc_year']}")
        dest = pdf_dir / f"{stem}.pdf"
        if dest.exists():
            row["pdf_path"] = str(dest)
            n_ok += 1
            continue
        try:
            r = session.get(row["download_url"], timeout=args.timeout, verify=False)
            r.raise_for_status()
            if r.content.startswith(b"%PDF"):
                dest.write_bytes(r.content)
                row["pdf_path"] = str(dest)
                n_ok += 1
            else:
                logger.warning(f"[{i}] not a PDF: {row['download_url']}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}] failed {row['download_url']}: {e}")
        time.sleep(args.delay)

    with open(OUTPUT_DIR / "or_deq_dc_permits_index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS + ["pdf_path"], extrasaction="ignore")
        w.writeheader()
        for r in dc_rows:
            w.writerow(r)
    logger.success(f"Downloaded {n_ok}/{len(to_get)} PDFs to {pdf_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
