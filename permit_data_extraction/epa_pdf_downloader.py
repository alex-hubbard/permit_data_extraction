#!/usr/bin/env python3
"""
EPA Permit PDF Downloader

This script downloads Final Permit PDFs from EPA permit hub pages.
It looks for the "Permitting Authority Documents" table and downloads
the "Final Permit" document.

This uses Selenium because the EPA permit hub is a JavaScript-rendered SPA.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
import time
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from permit_data_extraction.config import RAW_DATA_DIR


class EPAPermitPDFDownloader:
    def __init__(self, output_dir=None, delay_seconds=2, headless=True):
        """
        Initialize the EPA Permit PDF Downloader.
        
        Args:
            output_dir (str): Directory where PDFs will be saved
            delay_seconds (int): Delay between requests to be respectful to EPA servers
            headless (bool): Run Chrome in headless mode
        """
        if output_dir is None:
            self.output_dir = Path(RAW_DATA_DIR) / "epa_final_permits"
        else:
            self.output_dir = Path(output_dir)
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay_seconds = delay_seconds
        self.headless = headless
        
        # Session for downloading PDFs
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Initialize Selenium driver
        self.driver = None
        self._setup_driver()
    
    def _setup_driver(self):
        """Setup Selenium Chrome driver."""
        import tempfile
        
        chrome_options = Options()
        if self.headless:
            chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        # Use a unique temporary user data directory
        temp_dir = tempfile.mkdtemp(prefix='chrome_user_data_')
        chrome_options.add_argument(f'--user-data-dir={temp_dir}')
        
        self.driver = webdriver.Chrome(options=chrome_options)
    
    def __del__(self):
        """Cleanup Selenium driver."""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
    
    def clean_filename(self, filename):
        """
        Clean the filename by removing invalid characters.
        
        Args:
            filename (str): The filename to clean
        
        Returns:
            str: Cleaned filename
        """
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace multiple spaces with single space
        filename = re.sub(r'\s+', '_', filename)
        # Strip leading/trailing spaces and underscores
        filename = filename.strip('_ ')
        return filename
    
    def find_and_download_final_permit(self, permit_url, output_filepath):
        """
        Find and download the Final Permit PDF by clicking the download button.
        Uses Selenium to render JavaScript and click the download button.
        
        Args:
            permit_url (str): The URL of the permit page
            output_filepath (str): Where to save the downloaded file
        
        Returns:
            dict: Dictionary with download info or None if not found
        """
        try:
            # Configure Chrome to download to a specific directory
            download_dir = str(Path(output_filepath).parent.absolute())
            
            # Setup Chrome preferences for downloads
            prefs = {
                "download.default_directory": download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True
            }
            
            # Note: We'll handle downloads differently - see below
            
            # Load the page
            self.driver.get(permit_url)
            
            # Wait for page to load and JavaScript to render
            time.sleep(5)
            
            # Try to find the Final Permit row and download button
            try:
                # Use XPath to find table cell containing "Final Permit"
                # Then find the download button in the same row
                final_permit_xpath = "//td[contains(text(), 'Final Permit')]/following-sibling::td//button[contains(@title, 'Download')]"
                
                download_button = self.driver.find_element(By.XPATH, final_permit_xpath)
                
                if download_button:
                    # Get the filename from the title attribute
                    title = download_button.get_attribute('title')
                    # Title is like "Download A530001F_3_00.pdf 3789kb"
                    if title and 'Download ' in title:
                        filename = title.replace('Download ', '').split()[0]  # Get just the filename
                        
                        # Click the button - this will trigger a download
                        # But we need to intercept the actual download URL
                        # Let's try to find it in the button's onclick or parent elements
                        
                        # Alternative: Look for the actual download URL in network requests
                        # For now, let's try clicking and see what happens
                        download_button.click()
                        
                        # Wait a bit for download to start
                        time.sleep(2)
                        
                        return {
                            'filename': filename,
                            'method': 'button_click',
                            'document_type': 'Final Permit'
                        }
                
            except NoSuchElementException:
                print(f"  Could not find Final Permit download button")
            except Exception as e:
                print(f"  Error finding/clicking download button: {e}")
            
            # Fallback: Try to construct the download URL from the filename
            # The EPA permit hub likely has a consistent API endpoint for downloads
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            tables = soup.find_all('table')
            
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all(['td', 'th'])
                    row_text = ' '.join([cell.get_text().strip() for cell in cells])
                    
                    if 'Final Permit' in row_text:
                        # Found the row, try to extract filename
                        for cell in cells:
                            cell_text = cell.get_text().strip()
                            if '.pdf' in cell_text:
                                # This might be the filename
                                return {
                                    'filename': cell_text,
                                    'method': 'extracted_from_table',
                                    'document_type': 'Final Permit'
                                }
            
        except Exception as e:
            print(f"  Error in find_and_download_final_permit: {e}")
        
        return None
    
    def _extract_final_permit_from_table(self, table, page_url):
        """
        Extract Final Permit link from a table.
        
        Args:
            table (BeautifulSoup): Table element to search
            page_url (str): The URL of the current page
        
        Returns:
            dict: Dictionary with link info or None if not found
        """
        rows = table.find_all('tr')
        
        for row in rows:
            # Get all cells in the row
            cells = row.find_all(['td', 'th'])
            
            # Look for a cell that contains "Final Permit"
            for cell in cells:
                cell_text = cell.get_text().strip().lower()
                
                if 'final permit' in cell_text:
                    # Found the row with Final Permit, now find the download link
                    # It might be in the same cell or another cell in the row
                    
                    # First check the same cell
                    link = cell.find('a', href=True)
                    if link:
                        full_url = urljoin(page_url, link['href'])
                        return {
                            'url': full_url,
                            'text': link.get_text().strip(),
                            'document_type': 'Final Permit'
                        }
                    
                    # Check other cells in the row
                    for other_cell in cells:
                        link = other_cell.find('a', href=True)
                        if link:
                            # Prefer links that look like downloads
                            href = link.get('href', '')
                            if any(ext in href.lower() for ext in ['.pdf', '.doc', '.docx', 'download']):
                                full_url = urljoin(page_url, link['href'])
                                return {
                                    'url': full_url,
                                    'text': link.get_text().strip(),
                                    'document_type': 'Final Permit'
                                }
        
        return None
    
    def download_permit_pdf(self, permit_url, permit_id, state_code=None):
        """
        Download the Final Permit PDF from an EPA permit page.
        
        Args:
            permit_url (str): URL of the EPA permit page
            permit_id (str): Unique permit ID
            state_code (str): State code (optional, for organizing files)
        
        Returns:
            dict: Result dictionary with status and details
        """
        result = {
            'permit_url': permit_url,
            'permit_id': permit_id,
            'state_code': state_code,
            'status': 'failed',
            'pdf_url': None,
            'pdf_path': None,
            'error': None
        }
        
        try:
            # Find the Final Permit link using Selenium
            permit_link = self.find_final_permit_link(permit_url)
            
            if not permit_link:
                result['error'] = 'Final Permit link not found'
                return result
            
            result['pdf_url'] = permit_link['url']
            
            # Download the PDF
            pdf_response = self.session.get(permit_link['url'], timeout=30)
            pdf_response.raise_for_status()
            
            # Determine filename
            # Try to get from Content-Disposition header
            filename = None
            content_disposition = pdf_response.headers.get('content-disposition')
            if content_disposition:
                filename_match = re.findall(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition)
                if filename_match:
                    filename = filename_match[0][0].strip('"\'')
            
            # If no filename from header, create one
            if not filename:
                # Get file extension from URL or content-type
                file_ext = os.path.splitext(urlparse(permit_link['url']).path)[1]
                if not file_ext:
                    content_type = pdf_response.headers.get('content-type', '').lower()
                    if 'pdf' in content_type:
                        file_ext = '.pdf'
                    elif 'word' in content_type or 'msword' in content_type:
                        file_ext = '.doc'
                    else:
                        file_ext = '.pdf'  # default
                
                # Create filename from permit_id
                filename = f"{state_code}_{permit_id}{file_ext}" if state_code else f"{permit_id}{file_ext}"
            
            # Clean the filename
            filename = self.clean_filename(filename)
            
            # Create state subdirectory if state_code provided
            if state_code:
                state_dir = self.output_dir / state_code
                state_dir.mkdir(exist_ok=True)
                filepath = state_dir / filename
            else:
                filepath = self.output_dir / filename
            
            # Check if file already exists
            if filepath.exists():
                result['status'] = 'already_exists'
                result['pdf_path'] = str(filepath)
                return result
            
            # Save the PDF
            with open(filepath, 'wb') as f:
                for chunk in pdf_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            result['status'] = 'success'
            result['pdf_path'] = str(filepath)
            
        except requests.exceptions.RequestException as e:
            result['error'] = f"Request error: {str(e)}"
        except Exception as e:
            result['error'] = f"Unexpected error: {str(e)}"
        
        return result
    
    def download_from_csv(self, csv_path, max_permits=None, resume_from=0):
        """
        Download Final Permit PDFs from URLs in a CSV file.
        
        Args:
            csv_path (str): Path to CSV file with permit URLs
            max_permits (int): Maximum number of permits to download (None for all)
            resume_from (int): Row index to resume from (0-based)
        
        Returns:
            dict: Summary of download results
        """
        # Read the CSV
        df = pd.read_csv(csv_path)
        
        print(f"Found {len(df)} permits in CSV file: {csv_path}")
        print(f"Output directory: {self.output_dir}")
        print("=" * 80)
        
        # Limit if requested
        if max_permits:
            df = df.iloc[resume_from:resume_from + max_permits]
        else:
            df = df.iloc[resume_from:]
        
        # Results tracking
        results = {
            'total_processed': 0,
            'successful': 0,
            'already_exists': 0,
            'failed': 0,
            'details': []
        }
        
        # Process each permit
        for idx, row in df.iterrows():
            url = row.get('url')
            permit_id = row.get('permit_id', '')
            state_code = row.get('state_code', '')
            
            print(f"\n[{idx + 1}/{len(df) + resume_from}] Processing: {state_code} - {permit_id}")
            print(f"  URL: {url}")
            
            # Download the permit
            result = self.download_permit_pdf(url, permit_id, state_code)
            
            results['total_processed'] += 1
            
            if result['status'] == 'success':
                results['successful'] += 1
                print(f"  ✓ Downloaded: {result['pdf_path']}")
            elif result['status'] == 'already_exists':
                results['already_exists'] += 1
                print(f"  ⊙ Already exists: {result['pdf_path']}")
            else:
                results['failed'] += 1
                print(f"  ✗ Failed: {result['error']}")
            
            results['details'].append(result)
            
            # Delay between requests
            if idx < len(df) - 1:
                time.sleep(self.delay_seconds)
        
        # Save results summary
        summary_file = self.output_dir / "download_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        # Print final summary
        print("\n" + "=" * 80)
        print("DOWNLOAD SUMMARY")
        print("=" * 80)
        print(f"Total processed: {results['total_processed']}")
        print(f"Successfully downloaded: {results['successful']}")
        print(f"Already existed: {results['already_exists']}")
        print(f"Failed: {results['failed']}")
        print(f"\nOutput directory: {self.output_dir}")
        print(f"Summary saved to: {summary_file}")
        
        return results


def main():
    """Main function for command-line usage."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Download Final Permit PDFs from EPA permit hub pages'
    )
    parser.add_argument(
        'csv_path',
        help='Path to CSV file containing permit URLs'
    )
    parser.add_argument(
        '--output-dir',
        help='Directory to save PDFs (default: data/raw/epa_final_permits)',
        default=None
    )
    parser.add_argument(
        '--max-permits',
        type=int,
        help='Maximum number of permits to download',
        default=None
    )
    parser.add_argument(
        '--resume-from',
        type=int,
        help='Row index to resume from (0-based)',
        default=0
    )
    parser.add_argument(
        '--delay',
        type=int,
        help='Delay between requests in seconds (default: 2)',
        default=2
    )
    parser.add_argument(
        '--no-headless',
        action='store_true',
        help='Run Chrome in visible mode (not headless)',
        default=False
    )
    
    args = parser.parse_args()
    
    # Create downloader
    downloader = EPAPermitPDFDownloader(
        output_dir=args.output_dir,
        delay_seconds=args.delay,
        headless=not args.no_headless
    )
    
    # Download permits
    downloader.download_from_csv(
        args.csv_path,
        max_permits=args.max_permits,
        resume_from=args.resume_from
    )


if __name__ == "__main__":
    main()

