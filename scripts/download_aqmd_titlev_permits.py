#!/usr/bin/env python3
"""
Download SCAQMD Title V Facility Permit PDFs from the OnBase Public Access portal.

Portal: https://onbase-pub.aqmd.gov/publicaccess/DatasourceTemplateParameter.aspx?MyQueryID=254

Workflow:
1) GET the search form to obtain ASP.NET __VIEWSTATE / __EVENTVALIDATION tokens.
2) POST the form with a date range (defaults: 1/1/2020 -> today) to fetch the
   results grid. Each result row carries a `docID`, `checksum`, and `docName`.
3) For each row, POST PublicAccessProvider.ashx (action=ViewDocument,
   overrideFormat=Native) to receive an HTML wrapper that embeds the real
   PDFProvider.ashx URL in an iframe.
4) GET that PDFProvider.ashx URL to stream the PDF to disk.

The script writes an index CSV alongside the PDFs and is resumable: rerunning
skips PDFs already on disk.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

PORTAL_BASE = "https://onbase-pub.aqmd.gov/PublicAccess/"
SEARCH_URL = (
    "https://onbase-pub.aqmd.gov/publicaccess/DatasourceTemplateParameter.aspx"
    "?MyQueryID=254"
)
PROVIDER_URL = (
    PORTAL_BASE + "PublicAccessProvider.ashx"
    "?action=ViewDocument&overlay=Print&overrideFormat=Native"
)

DEFAULT_OUTPUT_DIR = RAW_DATA_DIR / "aqmd_titlev_permits"
DEFAULT_FROM_DATE = "1/1/2020"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": SEARCH_URL,
}

ROW_RE = re.compile(
    r'docID="(?P<docid>[^"]+)"\s+checksum="(?P<chk>[^"]*)"\s+docName="(?P<name>[^"]+)"'
    r'(?P<rest>.*?)</tr>',
    re.S,
)
CELL_RE = re.compile(r"<td[^>]*>(?P<val>.*?)</td>", re.S)
HIDDEN_RE = re.compile(r'name="(?P<n>[^"]+)"[^>]*value="(?P<v>[^"]*)"')
PDF_URL_RE = re.compile(r'(PDFProvider\.ashx\?[^"\']+)')


def build_session(retries: int) -> requests.Session:
    retry = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def fetch_form_tokens(session: requests.Session, timeout: int) -> Dict[str, str]:
    r = session.get(SEARCH_URL, timeout=timeout)
    r.raise_for_status()
    tokens: Dict[str, str] = {}
    for m in HIDDEN_RE.finditer(r.text):
        if m.group("n") in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            tokens[m.group("n")] = m.group("v")
    missing = {"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"} - tokens.keys()
    if missing:
        raise RuntimeError(f"Missing ASP.NET form tokens: {missing}")
    return tokens


def submit_search(
    session: requests.Session,
    from_date: str,
    to_date: str,
    timeout: int,
) -> str:
    tokens = fetch_form_tokens(session, timeout)
    payload = {
        **tokens,
        "cultureSelect": "9",
        "CustomQueryDisplay": "254",
        "ctl07$ctl00$DocumentDateInput$defaultFromDate": "",
        "ctl07$ctl00$DocumentDateInput$defaultToDate": "",
        "ctl07$ctl00$DocumentDateInput$dateRangePicker$fromDate$fromDate_date": from_date,
        "ctl07$ctl00$DocumentDateInput$dateRangePicker$toDate$toDate_date": to_date,
        "KeywordDisplay$ctl01$textInput": "",
        "KeywordDisplay$ctl02$textInput": "",
        "KeywordDisplay$ctl03$textInput": "",
        "KeywordDisplay$ctl04$textInput": "",
        "KeywordDisplay$ctl05$textInput": "",
        "KeywordDisplay$ctl06$textInput": "",
        "searchButton": "Search",
    }
    r = session.post(SEARCH_URL, data=payload, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_results(html: str) -> List[Dict[str, str]]:
    headers = [
        "Revision Nbr",
        "Revision Date",
        "Facility ID",
        "Facility Name",
        "Address",
        "City",
        "Zip Code",
    ]
    out: List[Dict[str, str]] = []
    for m in ROW_RE.finditer(html):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c.group("val"))).strip()
            for c in CELL_RE.finditer(m.group("rest"))
        ]
        row = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(headers)}
        row["docID"] = m.group("docid")
        row["checksum"] = m.group("chk")
        row["docName"] = m.group("name")
        out.append(row)
    return out


def filename_for(row: Dict[str, str]) -> str:
    fac_id = row.get("Facility ID") or "unknown"
    rev = row.get("Revision Nbr") or "0"
    rev_date = (row.get("Revision Date") or "").replace("/", "-")
    fac_name = clean_filename(row.get("Facility Name") or "")[:60].rstrip(" .")
    parts = [f"facID-{fac_id}", f"rev-{rev}", rev_date, fac_name]
    base = "_".join(p for p in parts if p)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    return f"{base}.pdf"


def fetch_pdf_url(
    session: requests.Session,
    docid: str,
    checksum: str,
    docname: str,
    timeout: int,
) -> str:
    body = {
        "DocID": quote(docid, safe=""),
        "chksum": quote(checksum, safe=""),
        "DocName": quote(docname, safe=""),
    }
    r = session.post(PROVIDER_URL, data=body, timeout=timeout)
    r.raise_for_status()
    m = PDF_URL_RE.search(r.text)
    if not m:
        raise RuntimeError("Could not locate PDFProvider URL in viewer wrapper")
    return PORTAL_BASE + m.group(1).replace("&amp;", "&")


def download_pdf(
    session: requests.Session,
    pdf_url: str,
    dest: Path,
    timeout: int,
) -> int:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session.get(pdf_url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if not ctype.lower().startswith("application/pdf"):
            head = next(r.iter_content(512), b"")
            raise RuntimeError(
                f"Expected PDF, got Content-Type={ctype!r}, head={head[:80]!r}"
            )
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
    tmp.rename(dest)
    return dest.stat().st_size


def write_index(rows: List[Dict[str, str]], path: Path) -> None:
    fieldnames = [
        "Revision Nbr",
        "Revision Date",
        "Facility ID",
        "Facility Name",
        "Address",
        "City",
        "Zip Code",
        "docName",
        "docID",
        "checksum",
        "filename",
        "status",
        "size_bytes",
        "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def parse_date_arg(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            d = datetime.strptime(value, fmt).date()
            return f"{d.month}/{d.day}/{d.year}"
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Could not parse date: {value!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-date", type=parse_date_arg, default=DEFAULT_FROM_DATE,
                    help="Start date (default: 1/1/2020)")
    ap.add_argument("--to-date", type=parse_date_arg, default=None,
                    help="End date (default: today)")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                    help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    ap.add_argument("--max", type=int, default=None,
                    help="Limit number of permits to download (for testing)")
    ap.add_argument("--resume", type=int, default=0,
                    help="Skip the first N rows (for resuming)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds to sleep between downloads")
    ap.add_argument("--timeout", type=int, default=300,
                    help="HTTP timeout per request (seconds)")
    ap.add_argument("--retries", type=int, default=5,
                    help="Retry attempts on transient HTTP errors")
    args = ap.parse_args()

    to_date = args.to_date or f"{date.today().month}/{date.today().day}/{date.today().year}"
    out_dir = args.output_dir
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Searching {args.from_date} -> {to_date}")
    session = build_session(args.retries)

    html = submit_search(session, args.from_date, to_date, args.timeout)
    rows = parse_results(html)
    logger.info(f"Found {len(rows)} permits")

    if not rows:
        logger.warning("No results to download")
        return 0

    if args.resume:
        logger.info(f"Skipping first {args.resume} rows")
        rows_to_process = rows[args.resume :]
    else:
        rows_to_process = rows
    if args.max is not None:
        rows_to_process = rows_to_process[: args.max]

    n_ok = n_skip = n_fail = 0
    for i, row in enumerate(rows_to_process, start=args.resume + 1):
        fn = filename_for(row)
        dest = pdf_dir / fn
        row["filename"] = fn
        if dest.exists() and dest.stat().st_size > 0:
            row["status"] = "skipped"
            row["size_bytes"] = str(dest.stat().st_size)
            n_skip += 1
            logger.debug(f"[{i}/{len(rows)}] skip existing {fn}")
            continue
        try:
            pdf_url = fetch_pdf_url(
                session, row["docID"], row["checksum"], row["docName"], args.timeout
            )
            size = download_pdf(session, pdf_url, dest, args.timeout)
            row["status"] = "downloaded"
            row["size_bytes"] = str(size)
            n_ok += 1
            logger.info(f"[{i}/{len(rows)}] {fn} ({size/1_048_576:.1f} MB)")
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
            n_fail += 1
            logger.error(f"[{i}/{len(rows)}] {fn}: {exc}")
        if args.delay:
            time.sleep(args.delay)

    index_path = out_dir / "index.csv"
    write_index(rows, index_path)
    logger.info(
        f"Done. ok={n_ok} skipped={n_skip} failed={n_fail} index={index_path}"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
