#!/usr/bin/env python3
"""
Download Utah DAQ Title V issued permits from:
https://daqpermitting.utah.gov/OPS_Issued
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

# Ensure repository root is importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

START_URL = "https://daqpermitting.utah.gov/OPS_Issued"
AJAX_URL = "https://daqpermitting.utah.gov/ajax_report_data"
BASE_URL = "https://daqpermitting.utah.gov/"


def default_output_dir() -> Path:
    return RAW_DATA_DIR / "ut_daq_title_v_issued"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": START_URL,
        }
    )
    return session


def _get_csrf_token(session: requests.Session) -> str:
    resp = session.get(START_URL, timeout=120)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    csrf_input = soup.find("input", attrs={"name": "_csrf"})
    if csrf_input is None:
        raise RuntimeError("Could not find _csrf input on OPS_Issued page.")
    token = (csrf_input.get("value") or "").strip()
    if not token:
        raise RuntimeError("Found _csrf input, but value was empty.")
    return token


def fetch_rows(session: requests.Session, csrf_token: str) -> List[Dict]:
    payload = {
        "reportType": "opsissued",
        "startDate": "",
        "endDate": "",
        "_csrf": csrf_token,
    }
    resp = session.post(AJAX_URL, data=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise TypeError(f"Expected list in JSON data.data, got {type(rows)}")
    return rows


def _extract_pdf_links(pdf_info_html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(pdf_info_html or "", "html.parser")
    links: List[Dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_url = urljoin(BASE_URL, href)
        text = a.get_text(" ", strip=True)
        links.append({"url": abs_url, "label": text})
    return links


def _doc_id_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    vals = query.get("IntDocID") or query.get("intDocID") or []
    return vals[0] if vals else ""


def _issued_date(row: Dict) -> str:
    return (row.get("opIssuedDate") or row.get("aoIssuedDate") or "").strip()


def _build_filename(site_name: str, issued_date: str, label: str, doc_id: str) -> str:
    parts = ["ut_titlev", issued_date or "undated", site_name or "site", label or "permit", doc_id]
    stem = clean_filename(" - ".join(p for p in parts if p)) or "ut_titlev_permit"
    if not stem.lower().endswith(".pdf"):
        stem += ".pdf"
    return stem


def _download_pdf(session: requests.Session, url: str, timeout: int) -> bytes:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise RuntimeError(f"Response from {url} was not a PDF.")
    return resp.content


def write_index(index_csv: Path, rows: List[Dict[str, str]]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "site_name",
        "organization",
        "county",
        "issued_date",
        "link_label",
        "doc_id",
        "pdf_url",
        "status",
        "local_path",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Utah DAQ OPS_Issued Title V permits.")
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--index-csv", type=Path, default=default_output_dir() / "ut_daq_title_v_issued_index.csv")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--limit", type=int, default=None, help="Limit number of permit rows processed.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-skip-existing", action="store_true")
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    index_csv = args.index_csv.expanduser().resolve()
    skip_existing = not args.no_skip_existing

    session = _new_session()
    csrf_token = _get_csrf_token(session)
    rows = fetch_rows(session, csrf_token)
    logger.info(f"Fetched {len(rows)} rows from OPS_Issued.")

    # One row is one issued permit record. Keep all linked docs on that record.
    if args.limit is not None:
        rows = rows[: args.limit]

    index_rows: List[Dict[str, str]] = []
    for i, row in enumerate(rows, start=1):
        site_name = (row.get("masterAiName") or "").strip()
        org_name = (row.get("masterOrgName") or "").strip()
        county = (row.get("county") or "").strip()
        issued_date = _issued_date(row)
        links = _extract_pdf_links(row.get("pdfInfo") or "")
        if not links and row.get("intDocID"):
            doc_id = str(row.get("intDocID"))
            links = [
                {
                    "url": f"{BASE_URL}DocViewer?IntDocID={doc_id}&contentType=application/pdf",
                    "label": "View Title V Permit",
                }
            ]

        logger.info(f"[{i}/{len(rows)}] {site_name or '(no site name)'} | links: {len(links)}")
        if not links:
            index_rows.append(
                {
                    "site_name": site_name,
                    "organization": org_name,
                    "county": county,
                    "issued_date": issued_date,
                    "link_label": "",
                    "doc_id": "",
                    "pdf_url": "",
                    "status": "no_links",
                    "local_path": "",
                }
            )
            continue

        for link in links:
            pdf_url = link["url"]
            label = link["label"]
            doc_id = _doc_id_from_url(pdf_url)
            filename = _build_filename(site_name, issued_date, label, doc_id)
            dest = output_dir / filename

            rec = {
                "site_name": site_name,
                "organization": org_name,
                "county": county,
                "issued_date": issued_date,
                "link_label": label,
                "doc_id": doc_id,
                "pdf_url": pdf_url,
                "status": "",
                "local_path": "",
            }

            if args.dry_run:
                rec["status"] = "dry_run"
                index_rows.append(rec)
                continue

            if skip_existing and dest.exists():
                rec["status"] = "skipped_existing"
                rec["local_path"] = str(dest)
                index_rows.append(rec)
                continue

            try:
                pdf_bytes = _download_pdf(session, pdf_url, timeout=args.timeout)
                dest.write_bytes(pdf_bytes)
                rec["status"] = "downloaded"
                rec["local_path"] = str(dest)
            except Exception as exc:
                rec["status"] = f"failed: {exc}"

            index_rows.append(rec)

    write_index(index_csv, index_rows)
    downloaded = sum(1 for r in index_rows if r["status"] == "downloaded")
    skipped = sum(1 for r in index_rows if r["status"] == "skipped_existing")
    failed = sum(1 for r in index_rows if r["status"].startswith("failed:"))
    logger.info("=" * 60)
    logger.info("UTAH TITLE V OPS_ISSUED DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Rows processed: {len(rows)}")
    logger.info(f"Index rows: {len(index_rows)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output dir: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
