#!/usr/bin/env python3
"""
Search Wyoming IMPACT facilities with Title V permits and download PDFs.

Starts from the public facility search page, filters to Title V permits,
opens each facility detail page, and downloads the Title V permit PDFs.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename, is_probable_pdf

SEARCH_URL = "https://openair.wyo.gov/facilities/facilitySearch.jsf"


@dataclass(frozen=True)
class FacilityLink:
    name: str
    url: str


@dataclass(frozen=True)
class DownloadJob:
    url: str
    referer: str
    save_as: str


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _wait_for_body(driver, timeout: int = 25) -> None:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def wait_for_url_contains(driver, text: str, timeout: int = 25) -> None:
    WebDriverWait(driver, timeout).until(lambda d: text.lower() in (d.current_url or "").lower())


def wait_for_url_not_contains(driver, text: str, timeout: int = 25) -> None:
    WebDriverWait(driver, timeout).until(lambda d: text.lower() not in (d.current_url or "").lower())


def wait_for_post_disclaimer_ready(driver, timeout: int = 25) -> None:
    def _ready(_driver) -> bool:
        current_url = (_driver.current_url or "").lower()
        if "disclaimer" not in current_url:
            return True
        try:
            _driver.find_element(By.ID, "t0:facilitySearchBtn")
            return True
        except Exception:
            pass
        try:
            _driver.find_element(By.ID, "_idJsp3")
            return True
        except Exception:
            return False

    WebDriverWait(driver, timeout).until(lambda d: _ready(d))


def wait_for_search_page_ready(driver, timeout: int = 30) -> None:
    def _ready_state(_driver) -> bool:
        current_url = (_driver.current_url or "").lower()
        if "disclaimer" in current_url:
            return False
        if "facilitysearch" not in current_url:
            return False
        try:
            _driver.find_element(By.TAG_NAME, "form")
        except Exception:
            return False
        return True

    WebDriverWait(driver, timeout).until(lambda d: _ready_state(d))


def load_facility_ids(csv_path: Path) -> List[str]:
    facility_ids: List[str] = []
    encodings = ["utf-8-sig", "cp1252", "latin-1"]
    last_error: Optional[Exception] = None

    for encoding in encodings:
        try:
            with csv_path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    value = (row.get("Facility ID") or "").strip()
                    if value:
                        facility_ids.append(value)
            return facility_ids
        except UnicodeDecodeError as exc:
            facility_ids = []
            last_error = exc
            continue

    if last_error:
        raise last_error
    return facility_ids


def go_to_facilities_tab(driver) -> None:
    facilities_xpaths = [
        "//area[contains(@title, 'Facilities')]",
        "//area[contains(@alt, 'Facilities')]",
        "//a[normalize-space()='Facilities']",
    ]
    for xpath in facilities_xpaths:
        try:
            tab = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, xpath)))
        except Exception:
            continue
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", tab)
            if tab.is_displayed() and tab.is_enabled():
                try:
                    tab.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", tab)
                return
        except Exception:
            continue

    try:
        driver.execute_script("submitForm('_idJsp3',0,{source:'_idJsp4:1:_idJsp11'});")
    except Exception as exc:
        raise RuntimeError("Could not navigate to Facilities tab.") from exc


def _attempt_click_agree(driver) -> bool:
    agree_xpaths = [
        "//a[contains(normalize-space(.), 'Agree')]",
        "//button[contains(normalize-space(.), 'Agree')]",
        "//input[@type='submit' and contains(@value,'Agree')]",
        "//input[@type='button' and contains(@value,'Agree')]",
        "//a[contains(@id,'agreeBtn')]",
        "//img[@title='Agree' or @alt='Agree']/ancestor::a",
        "//span[contains(normalize-space(.), 'Agree')]/ancestor::*[self::a or self::button]",
    ]
    for xpath in agree_xpaths:
        try:
            button = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, xpath)))
        except Exception:
            continue
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            if button.is_displayed() and button.is_enabled():
                try:
                    button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", button)
                time.sleep(0.5)
                return True
        except Exception:
            continue
    return False


def accept_disclaimer_if_present(driver) -> bool:
    if _attempt_click_agree(driver):
        logger.info("Accepted disclaimer.")
        return True

    try:
        agree_link = driver.find_element(By.XPATH, "//a[contains(@id,'agreeBtn')]")
        link_id = agree_link.get_attribute("id") or ""
        driver.execute_script(
            """
            const el = arguments[0];
            const linkId = arguments[1];
            if (typeof submitForm === 'function' && linkId) {
              submitForm('_idJsp0', 1, { source: linkId });
              return true;
            }
            if (el && typeof el.click === 'function') {
              el.click();
              return true;
            }
            return false;
            """,
            agree_link,
            link_id,
        )
        time.sleep(0.5)
        logger.info("Accepted disclaimer via agreeBtn link.")
        return True
    except Exception:
        pass

    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
            if _attempt_click_agree(driver):
                driver.switch_to.default_content()
                logger.info("Accepted disclaimer in iframe.")
                return True
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return False


def _select_title_v_from_selects(selects: Iterable[object]) -> bool:
    for select_el in selects:
        try:
            options = select_el.find_elements(By.TAG_NAME, "option")
        except Exception:
            continue
        for option in options:
            if "title v" in _normalize_text(option.text):
                Select(select_el).select_by_visible_text(option.text)
                logger.info("Selected Title V option from <select>.")
                return True
    return False


def _select_title_v_facility_class(driver) -> bool:
    try:
        select_el = driver.find_element(By.ID, "t0:permitClassCds")
    except Exception:
        return False

    try:
        selector = Select(select_el)
        for option in selector.options:
            if "title v" in _normalize_text(option.text):
                selector.select_by_visible_text(option.text)
                driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                    select_el,
                )
                logger.info("Selected Title V in Facility Class select.")
                return True
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const el = arguments[0];
            if (!el) { return false; }
            for (const opt of el.options || []) {
              if ((opt.textContent || '').toLowerCase().includes('title v')) {
                opt.selected = true;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
            }
            return false;
            """,
            select_el,
        )
        logger.info("Selected Title V in Facility Class select via JS.")
        return True
    except Exception:
        return False


def _find_label_text(driver, element) -> str:
    label_text = element.get_attribute("aria-label") or ""
    if label_text:
        return label_text

    element_id = element.get_attribute("id")
    if not element_id:
        return ""

    labels = driver.find_elements(By.XPATH, f"//label[@for='{element_id}']")
    for label in labels:
        text = label.text.strip()
        if text:
            return text
    return ""


def _select_title_v_from_comboboxes(driver) -> bool:
    inputs = driver.find_elements(
        By.XPATH,
        "//input[@role='combobox' or contains(@class,'selectOneChoice') or contains(@class,'af_inputText')]",
    )
    for input_el in inputs:
        label_text = _normalize_text(_find_label_text(driver, input_el))
        if label_text and not any(key in label_text for key in ("permit", "title", "program", "type")):
            continue
        try:
            input_el.click()
            input_el.clear()
            input_el.send_keys("Title V")
            time.sleep(0.5)
            if _click_title_v_option(driver):
                logger.info("Selected Title V option from combo box.")
                return True
            input_el.send_keys(Keys.ENTER)
            logger.info("Typed Title V into combo box.")
            return True
        except Exception:
            continue
    return False


def _click_title_v_option(driver) -> bool:
    option_xpath = (
        "//*[self::li or self::div or self::span]"
        "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'title v')]"
    )
    for option in driver.find_elements(By.XPATH, option_xpath):
        try:
            if option.is_displayed():
                option.click()
                return True
        except Exception:
            continue
    return False


def apply_title_v_filter(driver) -> None:
    if _select_title_v_facility_class(driver):
        return
    selects = driver.find_elements(By.TAG_NAME, "select")
    if _select_title_v_from_selects(selects):
        return
    if _select_title_v_from_comboboxes(driver):
        return
    raise RuntimeError("Unable to locate a Title V filter on the search page.")


def click_search(driver) -> None:
    try:
        driver.execute_script("submitForm('_idJsp5',1,{source:'t0:facilitySearchBtn'});")
        _wait_for_body(driver)
        logger.info("Submitted search via submitForm.")
        return
    except Exception:
        pass

    search_xpaths = [
        "//a[@id='t0:facilitySearchBtn']",
        "//button[normalize-space()='Search']",
        "//input[@type='submit' and contains(@value,'Search')]",
        "//a[normalize-space()='Search']",
        "//a[.//img[@title='Submit' or @alt='Submit']]",
    ]
    for xpath in search_xpaths:
        try:
            button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            button.click()
            _wait_for_body(driver)
            logger.info("Submitted search.")
            return
        except Exception:
            continue
    raise RuntimeError("Could not locate the Search button on the Wyoming IMPACT page.")


def set_facility_id(driver, facility_id: str) -> None:
    input_id = "t0:_idJsp15"
    field = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, input_id)))
    try:
        field.clear()
    except Exception:
        pass
    field.click()
    field.send_keys(Keys.CONTROL + "a")
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(facility_id)


def open_facility_from_results(driver, facility_id: str) -> None:
    link_xpath = f"//a[normalize-space()='{facility_id}']"
    link = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.XPATH, link_xpath)))
    link.click()
    _wait_for_body(driver)


def _find_visible_input(driver, xpaths: List[str]) -> Optional[object]:
    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
    return None


def search_permits_for_facility(driver, facility_id: str) -> bool:
    if "permitSearch.jsf" not in driver.current_url:
        return False

    input_xpaths = [
        "//label[contains(normalize-space(.), 'Facility ID')]/following::input[1]",
        "//label[contains(normalize-space(.), 'Facility')]/following::input[1]",
        "//input[contains(@id, 'facility') or contains(@name, 'facility')]",
        "//input[contains(@class, 'autocomplete')]",
        "//input[@type='search']",
        "//input[@type='text']",
    ]
    field = _find_visible_input(driver, input_xpaths)
    if not field:
        raise RuntimeError("Could not locate Facility ID input on permit search page.")

    try:
        field.clear()
    except Exception:
        pass
    field.click()
    field.send_keys(Keys.CONTROL + "a")
    field.send_keys(Keys.BACKSPACE)
    field.send_keys(facility_id)
    field.send_keys(Keys.ENTER)

    submit_id = "_idJsp6:submitPermitSearch"
    try:
        submit = driver.find_element(By.ID, submit_id)
        submit.click()
    except Exception:
        driver.execute_script(
            "if (typeof submitForm === 'function') { submitForm(arguments[0], 0, { source: arguments[1] }); }",
            "_idJsp5",
            submit_id,
        )

    _wait_for_body(driver)
    return True


def find_results_table(soup: BeautifulSoup) -> Optional[BeautifulSoup]:
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            continue
        if any("facility" in header for header in headers) and any("permit" in header for header in headers):
            return table
    return None


def find_title_v_permits_table(soup: BeautifulSoup) -> Tuple[Optional[BeautifulSoup], bool]:
    table = soup.find("table", id=re.compile(r"permitsTV", re.IGNORECASE))
    if table:
        return table, True

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "span", "div"]):
        if "title v permits" in _normalize_text(heading.get_text(" ", strip=True)):
            table = heading.find_next("table")
            if table:
                return table, True

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if headers and any("permit" in header for header in headers) and any("title v" in header for header in headers):
            return table, True
    return None, False


def extract_facility_links(page_html: str, base_url: str) -> List[FacilityLink]:
    soup = BeautifulSoup(page_html, "html.parser")
    table = find_results_table(soup)
    links: List[FacilityLink] = []
    seen: Set[str] = set()

    if table:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            facility_name = cells[0].get_text(strip=True) if cells else ""
            link = row.find("a", href=True)
            if not link:
                continue
            href = urljoin(base_url, link["href"])
            if href in seen:
                continue
            seen.add(href)
            links.append(FacilityLink(name=facility_name or link.get_text(strip=True), url=href))

    if not links:
        for link in soup.find_all("a", href=True):
            href = urljoin(base_url, link["href"])
            link_text = link.get_text(strip=True)
            text_normalized = _normalize_text(link_text)
            if "facility" in text_normalized or "detail" in text_normalized:
                if href not in seen:
                    seen.add(href)
                    links.append(FacilityLink(name=link_text or "Unknown Facility", url=href))

    return links


def extract_final_title_v_permit_link(page_html: str, base_url: str) -> Optional[Tuple[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    table, table_is_title_v = find_title_v_permits_table(soup)
    if not table:
        return None

    candidates: List[Tuple[str, str, bool, int]] = []
    for row in table.find_all("tr"):
        row_text = _normalize_text(row.get_text(" ", strip=True))
        if not table_is_title_v and "title v" not in row_text:
            continue

        row_links: List[Tuple[str, str]] = []
        for anchor in row.find_all("a", href=True):
            href = anchor["href"].strip()
            if href and not href.lower().startswith("javascript"):
                full_href = urljoin(base_url, href)
                row_links.append((full_href, anchor.get_text(strip=True)))

        for element in row.find_all(["a", "button"]):
            pdf_url = _extract_pdf_from_onclick(element.get("onclick") or "", base_url)
            if pdf_url:
                row_links.append((pdf_url, element.get_text(strip=True)))

        if not row_links:
            continue

        is_final = "final" in row_text
        for link in row_links:
            link_text = _normalize_text(link[1])
            score = 0
            if "final" in link_text:
                score += 3
            if "permit" in link_text:
                score += 2
            if "title v" in link_text or "tv permit" in link_text:
                score += 2
            if link[0].lower().endswith(".pdf"):
                score += 1
            candidates.append((link[0], link[1] or "Title V Permit", is_final, score))

    if not candidates:
        return None

    final_links = [link for link in candidates if link[2]]
    ranked = sorted(final_links or candidates, key=lambda item: (item[3], item[2]))
    best = ranked[-1]
    return best[0], best[1]


def _extract_pdf_from_onclick(onclick: str, base_url: str) -> Optional[str]:
    if not onclick:
        return None
    match = re.search(r"(https?://[^'\"\\s]+\\.pdf)", onclick, re.IGNORECASE)
    if match:
        return match.group(1)
    rel_match = re.search(r"(['\"])([^'\"]+\\.pdf)\\1", onclick, re.IGNORECASE)
    if rel_match:
        return urljoin(base_url, rel_match.group(2))
    return None


def extract_title_v_pdf_links(page_html: str, base_url: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for row in soup.find_all("tr"):
        row_text = _normalize_text(row.get_text(" ", strip=True))
        if "title v" not in row_text:
            continue
        for anchor in row.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            if ".pdf" not in href.lower():
                continue
            if href in seen:
                continue
            seen.add(href)
            links.append((href, anchor.get_text(strip=True)))

        for element in row.find_all(["a", "button"]):
            onclick = element.get("onclick") or ""
            pdf_url = _extract_pdf_from_onclick(onclick, base_url)
            if pdf_url and pdf_url not in seen:
                seen.add(pdf_url)
                links.append((pdf_url, element.get_text(strip=True)))

    if not links:
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            text = _normalize_text(anchor.get_text(strip=True))
            if "title v" in text or "title v" in href.lower():
                if is_probable_pdf(href) and href not in seen:
                    seen.add(href)
                    links.append((href, anchor.get_text(strip=True)))

    if not links:
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            if href.lower().endswith(".pdf") and href not in seen:
                seen.add(href)
                links.append((href, anchor.get_text(strip=True)))

    return links


def go_to_facility_permits(driver) -> None:
    permits_xpaths = [
        "//a[normalize-space()='Permits']",
        "//area[contains(@title, 'Permits')]",
        "//area[contains(@alt, 'Permits')]",
        "//a[contains(normalize-space(.), 'Permits')]",
        "//span[contains(normalize-space(.), 'Permits')]/ancestor::a",
    ]
    for xpath in permits_xpaths:
        try:
            tab = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        except Exception:
            continue
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", tab)
            tab.click()
            _wait_for_body(driver)
            return
        except Exception:
            continue

    soup = BeautifulSoup(driver.page_source, "html.parser")
    for candidate in soup.find_all(["area", "a"]):
        label = " ".join(
            filter(None, [candidate.get("title"), candidate.get("alt"), candidate.get_text(strip=True)])
        )
        if "permits" not in _normalize_text(label):
            continue
        onclick = candidate.get("onclick") or ""
        match = re.search(r"submitForm\\('([^']+)'\\s*,\\s*\\d+\\s*,\\s*\\{source:'([^']+)'\\}\\)", onclick)
        if match:
            form_id, source_id = match.group(1), match.group(2)
            driver.execute_script(
                "if (typeof submitForm === 'function') { submitForm(arguments[0], 1, { source: arguments[1] }); }",
                form_id,
                source_id,
            )
            _wait_for_body(driver)
            return

    raise RuntimeError("Could not navigate to the facility Permits page.")


def _find_next_button(driver) -> Optional[object]:
    next_xpaths = [
        "//a[normalize-space()='Next']",
        "//button[normalize-space()='Next']",
        "//a[@title='Next']",
        "//button[@title='Next']",
    ]
    for xpath in next_xpaths:
        try:
            button = driver.find_element(By.XPATH, xpath)
        except Exception:
            continue
        try:
            if button.is_displayed() and button.is_enabled():
                return button
        except Exception:
            continue
    return None


def collect_facility_links(driver, base_url: str, max_pages: int = 10) -> List[FacilityLink]:
    all_links: List[FacilityLink] = []
    seen: Set[str] = set()

    for page_index in range(max_pages):
        page_links = extract_facility_links(driver.page_source, base_url)
        for link in page_links:
            if link.url in seen:
                continue
            seen.add(link.url)
            all_links.append(link)

        next_button = _find_next_button(driver)
        if not next_button:
            break

        logger.info(f"Moving to next results page ({page_index + 2}).")
        next_button.click()
        _wait_for_body(driver)
        time.sleep(1)

    return all_links


def _next_available_path(output_dir: Path, filename: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    idx = 2
    while True:
        candidate = output_dir / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def _download_job(job: DownloadJob, output_dir: Path, timeout_seconds: int = 60) -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": job.referer,
    }
    try:
        with requests.get(job.url, headers=headers, stream=True, timeout=timeout_seconds) as response:
            if response.status_code >= 400:
                logger.warning(f"HTTP {response.status_code} while downloading {job.url}")
                return False

            content_type = (response.headers.get("content-type") or "").lower()
            if "pdf" not in content_type and ".pdf" not in job.url.lower():
                logger.warning(f"Non-PDF response for {job.url} (content-type={content_type or 'unknown'})")
                return False

            target_path = _next_available_path(output_dir, job.save_as)
            with target_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            logger.info(f"Downloaded: {target_path.name} [PARALLEL]")
            return True
    except Exception as exc:
        logger.warning(f"Error downloading {job.url}: {exc}")
        return False


def _download_jobs_in_parallel(jobs: List[DownloadJob], output_dir: Path, workers: int) -> Tuple[int, int]:
    if not jobs:
        return 0, 0
    worker_count = max(1, workers)
    downloaded = 0
    failed = 0
    logger.info(f"Starting parallel download of {len(jobs)} files with {worker_count} workers.")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_download_job, job, output_dir) for job in jobs]
        for future in as_completed(futures):
            if future.result():
                downloaded += 1
            else:
                failed += 1
    return downloaded, failed


def download_title_v_permits(
    output_dir: Path,
    headless: bool = True,
    wait_seconds: int = 5,
    max_facilities: Optional[int] = None,
    max_pages: int = 10,
    facility_csv: Optional[Path] = None,
    download_workers: int = 6,
    download_batch_size: int = 20,
) -> None:
    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,
        use_llm=False,
    )

    try:
        driver = downloader.driver
        try:
            driver.get(SEARCH_URL)
            _wait_for_body(driver)
            accept_disclaimer_if_present(driver)
            try:
                wait_for_post_disclaimer_ready(driver, timeout=25)
            except Exception:
                debug_path = output_dir / "debug_disclaimer.html"
                debug_path.write_text(driver.page_source, encoding="utf-8")
                raise RuntimeError(
                    "Disclaimer acceptance did not transition pages. "
                    f"Saved HTML to {debug_path}."
                )
            go_to_facilities_tab(driver)
            wait_for_url_contains(driver, "facilitySearch.jsf", timeout=25)
            wait_for_search_page_ready(driver)

            if facility_csv:
                facility_ids = load_facility_ids(facility_csv)
                if max_facilities is not None:
                    facility_ids = facility_ids[:max_facilities]
            else:
                apply_title_v_filter(driver)
                click_search(driver)
                time.sleep(max(1, wait_seconds))
                facility_links = collect_facility_links(driver, SEARCH_URL, max_pages=max_pages)
        except Exception:
            debug_path = output_dir / "debug_last_page.html"
            debug_path.write_text(driver.page_source, encoding="utf-8")
            raise

        if facility_csv:
            logger.info(f"Found {len(facility_ids)} facility IDs to process.")
        else:
            if not facility_links:
                debug_path = output_dir / "debug_search_results.html"
                debug_path.write_text(driver.page_source, encoding="utf-8")
                raise RuntimeError(f"No facility links found. Saved HTML to {debug_path}.")

            if max_facilities is not None:
                facility_links = facility_links[:max_facilities]

            logger.info(f"Found {len(facility_links)} facility links to process.")
        downloaded = 0
        skipped = 0
        download_jobs: List[DownloadJob] = []

        def _flush_download_jobs(force: bool = False) -> None:
            nonlocal downloaded, skipped, download_jobs
            if not download_jobs:
                return
            if not force and len(download_jobs) < max(1, download_batch_size):
                return
            pending_count = len(download_jobs)
            logger.info(f"Flushing {pending_count} queued download jobs.")
            run_downloaded, run_failed = _download_jobs_in_parallel(
                jobs=download_jobs,
                output_dir=output_dir,
                workers=download_workers,
            )
            downloaded += run_downloaded
            skipped += run_failed
            download_jobs = []

        if facility_csv:
            search_page_url = driver.current_url
            for idx, facility_id in enumerate(facility_ids, start=1):
                logger.info(f"[{idx}/{len(facility_ids)}] Searching facility ID: {facility_id}")
                driver.get(search_page_url)
                _wait_for_body(driver)
                wait_for_search_page_ready(driver)

                set_facility_id(driver, facility_id)
                click_search(driver)
                time.sleep(max(1, wait_seconds))

                try:
                    open_facility_from_results(driver, facility_id)
                except Exception:
                    debug_path = output_dir / f"debug_results_{facility_id}.html"
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning(f"Could not open facility {facility_id} from results.")
                    skipped += 1
                    continue

                time.sleep(wait_seconds)
                try:
                    go_to_facility_permits(driver)
                except Exception as exc:
                    debug_path = output_dir / f"debug_facility_{facility_id}.html"
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning(f"Could not reach permits page for {facility_id}: {exc}")
                    skipped += 1
                    continue

                time.sleep(wait_seconds)
                try:
                    if search_permits_for_facility(driver, facility_id):
                        time.sleep(wait_seconds)
                except Exception as exc:
                    debug_path = output_dir / f"debug_permit_search_{facility_id}.html"
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning(f"Could not run permit search for {facility_id}: {exc}")
                    skipped += 1
                    continue

                permit = extract_final_title_v_permit_link(driver.page_source, driver.current_url)
                if not permit:
                    debug_path = output_dir / f"debug_permits_{facility_id}.html"
                    debug_path.write_text(driver.page_source, encoding="utf-8")
                    logger.warning(f"No Title V permit link found for {facility_id}")
                    skipped += 1
                    continue

                pdf_url, _link_text = permit
                filename = clean_filename(f"{facility_id} - Title V Final")
                if not filename.lower().endswith(".pdf"):
                    filename = f"{filename}.pdf"
                download_jobs.append(
                    DownloadJob(
                        url=pdf_url,
                        referer=driver.current_url,
                        save_as=filename,
                    )
                )
                _flush_download_jobs()
        else:
            for idx, facility in enumerate(facility_links, start=1):
                logger.info(f"[{idx}/{len(facility_links)}] Loading facility: {facility.name}")
                driver.get(facility.url)
                _wait_for_body(driver)
                time.sleep(wait_seconds)

                links = extract_title_v_pdf_links(driver.page_source, facility.url)
                if not links:
                    logger.warning(f"No Title V PDF links found for {facility.name}")
                    skipped += 1
                    continue

                for link_idx, (pdf_url, _link_text) in enumerate(links, start=1):
                    base_name = clean_filename(f"{facility.name} - Title V") if facility.name else "wyoming_title_v"
                    if len(links) > 1:
                        base_name = f"{base_name} {link_idx}"
                    if not base_name.lower().endswith(".pdf"):
                        base_name = f"{base_name}.pdf"
                    download_jobs.append(
                        DownloadJob(
                            url=pdf_url,
                            referer=facility.url,
                            save_as=base_name,
                        )
                    )
                    _flush_download_jobs()

        _flush_download_jobs(force=True)

        logger.info(f"Downloaded {downloaded} Title V permit PDFs.")
        if skipped:
            logger.info(f"Skipped or failed downloads: {skipped}")

    finally:
        downloader.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Wyoming IMPACT for Title V facilities and download permit PDFs."
    )
    default_output = Path(RAW_DATA_DIR) / "wyoming_title_v"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to store downloaded permits (default: {default_output}).",
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
        help="Seconds to wait for page rendering.",
    )
    parser.add_argument(
        "--max-facilities",
        type=int,
        default=None,
        help="Limit the number of facilities to process.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum number of result pages to scan.",
    )
    parser.add_argument(
        "--facility-csv",
        type=Path,
        default=Path("data/external/facilitySearch.xls.csv"),
        help="CSV file with Facility ID column to search.",
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=6,
        help="Number of parallel workers for downloading PDFs after links are discovered.",
    )
    parser.add_argument(
        "--download-batch-size",
        type=int,
        default=20,
        help="Flush queued downloads every N discovered files so downloads start before crawl completion.",
    )

    args = parser.parse_args()
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    download_title_v_permits(
        output_dir=output_dir,
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        max_facilities=args.max_facilities,
        max_pages=args.max_pages,
        facility_csv=args.facility_csv,
        download_workers=args.download_workers,
        download_batch_size=args.download_batch_size,
    )


if __name__ == "__main__":
    main()
