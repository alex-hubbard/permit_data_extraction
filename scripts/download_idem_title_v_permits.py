#!/usr/bin/env python3
"""
Download Indiana IDEM ECM Title V OAQ permit PDFs from the public search results.

Uses the same query as the agency UI: full-text \"Title V\" plus program OAQ and
document type Permit, sorted by document date descending. Pages until every row
on a page is before --min-year (default 2020), then stops (results are newest-first).

Requires Chrome/Chromium for Selenium (Cloudflare and ECM session cookies for GET_FILE).
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

IDEM_ORIGIN = "https://ecm.idem.in.gov"
SEARCH_BASE = f"{IDEM_ORIGIN}/cs/idcplg"

# Minimal query equivalent to the pre-filled search (avoids duplicated AND clauses in the URL).
QUERY_TEXT = (
    "<ftx>Title V</ftx> <AND> xProgram <Matches> `OAQ` <AND> "
    "xIDEMDocumentType <Matches> `Permit`"
)
QUERY_FILTER = "xProgram <Matches> `OAQ` <AND> xIDEMDocumentType <Matches> `Permit`"


def build_search_url(page: int, page_size: int) -> str:
    params = {
        "IdcService": "GET_SEARCH_RESULTS",
        "QueryText": QUERY_TEXT,
        "SortField": "xDocumentDate",
        "SortOrder": "Desc",
        "ResultCount": str(page_size),
        "SearchQueryFormat": "UNIVERSAL",
        "searchFormType": "standard",
        "listTemplateId": "SearchResultsIDEM",
        "SearchProviders": "WCC_IDEM",
        "ftx": "1",
        "QueryFilterUsed": "false",
        "QueryFilter": QUERY_FILTER,
        "FilterFields": "xProgram,xIDEMDocumentType",
        "PageNumber": str(page),
        "StartRow": str((page - 1) * page_size + 1),
        "EndRow": str(page * page_size),
    }
    return f"{SEARCH_BASE}?{urlencode(params)}"


def absolutize(href: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    return urljoin(IDEM_ORIGIN, href)


def to_primary_pdf_url(href: str) -> str:
    """Prefer native Primary rendition (PDF); listing uses Rendition=web but both serve PDF."""
    u = absolutize(href)
    parts = urlparse(u)
    q = parse_qs(parts.query, keep_blank_values=True)
    q["Rendition"] = ["Primary"]
    new_query = urlencode(q, doseq=True)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", new_query, ""))


_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_us_date(s: str) -> date | None:
    s = (s or "").strip()
    m = _US_DATE.match(s)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_search_rows(html: str) -> list[dict]:
    """
    Each hit is an <a IdcService=GET_FILE>; document date is in the td two columns after
    the content-id cell (spacer column in between), matching the IDEM list template.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for a in soup.select('a[href*="IdcService=GET_FILE"]'):
        href = (a.get("href") or "").strip()
        if "dDocName=" not in href or "dID=" not in href:
            continue
        parent_td = a.find_parent("td")
        if parent_td is None:
            continue
        tr = parent_td.find_parent("tr")
        if tr is None:
            continue
        tds = tr.find_all("td", recursive=False)
        try:
            idx = tds.index(parent_td)
        except ValueError:
            continue

        date_text = ""
        if idx + 2 < len(tds):
            date_text = tds[idx + 2].get_text(strip=True)

        qs = parse_qs(urlparse(absolutize(href)).query)
        d_doc_name = (qs.get("dDocName") or [""])[0]
        d_id = (qs.get("dID") or [""])[0]
        if not d_doc_name or not d_id:
            continue

        key = (d_doc_name, d_id)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "d_doc_name": d_doc_name,
                "d_id": d_id,
                "document_date_text": date_text,
                "document_date": parse_us_date(date_text),
                "file_href": href,
            }
        )
    return rows


def _wait_for_results(driver, timeout: float) -> None:
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, 'a[href*="IdcService=GET_FILE"]')
        )
    )


def download_title_v_permits(
    output_dir: Path,
    *,
    min_year: int,
    page_size: int,
    headless: bool,
    wait_seconds: int,
    sleep_between_pages: float,
    dry_run: bool,
    skip_existing: bool,
    max_pages: int | None,
) -> None:
    cutoff = date(min_year, 1, 1)
    index_path = output_dir / "idem_title_v_index.csv"
    fieldnames = [
        "page",
        "d_doc_name",
        "d_id",
        "document_date",
        "pdf_url",
        "filename",
        "status",
        "message",
    ]

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    try:
        driver = downloader.driver

        page = 1
        downloaded = 0
        skipped_old = 0
        skipped_bad_date = 0
        dry_run_count = 0
        failed = 0

        write_header = not index_path.is_file()
        with index_path.open("a", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            while True:
                if max_pages is not None and page > max_pages:
                    logger.info("Stopping: reached --max-pages {}", max_pages)
                    break

                url = build_search_url(page, page_size)
                logger.info("Loading search page {}", page)
                driver.get(url)
                try:
                    _wait_for_results(driver, timeout=max(25.0, float(wait_seconds) * 5))
                except Exception:
                    logger.warning("Timeout waiting for results on page {}; retrying once", page)
                    time.sleep(3.0)
                    driver.get(url)
                    _wait_for_results(driver, timeout=max(25.0, float(wait_seconds) * 5))

                time.sleep(max(1.0, wait_seconds * 0.25))
                parsed = parse_search_rows(driver.page_source)

                if not parsed:
                    logger.info("No GET_FILE rows on page {}; stopping.", page)
                    break

                dates_parseable = [r["document_date"] for r in parsed if r["document_date"] is not None]
                if (
                    dates_parseable
                    and len(dates_parseable) == len(parsed)
                    and all(d < cutoff for d in dates_parseable)
                ):
                    logger.info(
                        "Page {}: all {} rows have document dates before {}; stopping pagination.",
                        page,
                        len(parsed),
                        cutoff.isoformat(),
                    )
                    for row in parsed:
                        doc_dt = row["document_date"]
                        if doc_dt is None:
                            continue
                        rec = {
                            "page": page,
                            "d_doc_name": row["d_doc_name"],
                            "d_id": row["d_id"],
                            "document_date": doc_dt.isoformat(),
                            "pdf_url": "",
                            "filename": "",
                            "status": "skipped_too_old",
                            "message": f"before {cutoff.isoformat()} (page terminal)",
                        }
                        writer.writerow(rec)
                        csvfile.flush()
                        skipped_old += 1
                    break

                for row in parsed:
                    doc_dt = row["document_date"]
                    if doc_dt is None:
                        rec = {
                            "page": page,
                            "d_doc_name": row["d_doc_name"],
                            "d_id": row["d_id"],
                            "document_date": row["document_date_text"],
                            "pdf_url": "",
                            "filename": "",
                            "status": "skipped_bad_date",
                            "message": row["document_date_text"] or "unparseable",
                        }
                        writer.writerow(rec)
                        csvfile.flush()
                        skipped_bad_date += 1
                        continue

                    if doc_dt < cutoff:
                        rec = {
                            "page": page,
                            "d_doc_name": row["d_doc_name"],
                            "d_id": row["d_id"],
                            "document_date": doc_dt.isoformat(),
                            "pdf_url": "",
                            "filename": "",
                            "status": "skipped_too_old",
                            "message": f"before {cutoff.isoformat()}",
                        }
                        writer.writerow(rec)
                        csvfile.flush()
                        skipped_old += 1
                        continue

                    pdf_url = to_primary_pdf_url(row["file_href"])
                    # ECM Content Id (dDocName); server often uses identical Content-Disposition names.
                    fname = clean_filename(f"{row['d_doc_name']}.pdf")
                    dest = output_dir / fname

                    if skip_existing and dest.is_file():
                        rec = {
                            "page": page,
                            "d_doc_name": row["d_doc_name"],
                            "d_id": row["d_id"],
                            "document_date": doc_dt.isoformat(),
                            "pdf_url": pdf_url,
                            "filename": fname,
                            "status": "skipped_exists",
                            "message": str(dest),
                        }
                        writer.writerow(rec)
                        csvfile.flush()
                        continue

                    if dry_run:
                        rec = {
                            "page": page,
                            "d_doc_name": row["d_doc_name"],
                            "d_id": row["d_id"],
                            "document_date": doc_dt.isoformat(),
                            "pdf_url": pdf_url,
                            "filename": fname,
                            "status": "dry_run",
                            "message": "",
                        }
                        writer.writerow(rec)
                        csvfile.flush()
                        dry_run_count += 1
                        continue

                    ok = downloader.download_document(
                        pdf_url,
                        referer=url,
                        link_text=fname,
                        is_table_link=True,
                        save_as=fname,
                    )
                    rec = {
                        "page": page,
                        "d_doc_name": row["d_doc_name"],
                        "d_id": row["d_id"],
                        "document_date": doc_dt.isoformat(),
                        "pdf_url": pdf_url,
                        "filename": fname,
                        "status": "downloaded" if ok else "download_failed",
                        "message": "",
                    }
                    writer.writerow(rec)
                    csvfile.flush()
                    if ok:
                        downloaded += 1
                    else:
                        failed += 1

                    time.sleep(0.4)

                page += 1
                time.sleep(sleep_between_pages)

        logger.info(
            "Done. downloaded={} dry_run={} skipped_too_old={} skipped_bad_date={} failed={}",
            downloaded,
            dry_run_count,
            skipped_old,
            skipped_bad_date,
            failed,
        )
    finally:
        downloader.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download Indiana IDEM Title V OAQ permit PDFs (2020+ by default)."
    )
    default_out = Path(RAW_DATA_DIR) / "idem_title_v"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_out,
        help=f"Output directory (default: {default_out})",
    )
    parser.add_argument(
        "--min-year",
        type=int,
        default=2020,
        help="Do not download documents with document date before this year (default: 2020).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="Results per page (ResultCount / page window). Default: 20.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap on search pages (for testing).",
    )
    parser.add_argument(
        "--sleep-between-pages",
        type=float,
        default=1.5,
        help="Seconds to wait between search result pages.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Base wait for SeleniumPDFDownloader page rendering.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome visibly (helps if Cloudflare blocks headless).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List rows that would be downloaded without fetching PDFs.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip downloads when the target filename already exists.",
    )

    args = parser.parse_args()
    out = args.output_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.page_size < 1:
        logger.error("--page-size must be >= 1")
        return 1

    download_title_v_permits(
        out,
        min_year=args.min_year,
        page_size=args.page_size,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_between_pages=args.sleep_between_pages,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        max_pages=args.max_pages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
