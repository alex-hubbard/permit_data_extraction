#!/usr/bin/env python3
"""
Download Pinal County, AZ issued Title V permit PDFs.

Workflow:
1) Open the index page of issued Title V permits.
2) Discover facility links on that page.
3) Visit each facility page and find the best Title V permit PDF link.
4) Download each PDF and write an index CSV.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

INDEX_URL = "https://www.pinal.gov/370/Title-V-Permits-Issued"


@dataclass(frozen=True)
class FacilityLink:
    name: str
    url: str


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _wait_for_body(driver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def _is_valid_link(href: str) -> bool:
    lower = href.lower().strip()
    return bool(
        lower
        and not lower.startswith("#")
        and not lower.startswith("javascript:")
        and not lower.startswith("mailto:")
        and not lower.startswith("tel:")
    )


def _is_probable_permit_document_url(url: str) -> bool:
    lower = (url or "").lower()
    if ".pdf" in lower:
        return True
    # Pinal commonly serves PDFs at /DocumentCenter/View/<id> without .pdf suffix.
    if "/documentcenter/view/" in lower:
        return True
    return False


def _is_probable_facility_text(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize(text)
    blocked = {
        "home",
        "our county",
        "offices & departments",
        "offices and departments",
        "air quality",
        "permitting",
        "industrial permits",
        "title v permits issued",
        "site map",
        "accessibility",
        "terms of use policy",
        "public records request",
        "boards and commissions",
    }
    if normalized in blocked:
        return False
    # Most facility names include business-like terms; also reject very short labels.
    return len(normalized) >= 6


def extract_facility_links(page_html: str, base_url: str) -> List[FacilityLink]:
    soup = BeautifulSoup(page_html, "html.parser")
    facilities: List[FacilityLink] = []
    seen_urls: Set[str] = set()

    # Prefer anchors in the main content area first.
    containers = []
    for selector in ["main", "#main-content", ".main-content", "article", ".content", "body"]:
        container = soup.select_one(selector)
        if container is not None:
            containers.append(container)
    if not containers:
        containers = [soup]

    for container in containers:
        for anchor in container.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not _is_valid_link(href):
                continue
            full_url = urljoin(base_url, href)
            netloc = urlparse(full_url).netloc.lower()
            if "pinal.gov" not in netloc:
                continue
            if full_url.lower().endswith(".pdf"):
                continue

            text = anchor.get_text(" ", strip=True)
            if not _is_probable_facility_text(text):
                continue

            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            facilities.append(FacilityLink(name=text, url=full_url))

    return facilities


def _extract_pdf_from_onclick(onclick: str, base_url: str) -> Optional[str]:
    if not onclick:
        return None
    absolute = re.search(r"(https?://[^'\"\s]+\.pdf)", onclick, re.IGNORECASE)
    if absolute:
        return absolute.group(1)
    absolute_doccenter = re.search(
        r"(https?://[^'\"\s]*documentcenter/view/\d+[^'\"\s]*)",
        onclick,
        re.IGNORECASE,
    )
    if absolute_doccenter:
        return absolute_doccenter.group(1)
    relative = re.search(r"['\"]([^'\"]+\.pdf)['\"]", onclick, re.IGNORECASE)
    if relative:
        return urljoin(base_url, relative.group(1))
    relative_doccenter = re.search(r"['\"]([^'\"]*documentcenter/view/\d+[^'\"]*)['\"]", onclick, re.IGNORECASE)
    if relative_doccenter:
        return urljoin(base_url, relative_doccenter.group(1))
    return None


def find_title_v_pdf_link(page_html: str, page_url: str) -> Optional[Tuple[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    candidates: List[Tuple[str, str, int]] = []

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not _is_valid_link(href):
            continue
        full_url = urljoin(page_url, href)
        if not _is_probable_permit_document_url(full_url):
            continue
        text = anchor.get_text(" ", strip=True)
        context = " ".join(
            [
                text,
                anchor.parent.get_text(" ", strip=True) if anchor.parent else "",
            ]
        )
        ctx = _normalize(context)
        score = 0
        if "title v" in ctx or "title-v" in ctx or "title5" in ctx or "title 5" in ctx:
            score += 4
        if "permit" in ctx:
            score += 2
        if "issued" in ctx or "final" in ctx or "operating" in ctx:
            score += 1
        if full_url.lower().endswith(".pdf"):
            score += 1
        if "/documentcenter/view/" in full_url.lower():
            score += 2
        candidates.append((full_url, text or "Title V Permit", score))

    for element in soup.find_all(["a", "button"]):
        onclick = element.get("onclick") or ""
        pdf_url = _extract_pdf_from_onclick(onclick, page_url)
        if not pdf_url:
            continue
        text = element.get_text(" ", strip=True) or "Title V Permit"
        score = 2
        norm_text = _normalize(text)
        if "title v" in norm_text or "title 5" in norm_text:
            score += 3
        if "permit" in norm_text:
            score += 1
        candidates.append((pdf_url, text, score))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[2], reverse=True)
    best = candidates[0]
    return best[0], best[1]


def _next_available_path(output_dir: Path, filename: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    idx = 2
    while True:
        option = output_dir / f"{stem}_{idx}{suffix}"
        if not option.exists():
            return option
        idx += 1


def _write_index_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "facility_name",
        "facility_url",
        "pdf_url",
        "local_path",
        "status",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote index: {path}")


def download_pinal_title_v(
    output_dir: Path,
    headless: bool,
    wait_seconds: int,
    sleep_seconds: float,
    max_facilities: Optional[int],
    skip_existing: bool,
    index_csv: Optional[Path],
) -> None:
    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    downloaded = 0
    skipped = 0
    failed = 0
    index_rows: List[dict] = []
    seen_pdf_urls: Set[str] = set()

    try:
        driver = downloader.driver
        driver.get(INDEX_URL)
        _wait_for_body(driver)
        time.sleep(max(1, wait_seconds))

        facilities = extract_facility_links(driver.page_source, INDEX_URL)
        if not facilities:
            raise RuntimeError("No facility links found on the Pinal County Title V page.")
        if max_facilities is not None:
            facilities = facilities[:max_facilities]

        logger.info(f"Found {len(facilities)} facility page link(s).")

        for idx, facility in enumerate(facilities, start=1):
            logger.info(f"[{idx}/{len(facilities)}] Processing facility: {facility.name}")
            try:
                driver.get(facility.url)
                _wait_for_body(driver)
                time.sleep(max(1, wait_seconds))
            except Exception as exc:
                logger.warning(f"Failed to load facility page {facility.url}: {exc}")
                failed += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": "",
                            "local_path": "",
                            "status": "failed_page_load",
                            "error": str(exc),
                        }
                    )
                continue

            pdf = find_title_v_pdf_link(driver.page_source, facility.url)
            if not pdf:
                logger.warning(f"No Title V permit PDF found for {facility.name}")
                skipped += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": "",
                            "local_path": "",
                            "status": "skipped_no_pdf",
                            "error": "",
                        }
                    )
                continue

            pdf_url, _link_text = pdf
            if pdf_url in seen_pdf_urls:
                logger.info(f"Skipping duplicate PDF URL: {pdf_url}")
                skipped += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": pdf_url,
                            "local_path": "",
                            "status": "skipped_duplicate_pdf",
                            "error": "",
                        }
                    )
                continue
            seen_pdf_urls.add(pdf_url)

            desired_name = clean_filename(f"{facility.name} - Title V Permit")
            if not desired_name.lower().endswith(".pdf"):
                desired_name = f"{desired_name}.pdf"
            preferred_path = output_dir / desired_name
            if skip_existing and preferred_path.exists():
                skipped += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": pdf_url,
                            "local_path": str(preferred_path),
                            "status": "skipped_existing",
                            "error": "",
                        }
                    )
                continue
            target_path = (
                preferred_path
                if not preferred_path.exists()
                else _next_available_path(output_dir, desired_name)
            )

            ok = downloader.download_document(
                pdf_url,
                referer=facility.url,
                link_text=desired_name,
                is_table_link=True,
                save_as=target_path.name,
            )
            if ok:
                downloaded += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": pdf_url,
                            "local_path": str(output_dir / target_path.name),
                            "status": "downloaded",
                            "error": "",
                        }
                    )
            else:
                failed += 1
                if index_csv is not None:
                    index_rows.append(
                        {
                            "facility_name": facility.name,
                            "facility_url": facility.url,
                            "pdf_url": pdf_url,
                            "local_path": "",
                            "status": "failed_download",
                            "error": "",
                        }
                    )

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    finally:
        downloader.close()

    logger.info(
        f"Done. Downloaded: {downloaded}, skipped: {skipped}, failed: {failed}, output: {output_dir}"
    )
    if index_csv is not None:
        _write_index_csv(index_csv, index_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download issued Title V permit PDFs from Pinal County, Arizona."
    )
    default_output = RAW_DATA_DIR / "az_pinal_title_v_issued"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory for downloaded PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Seconds to wait for page rendering.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Pause between facility downloads.",
    )
    parser.add_argument(
        "--max-facilities",
        type=int,
        default=None,
        help="Limit number of facility pages to process.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Allow downloading even when destination file already exists.",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=None,
        help="Optional path to write an index CSV.",
    )

    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    index_csv = args.index_csv.expanduser().resolve() if args.index_csv else None

    download_pinal_title_v(
        output_dir=output_dir,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        max_facilities=args.max_facilities,
        skip_existing=not args.no_skip_existing,
        index_csv=index_csv,
    )


if __name__ == "__main__":
    main()
