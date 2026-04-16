#!/usr/bin/env python3
"""Download Maryland MDE issued Title V permit PDFs with show-all/pagination handling."""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

START_URL = (
    "https://mde.maryland.gov/programs/permits/AirManagementPermits/Pages/"
    "title5_issued_permits.aspx?&&p_SortBehavior=0&"
    "p_FileLeafRef=LafargeSparrowsPt2023IssuedT5FS%2epdf&&PageFirstRow=1&&"
    "View={05B933F6-6A2B-4D2C-84A3-BAEA8C8A5350}"
)

PAGE_51_URL = (
    "https://mde.maryland.gov/programs/permits/AirManagementPermits/Pages/"
    "title5_issued_permits.aspx?Paged=TRUE&p_SortBehavior=0&"
    "p_FileLeafRef=KMCThermo2024IssuedT5FS%2epdf&p_ID=197&PageFirstRow=51&&"
    "View={05B933F6-6A2B-4D2C-84A3-BAEA8C8A5350}"
)

PAGE_101_URL = (
    "https://mde.maryland.gov/programs/permits/AirManagementPermits/Pages/"
    "title5_issued_permits.aspx?Paged=TRUE&p_SortBehavior=0&"
    "p_FileLeafRef=USGypsum2025IssuedT5PermitandFactSheet%2epdf&p_ID=228&PageFirstRow=101&&"
    "View={05B933F6-6A2B-4D2C-84A3-BAEA8C8A5350}"
)


def build_driver(headless: bool) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=options)


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^\w\-. ]+", "_", name).strip()
    return safe[:180] if safe else "document.pdf"


def resolve_direct_pdf_url(url: str) -> str:
    """
    Google Translate proxy wraps sites as subdomain.translate.goog (e.g. mde-maryland-gov).
    Requests to that host often return 408; use the real mde.maryland.gov URL instead.
    """
    if not url or not url.strip():
        return url
    parsed = urlparse(url.strip())
    netloc = (parsed.netloc or "").lower()
    if ".translate.goog" not in netloc:
        return url
    # e.g. mde-maryland-gov.translate.goog -> mde.maryland.gov
    prefix = netloc.split(".translate.goog")[0]
    if not prefix:
        return url
    real_host = ".".join(prefix.split("-"))
    # Drop translate query noise (_x_tr_*)
    return urlunparse(("https", real_host, parsed.path, "", "", ""))


def canonical_pdf_url(url: str) -> str:
    """
    Create a stable key for a PDF URL to prevent duplicates.
    We strip query/fragment and normalize translate.* hosts to mde.maryland.gov.
    """
    resolved = resolve_direct_pdf_url(url)
    parsed = urlparse(resolved)
    # Normalize percent-encoding so duplicates like %20 vs spaces collapse.
    path_norm = quote(unquote(parsed.path or ""), safe="/-_.~%")
    return urlunparse((parsed.scheme or "https", parsed.netloc, path_norm, "", "", ""))


def _find_visible_select(driver: webdriver.Chrome):
    selectors = [
        "select[name$='_length']",
        "select[name*='length']",
        "select[title*='items']",
        "label select",
    ]
    for css in selectors:
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            if el.is_displayed():
                return el
    return None


def try_select_show_all(driver: webdriver.Chrome) -> bool:
    select_el = _find_visible_select(driver)
    if select_el is None:
        return False

    select = Select(select_el)
    all_text = None
    for opt in select.options:
        text = (opt.text or "").strip().lower()
        value = (opt.get_attribute("value") or "").strip().lower()
        if "all" in text or value == "-1":
            all_text = opt.text
            break

    if all_text is None:
        return False

    before = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
    select.select_by_visible_text(all_text)
    WebDriverWait(driver, 15).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) >= before
    )
    time.sleep(1.0)
    return True


def extract_rows(driver: webdriver.Chrome) -> list[dict]:
    soup = BeautifulSoup(driver.page_source, "html.parser")
    rows = []
    for tr in soup.select("table tbody tr, table tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        anchors = tr.find_all("a", href=True)
        pdf_links = []
        for a in anchors:
            href = a["href"].strip()
            if ".pdf" in href.lower():
                joined = urljoin(START_URL, href)
                pdf_links.append(canonical_pdf_url(joined))

        row_text = [c.get_text(" ", strip=True) for c in cells]
        unique_pdf_links = list(dict.fromkeys(pdf_links))
        rows.append(
            {
                "row_text": " | ".join(row_text),
                "primary_label": row_text[0] if row_text else "",
                "pdf_urls": unique_pdf_links,
            }
        )
    return rows


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def extract_pagination_urls(driver: webdriver.Chrome) -> list[str]:
    """
    Extract all available page URLs from SharePoint-style pagination links.
    """
    soup = BeautifulSoup(driver.page_source, "html.parser")
    urls = {_normalize_url(driver.current_url)}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        absolute = _normalize_url(urljoin(driver.current_url, href))
        parsed = urlparse(absolute)
        query = parse_qs(parsed.query)
        if "PageFirstRow" in query:
            urls.add(absolute)
    # Sort by PageFirstRow numeric position for stable traversal
    def page_first_row(u: str) -> int:
        q = parse_qs(urlparse(u).query)
        try:
            return int(q.get("PageFirstRow", ["1"])[0])
        except Exception:
            return 1

    return sorted(urls, key=page_first_row)


def wait_for_listing_content(driver: webdriver.Chrome, timeout: int = 25) -> None:
    """
    Wait until the page has either a table or document links.
    """
    WebDriverWait(driver, timeout).until(
        lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, "table")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "a[href*='.pdf'], a[href*='FileLeafRef=']")) > 0
            or "title5_issued_permits.aspx" in (d.current_url or "")
        )
    )
    time.sleep(1.0)


def collect_all_rows(driver: webdriver.Chrome) -> tuple[list[dict], bool]:
    driver.get(START_URL)
    try:
        wait_for_listing_content(driver, timeout=25)
    except Exception:
        # Retry once for intermittent render/cookie delays.
        driver.get(START_URL)
        wait_for_listing_content(driver, timeout=25)

    used_show_all = try_select_show_all(driver)
    all_rows = []
    seen = set()
    page_urls = [
        _normalize_url(START_URL),
        _normalize_url(PAGE_51_URL),
        _normalize_url(PAGE_101_URL),
    ]

    # Intentionally crawl only the explicit pages requested by the user
    # to avoid missing/duplicate behavior from dynamic pagination controls.

    for page_url in page_urls:
        driver.get(page_url)
        try:
            wait_for_listing_content(driver, timeout=20)
        except Exception:
            # Skip hard failure; still parse whatever rendered.
            time.sleep(1.0)

        for row in extract_rows(driver):
            key = (row["row_text"], tuple(row["pdf_urls"]))
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(row)

    return all_rows, used_show_all


def download_pdf(url: str, output_dir: Path, session: requests.Session, fallback: str) -> str:
    url = resolve_direct_pdf_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
    }
    last_err: Exception | None = None
    response = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=90, allow_redirects=True, headers=headers)
            if r.status_code in (408, 429, 500, 502, 503, 504):
                time.sleep(min(8, 2**attempt))
                continue
            r.raise_for_status()
            response = r
            break
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(min(8, 2**attempt))
    if response is None:
        raise last_err or RuntimeError(f"Failed to download after retries: {url}")

    content_disp = response.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', content_disp, re.IGNORECASE)
    filename = match.group(1).strip() if match else Path(urlparse(url).path).name
    if not filename:
        filename = fallback
    filename = sanitize_filename(filename)
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    path = output_dir / filename
    if path.exists():
        i = 1
        while True:
            candidate = output_dir / f"{path.stem}__dup{i}{path.suffix}"
            if not candidate.exists():
                path = candidate
                break
            i += 1

    path.write_bytes(response.content)
    return str(path)


def expected_stem_for_pdf_url(url: str) -> str:
    """
    Best-effort expected local filename stem for a given canonical URL,
    mirroring download_pdf's naming approach (URL path basename + sanitize).
    """
    parsed = urlparse(url)
    base_name = Path(parsed.path).name
    if not base_name:
        return ""
    expected = sanitize_filename(base_name)
    if not expected.lower().endswith(".pdf"):
        expected = f"{expected}.pdf"
    return Path(expected).stem


def main() -> None:
    parser = argparse.ArgumentParser(description="Download MDE issued Title V permit PDFs.")
    parser.add_argument(
        "--output-dir",
        default="data/raw/md_issued_title_v",
        help="Where PDFs and index CSV are written.",
    )
    parser.add_argument("--headless", action="store_true", default=False, help="Run headless Chrome.")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    driver = build_driver(headless=args.headless)
    try:
        rows, used_show_all = collect_all_rows(driver)
    finally:
        driver.quit()

    if not rows:
        raise RuntimeError("No table rows were collected from the MDE page.")

    session = requests.Session()
    downloads = 0
    out = []
    index_csv = output_dir / "md_issued_title_v_index.csv"

    # Avoid re-downloading PDFs already present in a previous run.
    downloaded_url_set: set[str] = set()
    if index_csv.exists():
        try:
            existing = pd.read_csv(index_csv)
            if "pdf_urls" in existing.columns:
                for cell in existing["pdf_urls"].dropna().astype(str).tolist():
                    for part in cell.split(" || "):
                        part = part.strip()
                        if part:
                            downloaded_url_set.add(canonical_pdf_url(part))
        except Exception:
            downloaded_url_set = set()

    # Precompute existing PDF base stems so file-existence checks are O(1).
    existing_pdf_bases: set[str] = set()
    for f in output_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() != ".pdf":
            continue
        stem = f.stem
        if "__dup" in stem:
            stem = stem.split("__dup", 1)[0]
        existing_pdf_bases.add(stem)

    for row in rows:
        pdf_paths = []
        for idx, pdf_url in enumerate(row["pdf_urls"], start=1):
            canonical = canonical_pdf_url(pdf_url)
            # Only skip if we've recorded it *and* the local file exists.
            expected_stem = expected_stem_for_pdf_url(canonical)
            if canonical in downloaded_url_set and expected_stem and expected_stem in existing_pdf_bases:
                continue
            fallback = f"{sanitize_filename(row['primary_label'])}_{idx}.pdf"
            local_path = download_pdf(canonical, output_dir, session, fallback)
            pdf_paths.append(local_path)
            downloads += 1
            downloaded_url_set.add(canonical)

        out.append(
            {
                "primary_label": row["primary_label"],
                "row_text": row["row_text"],
                "pdf_urls": " || ".join(row["pdf_urls"]),
                "downloaded_files": " || ".join(pdf_paths),
            }
        )

    pd.DataFrame(out).to_csv(index_csv, index=False)

    mode = "show-all mode" if used_show_all else "pagination mode"
    print(f"Collected {len(rows)} table rows using {mode}.")
    print(f"Downloaded {downloads} PDFs to: {output_dir}")
    print(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
