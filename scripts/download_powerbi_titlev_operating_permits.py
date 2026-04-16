#!/usr/bin/env python3
"""
Download Title V operating-permit PDFs from a Power BI report.

Flow:
1. Load the provided Power BI report.
2. Select filter value `Title V` for the permit type field.
3. Extract "operating permit" detail URLs from the rendered table/grid.
4. Follow each operating-permit detail page and download the embedded PDF.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename


DEFAULT_REPORT_URL = (
    "https://app.powerbi.com/view?r=eyJrIjoiMDkwN2IxYjItMGZjYi00YmIwLWI3ODgtNDU4MDUxOGUxMGIxIiwidCI6"
    "IjRmOTg2MTliLTIwMmQtNDEzZi04Y2NmLTM2MWQ1NzIxM2JjZCIsImMiOjF9"
)


def _wait_for_report(driver, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    # Power BI often bootstraps asynchronously.
    time.sleep(2.0)


def _iter_iframes(driver) -> Iterable[Optional[object]]:
    """Return (default_content first), then each iframe element (best-effort)."""
    yield None
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        return
    for iframe in iframes:
        yield iframe


def _switch_context(driver, iframe_elem: Optional[object]) -> None:
    driver.switch_to.default_content()
    if iframe_elem is None:
        return
    driver.switch_to.frame(iframe_elem)


def _click_first_visible_title_v(driver) -> bool:
    """
    Best-effort click on a UI element with text 'Title V'.
    Works across many Power BI slicer implementations.
    """
    # Try a few common element types/roles.
    xpaths = [
        # Exact text nodes.
        "//*[normalize-space(.)='Title V']",
        # Case-insensitive match (we'll still match exact by lower).
        "//*[contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'TITLE V')]",
        # Common slicer option wrappers.
        "//*[self::button or self::div or self::span or self::label][contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'TITLE V')]",
    ]
    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except Exception:
            continue
        for el in elements:
            try:
                if not el.is_displayed():
                    continue
                # Clicking can fail if covered; try both click and JS click.
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(1.0)
                return True
            except Exception:
                continue
    return False


def _extract_url_candidates_from_html(html: str, base_url: str) -> Tuple[Set[str], Set[str]]:
    """
    Parse the page HTML and extract operating-permit detail URLs.

    Returns:
      (urls, texts)
    """
    soup = BeautifulSoup(html, "html.parser")

    urls: Set[str] = set()
    texts: Set[str] = set()

    # Collect <a href="..."> candidates.
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href:
            continue
        if href.lower().startswith("javascript:"):
            continue

        resolved = urljoin(base_url, href)
        href_lower = resolved.lower()

        text = a.get_text(" ", strip=True) or ""
        text_lower = text.lower()

        if "powerbi" in href_lower and "operating" not in href_lower:
            continue

        # Primary heuristic: operating-permit detail links mention both "permit" and "operating".
        if ("permit" in href_lower or "permit" in text_lower) and (
            "operating" in href_lower or "operating" in text_lower
        ):
            urls.add(resolved)
            if text:
                texts.add(text)

    # Fallback: parse onclick handlers for URLs containing 'permit'.
    onclick_re = re.compile(r"(https?://[^'\"\\s]+|/[^'\"\\s]+)")
    for el in soup.find_all(attrs={"onclick": True}):
        onclick = str(el.get("onclick") or "")
        if "permit" not in onclick.lower():
            continue
        for match in onclick_re.findall(onclick):
            resolved = urljoin(base_url, match)
            if "permit" in resolved.lower():
                if "operating" in onclick.lower() or "operating" in resolved.lower():
                    urls.add(resolved)

    return urls, texts


def _extract_operating_permit_urls(driver, base_url: str) -> Tuple[Set[str], Set[str]]:
    return _extract_url_candidates_from_html(driver.page_source, base_url=base_url)


def _scroll_and_collect(
    driver,
    base_url: str,
    max_scrolls: int,
    min_new_to_continue: int = 1,
) -> Tuple[Set[str], Set[str]]:
    """
    Scroll until we stop discovering new operating permit URLs.
    Power BI grids are often virtualized; this best-effort strategy works when URLs exist in DOM.
    """
    seen_urls: Set[str] = set()
    seen_texts: Set[str] = set()
    consecutive_no_growth = 0

    for _ in range(max_scrolls):
        urls, texts = _extract_operating_permit_urls(driver, base_url=base_url)
        new = urls - seen_urls
        if len(new) >= min_new_to_continue:
            seen_urls |= urls
            seen_texts |= texts
            consecutive_no_growth = 0
        else:
            consecutive_no_growth += 1

        if consecutive_no_growth >= 2 and seen_urls:
            break

        # Scroll attempts; some reports use nested scroll containers so multiple strategies help.
        try:
            driver.execute_script("window.scrollBy(0, window.innerHeight * 0.9);")
        except Exception:
            pass
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.END)
        except Exception:
            pass
        time.sleep(1.2)

    return seen_urls, seen_texts


def _choose_identifier(texts: Iterable[str], default: str) -> str:
    # Keep it short and filesystem-safe.
    for t in texts:
        t = (t or "").strip()
        if t and len(t) <= 200:
            safe = clean_filename(t)
            return safe[:180] if safe else default
    return default


def download_powerbi_titlev_operating_permits(
    report_url: str,
    output_dir: Path,
    index_csv_path: Path,
    headless: bool,
    wait_seconds: int,
    max_permits: Optional[int],
    max_scrolls: int,
    resume: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resume support: load operating permit URLs from existing index.
    existing_urls: Set[str] = set()
    if resume and index_csv_path.exists():
        try:
            import pandas as pd

            df = pd.read_csv(index_csv_path)
            if "operating_permit_url" in df.columns:
                existing_urls = set(x for x in df["operating_permit_url"].dropna().astype(str).tolist() if x)
        except Exception:
            existing_urls = set()

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,  # we provide "operating permit detail" URLs directly
        use_llm=False,
    )

    try:
        driver = downloader.driver
        driver.get(report_url)
        _wait_for_report(driver, timeout=max(30, wait_seconds * 6))

        # Apply Title V filter and extract links.
        chosen_urls: Set[str] = set()
        chosen_texts: Set[str] = set()

        for iframe in _iter_iframes(driver):
            _switch_context(driver, iframe)
            try:
                if not _click_first_visible_title_v(driver):
                    continue
            except Exception:
                continue

            # Allow report to refresh.
            time.sleep(max(2.0, wait_seconds))

            urls, texts = _scroll_and_collect(
                driver,
                base_url=driver.current_url or report_url,
                max_scrolls=max_scrolls,
            )
            if urls:
                chosen_urls = urls
                chosen_texts = texts
                break

        if not chosen_urls:
            # Last attempt: extract without forcing the filter (useful if filter is pre-selected).
            driver.switch_to.default_content()
            urls, texts = _scroll_and_collect(
                driver,
                base_url=driver.current_url or report_url,
                max_scrolls=min(10, max_scrolls),
            )
            chosen_urls = urls
            chosen_texts = texts

        if not chosen_urls:
            debug_path = output_dir / "debug_powerbi_no_operating_permit_links.html"
            debug_path.write_text(driver.page_source, encoding="utf-8")
            raise RuntimeError(
                "No operating-permit links were found after attempting to select 'Title V'. "
                f"Saved debug HTML to: {debug_path}"
            )

        # De-duplicate and optionally cap.
        sorted_urls = sorted(chosen_urls)
        if max_permits is not None:
            sorted_urls = sorted_urls[:max_permits]

        to_process = [u for u in sorted_urls if u not in existing_urls]
        if not to_process:
            print(f"All {len(sorted_urls)} permits already present in {index_csv_path}. Nothing to download.")
            return

        # Initialize/append index.
        import pandas as pd

        index_rows = []
        if resume and index_csv_path.exists():
            try:
                df_existing = pd.read_csv(index_csv_path)
                if not df_existing.empty:
                    index_rows = df_existing.to_dict(orient="records")
            except Exception:
                index_rows = []

        # Download loop.
        downloaded = 0
        skipped = 0

        for idx, operating_url in enumerate(to_process, start=1):
            print(f"[{idx}/{len(to_process)}] Downloading operating permit: {operating_url}")

            # Try to generate a short filename hint.
            filename_hint = _choose_identifier(chosen_texts, default="louisville_ky_titlev_operating_permit")

            try:
                success = downloader.download_document(
                    operating_url,
                    referer=report_url,
                    link_text=filename_hint,
                    is_table_link=True,
                )
            except Exception as exc:
                success = False
                err = str(exc)
            else:
                err = ""

            row = {
                "permit_type": "Title V",
                "operating_permit_url": operating_url,
                "operating_permit_text_hint": filename_hint,
                "download_success": bool(success),
                "error": err,
            }
            if resume and index_rows:
                # Avoid duplicates by URL.
                if any(r.get("operating_permit_url") == operating_url for r in index_rows):
                    index_rows = [r for r in index_rows if r.get("operating_permit_url") != operating_url]
                    index_rows.append(row)
                else:
                    index_rows.append(row)
            else:
                index_rows.append(row)

            if success:
                downloaded += 1
            else:
                skipped += 1

            time.sleep(1.2)

        # Write final index.
        df_out = pd.DataFrame(index_rows)
        df_out.to_csv(index_csv_path, index=False)

        print(f"Download complete. Downloaded={downloaded}, Skipped/failed={skipped}.")
        print(f"Index CSV: {index_csv_path}")
    finally:
        downloader.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Title V operating-permit PDFs from a Power BI report."
    )
    parser.add_argument(
        "--report-url",
        default=DEFAULT_REPORT_URL,
        help="Power BI report view URL.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RAW_DATA_DIR / "louisville_ky_titleV",
        help="Directory to store downloaded permits and index CSV.",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=None,
        help="Optional override for index CSV path.",
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
        help="Seconds to wait for report rendering/filter refresh.",
    )
    parser.add_argument(
        "--max-permits",
        type=int,
        default=None,
        help="Safety cap on number of operating permits to download.",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=30,
        help="How many scroll iterations to use while discovering permits.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume using existing index CSV (skips URLs already listed).",
    )
    args = parser.parse_args()

    output_dir: Path = args.output_dir.expanduser()
    index_csv_path = args.index_csv.expanduser() if args.index_csv else output_dir / "title_v_operating_permit_links.csv"

    download_powerbi_titlev_operating_permits(
        report_url=args.report_url,
        output_dir=output_dir,
        index_csv_path=index_csv_path,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        max_permits=args.max_permits,
        max_scrolls=args.max_scrolls,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()

