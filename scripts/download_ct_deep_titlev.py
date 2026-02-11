#!/usr/bin/env python3
"""
Specialized downloader for Connecticut DEEP Title V permits.

This script uses Selenium to render the main permit listing page,
extracts all permit document links from company sections, and downloads
the PDFs using the shared SeleniumPDFDownloader utilities.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import SeleniumPDFDownloader, clean_filename

BASE_URL = "https://portal.ct.gov"
PERMIT_LISTING_URL = "https://portal.ct.gov/deep/air/permits/title-v-operating-permit-program"


def extract_company_links(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    Extract company name and anchor link from the main table.
    Returns list of (company_name, anchor_id) tuples.
    """
    company_links = []
    
    # Find the main table with company names
    # The table has headers "Company Name" and "Town"
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        header_text = " ".join(headers)
        logger.debug(f"Table headers: {header_text[:100]}")
        
        if "company name" in header_text or "town" in header_text:
            logger.info(f"Found main company table with headers: {headers}")
            # Found the main company table
            rows = table.find_all("tr")
            logger.debug(f"Found {len(rows)} rows in company table")
            
            for row_idx, row in enumerate(rows):
                # Find the first link in the row (company name link)
                company_link = row.find("a")
                if company_link:
                    href = company_link.get("href", "")
                    company_name = company_link.get_text(strip=True)
                    
                    # Extract anchor ID from href (e.g., #Ahlstrom)
                    if href.startswith("#"):
                        anchor_id = href[1:]  # Remove the #
                        company_links.append((company_name, anchor_id))
                        if row_idx < 3:  # Log first few
                            logger.debug(f"  Row {row_idx}: {company_name} -> {anchor_id}")
            
            if company_links:
                break  # Only process the first matching table
    
    # If no company links found in tables, try finding all anchor links
    if not company_links:
        logger.info("No company links found in tables, trying to find all anchor links...")
        for link in soup.find_all("a", href=re.compile(r"^#")):
            href = link.get("href", "")
            company_name = link.get_text(strip=True).strip()
            if company_name and len(company_name) > 2:  # Filter out empty or very short names
                anchor_id = href[1:] if href.startswith("#") else href
                company_links.append((company_name, anchor_id))
                if len(company_links) <= 5:  # Log first few
                    logger.debug(f"  Found anchor link: {company_name} -> {anchor_id}")
    
    return company_links


def extract_permit_links_from_section(soup: BeautifulSoup, company_name: str, anchor_id: str, all_anchors: List[str]) -> List[dict]:
    """
    Extract all permit PDF links from a company section.
    Returns list of dicts with url, permit_type, permit_number, and description.
    
    Args:
        soup: The full page soup to search
        company_name: Name of the company
        anchor_id: The anchor ID for this company
        all_anchors: List of all anchor IDs to know when to stop
    """
    permit_links = []
    
    # Find the anchor element
    anchor = soup.find("a", id=anchor_id)
    if not anchor:
        # Try case-insensitive
        for a_tag in soup.find_all("a"):
            if a_tag.get("id", "").lower() == anchor_id.lower():
                anchor = a_tag
                break
    
    if not anchor:
        return permit_links
    
    # Find all elements in document order
    all_elements = list(soup.descendants)
    anchor_index = None
    next_anchor_index = None
    
    # Find the index of our anchor
    for i, elem in enumerate(all_elements):
        if elem == anchor:
            anchor_index = i
            break
    
    if anchor_index is None:
        return permit_links
    
    # Find the index of the next company anchor
    for i in range(anchor_index + 1, len(all_elements)):
        elem = all_elements[i]
        if hasattr(elem, "name") and elem.name == "a":
            elem_id = elem.get("id", "")
            if elem_id and elem_id.lower() != anchor_id.lower():
                if elem_id.lower() in [a.lower() for a in all_anchors]:
                    next_anchor_index = i
                    break
    
    # Find all tables between anchor and next anchor
    found_tables = []
    for i in range(anchor_index, next_anchor_index if next_anchor_index else len(all_elements)):
        elem = all_elements[i]
        if hasattr(elem, "name") and elem.name == "table":
            if elem not in found_tables:
                found_tables.append(elem)
    
    logger.debug(f"  Found {len(found_tables)} tables for {company_name}")
    
    # Process each table
    for table_idx, table in enumerate(found_tables):
        # Check if this is a permit table (has headers like "Title V Permit No." or "NSR Permit No.")
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        header_text = " ".join(headers)
        
        permit_type = None
        if "title v" in header_text or "title-v" in header_text:
            permit_type = "Title V"
        elif "nsr" in header_text:
            permit_type = "NSR"
        elif "registration" in header_text:
            permit_type = "Registration"
        
        if not permit_type:
            continue
        
        # Extract permit links from table rows
        rows = table.find_all("tr")
        logger.debug(f"    Table {table_idx}: {len(rows)} rows, permit_type: {permit_type}")
        
        for row_idx, row in enumerate(rows):
            cells = row.find_all(["td", "th"])
            if len(cells) < 1:  # Changed from 2 to 1 - some rows might only have one cell
                continue
            
            # Find the permit number link - prefer links with class="pdf-link"
            permit_link = None
            for cell in cells:
                # First try to find a link with class="pdf-link"
                permit_link = cell.find("a", class_="pdf-link")
                if permit_link:
                    break
                # Fallback to any link
                if not permit_link:
                    permit_link = cell.find("a")
                    if permit_link:
                        break
            
            if not permit_link:
                continue
            
            href = permit_link.get("href", "")
            link_text = permit_link.get_text(strip=True)
            
            if not href:
                continue
            
            # Check if it's a Title V permit (contains "-tv" or "-TV" in href or text)
            href_lower = href.lower()
            if "-tv" not in href_lower and not re.search(r"-tv", link_text, re.I):
                continue
            
            logger.debug(f"      Row {row_idx}: Found Title V PDF link: {href[:80]}")
            
            permit_number = permit_link.get_text(strip=True)
            
            # Try to get description from other cells
            description = ""
            if len(cells) > 1:
                description = cells[1].get_text(strip=True)
            if len(cells) > 2:
                issuance_date = cells[2].get_text(strip=True)
                if issuance_date:
                    description = f"{description} - {issuance_date}".strip(" -")
            
            permit_links.append({
                "url": href,
                "permit_type": permit_type,
                "permit_number": permit_number,
                "description": description,
                "company_name": company_name,
            })
    
    return permit_links


def derive_filename(permit_info: dict) -> str:
    """
    Create a descriptive filename for a permit PDF.
    """
    name_parts = []
    
    # Add company name
    company = clean_filename(permit_info.get("company_name", "Unknown"))
    if company:
        name_parts.append(company)
    
    # Add permit type
    permit_type = permit_info.get("permit_type", "")
    if permit_type:
        name_parts.append(permit_type)
    
    # Add permit number
    permit_number = permit_info.get("permit_number", "")
    if permit_number:
        name_parts.append(permit_number)
    
    # Add description if available and not too long
    description = permit_info.get("description", "")
    if description and len(description) < 50:
        desc_clean = clean_filename(description)
        if desc_clean:
            name_parts.append(desc_clean)
    
    if not name_parts:
        return "ct_permit"
    
    filename = " - ".join(name_parts)
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    
    return filename


def download_permits(output_dir: Path, headless: bool = True, wait_seconds: int = 4) -> None:
    """
    Download all Title V permits from the Connecticut DEEP website.
    """
    downloader = SeleniumPDFDownloader(
        output_dir=output_dir,
        headless=headless,
        wait_seconds=wait_seconds,
        max_depth=0,  # We supply explicit document URLs, so no recursion needed
        use_llm=False,
    )

    try:
        driver = downloader.driver
        logger.info(f"Loading permit listing page: {PERMIT_LISTING_URL}")
        driver.get(PERMIT_LISTING_URL)

        # Wait for the page to load
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))

        # Give extra time for any dynamic content
        time.sleep(wait_seconds)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Debug: Save HTML for inspection
        debug_html_path = output_dir / "debug_page_source.html"
        with open(debug_html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.debug(f"Saved page source to {debug_html_path}")
        
        # Debug: Count tables
        all_tables = soup.find_all("table")
        logger.info(f"Found {len(all_tables)} total tables on the page")
        
        # Find all links with class="pdf-link" that contain "-tv" or "-TV"
        # This is the simplest and most reliable method
        all_pdf_links = []
        for link in soup.find_all("a", class_="pdf-link"):
            href = link.get("href", "")
            link_text = link.get_text(strip=True)
            
            # Check if it's a Title V permit (contains "-tv" or "-TV" in href or text)
            if re.search(r"-tv", href, re.I) or re.search(r"-tv", link_text, re.I):
                all_pdf_links.append(link)
        
        logger.info(f"Found {len(all_pdf_links)} Title V PDF links (with class='pdf-link' and '-tv' or '-TV')")
        if all_pdf_links:
            logger.info("Sample Title V links:")
            for i, link in enumerate(all_pdf_links[:5]):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                logger.info(f"  {i+1}. Text: '{text[:50]}', Href: '{href[:80]}'")
        
        # Extract company links
        company_links = extract_company_links(soup)
        logger.info(f"Found {len(company_links)} companies in the listing")
        
        all_permit_links: List[dict] = []
        
        if company_links:
            logger.debug(f"Sample company links: {company_links[:3]}")
        else:
            logger.warning("No company links found. The page structure may have changed.")
            # Try alternative extraction method - just get all Title V PDF links
            if all_pdf_links:
                logger.info(f"Found {len(all_pdf_links)} Title V PDF links directly. Using fallback method to extract all Title V permits.")
                # Extract all Title V links (they're already filtered by class="pdf-link" and "-tv")
                for pdf_link in all_pdf_links:
                    href = pdf_link.get("href", "")
                    link_text = pdf_link.get_text(strip=True) or ""
                    
                    if not href:
                        continue
                    
                    # Resolve relative URLs (though they should already be absolute)
                    if not href.startswith("http"):
                        href = urljoin(BASE_URL, href)
                    
                    # Extract permit number from link text (e.g., "144-0032-TV")
                    permit_number = "Unknown"
                    if link_text:
                        # Try to extract permit number from link text
                        match = re.search(r"(\d+-\d+-TV)", link_text, re.I)
                        if match:
                            permit_number = match.group(1).upper()
                        else:
                            # Use the link text as permit number if it looks like one
                            permit_number = link_text.strip()
                    
                    all_permit_links.append({
                        "url": href,
                        "permit_type": "Title V",
                        "permit_number": permit_number,
                        "description": link_text,
                        "company_name": "Unknown",
                    })
                
                logger.info(f"Extracted {len(all_permit_links)} Title V permit links using fallback method")
                # Skip the company-by-company processing
                company_links = []
            else:
                # Look for any links with href starting with #
                anchor_links = soup.find_all("a", href=re.compile(r"^#"))
                logger.info(f"Found {len(anchor_links)} anchor links on the page")
                if anchor_links:
                    logger.debug(f"Sample anchor links: {[(link.get_text(strip=True), link.get('href', '')) for link in anchor_links[:5]]}")
                return

        if not all_permit_links:  # Only process companies if we didn't use fallback
            processed_companies = 0

            # Process each company section
            all_anchor_ids = [aid for _, aid in company_links]
            
            for company_name, anchor_id in company_links:
                logger.info(f"Processing company: {company_name} (anchor: {anchor_id})")
                
                # Extract permit links from this company's section
                permit_links = extract_permit_links_from_section(soup, company_name, anchor_id, all_anchor_ids)
                
                if not permit_links:
                    logger.warning(f"  No permit links found for {company_name} (anchor: {anchor_id})")
                    # Try scrolling to the anchor and re-parsing in case content loads dynamically
                    try:
                        driver.execute_script(f"document.getElementById('{anchor_id}')?.scrollIntoView();")
                        time.sleep(1)
                        soup = BeautifulSoup(driver.page_source, "html.parser")
                        permit_links = extract_permit_links_from_section(soup, company_name, anchor_id, all_anchor_ids)
                    except Exception as e:
                        logger.debug(f"Error scrolling to anchor: {e}")
                
                logger.info(f"  Found {len(permit_links)} permit links for {company_name}")
                all_permit_links.extend(permit_links)
                processed_companies += 1

            logger.info(f"\nTotal permits found: {len(all_permit_links)}")
            logger.info(f"Processed {processed_companies} companies\n")
        else:
            logger.info(f"\nTotal permits found: {len(all_permit_links)} (using fallback method)\n")

        # Download each permit
        downloaded = 0
        skipped = 0
        failed = 0

        for idx, permit_info in enumerate(all_permit_links, start=1):
            permit_url = permit_info["url"]
            
            # Resolve relative URLs
            if not permit_url.startswith("http"):
                permit_url = urljoin(BASE_URL, permit_url)
            
            permit_info["url"] = permit_url
            
            filename = derive_filename(permit_info)
            
            logger.info(f"[{idx}/{len(all_permit_links)}] Downloading: {filename}")
            logger.debug(f"  URL: {permit_url}")
            
            success = downloader.download_document(
                permit_url,
                referer=PERMIT_LISTING_URL,
                link_text=filename,
                is_table_link=True,
            )
            
            if success:
                downloaded += 1
            else:
                failed += 1
                logger.warning(f"  Failed to download: {permit_url}")

        logger.info("\n" + "=" * 60)
        logger.info("DOWNLOAD SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total permits found: {len(all_permit_links)}")
        logger.info(f"Successfully downloaded: {downloaded}")
        logger.info(f"Failed downloads: {failed}")
        logger.info(f"Output directory: {output_dir}")

    finally:
        downloader.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Connecticut DEEP Title V permit PDFs.")
    default_output = Path(RAW_DATA_DIR) / "connecticut_title_v"
    parser.add_argument(
        "--output-dir",
        default=default_output,
        type=Path,
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
        default=4,
        help="Seconds to wait for page rendering.",
    )

    args = parser.parse_args()
    output_dir: Path = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    download_permits(output_dir, headless=not args.no_headless, wait_seconds=args.wait_seconds)


if __name__ == "__main__":
    main()

