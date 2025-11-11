#!/usr/bin/env python3
"""
EPA Permit PDF Downloader (Version 2)

This script downloads Final Permit PDFs from EPA permit hub pages by using
Selenium to click download buttons.

The EPA permit hub is a JavaScript SPA, so we need Selenium to:
1. Load and render the page
2. Find the "Final Permit" row in the "Permitting Authority Documents" table
3. Click the download button
4. Wait for and manage the download
"""

import pandas as pd
import os
import time
import re
from pathlib import Path
import json
import glob
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from permit_data_extraction.config import RAW_DATA_DIR


class EPAPermitPDFDownloader:
    def __init__(self, output_dir=None, delay_seconds=2, headless=True):
        """
        Initialize the EPA Permit PDF Downloader.
        
        Args:
            output_dir (str): Directory where PDFs will be saved
            delay_seconds (int): Delay between requests
            headless (bool): Run Chrome in headless mode
        """
        if output_dir is None:
            self.output_dir = Path(RAW_DATA_DIR) / "epa_final_permits"
        else:
            self.output_dir = Path(output_dir)
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay_seconds = delay_seconds
        self.headless = headless
        
        # Temp download directory for Chrome
        self.download_dir = self.output_dir / "_temp_downloads"
        self.download_dir.mkdir(exist_ok=True)
        
        # Initialize Selenium driver
        self.driver = None
        self._setup_driver()
    
    def _setup_driver(self):
        """Setup Selenium Chrome driver with download preferences."""
        chrome_options = Options()
        
        if self.headless:
            chrome_options.add_argument('--headless=new')
        
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        
        # Configure download behavior
        prefs = {
            "download.default_directory": str(self.download_dir.absolute()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
            "safebrowsing.disable_download_protection": True,
            "profile.default_content_setting_values.automatic_downloads": 1
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        self.driver = webdriver.Chrome(options=chrome_options)
    
    def __del__(self):
        """Cleanup Selenium driver."""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
    
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
            'filename': None,
            'pdf_path': None,
            'error': None
        }
        
        try:
            # Load the page
            self.driver.get(permit_url)
            
            # Wait for Angular app to load - look for permit-hub-details tag
            print(f"  Waiting for Angular app to load...")
            time.sleep(3)
            
            # Wait for the permit details content to render
            # Look for tables which indicate content is loaded
            max_wait = 20
            tables_found = False
            
            for i in range(max_wait):
                tables = self.driver.find_elements(By.TAG_NAME, 'table')
                if len(tables) >= 2:  # Should have at least 2 tables
                    tables_found = True
                    print(f"  ✓ Content loaded after {i+3} seconds")
                    break
                time.sleep(1)
            
            if not tables_found:
                result['error'] = 'Page content did not load (Angular rendering issue)'
                return result
            
            # Give it one more second to stabilize
            time.sleep(1)
            
            # Find the Final Permit download button
            try:
                # XPath to find button in same row as "Final Permit" text
                final_permit_xpath = "//td[contains(text(), 'Final Permit')]/following-sibling::td//button[contains(@title, 'Download')]"
                
                download_button = self.driver.find_element(By.XPATH, final_permit_xpath)
                
                # Get the filename from button title
                title = download_button.get_attribute('title')
                # Title format: "Download A530001F_3_00.pdf 3789kb"
                if title and 'Download ' in title:
                    filename = title.replace('Download ', '').split()[0]
                    result['filename'] = filename
                    
                    # Clear download directory before downloading
                    for f in self.download_dir.glob('*'):
                        if f.is_file():
                            f.unlink()
                    
                    # Click the download button
                    download_button.click()
                    
                    # Wait for download to complete
                    max_wait = 30  # seconds
                    downloaded_file = None
                    
                    for i in range(max_wait):
                        time.sleep(1)
                        
                        # Check for downloaded files
                        downloaded_files = list(self.download_dir.glob('*.pdf'))
                        
                        # Also check for .crdownload files (in-progress downloads)
                        in_progress = list(self.download_dir.glob('*.crdownload'))
                        
                        if downloaded_files and not in_progress:
                            downloaded_file = downloaded_files[0]
                            break
                    
                    if not downloaded_file:
                        result['error'] = 'Download timeout or failed'
                        return result
                    
                    # Move file to final location with proper naming
                    if state_code:
                        state_dir = self.output_dir / state_code
                        state_dir.mkdir(exist_ok=True)
                        final_path = state_dir / filename
                    else:
                        final_path = self.output_dir / filename
                    
                    # Check if file already exists
                    if final_path.exists():
                        result['status'] = 'already_exists'
                        result['pdf_path'] = str(final_path)
                        downloaded_file.unlink()  # Delete temp file
                        return result
                    
                    # Move to final location
                    downloaded_file.rename(final_path)
                    
                    result['status'] = 'success'
                    result['pdf_path'] = str(final_path)
                    
                else:
                    result['error'] = 'Could not extract filename from button title'
                
            except NoSuchElementException:
                result['error'] = 'Final Permit download button not found'
            except Exception as e:
                result['error'] = f'Error finding/clicking download button: {str(e)}'
        
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

