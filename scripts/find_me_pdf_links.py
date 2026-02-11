#!/usr/bin/env python3
"""
Simple script to find and download all PDF links on the Maine DEP Title V permit page.
Handles pagination to get all permits from all pages.
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

BASE_URL = "https://www.maine.gov"
PERMIT_LISTING_URL = "https://www.maine.gov/tools/whatsnew/index.php?topic=DEP+Title+V"

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


def extract_pdf_links_from_page(soup: BeautifulSoup) -> list:
    """
    Extract all PDF links from the current page's table.
    """
    pdf_links = []
    
    # Find the main table with permit information
    tables = soup.find_all("table")
    
    for table in tables:
        # Look for rows in the table
        rows = table.find_all("tr")
        
        for row in rows:
            # Find all links in the row
            links = row.find_all("a", href=re.compile(r"\.pdf", re.I))
            
            for link in links:
                href = link.get("href", "")
                if not href:
                    continue
                
                # Resolve relative URLs
                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)
                
                link_text = link.get_text(strip=True)
                
                # Get additional info from the row
                cells = row.find_all(["td", "th"])
                facility_name = ""
                license_number = link_text
                date_effective = ""
                
                if len(cells) >= 4:
                    facility_name = cells[0].get_text(strip=True)
                    license_number = link_text or cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    date_effective = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                
                pdf_links.append({
                    "url": href,
                    "license_number": license_number,
                    "facility_name": facility_name,
                    "date_effective": date_effective,
                })
    
    return pdf_links


def get_all_pages(driver, base_url: str) -> list:
    """
    Get all page URLs by finding pagination links.
    """
    pages = [base_url]  # Start with the first page
    
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Find pagination links - look for links with page numbers
        # The pagination shows "Previous 1 2 3 4 5 6 7 8 Next"
        pagination_links = soup.find_all("a", href=re.compile(r"page=", re.I))
        
        seen_pages = {base_url}
        for link in pagination_links:
            href = link.get("href", "")
            if not href:
                continue
            
            # Resolve relative URLs
            if not href.startswith("http"):
                href = urljoin(BASE_URL, href)
            
            # Check if it's a page number link (not "Previous" or "Next")
            link_text = link.get_text(strip=True)
            if link_text.isdigit() and href not in seen_pages:
                pages.append(href)
                seen_pages.add(href)
        
        # Also try to find page links by looking for numbered links
        # Sometimes pagination uses different patterns
        all_links = soup.find_all("a")
        for link in all_links:
            href = link.get("href", "")
            link_text = link.get_text(strip=True)
            
            # Look for links that might be page numbers
            if link_text.isdigit() and "1" <= link_text <= "20":  # Reasonable page range
                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)
                
                if "topic=DEP" in href and href not in seen_pages:
                    pages.append(href)
                    seen_pages.add(href)
        
        # Sort pages to ensure correct order
        pages = sorted(set(pages))
        
    except Exception as e:
        logger.warning(f"Error finding pagination: {e}")
    
    return pages


def find_all_pdf_links(headless: bool = True, wait_seconds: int = 4, download: bool = False, output_dir: Path = None):
    """
    Find all PDF links on the Maine DEP Title V permit page.
    Handles pagination to get all permits from all pages.
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
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))

        # Give extra time for any dynamic content
        time.sleep(wait_seconds)

        # Get all page URLs
        logger.info("Finding all pages...")
        all_pages = get_all_pages(driver, PERMIT_LISTING_URL)
        logger.info(f"Found {len(all_pages)} pages to process")

        all_pdf_links = []
        
        # Process each page
        for page_num, page_url in enumerate(all_pages, start=1):
            logger.info(f"\n{'=' * 80}")
            logger.info(f"Processing page {page_num}/{len(all_pages)}: {page_url}")
            logger.info("=" * 80)
            
            # Navigate to the page
            driver.get(page_url)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
            time.sleep(1)  # Brief pause for page to render
            
            soup = BeautifulSoup(driver.page_source, "html.parser")
            
            # Extract PDF links from this page
            page_pdf_links = extract_pdf_links_from_page(soup)
            logger.info(f"Found {len(page_pdf_links)} PDF links on page {page_num}")
            
            all_pdf_links.extend(page_pdf_links)
            
            # Show sample links
            if page_pdf_links:
                for i, link_info in enumerate(page_pdf_links[:3], start=1):
                    logger.info(f"  {i}. {link_info['license_number']} - {link_info['facility_name']}")

        logger.info(f"\n{'=' * 80}")
        logger.info(f"TOTAL PDF LINKS FOUND: {len(all_pdf_links)}")
        logger.info("=" * 80)

        for i, link_info in enumerate(all_pdf_links[:10], start=1):
            logger.info(f"\n{i}. {link_info['license_number']}")
            logger.info(f"   Facility: {link_info['facility_name']}")
            logger.info(f"   URL: {link_info['url']}")

        # Download if requested
        if download and output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOADING PDFs...")
            logger.info("=" * 80)
            
            downloaded = 0
            failed = 0
            
            for i, link_info in enumerate(all_pdf_links, start=1):
                logger.info(f"\n[{i}/{len(all_pdf_links)}] {link_info['license_number']}")
                logger.info(f"  Facility: {link_info['facility_name']}")
                logger.info(f"  URL: {link_info['url']}")
                
                # Use the filename from the URL (keep original name)
                parsed = urlparse(link_info['url'])
                filename = os.path.basename(parsed.path)
                if not filename or not filename.endswith(".pdf"):
                    # Fallback to license number
                    filename = f"{link_info['license_number']}.pdf"
                
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

        return all_pdf_links

    finally:
        driver.quit()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Find and download PDF links on Maine DEP Title V permit page.")
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
        default=Path(RAW_DATA_DIR) / "maine_title_v",
        help=f"Directory to save downloaded PDFs (default: {Path(RAW_DATA_DIR) / 'maine_title_v'}).",
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

