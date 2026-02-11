#!/usr/bin/env python3
"""
Simple script to find and download all PDF links on the Vermont DEC Title V permit page.
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

BASE_URL = "https://dec.vermont.gov"
PERMIT_LISTING_URL = "https://dec.vermont.gov/air-quality/permits/current-operating-permits-title-v-subject-sources-and-or-major-nsr-construction"

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


def find_pdf_on_document_page(driver, document_url: str) -> str:
    """
    Visit a document page and find the PDF link on that page.
    Returns the PDF URL or None if not found.
    """
    try:
        logger.debug(f"  Visiting document page: {document_url}")
        driver.get(document_url)
        
        # Wait for page to load
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(1)  # Give a moment for dynamic content
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Look for PDF links - try multiple patterns
        pdf_links = []
        
        # Pattern 1: Direct PDF links
        pdf_links.extend(soup.find_all("a", href=re.compile(r"\.pdf", re.I)))
        
        # Pattern 2: Links with PDF-related classes
        pdf_links.extend(soup.find_all("a", class_=re.compile(r"pdf|download|file", re.I)))
        
        # Pattern 3: Links with text containing "PDF" or "Download"
        for link in soup.find_all("a"):
            text = link.get_text(strip=True).lower()
            if "pdf" in text or "download" in text:
                href = link.get("href", "")
                if href and ".pdf" in href.lower():
                    pdf_links.append(link)
        
        # Pattern 4: Look for iframe or embed with PDF
        for iframe in soup.find_all(["iframe", "embed"]):
            src = iframe.get("src", "")
            if src and ".pdf" in src.lower():
                pdf_links.append(iframe)
        
        # Deduplicate and return first valid PDF URL
        seen = set()
        for link in pdf_links:
            if hasattr(link, "get"):
                href = link.get("href") or link.get("src", "")
            else:
                href = str(link)
            
            if href and href not in seen:
                seen.add(href)
                # Resolve relative URLs
                if not href.startswith("http"):
                    href = urljoin(document_url, href)
                
                if ".pdf" in href.lower():
                    return href
        
        return None
        
    except Exception as e:
        logger.debug(f"  Error visiting document page: {e}")
        return None


def find_all_pdf_links(headless: bool = True, wait_seconds: int = 4, download: bool = False, output_dir: Path = None):
    """
    Find all PDF links on the Vermont DEC Title V permit page.
    First finds document links, then visits each to find the PDF.
    """
    # Set up Chrome options with better anti-detection settings
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # Better user agent
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=chrome_options)
    
    # Execute script to remove webdriver property
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': '''
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            })
        '''
    })

    try:
        logger.info(f"Loading page: {PERMIT_LISTING_URL}")
        
        # Try to load the page
        driver.get(PERMIT_LISTING_URL)

        # Wait for the page to load
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # Give extra time for any dynamic content and to avoid rate limiting
        time.sleep(wait_seconds + 2)  # Add extra delay
        
        # Check if we got blocked
        page_source = driver.page_source
        if "403 ERROR" in page_source or "Request blocked" in page_source or "CloudFront" in page_source:
            logger.warning("Page appears to be blocked (403 error). Trying with longer delay...")
            time.sleep(5)
            driver.get(PERMIT_LISTING_URL)
            time.sleep(wait_seconds + 5)
            page_source = driver.page_source
            
            if "403 ERROR" in page_source or "Request blocked" in page_source:
                logger.error("Still getting 403 error. The site may be blocking automated access.")
                logger.error("Try running with --no-headless to use a visible browser, or increase --wait-seconds")
                return [], []

        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Debug: Save HTML for inspection
        debug_html_path = Path(RAW_DATA_DIR) / "vermont_title_v" / "debug_page_source.html"
        debug_html_path.parent.mkdir(parents=True, exist_ok=True)
        with open(debug_html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.debug(f"Saved page source to {debug_html_path}")
        
        # Debug: Count tables
        all_tables = soup.find_all("table")
        logger.info(f"Found {len(all_tables)} tables on the page")
        
        # Find all links to document pages (e.g., /document/aop12005)
        # Try multiple patterns
        document_links = soup.find_all("a", href=re.compile(r"/document/", re.I))
        logger.info(f"Found {len(document_links)} links with '/document/' in href")
        
        # Also look for links in tables
        table_links = []
        for table in all_tables:
            for link in table.find_all("a"):
                href = link.get("href", "")
                if href:
                    table_links.append(link)
        logger.info(f"Found {len(table_links)} total links in tables")
        
        # Check for direct PDF links in tables
        pdf_links_in_tables = []
        for table in all_tables:
            for link in table.find_all("a", href=re.compile(r"\.pdf", re.I)):
                pdf_links_in_tables.append(link)
        logger.info(f"Found {len(pdf_links_in_tables)} direct PDF links in tables")
        
        # Combine and deduplicate
        all_doc_links = []
        seen_hrefs = set()
        for link in document_links + table_links:
            href = link.get("href", "")
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                all_doc_links.append(link)
        
        logger.info(f"Found {len(all_doc_links)} unique links to check")
        
        # Show sample links
        if all_doc_links:
            logger.info("Sample links found:")
            for i, link in enumerate(all_doc_links[:10]):  # Show more samples
                href = link.get("href", "")
                text = link.get_text(strip=True)
                logger.info(f"  {i+1}. Text: '{text[:50]}', Href: '{href[:80]}'")
        
        # If we found direct PDF links in tables, use those instead
        if pdf_links_in_tables:
            logger.info(f"\nFound {len(pdf_links_in_tables)} direct PDF links in tables - using those!")
            document_links = pdf_links_in_tables
        else:
            document_links = all_doc_links

        # Deduplicate document URLs
        document_urls = []
        seen_docs = set()
        
        for link in document_links:
            href = link.get("href", "")
            if not href:
                continue
            
            # Resolve relative URLs
            if not href.startswith("http"):
                href = urljoin(BASE_URL, href)
            
            if href not in seen_docs:
                seen_docs.add(href)
                link_text = link.get_text(strip=True)
                document_urls.append({
                    "url": href,
                    "text": link_text,
                })

        logger.info(f"Found {len(document_urls)} unique document pages")
        logger.info("\n" + "=" * 80)
        logger.info("DOCUMENT PAGES:")
        logger.info("=" * 80)

        for i, doc_info in enumerate(document_urls, start=1):
            logger.info(f"\n{i}. {doc_info['text']}")
            logger.info(f"   URL: {doc_info['url']}")

        # Check if we have direct PDF links or need to visit document pages
        pdf_links = []
        
        # If any of the links are direct PDFs, use them directly
        direct_pdfs = [doc for doc in document_urls if doc['url'].lower().endswith('.pdf') or '.pdf' in doc['url'].lower()]
        
        if direct_pdfs:
            logger.info("\n" + "=" * 80)
            logger.info(f"FOUND {len(direct_pdfs)} DIRECT PDF LINKS - NO NEED TO VISIT PAGES")
            logger.info("=" * 80)
            
            for doc_info in direct_pdfs:
                pdf_links.append({
                    "url": doc_info['url'],
                    "text": doc_info['text'],
                    "document_url": doc_info['url'],
                })
        else:
            # Visit each document page to find PDF links
            logger.info("\n" + "=" * 80)
            logger.info("FINDING PDF LINKS ON DOCUMENT PAGES...")
            logger.info("=" * 80)

            for i, doc_info in enumerate(document_urls, start=1):
                logger.info(f"\n[{i}/{len(document_urls)}] {doc_info['text']}")
                pdf_url = find_pdf_on_document_page(driver, doc_info['url'])
                
                if pdf_url:
                    logger.info(f"  ✓ Found PDF: {pdf_url}")
                    pdf_links.append({
                        "url": pdf_url,
                        "text": doc_info['text'],
                        "document_url": doc_info['url'],
                    })
                else:
                    logger.warning(f"  ✗ No PDF found on this page")

        logger.info("\n" + "=" * 80)
        logger.info(f"TITLE V PERMITS (PDFs found): {len(pdf_links)}")
        logger.info("=" * 80)

        for i, link_info in enumerate(pdf_links, start=1):
            logger.info(f"\n{i}. {link_info['text']}")
            logger.info(f"   PDF URL: {link_info['url']}")
            logger.info(f"   Document URL: {link_info['document_url']}")

        # Download if requested
        if download and output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info("\n" + "=" * 80)
            logger.info("DOWNLOADING PDFs...")
            logger.info("=" * 80)
            
            # Download PDFs
            downloaded = 0
            failed = 0
            
            for i, link_info in enumerate(pdf_links, start=1):
                logger.info(f"\n[{i}/{len(pdf_links)}] {link_info['text']}")
                logger.info(f"  PDF URL: {link_info['url']}")
                
                # Use the original filename from the URL, not the link text
                # Extract from document URL (e.g., aop12005 from /document/aop12005)
                doc_url = link_info['document_url']
                match = re.search(r"/document/([^/]+)", doc_url)
                if match:
                    filename = f"{match.group(1)}.pdf"
                else:
                    # Fallback to PDF URL filename
                    parsed = urlparse(link_info['url'])
                    filename = os.path.basename(parsed.path) or "vermont_permit.pdf"
                    if not filename.endswith(".pdf"):
                        filename += ".pdf"
                
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

        return document_urls, pdf_links

    finally:
        driver.quit()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Find and download PDF links on VT DEC Title V permit page.")
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
        default=Path(RAW_DATA_DIR) / "vermont_title_v",
        help=f"Directory to save downloaded PDFs (default: {Path(RAW_DATA_DIR) / 'vermont_title_v'}).",
    )

    args = parser.parse_args()

    document_urls, pdf_links = find_all_pdf_links(
        headless=not args.no_headless,
        wait_seconds=args.wait_seconds,
        download=args.download,
        output_dir=args.output_dir.expanduser() if args.download else None,
    )

    logger.info(f"\n\nSummary:")
    logger.info(f"  Total document pages: {len(document_urls)}")
    logger.info(f"  PDFs found: {len(pdf_links)}")
    
    if not args.download:
        logger.info("\nUse --download to download the PDFs.")

