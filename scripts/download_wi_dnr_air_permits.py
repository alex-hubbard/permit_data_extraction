#!/usr/bin/env python3
"""
Download Wisconsin DNR Air Management permit PDFs from:
https://apps.dnr.wi.gov/warp_ext/AM_PermitTrackingSearch.aspx?id=0

The script submits the public search form, crawls result pages, and downloads
PDF links discovered on result/detail pages.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

SEARCH_URL = "https://apps.dnr.wi.gov/warp_ext/AM_PermitTrackingSearch.aspx?id=0"
DEFAULT_MILESTONE = "Permit Authority Issues Permit"
FACILITY_PERMITS_TAB_TEXT = "Permits and Permit Applications"


def _permit_tab_panel_is_visible(driver) -> bool:
    """True when tabber has shown the #permitTab panel (gvPermits lives here)."""
    try:
        el = driver.find_element(By.ID, "permitTab")
        cls = el.get_attribute("class") or ""
        return "tabbertabhide" not in cls
    except Exception:
        return False


def _switch_to_facility_permits_tab(driver) -> bool:
    """
    Wisconsin facility pages (e.g. AM_PermitTracking2.aspx) use tabber.js (#myTab).
    The Permits grid is inside #permitTab, hidden until that tab is activated.
    Prefer title-based nav links, then tabber.tabShow(3) as used by the site itself.
    """
    # Fast path: tabber API (see activetabs.js — div#myTab gets a .tabber handle).
    try:
        if driver.execute_script(
            """
            var root = document.getElementById('myTab');
            if (!root || !root.tabber || typeof root.tabber.tabShow !== 'function') return false;
            var tabs = root.tabber.tabs || [];
            var i;
            for (i = 0; i < tabs.length; i++) {
              var ht = String(tabs[i].headingText || '').toLowerCase();
              if (ht.indexOf('permits') !== -1 && ht.indexOf('permit') !== -1 && ht.indexOf('application') !== -1) {
                root.tabber.tabShow(i);
                return true;
              }
            }
            return false;
            """
        ):
            time.sleep(0.6)
            if _permit_tab_panel_is_visible(driver):
                return True
    except Exception:
        pass

    # Click the tab nav anchor (tabber sets title + onclick on these <a> elements).
    selectors: List[Tuple[str, str]] = [
        (By.CSS_SELECTOR, "#myTab ul.tabbernav a[title='Permits and Permit Applications']"),
        (By.XPATH, "//div[@id='myTab']//ul[contains(@class,'tabbernav')]//a[@title='Permits and Permit Applications']"),
        (
            By.XPATH,
            "//div[@id='myTab']//a[contains(translate(@title,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'permits and permit applications')]",
        ),
        (
            By.XPATH,
            "//div[@id='myTab']//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'permits and permit applications')]",
        ),
    ]
    for by, sel in selectors:
        try:
            elems = driver.find_elements(by, sel)
        except Exception:
            elems = []
        for el in elems:
            try:
                if not el.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.15)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                time.sleep(0.7)
                if _permit_tab_panel_is_visible(driver):
                    return True
            except Exception:
                continue

    # Last resort: fourth tab (0-based index 3) matches server tab order on current WDNR pages.
    try:
        if driver.execute_script(
            """
            var root = document.getElementById('myTab');
            if (!root || !root.tabber || typeof root.tabber.tabShow !== 'function') return false;
            root.tabber.tabShow(3);
            return true;
            """
        ):
            time.sleep(0.6)
            return _permit_tab_panel_is_visible(driver)
    except Exception:
        pass

    return False


def _wait_for_search_form(driver, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='submit'], input[type='button']"))
    )
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "form")))


def _find_select_with_option(driver, option_text: str):
    xpath = (
        "//select[.//option[contains("
        "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"'{option_text.strip().lower()}')]]"
    )
    elems = driver.find_elements(By.XPATH, xpath)
    return elems[0] if elems else None


def _set_select_by_partial_text(driver, option_family: str, desired_text: str) -> bool:
    if not desired_text.strip():
        return False
    select_elem = _find_select_with_option(driver, option_family)
    if not select_elem:
        return False
    select = Select(select_elem)
    needle = desired_text.strip().lower()
    for opt in select.options:
        label = (opt.text or "").strip()
        if needle in label.lower():
            select.select_by_visible_text(label)
            return True
    return False


def _click_search_button(driver) -> bool:
    candidates = driver.find_elements(By.XPATH, "//input[@type='submit' or @type='button' or @type='image']")
    for elem in candidates:
        value_text = f"{elem.get_attribute('value') or ''} {elem.get_attribute('title') or ''}".strip().lower()
        if "search" in value_text:
            driver.execute_script("arguments[0].click();", elem)
            return True
    links = driver.find_elements(By.XPATH, "//a[contains(normalize-space(.), 'Search')]")
    for elem in links:
        if elem.is_displayed():
            driver.execute_script("arguments[0].click();", elem)
            return True
    return False


def _wait_for_results_or_validation(driver, timeout: int) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: (
            "Please enter one or more search terms" in (d.page_source or "")
            or "Permit No" in (d.page_source or "")
            or "Search Results" in (d.page_source or "")
            or len(d.find_elements(By.XPATH, "//a[contains(@href, '__doPostBack')]")) > 0
        )
    )


def _extract_pdf_links(page_html: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    results: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if "javascript:" in href.lower():
            continue
        full_url = urljoin(base_url, href)
        if ".pdf" not in full_url.lower():
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        row = anchor.find_parent("tr")
        row_text = row.get_text(" ", strip=True) if row else ""
        results.append(
            {
                "pdf_url": full_url,
                "link_text": anchor.get_text(" ", strip=True),
                "row_text": row_text,
            }
        )
    return results


def _parse_permit_no(row_text: str) -> str:
    text = (row_text or "").strip()
    if not text:
        return ""
    patterns = [
        r"Permit\s*No\.?\s*[:\s]*([A-Za-z0-9\-\_]+)",
        r"\bPermit\s*V[-\s]*([A-Za-z0-9]+)",
        r"\b(ROP[A-Za-z0-9\-]+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m and m.group(1):
            return m.group(1).strip()

    # Fallback: some rows show a "V-##-###" style number without label.
    m = re.search(r"\b([A-Za-z]{0,3}\s*V[-\s]*\d+\s*[-]\s*\d+[A-Za-z0-9\-]*)\b", text, flags=re.IGNORECASE)
    return m.group(1).replace(" ", "").strip() if m else ""


def _get_result_go_to_links(driver) -> List:
    # Try to find the explicit "Go to" navigation per result row.
    # On this site the "Go to" control can be icon-only (no visible text),
    # so we fall back to clicking the __doPostBack link within each row.
    #
    # Important: restrict search to the results table containing headers like
    # "FID Facility Name Address City County" to avoid accidentally clicking
    # header-row controls.

    grid_candidates = driver.find_elements(By.ID, "ctl00_ContentPlaceHolder1_gvResult")
    if grid_candidates:
        grid = grid_candidates[0]
        go_links = grid.find_elements(
            By.XPATH,
            ".//a[normalize-space(.)='Go' or contains(normalize-space(.), 'Go')]",
        )
        go_links = [e for e in go_links if e.is_displayed()]
        if go_links:
            return go_links

    def _is_known_header_row(tr_text: str) -> bool:
        low = re.sub(r"\s+", " ", (tr_text or "").strip().lower())
        has_digits = any(ch.isdigit() for ch in low)
        # Header row tends to contain column labels but not permit/facility numeric identifiers.
        return (not has_digits) and all(
            word in low for word in ("fid", "facility", "name", "address", "city", "county")
        )

    # (1) Scoped: results table by header keywords, then per-row __doPostBack anchors.
    result_tables = driver.find_elements(
        By.XPATH,
        "//table[contains(., 'FID') and contains(., 'Facility')]",
    )
    for table in result_tables:
        anchors = []
        try:
            anchors = table.find_elements(By.XPATH, ".//a[contains(@href, '__doPostBack')]")
        except Exception:
            anchors = []
        candidates: List = []
        seen_keys: Set[str] = set()
        for a in anchors:
            try:
                if not a.is_displayed():
                    continue
            except Exception:
                continue
            try:
                tr = a.find_element(By.XPATH, "ancestor::tr[1]")
                tr_text = tr.text if tr else ""
            except Exception:
                tr_text = ""
            if _is_known_header_row(tr_text):
                continue
            href = (a.get_attribute("href") or "").strip()
            txt = (a.text or "").strip().lower()
            if txt in {"search", "clear selections", "next", "previous", "previous page", "next page"}:
                continue
            key = href or txt
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(a)
        if candidates:
            return candidates

    # (2) Unscoped: visible "Go to" text-based controls, but still exclude known header rows.
    xpaths = [
        "//tr[.//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to')]]//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to')]",
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to') and (contains(@href, '__doPostBack') or @href)]",
        "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'go to')]",
    ]
    for xp in xpaths:
        elems = driver.find_elements(By.XPATH, xp)
        visible: List = []
        for e in elems:
            try:
                tr = e.find_element(By.XPATH, "ancestor::tr[1]")
                tr_text = (tr.text or "") if tr else ""
            except Exception:
                tr_text = ""
            if _is_known_header_row(tr_text):
                continue
            try:
                if not e.is_displayed():
                    continue
            except Exception:
                continue
            visible.append(e)
        if visible:
            return visible

    # (3) Final fallback: click the first __doPostBack link within each visible (non-header) row.
    candidate_controls: List = []
    seen_hrefs: Set[str] = set()
    rows = driver.find_elements(By.XPATH, "//tr[.//a[contains(@href, '__doPostBack')]]")
    for tr in rows:
        try:
            if _is_known_header_row(tr.text):
                continue
        except Exception:
            pass
        try:
            anchors = tr.find_elements(By.XPATH, ".//a[contains(@href, '__doPostBack')]")
        except Exception:
            continue
        if not anchors:
            continue
        for a in anchors:
            try:
                if not a.is_displayed():
                    continue
            except Exception:
                continue
            txt = (a.text or "").strip().lower()
            if txt in {"search", "clear selections", "next", "previous", "previous page", "next page"}:
                continue
            href = (a.get_attribute("href") or "").strip()
            key = href or txt
            if key in seen_hrefs or not key:
                continue
            seen_hrefs.add(key)
            candidate_controls.append(a)
            break

    return candidate_controls


def _wait_for_text(driver, text: str, timeout: int) -> bool:
    needle = (text or "").strip().lower()
    if not needle:
        return True

    def _cond(d):
        return needle in (d.page_source or "").lower() or len(d.find_elements(By.XPATH, f"//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{needle}') ]")) > 0

    try:
        WebDriverWait(driver, timeout).until(_cond)
        return True
    except Exception:
        return False


def _download_via_click(
    downloader: SeleniumPDFDownloader,
    driver,
    element,
    timeout: int,
    save_as: str,
) -> bool:
    downloader._clear_temp_downloads()
    try:
        driver.execute_script("arguments[0].click();", element)
    except Exception:
        element.click()

    downloaded_path = downloader._wait_for_download(timeout=timeout)
    if not downloaded_path or not downloaded_path.exists():
        return False

    final_name = clean_filename(save_as.strip()) or downloaded_path.name
    if not final_name.lower().endswith(".pdf"):
        final_name = f"{final_name}.pdf"

    final_path = downloader.output_dir / final_name
    downloaded_path.replace(final_path)
    logger.info(f"Downloaded via click: {final_path.name}")
    return True


def _select_permit_on_facility_page(driver, permit_no: str) -> bool:
    if permit_no:
        permit_no_norm = permit_no.strip().lower()
    else:
        permit_no_norm = ""

    # Facility pages use tabber.js; gvPermits is inside #permitTab until that tab is activated.
    try:
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.ID, "myTab")))
    except TimeoutException:
        pass
    _switch_to_facility_permits_tab(driver)

    # First, try the permits grid (gvPermits) because that's likely what controls
    # the permit documents section on this page.
    try:
        select_links = driver.find_elements(
            By.XPATH,
            "//a[contains(@href,'gvPermits') and contains(@href,'Select$')]",
        )
    except Exception:
        select_links = []
    if select_links:
        # If we know the permit id, try to match by row text.
        if permit_no_norm:
            for a in select_links:
                try:
                    tr = a.find_element(By.XPATH, "ancestor::tr[1]")
                    tr_text = (tr.text or "").strip().lower()
                except Exception:
                    tr_text = ""
                if permit_no_norm in tr_text:
                    driver.execute_script("arguments[0].click();", a)
                    time.sleep(1.0)
                    return True
        # Otherwise click the first available permit row.
        for a in select_links:
            try:
                if a.is_displayed():
                    driver.execute_script("arguments[0].click();", a)
                    time.sleep(1.0)
                    return True
            except Exception:
                continue

    selects = driver.find_elements(By.TAG_NAME, "select")
    best_select = None
    best_option = None
    best_score = -1

    for sel in selects:
        try:
            options = sel.find_elements(By.TAG_NAME, "option")
        except Exception:
            continue
        option_texts = [(o.text or "").strip() for o in options]
        option_values = [(o.get_attribute("value") or "").strip() for o in options]
        score = 0
        for t in option_texts:
            low = t.lower()
            if "permit" in low:
                score += 10
            if permit_no_norm and permit_no_norm in low:
                score += 1000
        for v in option_values:
            low = v.lower()
            if permit_no_norm and permit_no_norm in low:
                score += 800
        if score > best_score and option_texts:
            best_score = score
            best_select = sel
            # pick best matching option if permit_no known; else pick first non-placeholder
            if permit_no_norm:
                for opt in options:
                    t = (opt.text or "").strip().lower()
                    if permit_no_norm in t:
                        best_option = opt
                        break
            else:
                for opt in options:
                    t = (opt.text or "").strip()
                    if t and "select" not in t.lower():
                        best_option = opt
                        break

    if not best_select or not best_option:
        return False

    try:
        sel = Select(best_select)
        if permit_no_norm and best_option is not None and best_option.text:
            sel.select_by_visible_text(best_option.text.strip())
        else:
            # Default selection to first useful option.
            sel.select_by_visible_text(best_option.text.strip())
        time.sleep(1.0)
        return True
    except Exception as exc:
        logger.debug(f"Permit selection failed: {exc}")
        return False


def _find_documents_table(driver) -> Optional[object]:
    # Try to locate the "Permit Documents" table.
    xpaths = [
        "//table[.//th[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'document')]]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'permit document')]//following::table[1]",
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'permit documents')]//following::table[1]",
        "//table[contains(@id,'document')]",
    ]
    for xp in xpaths:
        tables = driver.find_elements(By.XPATH, xp)
        if tables:
            return tables[0]
    return None


def _find_download_control_in_row(row) -> Tuple[Optional[str], Optional[object]]:
    # Return (pdf_url_if_discovered, click_element).
    xpaths = [
        ".//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') or contains(@title,'Download') or contains(@aria-label,'Download')]",
        ".//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download')]",
        ".//a[contains(@href, '.pdf') or contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'pdf')]",
    ]
    for xp in xpaths:
        elems = row.find_elements(By.XPATH, xp)
        for e in elems:
            if not e.is_displayed():
                continue
            try:
                href = e.get_attribute("href") or ""
            except Exception:
                href = ""
            if href and ".pdf" in href.lower():
                return href, e
            return None, e

    # Fallback: icon-only download controls often trigger postback without a visible label.
    fallback_xp = (
        ".//a[contains(@href,'__doPostBack') or contains(@href,'WebForm_DoPostBackWithOptions') or "
        "contains(@href,'DoPostBackWithOptions')] | "
        ".//*[@onclick[contains(.,'__doPostBack') or contains(.,'DoPostBackWithOptions')]] | "
        ".//input[contains(@onclick,'__doPostBack') or contains(@onclick,'DoPostBackWithOptions')] | "
        ".//button[contains(@onclick,'__doPostBack') or contains(@onclick,'DoPostBackWithOptions')]"
    )
    try:
        elems = row.find_elements(By.XPATH, fallback_xp)
    except Exception:
        elems = []
    for e in elems:
        try:
            if not e.is_displayed():
                continue
        except Exception:
            continue
        href = ""
        try:
            href = e.get_attribute("href") or ""
        except Exception:
            href = ""
        if href and ".pdf" in href.lower():
            return href, e
        return None, e

    return None, None


def _download_final_permit_from_facility_page(
    downloader: SeleniumPDFDownloader,
    driver,
    save_as_prefix: str,
    timeout: int,
) -> Tuple[bool, str, str]:
    """
    Returns (ok, saved_filename, pdf_url_if_known)
    """
    # First, try the deterministic "select final row -> click Dwnld" path.
    ok, saved_name, download_url = _download_final_permit_via_dwnld(
        downloader=downloader,
        driver=driver,
        save_as_prefix=save_as_prefix,
        timeout=min(timeout, 90),
    )
    if ok:
        return ok, saved_name, download_url

    # Wait for documents area to show up (best-effort). The wording on this page
    # varies by record state, so wait for multiple signals before scanning.
    _wait_for_text(driver, "permit document", timeout=min(20, timeout // 2))
    _wait_for_text(driver, "final", timeout=min(20, timeout // 2))
    _wait_for_text(driver, "download", timeout=min(15, timeout // 3))
    _wait_for_text(driver, "pdf", timeout=min(15, timeout // 3))
    table = _find_documents_table(driver)
    if table is None:
        logger.info("No documents table found; scanning for download controls…")
        # Fallback 1: scan for download controls tied to 'Final' rows.
        download_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') or contains(@title,'Download') or contains(@aria-label,'Download') or contains(@href,'.pdf')] | //button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download')]",
        )
        final_trs = driver.find_elements(
            By.XPATH,
            "//tr[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'final') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'permit')]",
        )
        logger.info(
            f"Facility scan: download_controls={len(download_buttons)} final_trs={len(final_trs)}"
        )
        best = None
        best_score = -1
        for btn in download_buttons:
            try:
                row = btn.find_element(By.XPATH, "ancestor::tr[1]")
                row_text = row.text or ""
            except Exception:
                row = None
                row_text = ""
            low = (row_text or "").lower()
            score = 0
            if "final" in low:
                score += 50
            if "final permit" in low:
                score += 25
            if "issued" in low:
                score += 8
            if score > best_score:
                best_score = score
                best = (btn, row_text)

        if best and best_score >= 0:
            btn, row_text = best
            short = re.sub(r"\s+", " ", (row_text or "").strip())[:80].strip(" -_") or "final_permit"
            save_as = f"{save_as_prefix}_{short}"
            # Direct pdf href?
            try:
                href = btn.get_attribute("href") or ""
            except Exception:
                href = ""
            if href and ".pdf" in href.lower():
                ok = downloader.download_document(
                    href,
                    referer=driver.current_url,
                    link_text=short,
                    is_table_link=True,
                    save_as=save_as,
                )
                return ok, clean_filename(Path(urlparse(href).path).name) or save_as, href if ok else ""

            ok = _download_via_click(
                downloader=downloader,
                driver=driver,
                element=btn,
                timeout=timeout,
                save_as=save_as,
            )
            return ok, clean_filename(save_as), ""

        # Fallback 2: click a download control from the first 'Final' row.
        if final_trs:
            for idx, row in enumerate(final_trs[:10], start=1):
                try:
                    row_text = (row.text or "").strip()
                except Exception:
                    row_text = ""
                short = re.sub(r"\s+", " ", row_text)[:80].strip(" -_") or "final_permit"
                # Prefer the icon-only download link explicitly (lnkDownload).
                try:
                    dl_links = row.find_elements(
                        By.XPATH,
                        ".//a[contains(@href,'lnkDownload') and not(contains(@href,'gvEIReports'))]",
                    )
                except Exception:
                    dl_links = []
                if dl_links:
                    for dl in dl_links[:1]:
                        try:
                            if not dl.is_displayed():
                                continue
                        except Exception:
                            continue
                        save_as = f"{save_as_prefix}_{short}"
                        logger.info(f"Clicking lnkDownload in final row #{idx}: {short[:60]}")
                        ok = _download_via_click(
                            downloader=downloader,
                            driver=driver,
                            element=dl,
                            timeout=timeout,
                            save_as=save_as,
                        )
                        if ok:
                            return True, clean_filename(save_as), ""
                _, download_elem = _find_download_control_in_row(row)
                if download_elem:
                    save_as = f"{save_as_prefix}_{short}"
                    logger.info(f"Clicking download from final row #{idx}: {short[:60]}")
                    ok = _download_via_click(
                        downloader=downloader,
                        driver=driver,
                        element=download_elem,
                        timeout=timeout,
                        save_as=save_as,
                    )
                    if ok:
                        return True, clean_filename(save_as), ""

        # Fallback 3: look for PDF links already on page.
        hits = _extract_pdf_links(driver.page_source, base_url=driver.current_url)
        if hits:
            hit = hits[0]
            name = f"{save_as_prefix}_{Path(urlparse(hit['pdf_url']).path).name}"
            ok = downloader.download_document(
                hit["pdf_url"],
                referer=driver.current_url,
                link_text=hit.get("link_text", ""),
                is_table_link=True,
                save_as=name,
            )
            return ok, clean_filename(Path(urlparse(hit["pdf_url"]).path).name) or "", hit["pdf_url"] if ok else ""
        return False, "", ""

    rows = table.find_elements(By.XPATH, ".//tr")
    if not rows:
        return False, "", ""

    candidates = []
    for r in rows:
        try:
            row_text = r.text or ""
        except Exception:
            row_text = ""
        low = row_text.lower()
        if ("final" in low and "permit" in low) or "final permit" in low or "permit authority issues permit" in low:
            candidates.append((row_text, r))

    # If no explicit "final" rows, just use all rows as backup.
    if not candidates:
        candidates = [(r.text or "", r) for r in rows[:20]]

    logger.info(f"Documents table rows={len(rows)} final-ish candidates={len(candidates)}")

    # Pick the best candidate: prefer those with "final permit" or "permit authority issues permit"
    def score(item):
        rt = item[0].lower()
        s = 0
        if "final permit" in rt:
            s += 50
        if "final" in rt:
            s += 20
        if "permit authority issues permit" in rt:
            s += 15
        return s

    candidates.sort(key=score, reverse=True)

    for idx, (row_text, row) in enumerate(candidates[:8]):
        _, download_elem = _find_download_control_in_row(row)
        if not download_elem:
            continue

        # Derive filename from row text (shortened).
        short = re.sub(r"\s+", " ", (row_text or "").strip())
        short = short[:80].strip(" -_") or "final_permit"
        save_as = f"{save_as_prefix}_{short}"
        logger.info(f"Attempt {idx+1}: clicking download for row fragment: {short[:60]}")

        # If we can discover direct pdf href, try direct first.
        pdf_url = None
        try:
            # Re-run to capture pdf_url.
            pdf_url, _ = _find_download_control_in_row(row)
        except Exception:
            pdf_url = None

        if pdf_url and ".pdf" in pdf_url.lower():
            ok = downloader.download_document(
                pdf_url,
                referer=driver.current_url,
                link_text=short,
                is_table_link=True,
                save_as=save_as,
            )
            if ok:
                return True, clean_filename(Path(urlparse(pdf_url).path).name) or save_as, pdf_url
            # If direct failed, try clicking anyway.

        ok = _download_via_click(
            downloader=downloader,
            driver=driver,
            element=download_elem,
            timeout=timeout,
            save_as=save_as,
        )
        if ok:
            return True, clean_filename(save_as), ""
        time.sleep(0.8)

    return False, "", ""


def _discover_result_links(driver) -> List:
    # Result rows are ASP.NET link buttons with __doPostBack hrefs.
    links = driver.find_elements(By.XPATH, "//a[contains(@href, '__doPostBack')]")
    filtered = []
    for link in links:
        text = (link.text or "").strip()
        if not text:
            continue
        low = text.lower()
        if low in {"search", "clear selections", "next", "previous", "..."}:
            continue
        if len(text) > 140:
            continue
        filtered.append(link)
    return filtered


def _goto_next_page(driver, timeout: int) -> bool:
    xpath_candidates = [
        "//a[contains(normalize-space(.), 'Next')]",
        "//a[@title='Next']",
        "//a[normalize-space(text())='>']",
    ]
    for xpath in xpath_candidates:
        for elem in driver.find_elements(By.XPATH, xpath):
            if not elem.is_displayed():
                continue
            href = (elem.get_attribute("href") or "").lower()
            if "javascript:void" in href:
                continue
            try:
                driver.execute_script("arguments[0].click();", elem)
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(1.0)
                return True
            except TimeoutException:
                return False
            except Exception:
                continue
    return False


def _extract_download_object_id_from_element_text(element) -> Optional[str]:
    try:
        candidates = [
            (element.get_attribute("href") or ""),
            (element.get_attribute("onclick") or ""),
            (element.get_attribute("title") or ""),
            (element.get_attribute("aria-label") or ""),
            (element.get_attribute("alt") or ""),
        ]
    except Exception:
        candidates = []

    # Sometimes the element isn't directly informative, but its onclick may embed the id.
    blob = " ".join([c for c in candidates if c])
    m = re.search(r"AM_DownloadObject\.aspx\?id=(\d+)", blob, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _download_final_permit_via_dwnld(
    downloader: SeleniumPDFDownloader,
    driver,
    save_as_prefix: str,
    timeout: int,
) -> Tuple[bool, str, str]:
    """
    Click the 'Dwnld' control in the 'Final' permit row, and prefer direct
    download URLs when the underlying AM_DownloadObject.aspx?id=... can be extracted.
    """
    end_time = time.time() + timeout

    # Wait until we see any download-ish controls (Dwnld / AM_DownloadObject).
    while time.time() < end_time:
        dwnld_candidates = driver.find_elements(
            By.XPATH,
            "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dwnld')] | "
            "//a[contains(@href,'lnkDownload') and not(contains(@href,'gvEIReports'))] | "
            " //a[contains(@onclick,'lnkDownload') and not(contains(@onclick,'gvEIReports'))] | "
            "//a[contains(@href,'AM_DownloadObject.aspx?id=') and not(contains(@href,'gvEIReports'))] | "
            "//a[contains(@onclick,'AM_DownloadObject.aspx?id=') and not(contains(@onclick,'gvEIReports'))]",
        )
        if dwnld_candidates:
            break
        time.sleep(1.0)

    # Candidate strategies:
    # 1) Prefer links whose row contains 'Final' (best match)
    # 2) Otherwise, first dwnld candidate
    all_dwnld = driver.find_elements(
        By.XPATH,
        "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dwnld')] | "
        "//a[contains(@href,'lnkDownload') and not(contains(@href,'gvEIReports'))] | "
        "//a[contains(@onclick,'lnkDownload') and not(contains(@onclick,'gvEIReports'))] | "
        "//a[contains(@href,'AM_DownloadObject.aspx?id=') and not(contains(@href,'gvEIReports'))] | "
        "//a[contains(@onclick,'AM_DownloadObject.aspx?id=') and not(contains(@onclick,'gvEIReports'))]",
    )

    scored: List[Tuple[int, object, str]] = []
    for cand in all_dwnld:
        try:
            if not cand.is_displayed():
                continue
        except Exception:
            continue
        row_text = ""
        try:
            row = cand.find_element(By.XPATH, "ancestor::tr[1]")
            row_text = (row.text or "").lower()
        except Exception:
            row_text = ""

        score = 0
        if "final" in row_text:
            score += 100
        if "permit" in row_text:
            score += 20
        if score == 0:
            score = 1

        download_id = _extract_download_object_id_from_element_text(cand)
        scored.append((score, cand, download_id or ""))

    if not scored:
        return False, "", ""

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_elem, best_id = scored[0]

    short = f"final_permit_dwnld_{best_id}" if best_id else "final_permit_dwnld"
    save_as = f"{save_as_prefix}_{short}"

    # Some pages require explicitly selecting the "Final" row before the Dwnld
    # control becomes active. If a Select$... control exists in the same row, click it.
    try:
        best_row = best_elem.find_element(By.XPATH, "ancestor::tr[1]")
        select_in_row = best_row.find_elements(
            By.XPATH,
            ".//a[contains(@href,'Select$') or contains(@onclick,'Select$') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'select')]",
        )
        for s in select_in_row[:2]:
            try:
                if s.is_displayed():
                    driver.execute_script("arguments[0].click();", s)
                    time.sleep(1.0)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Prefer direct AM_DownloadObject URL if we can extract the id.
    if best_id:
        download_url = f"https://apps.dnr.wi.gov/warp_ext/AM_DownloadObject.aspx?id={best_id}"
        ok = downloader.download_document(
            download_url,
            referer=driver.current_url,
            link_text="Dwnld",
            is_table_link=True,
            save_as=save_as,
        )
        if ok:
            return True, clean_filename(Path(urlparse(download_url).path).name) or save_as, download_url

    # Fall back to clicking the 'Dwnld' control.
    ok = _download_via_click(
        downloader=downloader,
        driver=driver,
        element=best_elem,
        timeout=max(45, timeout),
        save_as=save_as,
    )
    return ok, clean_filename(save_as), ""


def crawl_and_download(
    output_dir: Path,
    index_csv: Path,
    headless: bool,
    wait_seconds: int,
    sleep_seconds: float,
    milestone_text: str,
    permit_type_text: Optional[str],
    max_pages: Optional[int],
    max_rows: Optional[int],
    debug_results: bool,
    debug_facility_final: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: List[dict] = []

    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    downloaded = 0
    failed = 0
    processed_rows = 0
    seen_keys: Set[str] = set()
    seen_pdf_urls: Set[str] = set()

    try:
        driver = downloader.driver
        driver.get(SEARCH_URL)
        _wait_for_search_form(driver, timeout=max(20, wait_seconds * 6))

        if milestone_text:
            selected = _set_select_by_partial_text(driver, "Permit Authority Issues Permit", milestone_text)
            logger.info(f"Milestone filter {'set' if selected else 'not set'}: {milestone_text}")
        if permit_type_text:
            selected = _set_select_by_partial_text(driver, "ROPA", permit_type_text)
            logger.info(f"Permit-type filter {'set' if selected else 'not set'}: {permit_type_text}")

        if not _click_search_button(driver):
            raise RuntimeError("Failed to locate/search button on Wisconsin form.")
        _wait_for_results_or_validation(driver, timeout=max(20, wait_seconds * 6))

        if "Please enter one or more search terms" in driver.page_source:
            logger.warning("Search form requested at least one term; attempting milestone-only search.")
            _set_select_by_partial_text(driver, "Permit Authority Issues Permit", DEFAULT_MILESTONE)
            if not _click_search_button(driver):
                raise RuntimeError("Search re-submit failed after setting milestone filter.")
            _wait_for_results_or_validation(driver, timeout=max(20, wait_seconds * 6))

        if debug_results:
            logger.info("DEBUG: Listing clickable candidate controls on results page…")
            grid_candidates = driver.find_elements(By.ID, "ctl00_ContentPlaceHolder1_gvResult")
            if grid_candidates:
                grid = grid_candidates[0]
                rows = grid.find_elements(By.XPATH, ".//tr")
                logger.info(f"DEBUG: gvResult rows found: {len(rows)}")
                # List click-like elements inside the grid that trigger postback.
                candidates = grid.find_elements(
                    By.XPATH,
                    ".//a[@href] | .//button | .//input",
                )
            else:
                candidates = driver.find_elements(
                    By.XPATH,
                    "//*[@onclick[contains(.,'__doPostBack')] or (@href and contains(@href,'__doPostBack'))]",
                )
            logged = 0
            for i, c in enumerate(candidates[:400], start=1):
                try:
                    tr = c.find_element(By.XPATH, "ancestor::tr[1]")
                    tr_text = (tr.text or "").strip().replace("\n", " ")
                except Exception:
                    tr_text = ""
                txt = (c.text or "").strip()
                title = (c.get_attribute("title") or "").strip()
                aria = (c.get_attribute("aria-label") or "").strip()
                href = (c.get_attribute("href") or "").strip()
                onclick = (c.get_attribute("onclick") or "").strip()
                # Skip sorting controls; we want the actual row/navigation commands.
                if "Sort$" in (href + " " + onclick):
                    continue
                tag = ""
                try:
                    tag = c.tag_name
                except Exception:
                    tag = ""
                logger.info(
                    f"  cand[{i}] tag={tag} text='{txt}' title='{title}' aria='{aria}' href='{href}' onclick='{onclick[:70]}' tr='{tr_text[:120]}'"
                )
                logged += 1
                if logged >= 25:
                    break
            return

        def _debug_final_row_controls() -> None:
            _wait_for_text(driver, "final", timeout=min(30, max(10, wait_seconds * 6)))
            final_trs_dbg = driver.find_elements(
                By.XPATH,
                "//tr[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'final') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'permit')]",
            )
            logger.info(f"DEBUG: final_trs on facility page: {len(final_trs_dbg)}")
            for idx, row in enumerate(final_trs_dbg[:3], start=1):
                try:
                    row_text = (row.text or "").strip().replace("\n", " ")
                except Exception:
                    row_text = ""
                logger.info(f"DEBUG: final row #{idx} text='{row_text[:140]}'")
                try:
                    elems = row.find_elements(By.XPATH, ".//a | .//button | .//input | .//img")
                except Exception:
                    elems = []
                for j, el in enumerate(elems[:40], start=1):
                    try:
                        tag = el.tag_name
                    except Exception:
                        tag = ""
                    try:
                        txt = (el.text or "").strip()
                    except Exception:
                        txt = ""
                    try:
                        href = el.get_attribute("href") or ""
                    except Exception:
                        href = ""
                    try:
                        onclick = el.get_attribute("onclick") or ""
                    except Exception:
                        onclick = ""
                    try:
                        title = el.get_attribute("title") or ""
                    except Exception:
                        title = ""
                    try:
                        aria = el.get_attribute("aria-label") or ""
                    except Exception:
                        aria = ""
                    try:
                        alt = el.get_attribute("alt") or ""
                    except Exception:
                        alt = ""
                    if not (href or onclick or title or aria or alt or txt):
                        continue
                    logger.info(
                        f"  el[{idx}.{j}] tag={tag} text='{txt}' href='{href[:80]}' onclick='{onclick[:80]}' title='{title}' aria='{aria}' alt='{alt}'"
                    )
            raise SystemExit(0)

        page_num = 1
        while True:
            go_links = _get_result_go_to_links(driver)
            logger.info(f"Page {page_num}: found {len(go_links)} 'Go to' links.")

            row_idx = 0
            while row_idx < len(go_links):
                go_link = go_links[row_idx]
                row_elem = None
                try:
                    row_elem = go_link.find_element(By.XPATH, "ancestor::tr[1]")
                except Exception:
                    row_elem = None

                row_text = ""
                try:
                    row_text = row_elem.text if row_elem else (go_link.text or "")
                except Exception:
                    row_text = go_link.text or ""

                permit_no = _parse_permit_no(row_text)
                key = f"{page_num}|{permit_no}|{row_text[:60]}"
                if key in seen_keys:
                    row_idx += 1
                    continue
                seen_keys.add(key)

                save_as_prefix = clean_filename(
                    f"wi_{permit_no or f'row{processed_rows+1}'}_{row_text[:40]}".strip()
                ) or f"wi_row_{processed_rows+1}"

                logger.info(f"[{processed_rows+1}] Clicking 'Go to' (permit_no='{permit_no}')")
                try:
                    downloader._clear_temp_downloads()
                    driver.execute_script("arguments[0].click();", go_link)
                    WebDriverWait(driver, max(20, wait_seconds * 6)).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                    time.sleep(1.0)
                except Exception as exc:
                    failed += 1
                    processed_rows += 1
                    logger.warning(f"Failed to click Go to for permit_no='{permit_no}': {exc}")
                    index_rows.append(
                        {
                            "page": page_num,
                            "result_row_text": row_text,
                            "permit_no": permit_no,
                            "detail_url": driver.current_url,
                            "pdf_url": "",
                            "status": "failed_open_facility",
                            "local_path": "",
                        }
                    )
                    if max_rows is not None and processed_rows >= max_rows:
                        break
                    # Best effort to return.
                    try:
                        driver.back()
                        time.sleep(1.0)
                    except Exception:
                        pass
                    go_links = _get_result_go_to_links(driver)
                    row_idx += 1
                    continue

                detail_url = driver.current_url

                # Select permit (best-effort; the "Go to" link may already pick it).
                selected_ok = _select_permit_on_facility_page(driver, permit_no)
                if permit_no and not selected_ok:
                    logger.info(f"Permit selection may be skipped for permit_no='{permit_no}'.")

                if debug_facility_final:
                    _debug_final_row_controls()

                ok, saved_name, pdf_url = _download_final_permit_from_facility_page(
                    downloader=downloader,
                    driver=driver,
                    save_as_prefix=save_as_prefix,
                    timeout=max(40, wait_seconds * 10),
                )
                processed_rows += 1

                if ok:
                    downloaded += 1
                    if pdf_url:
                        seen_pdf_urls.add(pdf_url)
                    local_filename = saved_name or ""
                    if local_filename and not local_filename.lower().endswith(".pdf"):
                        local_filename = f"{local_filename}.pdf"
                    local_path = str(downloader.output_dir / local_filename) if local_filename else ""
                    status = "downloaded"
                else:
                    failed += 1
                    local_path = ""
                    status = "failed_download"

                index_rows.append(
                    {
                        "page": page_num,
                        "result_row_text": row_text,
                        "permit_no": permit_no,
                        "detail_url": detail_url,
                        "pdf_url": pdf_url,
                        "status": status,
                        "local_path": local_path,
                    }
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

                if max_rows is not None and processed_rows >= max_rows:
                    logger.info(f"Reached --max-rows={max_rows}.")
                    break

                # Return to results list.
                try:
                    driver.back()
                    WebDriverWait(driver, max(20, wait_seconds * 6)).until(
                        EC.presence_of_element_located((By.TAG_NAME, "body"))
                    )
                    time.sleep(1.0)
                    go_links = _get_result_go_to_links(driver)
                except Exception:
                    break

                row_idx += 1

            if max_rows is not None and processed_rows >= max_rows:
                break
            if max_pages is not None and page_num >= max_pages:
                logger.info(f"Reached --max-pages={max_pages}.")
                break
            if not _goto_next_page(driver, timeout=max(20, wait_seconds * 6)):
                break
            page_num += 1
    finally:
        downloader.close()

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["page", "result_row_text", "permit_no", "detail_url", "pdf_url", "status", "local_path"],
        )
        writer.writeheader()
        writer.writerows(index_rows)

    logger.info("=" * 60)
    logger.info("WISCONSIN DNR AIR PERMIT SCRAPE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Rows processed: {processed_rows}")
    logger.info(f"Unique PDF URLs: {len(seen_pdf_urls)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Wisconsin DNR Air Management permit tracking records and download permit PDFs."
    )
    default_output = RAW_DATA_DIR / "wi_dnr_air_permits"
    default_index = default_output / "wi_dnr_air_permits_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloaded PDFs (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"Path to write index CSV (default: {default_index}).",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome in visible mode.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5,
        help="Base wait time for Selenium waits.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.25,
        help="Pause between PDF download attempts.",
    )
    parser.add_argument(
        "--milestone",
        type=str,
        default=DEFAULT_MILESTONE,
        help=(
            "Milestone text to select before searching (partial match). "
            "Set empty string to skip milestone selection."
        ),
    )
    parser.add_argument(
        "--permit-type",
        type=str,
        default=None,
        help="Optional permit-type text to select (partial match, e.g. ROPA, ROPB, ROPC).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit on result pages.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional limit on opened result rows.",
    )
    parser.add_argument(
        "--debug-results",
        action="store_true",
        help="Print candidate clickable controls on the results page and exit.",
    )
    parser.add_argument(
        "--debug-facility-final",
        action="store_true",
        help="After clicking the first results 'Go' row, print final-row controls and exit.",
    )
    args = parser.parse_args()

    crawl_and_download(
        output_dir=args.output_dir.expanduser().resolve(),
        index_csv=args.index_csv.expanduser().resolve(),
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        sleep_seconds=args.sleep_seconds,
        milestone_text=args.milestone,
        permit_type_text=args.permit_type,
        max_pages=args.max_pages,
        max_rows=args.max_rows,
        debug_results=args.debug_results,
        debug_facility_final=args.debug_facility_final,
    )


if __name__ == "__main__":
    main()
