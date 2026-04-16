#!/usr/bin/env python3
"""Download NY DEC issued Title V permit PDFs with pagination-aware table handling."""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = "https://extapps.dec.ny.gov/data/dar/afs/issued_atv.html"


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


def detect_and_select_show_all(driver: webdriver.Chrome) -> bool:
    """Select 'all entries' if table length selector supports it."""
    selectors = [
        "select[name$='_length']",
        "select[name*='length']",
        "label select",
    ]
    select_el = None
    for css in selectors:
        elements = driver.find_elements(By.CSS_SELECTOR, css)
        for element in elements:
            if element.is_displayed():
                select_el = element
                break
        if select_el is not None:
            break

    if select_el is None:
        return False

    selector = Select(select_el)
    all_candidate = None
    for option in selector.options:
        text = (option.text or "").strip().lower()
        value = (option.get_attribute("value") or "").strip().lower()
        if "all" in text or value == "-1":
            all_candidate = option
            break

    if all_candidate is None:
        return False

    before = len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr"))
    selector.select_by_visible_text(all_candidate.text)
    WebDriverWait(driver, 15).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) >= before
    )
    time.sleep(1.0)
    return True


def parse_current_page_rows(driver: webdriver.Chrome) -> list[dict]:
    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    records: list[dict] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        permit_anchor = None
        prr_anchor = None
        for a in tr.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True).lower()
            if "permits/" in href.lower() and "prr" not in href.lower():
                permit_anchor = a
            if "prr" in text or "prr" in href.lower():
                prr_anchor = a

        records.append(
            {
                "issued_date": cells[0].get_text(" ", strip=True) if len(cells) > 0 else "",
                "expiration_date": cells[1].get_text(" ", strip=True) if len(cells) > 1 else "",
                "facility_name": cells[2].get_text(" ", strip=True) if len(cells) > 2 else "",
                "municipality": cells[3].get_text(" ", strip=True) if len(cells) > 3 else "",
                "permit_id": permit_anchor.get_text(" ", strip=True) if permit_anchor else "",
                "permit_url": urljoin(URL, permit_anchor["href"]) if permit_anchor else "",
                "prr_url": urljoin(URL, prr_anchor["href"]) if prr_anchor else "",
            }
        )
    return records


def next_button(driver: webdriver.Chrome):
    candidates = driver.find_elements(By.CSS_SELECTOR, "a.paginate_button.next, a[id$='_next']")
    for btn in candidates:
        cls = (btn.get_attribute("class") or "").lower()
        if "disabled" in cls or "paginate_button_disabled" in cls:
            return None
        if btn.is_displayed():
            return btn
    return None


def collect_records(driver: webdriver.Chrome) -> tuple[list[dict], bool]:
    driver.get(URL)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
    time.sleep(1.0)

    used_show_all = detect_and_select_show_all(driver)
    seen_keys = set()
    records: list[dict] = []

    while True:
        page_rows = parse_current_page_rows(driver)
        for row in page_rows:
            key = (row.get("permit_id"), row.get("facility_name"), row.get("permit_url"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            records.append(row)

        if used_show_all:
            break

        btn = next_button(driver)
        if btn is None:
            break
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.2)

    return records, used_show_all


def download_file(url: str, output_dir: Path, session: requests.Session, fallback_name: str) -> str:
    if not url:
        return ""
    response = session.get(url, timeout=45)
    response.raise_for_status()

    content_disp = response.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', content_disp, re.IGNORECASE)
    filename = match.group(1).strip() if match else Path(urlparse(url).path).name
    if not filename:
        filename = fallback_name
    filename = sanitize_filename(filename)
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"

    destination = output_dir / filename
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        i = 1
        while True:
            candidate = output_dir / f"{stem}__dup{i}{suffix}"
            if not candidate.exists():
                destination = candidate
                break
            i += 1

    destination.write_bytes(response.content)
    return str(destination)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all NY DEC Issued Title V permit PDFs."
    )
    parser.add_argument(
        "--output-dir",
        default="data/raw/ny_issued_title_v",
        help="Directory to save downloaded PDFs and index CSV.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run Chrome in headless mode.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    driver = build_driver(headless=args.headless)
    try:
        records, used_show_all = collect_records(driver)
    finally:
        driver.quit()

    if not records:
        raise RuntimeError("No table records found on NY DEC page.")

    session = requests.Session()
    downloaded = 0
    for row in records:
        permit_hint = f"{row.get('permit_id') or row.get('facility_name') or 'permit'}.pdf"
        prr_hint = f"{row.get('permit_id') or row.get('facility_name') or 'permit'}_prr.pdf"
        row["permit_file"] = download_file(row.get("permit_url", ""), output_dir, session, permit_hint)
        row["prr_file"] = download_file(row.get("prr_url", ""), output_dir, session, prr_hint)
        downloaded += int(bool(row["permit_file"])) + int(bool(row["prr_file"]))

    df = pd.DataFrame(records)
    df.to_csv(output_dir / "ny_issued_title_v_index.csv", index=False)

    mode = "show-all mode" if used_show_all else "pagination mode"
    print(f"Collected {len(records)} records using {mode}.")
    print(f"Downloaded {downloaded} files to: {output_dir}")
    print(f"Index CSV: {output_dir / 'ny_issued_title_v_index.csv'}")


if __name__ == "__main__":
    main()
