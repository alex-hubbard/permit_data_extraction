#!/usr/bin/env python3
"""
Generic Selenium-based permit downloader.

Given any webpage URL, this module will:
1. Load the page in a headless Chrome session.
2. Discover candidate permit/document links (default: PDF links that mention
   "permit" or user-supplied keywords in the link text or URL).
3. Download the discovered documents to the configured output directory.

It is designed to complement the specialized EPA downloader by providing a
fallback for arbitrary permit hubs that expose downloadable documents via
standard anchor tags.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

import typer
from loguru import logger
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from permit_data_extraction.config import RAW_DATA_DIR


DEFAULT_KEYWORDS = ["permit", "final", "air", "authorization", "license"]


class GenericPermitDownloader:
    """Discover and download permit PDFs from arbitrary webpages using Selenium."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        headless: bool = True,
        wait_seconds: int = 5,
        keywords: Optional[Sequence[str]] = None,
    ) -> None:
        self.output_dir = Path(output_dir or (RAW_DATA_DIR / "generic_permits")).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.download_dir = self.output_dir / "_temp_downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.headless = headless
        self.wait_seconds = wait_seconds
        self.keywords = [kw.lower() for kw in (keywords or DEFAULT_KEYWORDS)]

        self.driver: webdriver.Chrome = self._setup_driver()

    def _setup_driver(self) -> webdriver.Chrome:
        """Configure a Chrome driver that automatically downloads PDFs."""
        chrome_options = Options()
        if self.headless:
            # Using new flag prevents headless PDF viewer override issues on recent Chrome
            chrome_options.add_argument("--headless=new")

        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")

        prefs = {
            "download.default_directory": str(self.download_dir.absolute()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
            "safebrowsing.disable_download_protection": True,
            # Force Chrome to download PDFs instead of opening them in-viewer.
            "plugins.always_open_pdf_externally": True,
        }
        chrome_options.add_experimental_option("prefs", prefs)

        try:
            driver = webdriver.Chrome(options=chrome_options)
        except WebDriverException as exc:
            logger.error("Failed to initialize Chrome WebDriver: {}", exc)
            raise

        return driver

    def __del__(self) -> None:
        """Ensure that the Chrome driver shuts down cleanly."""
        try:
            if hasattr(self, "driver") and self.driver:
                self.driver.quit()
        except Exception:
            pass

    def _clear_temp_downloads(self) -> None:
        for path in self.download_dir.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _wait_for_download(self, timeout: int = 60) -> Optional[Path]:
        """Wait until Chrome finishes downloading a file."""
        elapsed = 0
        while elapsed < timeout:
            pdfs = [p for p in self.download_dir.glob("*") if p.suffix.lower() != ".crdownload"]
            partials = list(self.download_dir.glob("*.crdownload"))

            if pdfs and not partials:
                return pdfs[0]

            time.sleep(1)
            elapsed += 1

        return None

    @staticmethod
    def _normalize_url(base_url: str, href: str) -> Optional[str]:
        if not href:
            return None

        href = href.strip()
        if href.startswith("javascript:") or href.startswith("#"):
            return None

        return urljoin(base_url, href)

    def _matches_keywords(self, text: str) -> bool:
        lower_text = text.lower()
        return any(keyword in lower_text for keyword in self.keywords)

    def collect_candidate_links(self, page_url: str, include_all_pdfs: bool = False) -> List[dict]:
        """Return a list of candidate document links discovered on the page."""
        logger.info("Loading page: {}", page_url)
        self.driver.get(page_url)

        # Allow dynamic content to render.
        time.sleep(self.wait_seconds)

        links = self.driver.find_elements(By.TAG_NAME, "a")
        logger.info("Discovered {} anchor tags", len(links))

        candidates = []
        for element in links:
            href = element.get_attribute("href")
            url = self._normalize_url(page_url, href)
            if not url:
                continue

            link_text = (element.text or "").strip()
            candidate = {
                "text": link_text,
                "url": url,
            }

            if include_all_pdfs:
                if url.lower().endswith(".pdf"):
                    candidates.append(candidate)
                continue

            if url.lower().endswith(".pdf") and self._matches_keywords(url):
                candidates.append(candidate)
                continue

            if link_text and self._matches_keywords(link_text):
                candidates.append(candidate)

        logger.info("Filtered to {} candidate permit links", len(candidates))
        return candidates

    def download_link(self, link_url: str, target_filename: Optional[str] = None) -> dict:
        """Download a single document through Chrome."""
        self._clear_temp_downloads()
        logger.info("Attempting download: {}", link_url)

        result = {
            "url": link_url,
            "status": "failed",
            "filename": None,
            "output_path": None,
            "error": None,
        }

        try:
            self.driver.get(link_url)
            downloaded = self._wait_for_download()
            if not downloaded:
                result["error"] = "Download timed out or no file detected"
                return result

            filename = target_filename or downloaded.name
            output_path = self.output_dir / filename

            # Avoid overwriting existing files by appending a timestamp.
            if output_path.exists():
                timestamp = int(time.time())
                stem = output_path.stem
                suffix = output_path.suffix
                output_path = self.output_dir / f"{stem}_{timestamp}{suffix}"

            downloaded.replace(output_path)
            result.update(
                {
                    "status": "success",
                    "filename": output_path.name,
                    "output_path": str(output_path),
                }
            )
            logger.info("Downloaded {}", output_path)
            return result
        except WebDriverException as exc:
            result["error"] = str(exc)
            logger.warning("Download failed: {}", exc)
            return result

    def download_from_page(
        self,
        page_url: str,
        limit: Optional[int] = None,
        include_all_pdfs: bool = False,
    ) -> dict:
        """
        Discover candidate links on a page and download them.

        Args:
            page_url: Webpage to inspect.
            limit: Stop after downloading N documents (None = all candidates).
            include_all_pdfs: If True, grab every PDF link on the page regardless
                              of keywords.
        """
        candidates = self.collect_candidate_links(page_url, include_all_pdfs=include_all_pdfs)

        if limit is not None:
            candidates = candidates[:limit]

        summary = {
            "page_url": page_url,
            "total_candidates": len(candidates),
            "downloads": [],
        }

        for idx, candidate in enumerate(candidates, start=1):
            logger.info("Downloading candidate {}/{}: {}", idx, len(candidates), candidate["url"])
            parsed = urlparse(candidate["url"])
            default_name = os.path.basename(parsed.path) or f"permit_{idx}.pdf"
            result = self.download_link(candidate["url"], target_filename=default_name)
            summary["downloads"].append(result)

        summary["successful"] = sum(1 for r in summary["downloads"] if r["status"] == "success")
        summary["failed"] = sum(1 for r in summary["downloads"] if r["status"] != "success")
        return summary


app = typer.Typer(help="Discover and download permit PDFs from arbitrary webpages.")


def _split_keywords(keyword_values: Optional[Iterable[str]]) -> List[str]:
    if not keyword_values:
        return DEFAULT_KEYWORDS

    collected: List[str] = []
    for value in keyword_values:
        if not value:
            continue
        # Support comma-separated values in a single option.
        for item in value.split(","):
            cleaned = item.strip()
            if cleaned:
                collected.append(cleaned)

    return collected or DEFAULT_KEYWORDS


@app.command()
def download(
    url: str,
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Directory where downloaded permits will be stored",
    ),
    keyword: Optional[List[str]] = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Keyword(s) that link text or URLs should contain (can repeat, or comma separate).",
    ),
    include_all_pdfs: bool = typer.Option(
        False,
        "--all-pdfs",
        help="Download every PDF on the page regardless of keywords.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="Maximum number of documents to download.",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--no-headless",
        help="Run Chrome in headless mode.",
    ),
    wait_seconds: int = typer.Option(
        5,
        "--wait-seconds",
        help="Seconds to wait after initial page load to allow dynamic content.",
    ),
    summary_path: Optional[Path] = typer.Option(
        None,
        "--summary-path",
        help="Optional path to write a JSON summary report.",
    ),
) -> None:
    """Discover and download permit PDFs from the provided URL."""
    keywords = _split_keywords(keyword)

    downloader = GenericPermitDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        keywords=keywords,
    )

    summary = downloader.download_from_page(
        page_url=url,
        limit=limit,
        include_all_pdfs=include_all_pdfs,
    )

    typer.echo(f"Processed {summary['total_candidates']} candidates.")
    typer.echo(f"Successful downloads: {summary['successful']}")
    typer.echo(f"Failed downloads: {summary['failed']}")

    if summary_path:
        summary_path = summary_path.expanduser().resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        typer.echo(f"Summary written to {summary_path}")


if __name__ == "__main__":
    app()

