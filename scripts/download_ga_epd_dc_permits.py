#!/usr/bin/env python3
"""Download Georgia EPD air permits for data centers.

Georgia permit numbers embed the facility's SIC code, and the Air Protection
Branch search engine (https://permitsearch.gaepd.org, ASP.NET/Telerik — driven
here with Selenium) supports SIC search. SIC 7374 (data processing) therefore
enumerates the state's data center permits directly — no name matching needed.
Optionally sweeps extra SIC codes and operator-name searches for stragglers.

Each result row carries links to the final permit PDF and the permitting
narrative (permit.aspx?id=PDF-OP-<n> / PDF-ON-<n>); both are downloaded with
requests using the Selenium session's cookies.

Output layout
-------------
  <RAW_DATA_DIR>/ga_epd_dc_permits/
    pdfs/<airs>_<permit_no>_{permit,narrative}.pdf
    ga_epd_dc_permits_index.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin

import requests
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

BASE_URL = "https://permitsearch.gaepd.org/"
OUTPUT_DIR = RAW_DATA_DIR / "ga_epd_dc_permits"
DEFAULT_SICS = ["7374"]  # data processing & computer services

INDEX_FIELDS = [
    "search_sic", "airs_number", "facility_name", "permit_number",
    "issuance_date", "permit_type", "permit_url", "narrative_url",
    "permit_pdf", "narrative_pdf",
]

ROW_ID_RE = re.compile(r"gvwPermits_ctl00_ctl\d+")


def make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=opts)


def parse_results_page(driver) -> List[Dict[str, str]]:
    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "table[id*='gvwPermits'] tr"):
        links = tr.find_elements(By.TAG_NAME, "a")
        permit_link = next((a for a in links if "hlFinalPermit" in (a.get_attribute("id") or "")), None)
        if permit_link is None:
            continue
        narrative = next((a for a in links if "hlNarrative" in (a.get_attribute("id") or "")), None)
        cells = [td.text.strip() for td in tr.find_elements(By.TAG_NAME, "td")]
        # Grid columns: AIRS | Facility | Permit (link) | Issuance | Other docs | Type
        rows.append({
            "airs_number": cells[0] if cells else "",
            "facility_name": cells[1] if len(cells) > 1 else "",
            "permit_number": permit_link.text.strip(),
            "issuance_date": cells[3] if len(cells) > 3 else "",
            "permit_type": cells[-1] if cells else "",
            "permit_url": urljoin(BASE_URL, permit_link.get_attribute("href")),
            "narrative_url": urljoin(BASE_URL, narrative.get_attribute("href")) if narrative else "",
        })
    return rows


def collect_for_sic(driver, sic: str, delay: float) -> List[Dict[str, str]]:
    driver.get(BASE_URL)
    time.sleep(delay + 2)
    box = driver.find_element(By.ID, "ctl00_ContentPlaceHolder2_txtSIC")
    box.clear()
    box.send_keys(sic)
    driver.find_element(By.ID, "ctl00_ContentPlaceHolder2_btnSearch").click()
    time.sleep(delay + 3)

    all_rows, page = [], 1
    while True:
        rows = parse_results_page(driver)
        for r in rows:
            r["search_sic"] = sic
        all_rows.extend(rows)
        logger.info(f"SIC {sic} page {page}: {len(rows)} permits")
        # Telerik pager: anchor whose text is the next page number.
        nxt = [a for a in driver.find_elements(By.CSS_SELECTOR, "div[id*='gvwPermits'] a")
               if a.text.strip() == str(page + 1)]
        if not nxt:
            break
        nxt[0].click()
        page += 1
        time.sleep(delay + 3)
    return all_rows


def download_pdfs(driver, rows: List[Dict[str, str]], pdf_dir: Path, delay: float) -> None:
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36"})
    for c in driver.get_cookies():
        sess.cookies.set(c["name"], c["value"])
    for i, row in enumerate(rows, 1):
        for kind, url_key, path_key in (
            ("permit", "permit_url", "permit_pdf"),
            ("narrative", "narrative_url", "narrative_pdf"),
        ):
            url = row.get(url_key)
            if not url:
                continue
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{row['airs_number']}_{row['permit_number']}_{kind}")
            dest = pdf_dir / f"{stem}.pdf"
            dest_doc = pdf_dir / f"{stem}.doc"
            if dest.exists() or dest_doc.exists():
                row[path_key] = str(dest if dest.exists() else dest_doc)
                continue
            try:
                r = sess.get(url, timeout=120)
                r.raise_for_status()
                # Older permits are stored as Word documents rather than PDFs.
                if not r.content.startswith(b"%PDF"):
                    if "msword" in r.headers.get("Content-Type", "") or r.content[:2] == b"\xd0\xcf":
                        dest = dest_doc
                    else:
                        logger.warning(f"{url}: unexpected Content-Type {r.headers.get('Content-Type')}")
                        continue
                dest.write_bytes(r.content)
                row[path_key] = str(dest)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}/{len(rows)}] {kind} download failed for {row['permit_number']}: {e}")
            time.sleep(delay)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sics", default=",".join(DEFAULT_SICS),
                    help="Comma-separated SIC codes to sweep (default 7374)")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-headless", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)

    driver = make_driver(headless=not args.no_headless)
    try:
        all_rows: List[Dict[str, str]] = []
        for sic in [s.strip() for s in args.sics.split(",") if s.strip()]:
            all_rows.extend(collect_for_sic(driver, sic, args.delay))
        # Dedupe on (permit_number, issuance_date) — reissues share numbers.
        seen, rows = set(), []
        for r in all_rows:
            key = (r["permit_number"], r["issuance_date"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
        logger.success(f"{len(rows)} unique permits found")

        if not args.no_download:
            download_pdfs(driver, rows, pdf_dir, args.delay)
    finally:
        driver.quit()

    index_path = OUTPUT_DIR / "ga_epd_dc_permits_index.csv"
    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    logger.success(f"Index written to {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
