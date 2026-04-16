#!/usr/bin/env python3
"""
Download Missouri DNR operating permit PDFs from:
https://dnr.mo.gov/air/business-industry/permits/issued

The script walks all list pages, keeps only rows where Permit Type starts with
"Operating Permit", opens each permit detail page, extracts the permit PDF URL,
and downloads the PDF files.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

LIST_URL = "https://dnr.mo.gov/air/business-industry/permits/issued"
OPERATING_PREFIX = "operating permit"

ABS_PDF_RE = re.compile(r"https?://dnr\.mo\.gov[^\s\"'<>]+\.pdf", flags=re.IGNORECASE)
REL_PDF_RE = re.compile(r"/sites/[^\s\"'<>]+\.pdf", flags=re.IGNORECASE)


def _extract_max_page(soup: BeautifulSoup) -> int:
    pages = [0]
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        full = urljoin(LIST_URL, href)
        qs = parse_qs(urlparse(full).query)
        vals = qs.get("page") or []
        if not vals:
            continue
        try:
            pages.append(int(vals[0]))
        except ValueError:
            continue
    return max(pages)


def _parse_operating_rows(soup: BeautifulSoup) -> List[Dict[str, str]]:
    table = soup.find("table")
    if table is None:
        return []

    out: List[Dict[str, str]] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue

        facility_link = tds[0].find("a", href=True)
        if facility_link is None:
            continue
        detail_url = urljoin(LIST_URL, (facility_link.get("href") or "").strip())
        if not detail_url:
            continue

        permit_type = tds[3].get_text(" ", strip=True)
        if not permit_type.lower().startswith(OPERATING_PREFIX):
            continue

        out.append(
            {
                "facility": facility_link.get_text(" ", strip=True),
                "detail_url": detail_url,
                "site_id": tds[1].get_text(" ", strip=True),
                "city_county": tds[2].get_text(" ", strip=True),
                "permit_type": permit_type,
                "permit_number": tds[4].get_text(" ", strip=True),
                "date_issued": tds[5].get_text(" ", strip=True),
            }
        )
    return out


def _extract_pdf_candidates(detail_html: str, detail_url: str) -> List[str]:
    text = html.unescape(detail_html)
    candidates: List[str] = []
    seen = set()

    for match in ABS_PDF_RE.findall(text):
        url = match.strip()
        if url not in seen:
            seen.add(url)
            candidates.append(url)

    for match in REL_PDF_RE.findall(text):
        url = urljoin(detail_url, match.strip())
        if url not in seen:
            seen.add(url)
            candidates.append(url)

    return candidates


def _choose_pdf_url(candidates: List[str], permit_number: str) -> Optional[str]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    permit_norm = re.sub(r"[^a-z0-9]", "", (permit_number or "").lower())
    scored: List[tuple[int, str]] = []
    for url in candidates:
        low = url.lower()
        score = 0
        if permit_norm and permit_norm in re.sub(r"[^a-z0-9]", "", low):
            score += 100
        if "operating" in low or "/op" in low:
            score += 10
        scored.append((score, url))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _download_pdf(session: requests.Session, pdf_url: str, dest: Path, timeout: int) -> bool:
    with session.get(pdf_url, timeout=timeout, stream=True) as resp:
        if resp.status_code != 200:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    handle.write(chunk)
    if not dest.exists() or dest.stat().st_size == 0:
        return False
    return True


def crawl_and_download(
    output_dir: Path,
    index_csv: Path,
    sleep_seconds: float,
    timeout: int,
    max_pages: Optional[int],
    limit: Optional[int],
    skip_existing: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, str]] = []
    seen_detail_urls = set()

    downloaded = 0
    skipped = 0
    failed = 0
    seen_operating_rows = 0

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )

    first_resp = session.get(LIST_URL, timeout=timeout)
    first_resp.raise_for_status()
    first_soup = BeautifulSoup(first_resp.text, "html.parser")
    discovered_max_page = _extract_max_page(first_soup)
    end_page = discovered_max_page if max_pages is None else min(discovered_max_page, max_pages - 1)

    logger.info(f"Missouri issued permit pages discovered: 0..{discovered_max_page}")
    logger.info(f"Crawling pages: 0..{end_page}")

    for page in range(0, end_page + 1):
        page_url = LIST_URL if page == 0 else f"{LIST_URL}?page={page}"
        try:
            resp = session.get(page_url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            failed += 1
            logger.warning(f"Failed to fetch page {page}: {exc}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        operating_rows = _parse_operating_rows(soup)
        logger.info(f"Page {page}: found {len(operating_rows)} operating permit rows.")

        for item in operating_rows:
            detail_url = item["detail_url"]
            if detail_url in seen_detail_urls:
                continue
            seen_detail_urls.add(detail_url)
            seen_operating_rows += 1

            permit_number = item["permit_number"] or f"page{page}_{seen_operating_rows}"
            facility = item["facility"] or "facility"
            filename = clean_filename(f"{permit_number}_{facility}").strip() or permit_number
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            dest = output_dir / filename

            if skip_existing and dest.exists():
                skipped += 1
                rows.append(
                    {
                        **item,
                        "page": str(page),
                        "pdf_url": "",
                        "status": "skipped_existing",
                        "local_path": str(dest),
                    }
                )
                continue

            try:
                detail_resp = session.get(detail_url, timeout=timeout)
                detail_resp.raise_for_status()
            except Exception as exc:
                failed += 1
                rows.append(
                    {
                        **item,
                        "page": str(page),
                        "pdf_url": "",
                        "status": f"failed_detail_fetch: {exc}",
                        "local_path": "",
                    }
                )
                continue

            candidates = _extract_pdf_candidates(detail_resp.text, detail_url=detail_url)
            pdf_url = _choose_pdf_url(candidates, permit_number=permit_number)
            if not pdf_url:
                failed += 1
                rows.append(
                    {
                        **item,
                        "page": str(page),
                        "pdf_url": "",
                        "status": "no_pdf_found_on_detail",
                        "local_path": "",
                    }
                )
                continue

            ok = False
            try:
                ok = _download_pdf(session, pdf_url=pdf_url, dest=dest, timeout=timeout)
            except Exception as exc:
                logger.warning(f"Download failed for {permit_number}: {exc}")

            if ok:
                downloaded += 1
                status = "downloaded"
                local_path = str(dest)
            else:
                failed += 1
                status = "failed_download"
                local_path = ""

            rows.append(
                {
                    **item,
                    "page": str(page),
                    "pdf_url": pdf_url,
                    "status": status,
                    "local_path": local_path,
                }
            )

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            if limit is not None and downloaded >= limit:
                logger.info(f"Reached --limit={limit}.")
                break

        if limit is not None and downloaded >= limit:
            break

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "page",
        "facility",
        "site_id",
        "city_county",
        "permit_type",
        "permit_number",
        "date_issued",
        "detail_url",
        "pdf_url",
        "status",
        "local_path",
    ]
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("=" * 60)
    logger.info("MISSOURI OPERATING PERMIT DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Operating rows seen: {seen_operating_rows}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped existing: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download Missouri DNR operating permit PDFs from the issued permits listing."
        )
    )
    default_output = RAW_DATA_DIR / "mo_dnr_operating_permits"
    default_index = default_output / "mo_dnr_operating_permits_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for downloaded PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"Index CSV path (default: {default_index}).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.1,
        help="Pause between downloads.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="HTTP timeout seconds.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional max number of listing pages to crawl (starting from page 0).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of PDFs to download.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download files even if destination filename already exists.",
    )
    args = parser.parse_args()

    crawl_and_download(
        output_dir=args.output_dir.expanduser().resolve(),
        index_csv=args.index_csv.expanduser().resolve(),
        sleep_seconds=args.sleep_seconds,
        timeout=args.timeout,
        max_pages=args.max_pages,
        limit=args.limit,
        skip_existing=not args.no_skip_existing,
    )


if __name__ == "__main__":
    main()
