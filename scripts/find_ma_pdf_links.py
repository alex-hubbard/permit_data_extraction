#!/usr/bin/env python3
"""
Simple script to find and download all PDF links on the Massachusetts MassDEP Title V permit page.
"""

import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

BASE_URL = "https://www.mass.gov"
PERMIT_LISTING_URL = "https://www.mass.gov/lists/massachusetts-operating-permit-facilities"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
}




def find_all_pdf_links(headless: bool = True, wait_seconds: int = 4, download: bool = False, output_dir: Path = None):
    """
    Find all PDF links on the Massachusetts MassDEP Title V permit page.
    """
    # Use SeleniumPDFDownloader for better download handling (handles 403 errors)
    downloader = None
    if download:
        downloader = SeleniumPDFDownloader(
            output_dir=output_dir,
            headless=headless,
            wait_seconds=wait_seconds,
            max_depth=0,
            use_llm=False,
        )
        driver = downloader.driver
    else:
        # Set up Chrome options for just finding links
        chrome_options = Options()
        if headless:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        driver = webdriver.Chrome(options=chrome_options)

    try:
        logger.info(f"Loading page: {PERMIT_LISTING_URL}")
        driver.get(PERMIT_LISTING_URL)

        # Wait for the page to load
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # Give extra time for any dynamic content
        time.sleep(wait_seconds)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Debug: Save HTML for inspection
        debug_html_path = Path(RAW_DATA_DIR) / "massachusetts_title_v" / "debug_page_source.html"
        debug_html_path.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.debug(f"Saved page source to {debug_html_path}")

        # Find all document download links - look for links with /doc/.../download
        doc_download_links = soup.find_all("a", href=re.compile(r"/doc/.*/download", re.I))
        logger.info(f"Found {len(doc_download_links)} links with '/doc/.../download' pattern")

        # Also look for links with class "ma__download-link__file-link" (Mass.gov specific)
        ma_download_links = soup.find_all("a", class_=re.compile(r"download-link|file-link", re.I))
        logger.info(f"Found {len(ma_download_links)} links with download-link class")

        # Combine and deduplicate
        all_links = []
        seen_hrefs = set()

        for link in doc_download_links + ma_download_links:
            href = link.get("href", "")
            if not href:
                continue

            # Resolve relative URLs
            if not href.startswith("http"):
                href = urljoin(BASE_URL, href)

            # Only include links that have /doc/ and /download
            if "/doc/" not in href or "/download" not in href:
                continue

            if href not in seen_hrefs:
                seen_hrefs.add(href)
                link_text = link.get_text(strip=True)
                
                # Try to get facility name from parent elements or link text
                facility_name = link_text
                
                # Look for facility name in parent divs
                parent = link.find_parent()
                if parent:
                    # Try to find description or title in parent
                    desc = parent.find(class_=re.compile(r"description|title", re.I))
                    if desc:
                        desc_text = desc.get_text(strip=True)
                        if desc_text:
                            facility_name = desc_text
                
                # Extract facility name from link text if it has format "City: Facility Name"
                if ":" in link_text:
                    parts = link_text.split(":", 1)
                    if len(parts) > 1:
                        facility_name = parts[1].strip()
                        # Remove "(English, PDF ...)" part
                        facility_name = re.sub(r"\s*\(.*?\)\s*$", "", facility_name)
                
                # Extract document name from URL (e.g., "oak-bluffs-nrg-canal-llc-oak-bluffs-station" from /doc/oak-bluffs-nrg-canal-llc-oak-bluffs-station/download)
                doc_name = ""
                match = re.search(r"/doc/([^/]+)/download", href)
                if match:
                    doc_name = match.group(1)
                
                all_links.append({
                    "url": href,
                    "text": link_text,
                    "facility_name": facility_name,
                    "doc_name": doc_name,
                })

        logger.info(f"\nTotal unique PDF links found: {len(all_links)}")
        logger.info("\n" + "=" * 80)
        logger.info("PDF LINKS:")
        logger.info("=" * 80)

        for i, link_info in enumerate(all_links[:10], start=1):
            logger.info(f"\n{i}. {link_info['facility_name']}")
            logger.info(f"   URL: {link_info['url'][:80]}")

        # Download if requested
        if download and output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOADING PDFs...")
            logger.info("=" * 80)
            
            downloaded = 0
            failed = 0
            
            for i, link_info in enumerate(all_links, start=1):
                logger.info(f"\n[{i}/{len(all_links)}] {link_info['facility_name']}")
                logger.info(f"  URL: {link_info['url']}")
                
                # Use the document name from the URL (e.g., "oak-bluffs-nrg-canal-llc-oak-bluffs-station")
                if link_info.get('doc_name'):
                    filename = f"{link_info['doc_name']}.pdf"
                else:
                    # Extract from URL
                    match = re.search(r"/doc/([^/]+)/download", link_info['url'])
                    if match:
                        filename = f"{match.group(1)}.pdf"
                    else:
                        # Fallback to facility name
                        filename = f"{clean_filename(link_info['facility_name'])}.pdf"
                
                # Use the SeleniumPDFDownloader to download (handles 403 errors)
                if downloader:
                    success = downloader.download_document(
                        link_info['url'],
                        referer=PERMIT_LISTING_URL,
                        link_text=filename,
                        is_table_link=True,
                    )
                    # Rename the downloaded file to our desired filename
                    if success:
                        # Find the downloaded file and rename it
                        downloaded_files = list(output_dir.glob("*.pdf"))
                        if downloaded_files:
                            # Get the most recently created file
                            latest_file = max(downloaded_files, key=lambda p: p.stat().st_mtime)
                            if latest_file.name != filename:
                                target_path = output_dir / filename
                                if target_path.exists():
                                    target_path.unlink()
                                latest_file.rename(target_path)
                        downloaded += 1
                    else:
                        failed += 1
                else:
                    # Should not happen if download=True
                    failed += 1
                
                # Small delay between downloads
                time.sleep(0.5)
            
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOAD SUMMARY")
            logger.info("=" * 80)
            logger.info(f"Successfully downloaded: {downloaded}")
            logger.info(f"Failed: {failed}")
            logger.info(f"Output directory: {output_dir}")

        return all_links

    finally:
        if downloader:
            downloader.close()
        elif driver:
            driver.quit()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Find and download PDF links on Massachusetts MassDEP Title V permit page.")
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
        "--download",
        action="store_true",
        help="Download the Title V permit PDFs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(RAW_DATA_DIR) / "massachusetts_title_v",
        help=f"Directory to save downloaded PDFs (default: {Path(RAW_DATA_DIR) / 'massachusetts_title_v'}).",
    )

    args = parser.parse_args()

    pdf_links = find_all_pdf_links(
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        download=args.download,
        output_dir=args.output_dir.expanduser() if args.download else None,
    )

    logger.info(f"\n\nSummary:")
    logger.info(f"  Total PDF links found: {len(pdf_links)}")
    
    if not args.download:
        logger.info("\nUse --download to download the PDFs.")

