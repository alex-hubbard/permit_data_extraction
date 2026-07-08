#!/usr/bin/env python3
"""Download Texas TCEQ New Source Review (NSR) permits for data centers.

Data center backup-generator fleets in Texas are permitted as minor NSR
(often via Permits by Rule or standard permits), not Title V, so this script
targets Central File Room record series 1081 ("AIR / New Source Review
Permit") instead of 1051. It reuses the CFR session/search/download machinery
from download_tx_tceq_titlev_permits.py (see that script's docstring for the
non-obvious request-shape requirements).

Two discovery modes, combinable:
  * entity mode (default): search CFR by Regulated Entity Name for every
    data-center operator/company term from dc_targets (known-facility list +
    national aliases).
  * full-text mode (--full-text-sweep): CFR full-text search for
    "data center" within record series 1081, which catches shell-named
    entities (e.g. project LLCs) the alias list misses.

Output layout
-------------
  <RAW_DATA_DIR>/tx_tceq_dc_nsr_permits/
    pdfs/<entity>_<dDocName>_<title>.pdf
    tx_tceq_dc_nsr_index.csv      one row per CFR document hit (metadata
                                  always recorded, even with --no-download)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Dict, List

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

import download_tx_tceq_titlev_permits as tv
import dc_targets

NSR_RECORD_SERIES = "1081"  # AIR / New Source Review Permit
OUTPUT_DIR = RAW_DATA_DIR / "tx_tceq_dc_nsr_permits"

INDEX_FIELDS = [
    "search_term", "search_mode", "dDocName", "dDocTitle", "xRegEntName",
    "xPrimaryID", "xSecondaryID", "xRecordSeries", "dInDate", "dOriginalName",
    "downloaded", "pdf_path",
]


def rows_for_term(session, access_id, client_ip, term: str, mode: str,
                  max_rows: int, timeout: int) -> List[Dict[str, str]]:
    """Paginate CFR search results for one search term."""
    out, start = [], 1
    while start <= max_rows:
        kwargs = dict(record_series=NSR_RECORD_SERIES, result_count=50,
                      start_row=start, timeout=timeout)
        if mode == "entity":
            kwargs["reg_entity"] = term
        else:
            kwargs["full_text"] = term
        payload = tv.cfr_search(session, access_id, client_ip, **kwargs)
        rows = tv.cfr_extract_rows(payload)
        if not rows:
            break
        for r in rows:
            r["search_term"], r["search_mode"] = term, mode
        out.extend(rows)
        if len(rows) < 50:
            break
        start += 50
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entities", help="Comma-separated entity names (skip dc_targets)")
    ap.add_argument("--include-facilities", action="store_true",
                    help="Also search individual facility names from the DC list")
    ap.add_argument("--full-text-sweep", action="store_true",
                    help='Add a full-text "data center" sweep of series 1081')
    ap.add_argument("--no-download", action="store_true", help="Index only, no PDFs")
    ap.add_argument("--from-index", action="store_true",
                    help="Skip searching; download from the existing index CSV "
                         "(use with --doc-titles to fetch additional doc types)")
    ap.add_argument("--doc-titles", default="Final Action",
                    help="Comma-separated dDocTitle filter for downloads "
                         "(default: Final Action = issued permit + MAERT; "
                         "use '' to download everything)")
    ap.add_argument("--max-rows-per-term", type=int, default=500)
    ap.add_argument("--max-rows-fulltext", type=int, default=5000,
                    help="Row cap for the full-text sweep (statewide, so higher)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    index_path = OUTPUT_DIR / "tx_tceq_dc_nsr_index.csv"

    session = tv.build_session(args.retries)
    access_id, client_ip = tv.cfr_get_access(session, args.timeout)

    seen_docs: Dict[str, Dict[str, str]] = {}

    if args.from_index:
        with open(index_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                seen_docs[row["dDocName"]] = row
        logger.info(f"Loaded {len(seen_docs)} documents from existing index")
        terms = []
    elif args.entities:
        terms = [t.strip() for t in args.entities.split(",") if t.strip()]
    else:
        terms = dc_targets.entity_terms("TX", include_facilities=args.include_facilities)
    for i, term in enumerate(terms, 1):
        try:
            rows = rows_for_term(session, access_id, client_ip, term, "entity",
                                 args.max_rows_per_term, args.timeout)
        except Exception as e:  # noqa: BLE001 — one bad term shouldn't kill the sweep
            logger.warning(f"[{i}/{len(terms)}] {term!r}: search failed: {e}")
            continue
        fresh = [r for r in rows if r.get("dDocName") not in seen_docs]
        for r in fresh:
            seen_docs[r["dDocName"]] = r
        if rows:
            logger.info(f"[{i}/{len(terms)}] {term!r}: {len(rows)} hits ({len(fresh)} new)")
        time.sleep(args.delay)

    if args.full_text_sweep:
        rows = rows_for_term(session, access_id, client_ip, '"data center"',
                             "fulltext", args.max_rows_fulltext, args.timeout)
        fresh = [r for r in rows if r.get("dDocName") not in seen_docs]
        for r in fresh:
            seen_docs[r["dDocName"]] = r
        logger.info(f'full-text "data center": {len(rows)} hits ({len(fresh)} new)')

    logger.success(f"{len(seen_docs)} unique CFR documents across {len(terms)} terms")

    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for doc in seen_docs.values():
            doc.setdefault("downloaded", "")
            doc.setdefault("pdf_path", "")
            w.writerow(doc)
    logger.success(f"Index written to {index_path}")

    if args.no_download:
        return 0

    wanted = {t.strip().lower() for t in args.doc_titles.split(",") if t.strip()}
    docs = [
        d for d in seen_docs.values()
        if not wanted or (d.get("dDocTitle") or "").strip().lower() in wanted
    ]
    logger.info(f"{len(docs)} of {len(seen_docs)} documents match --doc-titles filter")
    n_ok = 0
    for i, doc in enumerate(docs, 1):
        name = tv.safe_filename([
            doc.get("xRegEntName", ""), doc.get("dDocName", ""), doc.get("dDocTitle", "")
        ]) + ".pdf"
        dest = pdf_dir / name
        if dest.exists():
            doc["downloaded"], doc["pdf_path"] = "cached", str(dest)
            n_ok += 1
            continue
        try:
            tv.cfr_download(session, doc["dDocName"], dest, timeout=args.timeout)
            doc["downloaded"], doc["pdf_path"] = "yes", str(dest)
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            doc["downloaded"] = f"error: {e}"
            logger.warning(f"[{i}/{len(docs)}] download failed for {doc.get('dDocName')}: {e}")
        time.sleep(args.delay)

    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for doc in seen_docs.values():
            w.writerow(doc)
    logger.success(f"Downloaded {n_ok}/{len(seen_docs)} documents to {pdf_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
