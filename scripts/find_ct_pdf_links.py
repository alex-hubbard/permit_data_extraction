#!/usr/bin/env python3
"""
Simple script to find and download all PDF links on the Connecticut DEEP Title V permit page.
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
from permit_data_extraction.pdf_downloader import clean_filename

BASE_URL = "https://portal.ct.gov"
PERMIT_LISTING_URL = "https://portal.ct.gov/deep/air/permits/title-v-operating-permit-program"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*",
}


def download_pdf(url: str, output_dir: Path, filename: str = None) -> bool:
    """
    Download a PDF from a URL.
    """
    try:
        if not filename:
            # Extract filename from URL
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path)
            if not filename or not filename.endswith(".pdf"):
                filename = "document.pdf"
        
        output_path = output_dir / clean_filename(filename)
        
        # Skip if already exists
        if output_path.exists():
            logger.debug(f"  Already exists: {output_path.name}")
            return True
        
        response = requests.get(url, headers=DEFAULT_HEADERS, stream=True, timeout=30)
        response.raise_for_status()
        
        # Check if it's actually a PDF
        content_type = response.headers.get("content-type", "").lower()
        if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
            logger.warning(f"  Not a PDF (content-type: {content_type}), skipping")
            return False
        
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        logger.info(f"  ✓ Downloaded: {output_path.name}")
        return True
        
    except Exception as e:
        logger.error(f"  ✗ Failed to download {url}: {e}")
        return False


def find_all_pdf_links(headless: bool = True, wait_seconds: int = 4, download: bool = False, output_dir: Path = None):
    """
    Find all PDF links on the Connecticut DEEP Title V permit page.
    """
    # Set up Chrome options
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
        import time
        time.sleep(wait_seconds)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Find all links with class="pdf-link"
        pdf_links = soup.find_all("a", class_="pdf-link")
        logger.info(f"Found {len(pdf_links)} links with class='pdf-link'")

        # Also find all links that contain .pdf in href
        all_pdf_hrefs = soup.find_all("a", href=re.compile(r"\.pdf", re.I))
        logger.info(f"Found {len(all_pdf_hrefs)} links with '.pdf' in href")

        # Combine and deduplicate
        all_links = []
        seen_hrefs = set()

        for link in pdf_links + all_pdf_hrefs:
            href = link.get("href", "")
            if not href:
                continue

            # Resolve relative URLs
            if not href.startswith("http"):
                href = urljoin(BASE_URL, href)

            if href not in seen_hrefs:
                seen_hrefs.add(href)
                link_text = link.get_text(strip=True)
                all_links.append({
                    "url": href,
                    "text": link_text,
                    "has_pdf_class": link.get("class") == ["pdf-link"],
                })

        logger.info(f"\nTotal unique PDF links found: {len(all_links)}")
        logger.info("\n" + "=" * 80)
        logger.info("PDF LINKS:")
        logger.info("=" * 80)

        for i, link_info in enumerate(all_links, start=1):
            logger.info(f"\n{i}. {link_info['text']}")
            logger.info(f"   URL: {link_info['url']}")
            logger.info(f"   Has pdf-link class: {link_info['has_pdf_class']}")

        # Filter for Title V permits (containing "-tv" or "-TV")
        title_v_links = [
            link for link in all_links
            if re.search(r"-tv", link["url"], re.I) or re.search(r"-tv", link["text"], re.I)
        ]

        logger.info("\n" + "=" * 80)
        logger.info(f"TITLE V PERMITS (containing '-tv' or '-TV'): {len(title_v_links)}")
        logger.info("=" * 80)

        for i, link_info in enumerate(title_v_links, start=1):
            logger.info(f"\n{i}. {link_info['text']}")
            logger.info(f"   URL: {link_info['url']}")

        # Download if requested
        if download and output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOADING PDFs...")
            logger.info("=" * 80)
            
            # Download Title V permits
            downloaded = 0
            failed = 0
            
            for i, link_info in enumerate(title_v_links, start=1):
                logger.info(f"\n[{i}/{len(title_v_links)}] {link_info['text']}")
                logger.info(f"  URL: {link_info['url']}")
                
                # Create filename from permit number
                permit_text = link_info['text'].strip()
                filename = f"{clean_filename(permit_text)}.pdf" if permit_text else None
                
                if download_pdf(link_info['url'], output_dir, filename):
                    downloaded += 1
                else:
                    failed += 1
                
                # Small delay between downloads
                time.sleep(0.5)
            
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOAD SUMMARY")
            logger.info("=" * 80)
            logger.info(f"Successfully downloaded: {downloaded}")
            logger.info(f"Failed: {failed}")
            logger.info(f"Output directory: {output_dir}")

        return all_links, title_v_links

    finally:
        driver.quit()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Find and download PDF links on CT DEEP Title V permit page.")
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
        default=Path(RAW_DATA_DIR) / "connecticut_title_v",
        help=f"Directory to save downloaded PDFs (default: {Path(RAW_DATA_DIR) / 'connecticut_title_v'}).",
    )

    args = parser.parse_args()

    all_links, title_v_links = find_all_pdf_links(
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        download=args.download,
        output_dir=args.output_dir.expanduser() if args.download else None,
    )

    logger.info(f"\n\nSummary:")
    logger.info(f"  Total PDF links: {len(all_links)}")
    logger.info(f"  Title V permits: {len(title_v_links)}")
    
    if not args.download:
        logger.info("\nUse --download to download the Title V permit PDFs.")

