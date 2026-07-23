#!/usr/bin/env python3
"""
Download the latest Minnesota MPCA air permit PDF for each facility listed in
What's in My Neighborhood (WIMN):
https://webapp.pca.state.mn.us/wimn/list?activityTypeCode=AQ
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

LIST_URL = "https://webapp.pca.state.mn.us/wimn/list?activityTypeCode=AQ"
SITE_ID_RE = re.compile(r"/site/(\d+)")
PERMIT_WORD_RE = re.compile(r"\bpermit\b", re.IGNORECASE)
LOCALHOST_CONNECTION_REFUSED_RE = re.compile(
    r"(localhost|127\.0\.0\.1).*(connection refused|failed to establish a new connection)",
    re.IGNORECASE,
)


def _is_webdriver_down(exc: Exception) -> bool:
    msg = str(exc or "")
    if not msg:
        return False
    msg_l = msg.lower()
    return bool(LOCALHOST_CONNECTION_REFUSED_RE.search(msg)) or (
        "max retries exceeded" in msg_l and "localhost" in msg_l
    )


def parse_site_id(url: str) -> str:
    match = SITE_ID_RE.search(url)
    return match.group(1) if match else ""


def parse_date(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _resolve_site_url(href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href:
        return None
    if href.startswith("http"):
        candidate = href
    elif href.startswith("/"):
        candidate = urljoin("https://webapp.pca.state.mn.us", href)
    else:
        candidate = urljoin("https://webapp.pca.state.mn.us/wimn/", href)

    if "/site/" not in candidate or candidate.endswith("/documents"):
        return None
    if parse_site_id(candidate):
        return candidate
    return None


def _wait_for_facilities(driver, timeout: int) -> None:
    wait = WebDriverWait(driver, timeout)
    wait.until(
        lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, "a[href*='site/']")) > 0
            or "Download all" in (d.page_source or "")
        )
    )
    time.sleep(0.8)


def collect_facility_urls(driver, timeout: int = 40, max_pages: Optional[int] = None) -> Dict[str, str]:
    driver.get(LIST_URL)
    _wait_for_facilities(driver, timeout=timeout)

    site_map: Dict[str, str] = {}
    page = 1
    seen_page_signatures = set()

    while True:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        links_found = 0
        for anchor in soup.find_all("a", href=True):
            resolved = _resolve_site_url(anchor.get("href", ""))
            if not resolved:
                continue
            site_id = parse_site_id(resolved)
            if not site_id:
                continue
            name = anchor.get_text(" ", strip=True) or f"site_{site_id}"
            site_map[resolved] = name
            links_found += 1

        first_key = sorted(site_map.keys())[0] if site_map else ""
        signature = (page, links_found, first_key, len(site_map))
        if signature in seen_page_signatures:
            break
        seen_page_signatures.add(signature)

        logger.info(f"Collected {len(site_map)} facilities after page {page}.")

        if max_pages is not None and page >= max_pages:
            break

        next_clicked = False
        next_selectors = [
            "a[aria-label*='Next page']",
            "li.page-item.next a",
            "a[rel='next']",
            ".pagination a[aria-label*='Next']",
        ]
        for css in next_selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, css)
            for elem in elements:
                if not elem.is_displayed():
                    continue
                parent_class = (elem.find_element(By.XPATH, "..").get_attribute("class") or "").lower()
                if "disabled" in parent_class:
                    continue
                try:
                    driver.execute_script("arguments[0].click();", elem)
                    next_clicked = True
                    break
                except Exception:
                    continue
            if next_clicked:
                break

        if not next_clicked:
            # Fallback by visible text.
            for elem in driver.find_elements(By.XPATH, "//a[contains(normalize-space(.), 'Next')]"):
                if not elem.is_displayed():
                    continue
                classes = (elem.get_attribute("class") or "").lower()
                if "disabled" in classes:
                    continue
                try:
                    driver.execute_script("arguments[0].click();", elem)
                    next_clicked = True
                    break
                except Exception:
                    continue

        if not next_clicked:
            break

        page += 1
        time.sleep(1.0)
        _wait_for_facilities(driver, timeout=timeout)

    return site_map


def _open_documents_tab(driver, site_url: str, timeout: int) -> None:
    driver.get(site_url)
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(0.5)

    clicked = False
    tab_xpaths = [
        "//*[@role='tab' and contains(normalize-space(.), 'Documents')]",
        "//a[contains(normalize-space(.), 'Documents')]",
        "//button[contains(normalize-space(.), 'Documents')]",
    ]
    for xpath in tab_xpaths:
        for elem in driver.find_elements(By.XPATH, xpath):
            if not elem.is_displayed():
                continue
            try:
                driver.execute_script("arguments[0].click();", elem)
                clicked = True
                break
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        driver.get(site_url.rstrip("/") + "/documents")

    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(1.0)


def _extract_latest_permit_pdf(page_html: str, base_url: str) -> Optional[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    rows: List[dict] = []

    for row in soup.find_all("tr"):
        link = row.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "").strip()
        title = link.get_text(" ", strip=True)
        href_lower = href.lower()
        if (
            ".pdf" not in href_lower
            and ".pdf" not in title.lower()
            and "documentid=" not in href_lower
            and "/document?" not in href_lower
        ):
            continue
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        row_text = " ".join(cells)
        date_text = cells[1] if len(cells) > 1 else ""
        resolved_url = urljoin(base_url, href)
        # Some rows use malformed protocol prefixes like "https:////services..."
        resolved_url = re.sub(r"^https?:/{3,}", "https://", resolved_url, flags=re.IGNORECASE)
        if resolved_url.startswith("https://webapp.pca.state.mn.us//services."):
            resolved_url = resolved_url.replace("https://webapp.pca.state.mn.us//", "https://", 1)

        rows.append(
            {
                "href_raw": href,
                "url": resolved_url,
                "title": title,
                "date_text": date_text,
                "date": parse_date(date_text),
                "is_permit": bool(PERMIT_WORD_RE.search(f"{title} {row_text}")),
            }
        )

    if not rows:
        return None

    permit_rows = [r for r in rows if r["is_permit"]]
    candidates = permit_rows if permit_rows else rows
    candidates.sort(
        key=lambda r: (
            r["date"] is not None,
            r["date"] or datetime.min,
            r["title"],
        ),
        reverse=True,
    )
    return candidates[0]


def _download_mn_document_by_click(
    downloader: SeleniumPDFDownloader,
    driver,
    document_url: str,
    filename: str,
    timeout: int = 60,
) -> bool:
    document_id_match = re.search(r"documentId=(\d+)", document_url)
    if not document_id_match:
        return False
    document_id = document_id_match.group(1)
    try:
        link = driver.find_element(By.CSS_SELECTOR, f"a[href*='documentId={document_id}']")
    except Exception:
        return False

    try:
        downloader._clear_temp_downloads()
        driver.execute_script("arguments[0].click();", link)
        downloaded_path = downloader._wait_for_download(timeout=timeout)
        if not downloaded_path or not downloaded_path.exists():
            return False
        final_name = clean_filename(filename.strip()) or f"{document_id}.pdf"
        if not final_name.lower().endswith(".pdf"):
            final_name = f"{final_name}.pdf"
        final_path = downloader.output_dir / final_name
        downloaded_path.replace(final_path)
        logger.info(f"Downloaded via browser click: {final_path.name}")
        return True
    except Exception as exc:
        logger.warning(f"Browser-click download failed for documentId={document_id}: {exc}")
        return False


def download_mn_permits(
    output_dir: Path,
    headless: bool,
    wait_seconds: int,
    sleep_seconds: float,
    limit: Optional[int],
    skip_existing: bool,
    index_csv: Optional[Path],
    max_pages: Optional[int],
    max_driver_restarts: int,
    restart_browser_every: Optional[int],
) -> None:
    driver_restarts = 0
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

    try:
        driver = downloader.driver
        site_map = collect_facility_urls(driver, timeout=max(25, wait_seconds * 8), max_pages=max_pages)
        site_items = sorted(site_map.items(), key=lambda item: item[1].lower())
        logger.info(f"Discovered {len(site_items)} facilities with air activity entries.")

        if limit is not None:
            site_items = site_items[:limit]

        for idx, (site_url, facility_name) in enumerate(site_items, start=1):
            site_id = parse_site_id(site_url) or f"idx_{idx}"
            logger.info(f"[{idx}/{len(site_items)}] {facility_name} ({site_id})")

            try:
                try:
                    _open_documents_tab(driver, site_url=site_url, timeout=max(20, wait_seconds * 6))
                except Exception as exc:
                    if _is_webdriver_down(exc) and driver_restarts < max_driver_restarts:
                        driver_restarts += 1
                        logger.warning(
                            f"Selenium driver appears down while opening documents for site {site_id}; "
                            f"restarting driver (restart {driver_restarts}/{max_driver_restarts}). Error: {exc}"
                        )
                        try:
                            downloader.close()
                        except Exception:
                            pass
                        downloader = SeleniumPDFDownloader(
                            output_dir=output_dir,
                            headless=headless,
                            wait_seconds=wait_seconds,
                            max_depth=0,
                            use_llm=False,
                        )
                        driver = downloader.driver
                        try:
                            _open_documents_tab(
                                driver,
                                site_url=site_url,
                                timeout=max(20, wait_seconds * 6),
                            )
                        except Exception as exc2:
                            logger.warning(f"Retry after driver restart also failed for site {site_id}: {exc2}")
                            failed += 1
                            index_rows.append(
                                {
                                    "site_id": site_id,
                                    "facility_name": facility_name,
                                    "site_url": site_url,
                                    "documents_url": site_url.rstrip("/") + "/documents",
                                    "permit_pdf_url": "",
                                    "permit_date": "",
                                    "status": "failed_open_documents_after_restart",
                                    "local_path": "",
                                }
                            )
                            continue
                    else:
                        failed += 1
                        logger.warning(f"Failed to open documents tab for site {site_id}: {exc}")
                        index_rows.append(
                            {
                                "site_id": site_id,
                                "facility_name": facility_name,
                                "site_url": site_url,
                                "documents_url": site_url.rstrip("/") + "/documents",
                                "permit_pdf_url": "",
                                "permit_date": "",
                                "status": "failed_open_documents",
                                "local_path": "",
                            }
                        )
                        continue

                try:
                    latest = _extract_latest_permit_pdf(
                        driver.page_source,
                        base_url=driver.current_url or site_url,
                    )
                except Exception as exc:
                    if _is_webdriver_down(exc) and driver_restarts < max_driver_restarts:
                        driver_restarts += 1
                        logger.warning(
                            f"Selenium driver appears down while parsing documents for site {site_id}; "
                            f"restarting driver (restart {driver_restarts}/{max_driver_restarts}). Error: {exc}"
                        )
                        try:
                            downloader.close()
                        except Exception:
                            pass
                        downloader = SeleniumPDFDownloader(
                            output_dir=output_dir,
                            headless=headless,
                            wait_seconds=wait_seconds,
                            max_depth=0,
                            use_llm=False,
                        )
                        driver = downloader.driver
                        try:
                            _open_documents_tab(
                                driver,
                                site_url=site_url,
                                timeout=max(20, wait_seconds * 6),
                            )
                            latest = _extract_latest_permit_pdf(
                                driver.page_source,
                                base_url=driver.current_url or site_url,
                            )
                        except Exception as exc2:
                            failed += 1
                            logger.warning(f"Retry after driver restart failed for site {site_id}: {exc2}")
                            index_rows.append(
                                {
                                    "site_id": site_id,
                                    "facility_name": facility_name,
                                    "site_url": site_url,
                                    "documents_url": site_url.rstrip("/") + "/documents",
                                    "permit_pdf_url": "",
                                    "permit_date": "",
                                    "status": "failed_parse_documents_after_restart",
                                    "local_path": "",
                                }
                            )
                            continue
                    else:
                        failed += 1
                        logger.warning(f"Failed to parse documents for site {site_id}: {exc}")
                        index_rows.append(
                            {
                                "site_id": site_id,
                                "facility_name": facility_name,
                                "site_url": site_url,
                                "documents_url": site_url.rstrip("/") + "/documents",
                                "permit_pdf_url": "",
                                "permit_date": "",
                                "status": "failed_parse_documents",
                                "local_path": "",
                            }
                        )
                        continue
                if not latest:
                    skipped += 1
                    index_rows.append(
                        {
                            "site_id": site_id,
                            "facility_name": facility_name,
                            "site_url": site_url,
                            "documents_url": driver.current_url or (site_url.rstrip("/") + "/documents"),
                            "permit_pdf_url": "",
                            "permit_date": "",
                            "status": "no_pdf_rows",
                            "local_path": "",
                        }
                    )
                    continue

                filename = clean_filename(f"{site_id}_{facility_name}_{latest['title']}").strip() or f"{site_id}_permit"
                if not filename.lower().endswith(".pdf"):
                    filename += ".pdf"
                dest = output_dir / filename

                if skip_existing and dest.exists():
                    skipped += 1
                    status = "skipped_existing"
                    logger.info(f"Skip existing: {dest.name}")
                    index_rows.append(
                        {
                            "site_id": site_id,
                            "facility_name": facility_name,
                            "site_url": site_url,
                            "documents_url": driver.current_url or (site_url.rstrip("/") + "/documents"),
                            "permit_pdf_url": latest["url"],
                            "permit_date": latest["date_text"],
                            "status": status,
                            "local_path": str(dest),
                        }
                    )
                    continue

                ok = False
                if "documentId=" in latest["url"]:
                    ok = _download_mn_document_by_click(
                        downloader=downloader,
                        driver=driver,
                        document_url=latest["url"],
                        filename=filename,
                        timeout=max(40, wait_seconds * 12),
                    )

                if not ok:
                    ok = downloader.download_document(
                        latest["url"],
                        referer=driver.current_url or site_url,
                        link_text=filename,
                        is_table_link=True,
                        save_as=filename,
                    )
                if ok:
                    downloaded += 1
                    status = "downloaded"
                    local_path = str(dest)
                else:
                    failed += 1
                    status = "failed_download"
                    local_path = ""

                index_rows.append(
                    {
                        "site_id": site_id,
                        "facility_name": facility_name,
                        "site_url": site_url,
                        "documents_url": driver.current_url or (site_url.rstrip("/") + "/documents"),
                        "permit_pdf_url": latest["url"],
                        "permit_date": latest["date_text"],
                        "status": status,
                        "local_path": local_path,
                    }
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            finally:
                if (
                    restart_browser_every is not None
                    and restart_browser_every > 0
                    and idx % restart_browser_every == 0
                    and idx < len(site_items)
                ):
                    logger.info(
                        f"Periodic browser restart after {idx} facilities "
                        f"(restart-browser-every={restart_browser_every})."
                    )
                    try:
                        downloader.close()
                    except Exception:
                        pass
                    downloader = SeleniumPDFDownloader(
                        output_dir=output_dir,
                        headless=headless,
                        wait_seconds=wait_seconds,
                        max_depth=0,
                        use_llm=False,
                    )
                    driver = downloader.driver
    finally:
        downloader.close()

    if index_csv is not None:
        index_csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "site_id",
            "facility_name",
            "site_url",
            "documents_url",
            "permit_pdf_url",
            "permit_date",
            "status",
            "local_path",
        ]
        with index_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(index_rows)
        logger.info(f"Wrote index CSV: {index_csv}")

    logger.info("=" * 60)
    logger.info(
        f"Minnesota permits complete. Downloaded={downloaded}, skipped={skipped}, "
        f"failed={failed}, output={output_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download latest Minnesota MPCA air permit PDF for each listed facility."
    )
    default_output = RAW_DATA_DIR / "mn_pca_air_latest_permits"
    default_index = default_output / "mn_pca_air_latest_permits_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloaded permit PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"Path for download index CSV (default: {default_index}).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode (may miss rows if blocked by anti-bot checks).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=4,
        help="Base wait time used for Selenium waits.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.4,
        help="Pause between facility downloads.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of facilities to process.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Download even when output file already exists.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional safety cap on listing pages to crawl while collecting facilities.",
    )
    parser.add_argument(
        "--max-driver-restarts",
        type=int,
        default=3,
        help="How many times to recreate the Selenium driver if it crashes/connection-refuses.",
    )
    parser.add_argument(
        "--restart-browser-every",
        type=int,
        default=None,
        metavar="N",
        help="If set, close and recreate the browser after every N facilities (reduces memory creep).",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    index_csv = args.index_csv.expanduser().resolve() if args.index_csv else None

    download_mn_permits(
        output_dir=output_dir,
        headless=args.headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        limit=args.limit,
        skip_existing=not args.no_skip_existing,
        index_csv=index_csv,
        max_pages=args.max_pages,
        max_driver_restarts=args.max_driver_restarts,
        restart_browser_every=args.restart_browser_every,
    )


if __name__ == "__main__":
    main()
