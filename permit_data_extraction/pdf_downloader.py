import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import openai
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from loguru import logger
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry

from permit_data_extraction.config import RAW_DATA_DIR

# LLM Configuration (OpenAI-compatible API at cborg)
OPENAI_API_KEY = dotenv_values().get("CBORG_API_KEY") or os.getenv("CBORG_API_KEY")
LLM_MODEL = "lbl/cborg-deepthought"
LLM_ENABLED = OPENAI_API_KEY is not None

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _create_requests_session() -> requests.Session:
    retry_strategy = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[403, 408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def clean_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = re.sub(r"\s+", " ", filename).strip()
    return filename


def guess_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    filename = os.path.basename(path)
    return filename or "document"


def is_probable_pdf(url: str) -> bool:
    lower_url = url.lower()
    return lower_url.endswith(".pdf") or "permit" in lower_url


def is_permit_related_link_keywords(link_text: str, href: str) -> bool:
    text = link_text.lower()
    url = href.lower()
    return "permit" in text or "permit" in url


def evaluate_links_with_llm(
    links_data: List[Tuple[str, str, str, bool]],
    page_context: str = "",
) -> List[Tuple[str, str, float, str, bool]]:
    if not (LLM_ENABLED and links_data):
        return []

    try:
        payload_links = []
        for i, (url, link_text, context, is_table_link) in enumerate(links_data):
            payload_links.append(
                {
                    "id": i,
                    "url": url,
                    "link_text": link_text,
                    "context": context,
                    "in_table": is_table_link,
                }
            )

        prompt = f"""
You are analyzing links from a government or municipal website to identify which ones are likely to contain permits for SPECIFIC FACILITIES (like power plants, refineries, hospitals, manufacturing plants, etc.) rather than general permit information.

Page Context: {page_context[:500]}

Links to analyze:
{json.dumps(payload_links, indent=2)}

For each link, determine if it's likely to lead to permits or documents for a SPECIFIC NAMED FACILITY rather than general permit information, databases, or application forms.

Look for:
- Links that mention specific facility names (e.g., "Central Power Plant", "Memorial Hospital", "ABC Manufacturing")
- Permits for specific projects or locations
- Document repositories for particular facilities
- Links that suggest facility-specific regulatory documents

IMPORTANT: Give higher priority to links that are in tables (in_table: true), as these often contain structured permit data.

DO NOT select links that are:
- General permit application forms
- Permit databases or search pages
- General planning documents
- Municipal codes or regulations
- Generic permit information

Respond with a JSON array where each object has:
- "id": the link id number
- "likely_facility_specific": boolean (true if likely to contain permits for a specific facility)
- "confidence": float between 0.0 and 1.0
- "facility_name": string (name of the facility if identifiable, or "Unknown Facility" if not clear)
- "reasoning": brief explanation

Only return the JSON array, no other text.
"""

        client = openai.OpenAI(
            api_key=OPENAI_API_KEY,
            base_url="https://api.cborg.lbl.gov",
        )
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You analyze links from government or municipal websites. Always respond with a valid JSON array only, no other text.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1000,
            timeout=45,
        )

        if not response.choices:
            logger.warning("LLM returned no choices.")
            return []

        content = response.choices[0].message.content.strip()
        if not content:
            logger.warning("LLM returned empty content.")
            return []

        json_start = content.find("[")
        json_end = content.rfind("]")
        json_payload = (
            content[json_start : json_end + 1]
            if json_start != -1 and json_end != -1 and json_end > json_start
            else content
        )

        evaluations = json.loads(json_payload)
        permit_links: List[Tuple[str, str, float, str, bool]] = []
        for evaluation in evaluations:
            if evaluation.get("likely_facility_specific"):
                link_id = evaluation["id"]
                if link_id < len(links_data):
                    permit_links.append(
                        (
                            links_data[link_id][0],
                            links_data[link_id][1],
                            evaluation.get("confidence", 0.5),
                            evaluation.get("facility_name", "Unknown Facility"),
                            links_data[link_id][3],
                        )
                    )
        return permit_links
    except Exception as exc:
        logger.warning(f"LLM evaluation failed: {exc}")
        return []


def get_link_context(link_element, soup: BeautifulSoup) -> str:
    context_parts = []

    parent = link_element.parent
    if parent:
        parent_text = parent.get_text().strip()
        if parent_text and parent_text != link_element.get_text().strip():
            context_parts.append(parent_text[:200])

    prev_sibling = link_element.previous_sibling
    if prev_sibling and hasattr(prev_sibling, "get_text"):
        prev_text = prev_sibling.get_text().strip()
        if prev_text:
            context_parts.append(prev_text[-100:])

    next_sibling = link_element.next_sibling
    if next_sibling and hasattr(next_sibling, "get_text"):
        next_text = next_sibling.get_text().strip()
        if next_text:
            context_parts.append(next_text[:100])

    return " | ".join(context_parts)


@dataclass
class LinkCandidate:
    url: str
    text: str
    context: str
    in_table: bool


class SeleniumPDFDownloader:
    def __init__(
        self,
        output_dir: Path,
        headless: bool = True,
        wait_seconds: int = 4,
        max_depth: int = 2,
        use_llm: bool = True,
        user_agent: Optional[str] = None,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.temp_dir = self.output_dir / "_temp_downloads"
        self.temp_dir.mkdir(exist_ok=True)

        self.headless = headless
        self.wait_seconds = wait_seconds
        self.max_depth = max_depth
        self.use_llm = use_llm and LLM_ENABLED
        self.user_agent = user_agent

        self.visited_urls: Set[str] = set()
        self.session = _create_requests_session()
        self.driver = self._setup_driver()

    def _setup_driver(self) -> webdriver.Chrome:
        chrome_options = Options()
        if self.headless:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        if self.user_agent:
            chrome_options.add_argument(f"--user-agent={self.user_agent}")

        prefs = {
            "download.default_directory": str(self.temp_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
            "safebrowsing.disable_download_protection": True,
            "plugins.always_open_pdf_externally": True,
        }
        chrome_options.add_experimental_option("prefs", prefs)

        try:
            driver = webdriver.Chrome(options=chrome_options)
        except WebDriverException as exc:
            logger.error(f"Failed to initialize Chrome WebDriver: {exc}")
            raise
        return driver

    def close(self) -> None:
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

    def _clear_temp_downloads(self) -> None:
        for artifact in self.temp_dir.glob("*"):
            if artifact.is_file():
                artifact.unlink(missing_ok=True)

    def _wait_for_download(self, timeout: int = 60) -> Optional[Path]:
        elapsed = 0
        while elapsed < timeout:
            pdfs = [p for p in self.temp_dir.glob("*") if p.is_file() and not p.name.startswith(".")]
            pending = list(self.temp_dir.glob("*.crdownload"))
            if pdfs and not pending:
                return pdfs[0]
            if pdfs and not pending:
                return pdfs[0]
            time.sleep(1)
            elapsed += 1
        return None

    def _apply_driver_cookies_to_session(self) -> None:
        self.session.cookies.clear()
        for cookie in self.driver.get_cookies():
            try:
                self.session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))
            except Exception:
                continue

    @staticmethod
    def _find_pdf_link_in_html(html: bytes, base_url: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")

        # Meta refresh tag
        meta_refresh = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"})
        if meta_refresh:
            content = meta_refresh.get("content", "")
            if "url=" in content.lower():
                target = content.split("=", 1)[1].strip()
                if target:
                    resolved = urljoin(base_url, target)
                    if resolved.lower().endswith(".pdf"):
                        return resolved

        # Common tags that may embed PDF links
        for tag, attr in [("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data")]:
            for element in soup.find_all(tag):
                href = element.get(attr)
                if not href:
                    continue
                resolved = urljoin(base_url, href)
                if ".pdf" in resolved.lower():
                    return resolved
        return None

    def _download_pdf(
        self,
        pdf_url: str,
        referer: str,
        link_text: str,
        is_table_link: bool,
        attempt: int = 0,
    ) -> bool:
        if attempt > 3:
            logger.warning(f"Exceeded retry attempts while resolving {pdf_url}")
            return False

        probable_pdf = is_probable_pdf(pdf_url)

        self._clear_temp_downloads()

        downloaded_path: Optional[Path] = None

        try:
            self._apply_driver_cookies_to_session()
            response = self.session.get(
                pdf_url,
                headers={
                    "Referer": referer,
                    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                },
                stream=True,
                timeout=45,
            )

            if response.status_code == 403:
                logger.debug(f"Direct download blocked (403); attempting browser download for {pdf_url}")
                existing_handles = list(self.driver.window_handles)

                try:
                    self.driver.switch_to.new_window("tab")
                    self.driver.get(pdf_url)
                except Exception:
                    self.driver.execute_script("window.open(arguments[0], '_blank');", pdf_url)
                    time.sleep(1)
                    new_handles = self.driver.window_handles
                    target_handle = None
                    for handle in new_handles:
                        if handle not in existing_handles:
                            target_handle = handle
                            break
                    if target_handle:
                        self.driver.switch_to.window(target_handle)

                downloaded_path = self._wait_for_download()

                # Close the temporary tab/window if it still exists
                remaining_handles = self.driver.window_handles
                for handle in list(remaining_handles):
                    if handle not in existing_handles:
                        try:
                            self.driver.switch_to.window(handle)
                            self.driver.close()
                        except Exception:
                            logger.debug("Temporary download window already closed.")

                if existing_handles:
                    try:
                        self.driver.switch_to.window(existing_handles[0])
                    except Exception:
                        logger.debug("Original window already closed after download.")
                if not downloaded_path or not downloaded_path.exists():
                    resolved = self._resolve_pdf_via_browser(pdf_url)
                    if resolved and resolved != pdf_url:
                        return self._download_pdf(resolved, referer, link_text, is_table_link, attempt + 1)
                    return False
            else:
                content_type = response.headers.get("content-type", "").lower()
                if "application/pdf" not in content_type:
                    html_bytes = response.content
                    pdf_candidate = self._find_pdf_link_in_html(html_bytes, pdf_url)
                    if pdf_candidate and pdf_candidate != pdf_url:
                        logger.debug(f"Resolved HTML intermediate to PDF: {pdf_candidate}")
                        return self._download_pdf(pdf_candidate, referer, link_text, is_table_link, attempt + 1)
                    resolved = self._resolve_pdf_via_browser(pdf_url)
                    if resolved and resolved != pdf_url:
                        logger.debug(f"Browser resolved PDF URL: {resolved}")
                        return self._download_pdf(resolved, referer, link_text, is_table_link, attempt + 1)
                    logger.debug(f"Non-PDF content returned from {pdf_url}; skipping.")
                    return False

                filename = self._select_filename(pdf_url, response.headers, link_text)
                downloaded_path = self._prepare_temp_path(filename)

                with open(downloaded_path, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:
            logger.warning(f"Error downloading {pdf_url}: {exc}")
            return False

        if not downloaded_path or not downloaded_path.exists():
            logger.warning(f"Download did not complete for {pdf_url}")
            return False

        final_name = downloaded_path.name
        final_path = self._prepare_final_path(final_name)
        downloaded_path.replace(final_path)

        tag = " [TABLE]" if is_table_link else ""
        logger.info(f"Downloaded: {final_path.name}{tag}")
        return True

    def _resolve_pdf_via_browser(self, url: str) -> Optional[str]:
        """Open the URL in a temporary browser tab and try to discover an embedded PDF resource."""
        existing_handles = list(self.driver.window_handles)
        try:
            self.driver.switch_to.new_window("tab")
        except Exception:
            self.driver.execute_script("window.open('about:blank','_blank');")
            time.sleep(1)
            new_handles = self.driver.window_handles
            self.driver.switch_to.window(new_handles[-1])

        try:
            self.driver.get(url)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//iframe[@src]|//embed[@src]|//object[@data]|//a[contains(@href, '.pdf') or contains(@href, 'document')]",
                    )
                )
            )
        except Exception:
            pass

        resolved_url: Optional[str] = None
        for locator, attr in [
            ((By.TAG_NAME, "embed"), "src"),
            ((By.TAG_NAME, "iframe"), "src"),
            ((By.TAG_NAME, "object"), "data"),
        ]:
            try:
                element = self.driver.find_element(*locator)
                candidate = element.get_attribute(attr)
                if candidate:
                    resolved_url = urljoin(url, candidate)
                    break
            except Exception:
                continue

        if not resolved_url:
            try:
                download_link = self.driver.find_element(By.XPATH, "//a[contains(@href, '.pdf') or contains(@href, 'document')]")
                href = download_link.get_attribute("href")
                if href:
                    resolved_url = urljoin(url, href)
            except Exception:
                pass

        try:
            current_url = self.driver.current_url
            if current_url and current_url != "data:" and current_url != url:
                resolved_url = current_url
        except Exception:
            pass

        # Close temporary tabs/windows
        new_handles = list(self.driver.window_handles)
        for handle in new_handles:
            if handle not in existing_handles:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except Exception:
                    logger.debug("Temporary resolution window already closed.")

        if existing_handles:
            try:
                self.driver.switch_to.window(existing_handles[0])
            except Exception:
                logger.debug("Original window not available after resolution.")

        return resolved_url

    def _select_filename(self, url: str, headers: dict, link_text: str) -> str:
        disposition = headers.get("content-disposition", "")
        filename = ""
        if "filename=" in disposition.lower():
            parts = disposition.split(";")
            for part in parts:
                if "filename=" in part.lower():
                    filename = part.split("=", 1)[1].strip().strip("\"'")
                    break

        if not filename:
            parsed_name = os.path.basename(urlparse(url).path)
            if parsed_name:
                filename = parsed_name

        if not filename and link_text:
            filename = link_text

        if not filename:
            filename = "document.pdf"

        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

        return filename

    def _prepare_temp_path(self, filename: str) -> Path:
        return self.temp_dir / filename

    def _prepare_final_path(self, filename: str) -> Path:
        return self.output_dir / filename

    def download_document(
        self, doc_url: str, referer: str, link_text: str = "", is_table_link: bool = False
    ) -> bool:
        """
        Public wrapper for downloading a specific document URL using the current browser session.
        """
        return self._download_pdf(doc_url, referer, link_text, is_table_link)

    def _extract_links(self, soup: BeautifulSoup) -> Tuple[List[LinkCandidate], List[LinkCandidate]]:
        navigation_selectors = [
            "nav",
            ".nav",
            ".navigation",
            ".menu",
            ".navbar",
            ".header",
            ".footer",
            ".sidebar",
            ".breadcrumb",
            ".panel-sidebar",
            ".rail",
            ".left-rail",
            ".right-rail",
            ".quick-links",
            ".related-links",
            ".see-also",
            ".in-this-section",
        ]

        for selector in navigation_selectors:
            for element in soup.select(selector):
                element.decompose()

        table_links: List[LinkCandidate] = []
        other_links: List[LinkCandidate] = []

        for link in soup.find_all("a"):
            href = link.get("href")
            if not href:
                continue

            context = get_link_context(link, soup)
            candidate = LinkCandidate(
                url=href,
                text=link.get_text().strip(),
                context=context,
                in_table=bool(link.find_parent("table")),
            )

            if candidate.in_table:
                table_links.append(candidate)
            else:
                other_links.append(candidate)

        return table_links, other_links

    def _process_page(self, url: str, depth: int) -> int:
        if url in self.visited_urls:
            return 0

        logger.info(f"{'  ' * depth}Processing: {url}")
        self.visited_urls.add(url)

        try:
            self.driver.get(url)
        except Exception as exc:
            logger.warning(f"{'  ' * depth}Failed to load {url}: {exc}")
            return 0

        try:
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            logger.debug(f"{'  ' * depth}Timed out waiting for body tag on {url}")

        time.sleep(self.wait_seconds)

        current_domain = urlparse(url).netloc
        soup = BeautifulSoup(self.driver.page_source, "html.parser")

        table_links, other_links = self._extract_links(soup)
        logger.info(
            f"{'  ' * depth}Found {len(table_links)} table links and {len(other_links)} other content links"
        )

        download_count = 0
        permit_links: List[Tuple[str, str, bool]] = []
        llm_candidates: List[Tuple[str, str, str, bool]] = []

        for candidate in table_links + other_links:
            full_url = urljoin(url, candidate.url)

            if urlparse(full_url).netloc != current_domain:
                continue

            should_try_download = is_probable_pdf(full_url) or candidate.in_table
            if should_try_download:
                success = self._download_pdf(full_url, url, candidate.text, candidate.in_table)
                if success:
                    download_count += 1
                    time.sleep(1)
                    continue

            if depth < self.max_depth and full_url not in self.visited_urls:
                if is_permit_related_link_keywords(candidate.text, candidate.url):
                    permit_links.append((full_url, candidate.text, candidate.in_table))
                    logger.debug(
                        f"{'  ' * depth}Keyword permit link: {candidate.text[:60]}"
                        f"{' [TABLE]' if candidate.in_table else ''}"
                    )
                elif self.use_llm:
                    llm_candidates.append(
                        (full_url, candidate.text, candidate.context, candidate.in_table)
                    )

        if self.use_llm and llm_candidates and depth < self.max_depth:
            logger.info(f"{'  ' * depth}Sending {len(llm_candidates)} links to LLM for scoring…")
            page_title = soup.title.string if soup.title else ""
            llm_results = evaluate_links_with_llm(llm_candidates, f"Page title: {page_title}")
            for url_link, link_text, confidence, facility_name, in_table in llm_results:
                if confidence >= 0.6:
                    permit_links.append((url_link, link_text, in_table))
                    logger.info(
                        f"{'  ' * depth}LLM identified {facility_name} via '{link_text[:60]}' "
                        f"(confidence {confidence:.2f})"
                    )

        if depth < self.max_depth and permit_links:
            permit_links.sort(key=lambda item: (not item[2], item[0]))
            logger.info(f"{'  ' * depth}Exploring {len(permit_links)} deeper permit pages…")
            for link_url, link_text, in_table in permit_links:
                logger.info(
                    f"{'  ' * depth}→ Following: {link_text[:70]}"
                    f"{' [TABLE]' if in_table else ''}"
                )
                download_count += self._process_page(link_url, depth + 1)
                time.sleep(2)

        return download_count

    def crawl(self, start_url: str) -> int:
        total_downloaded = self._process_page(start_url, depth=0)
        return total_downloaded


def download_pdf(
    url: str,
    output_dir: str = f"{RAW_DATA_DIR}/downloaded_pdfs",
    max_depth: int = 2,
    use_llm: bool = True,
    headless: bool = True,
    wait_seconds: int = 4,
) -> int:
    output_path = Path(output_dir)
    downloader = SeleniumPDFDownloader(
        output_dir=output_path,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=max_depth,
        use_llm=use_llm,
    )

    logger.info(f"Starting PDF download from: {url}")
    logger.info(f"Maximum link depth: {max_depth}")
    logger.info("Target: Facility-specific permit documents only")
    if downloader.use_llm:
        logger.info(f"LLM-enhanced facility detection ENABLED ({LLM_MODEL})")
    elif use_llm and not LLM_ENABLED:
        logger.info("LLM-enhanced facility detection DISABLED (CBORG_API_KEY missing)")
    else:
        logger.info("LLM-enhanced facility detection DISABLED")
    logger.info("-" * 60)

    try:
        total_downloaded = downloader.crawl(url)
    finally:
        downloader.close()

    logger.info("-" * 60)
    logger.info(f"Download complete! Downloaded {total_downloaded} PDF files to {output_path}")
    logger.info(f"Visited {len(downloader.visited_urls)} unique URLs")

    return total_downloaded


def download_pdfs_from_csv(
    csv_path: str,
    output_dir: str = f"{RAW_DATA_DIR}/downloaded_pdfs",
    max_depth: int = 2,
    use_llm: bool = True,
    url_column: str = "url",
    headless: bool = True,
    wait_seconds: int = 4,
) -> dict:
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        logger.error(f"Error reading CSV file {csv_path}: {exc}")
        return {}

    if url_column not in df.columns:
        logger.error(f"Column '{url_column}' not found in CSV. Available: {list(df.columns)}")
        return {}

    urls = df[url_column].dropna().unique()
    logger.info(f"Found {len(urls)} unique URLs in CSV ({csv_path})")

    results = {}
    total_downloaded = 0

    for idx, url in enumerate(urls, start=1):
        logger.info("=" * 60)
        logger.info(f"Processing URL {idx}/{len(urls)}: {url}")

        safe_dir_name = re.sub(r'[<>:"/\\|?*]', "", url.replace("://", "_").replace("/", "_"))
        url_output_dir = Path(output_dir) / safe_dir_name

        try:
            count = download_pdf(
                url,
                output_dir=url_output_dir,
                max_depth=max_depth,
                use_llm=use_llm,
                headless=headless,
                wait_seconds=wait_seconds,
            )
            results[url] = {
                "status": "success",
                "downloaded": count,
                "output_dir": str(url_output_dir),
            }
            total_downloaded += count
        except Exception as exc:
            logger.error(f"Error processing {url}: {exc}")
            results[url] = {
                "status": "error",
                "error": str(exc),
                "downloaded": 0,
                "output_dir": str(url_output_dir),
            }

        if idx < len(urls):
            logger.info("Waiting 5 seconds before next URL…")
            time.sleep(5)

    logger.info("=" * 60)
    logger.info("BATCH DOWNLOAD SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total URLs processed: {len(urls)}")
    logger.info(f"Total PDFs downloaded: {total_downloaded}")
    logger.info(f"Output directory: {output_dir}")

    success_count = sum(1 for result in results.values() if result["status"] == "success")
    logger.info(f"Successful downloads: {success_count}")
    logger.info(f"Failed downloads: {len(results) - success_count}")

    if success_count != len(results):
        logger.info("Failed URLs:")
        for url, result in results.items():
            if result["status"] == "error":
                logger.info(f"  - {url}: {result['error']}")

    return results


if __name__ == "__main__":
    print("PDF Downloader - Choose input method:")
    print("1. Single URL")
    print("2. CSV file with URLs")

    choice = input("Enter your choice (1 or 2): ").strip()

    use_llm = False
    if LLM_ENABLED:
        use_llm_input = input("Use LLM to identify facility-specific permits? (y/n, default=y): ").lower()
        use_llm = use_llm_input != "n"
    else:
        print("LLM not available (set CBORG_API_KEY environment variable or .env to enable)")

    try:
        max_depth = int(
            input("Enter maximum link depth to follow (0=no following, 1=one level, 2=two levels, etc.): ") or "2"
        )
    except ValueError:
        max_depth = 2
        print("Invalid input, using default depth of 2")

    headless_choice = input("Run browser headless? (y/n, default=y): ").strip().lower()
    headless = headless_choice != "n"

    wait_seconds_input = input("Seconds to wait for page rendering (default=4): ").strip()
    try:
        wait_seconds = int(wait_seconds_input) if wait_seconds_input else 4
    except ValueError:
        wait_seconds = 4
        print("Invalid wait seconds, using default 4")

    if choice == "2":
        csv_path = input("Enter the path to your CSV file: ").strip()
        if not csv_path:
            print("No CSV path provided. Exiting.")
            raise SystemExit(0)

        url_column = input("Enter the name of the column containing URLs (default: 'url'): ").strip() or "url"
        download_pdfs_from_csv(
            csv_path,
            max_depth=max_depth,
            use_llm=use_llm,
            url_column=url_column,
            headless=headless,
            wait_seconds=wait_seconds,
        )
    else:
        website_url = input("Enter the website URL to download PDFs from: ").strip()
        if not website_url:
            print("No URL provided. Exiting.")
            raise SystemExit(0)

        download_pdf(
            website_url,
            max_depth=max_depth,
            use_llm=use_llm,
            headless=headless,
            wait_seconds=wait_seconds,
        )

