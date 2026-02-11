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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from permit_data_extraction.config import RAW_DATA_DIR


class EPAPermitPDFDownloader:
    def __init__(self, output_dir=None, delay_seconds=2, headless=True, temp_download_dir=None):
        """
        Initialize the EPA Permit PDF Downloader.
        
        Args:
            output_dir (str): Directory where PDFs will be saved
            delay_seconds (int): Delay between requests
            headless (bool): Run Chrome in headless mode
            temp_download_dir (str): Optional temp download directory for Chrome
        """
        if output_dir is None:
            self.output_dir = Path(RAW_DATA_DIR) / "epa_final_permits"
        else:
            self.output_dir = Path(output_dir)
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay_seconds = delay_seconds
        self.headless = headless
        
        # Temp download directory for Chrome
        self.download_dir = Path(temp_download_dir) if temp_download_dir else self.output_dir / "_temp_downloads"
        self.download_dir.mkdir(exist_ok=True)
        
        # File to track completed links
        self.completed_links_file = self.output_dir / "completed_links.txt"
        self.completed_links_lock = threading.Lock()
        
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

    def _wait_for_tables(self, min_tables=2, timeout=20):
        """Wait until the expected number of tables are present."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: len(d.find_elements(By.TAG_NAME, 'table')) >= min_tables
            )
            return True
        except TimeoutException:
            return False
    
    def __del__(self):
        """Cleanup Selenium driver."""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
    
    def _load_completed_links(self):
        """Load completed links from file."""
        completed_links = set()
        if self.completed_links_file.exists():
            try:
                with open(self.completed_links_file, 'r') as f:
                    for line in f:
                        url = line.strip()
                        if url:
                            completed_links.add(url)
            except Exception as e:
                print(f"Warning: Could not read completed links file: {e}")
        return completed_links
    
    def _is_link_completed(self, url):
        """Check if a link has already been completed."""
        if not url:
            return False
        with self.completed_links_lock:
            completed_links = self._load_completed_links()
            return url in completed_links
    
    def _mark_link_completed(self, url):
        """Mark a link as completed (thread-safe)."""
        if not url:
            return
        with self.completed_links_lock:
            # Check again to avoid duplicates
            completed_links = self._load_completed_links()
            if url not in completed_links:
                try:
                    with open(self.completed_links_file, 'a') as f:
                        f.write(url + '\n')
                except Exception as e:
                    print(f"Warning: Could not write to completed links file: {e}")

    def _download_from_button(self, download_button, state_code=None, permit_id=None):
        """Click a download button and move the downloaded file into place."""
        # Get the filename from button title
        title = download_button.get_attribute('title')
        button_text = download_button.text
        filename = None
        # Title format: "Download A530001F_3_00.pdf 3789kb" or "Download all files"
        if title and 'Download ' in title:
            # Extract filename if it's a specific file download
            title_parts = title.replace('Download ', '').split()
            if title_parts and '.' in title_parts[0]:
                filename = title_parts[0]
        
        # Clear download directory before downloading
        for f in self.download_dir.glob('*'):
            if f.is_file():
                f.unlink()
        
        # Click the download button
        download_button.click()
        
        # Wait for download to complete
        max_wait = 60  # seconds (longer for "Download all" which might be a ZIP)
        downloaded_file = None
        
        for _ in range(max_wait):
            time.sleep(1)
            
            # Check for downloaded files (PDF or ZIP)
            downloaded_files = list(self.download_dir.glob('*.pdf'))
            downloaded_files.extend(self.download_dir.glob('*.zip'))
            
            # Also check for .crdownload files (in-progress downloads)
            in_progress = list(self.download_dir.glob('*.crdownload'))
            
            if downloaded_files and not in_progress:
                downloaded_file = downloaded_files[0]
                break
        
        if not downloaded_file:
            return {
                'status': 'failed',
                'filename': filename,
                'pdf_path': None,
                'error': 'Download timeout or failed'
            }
        
        # Determine final filename
        if not filename:
            filename = downloaded_file.name
        
        # If this looks like a "Download all" (ZIP file) and we have permit_id, create a better name
        if downloaded_file.suffix.lower() == '.zip' and permit_id:
            # Create a name like: STATE_PERMITID_all_files.zip
            if state_code:
                filename = f"{state_code}_{permit_id}_all_files.zip"
            else:
                filename = f"{permit_id}_all_files.zip"
        
        # Move file to final location with proper naming
        if state_code:
            state_dir = self.output_dir / state_code
            state_dir.mkdir(exist_ok=True)
            final_path = state_dir / filename
        else:
            final_path = self.output_dir / filename
        
        # Check if file already exists
        if final_path.exists():
            downloaded_file.unlink()  # Delete temp file
            return {
                'status': 'already_exists',
                'filename': filename,
                'pdf_path': str(final_path),
                'error': None
            }
        
        # Move to final location
        downloaded_file.rename(final_path)
        
        return {
            'status': 'success',
            'filename': filename,
            'pdf_path': str(final_path),
            'error': None
        }

    def _process_dataframe(self, df, downloaded_urls, downloaded_lock):
        """Process a dataframe chunk and return results."""
        results = {
            'total_processed': 0,
            'successful': 0,
            'already_exists': 0,
            'already_downloaded': 0,
            'failed': 0,
            'details': []
        }
        
        for idx, row in df.iterrows():
            url = row.get('url')
            permit_id = row.get('permit_id', '')
            state_code = row.get('state_code', '')
            
            print(f"\n[{idx + 1}] Processing: {state_code} - {permit_id}")
            print(f"  URL: {url}")
            
            # Check completed links file first (most reliable)
            if self._is_link_completed(url):
                result = {
                    'permit_url': url,
                    'permit_id': permit_id,
                    'state_code': state_code,
                    'status': 'already_downloaded',
                    'filename': None,
                    'pdf_path': None,
                    'error': None
                }
                results['total_processed'] += 1
                results['already_downloaded'] += 1
                results['details'].append(result)
                print("  ⊙ Already completed (skipping - found in completed_links.txt)")
                continue
            
            # Also check in-memory set (for this run)
            with downloaded_lock:
                already_downloaded = url in downloaded_urls
            
            if already_downloaded:
                result = {
                    'permit_url': url,
                    'permit_id': permit_id,
                    'state_code': state_code,
                    'status': 'already_downloaded',
                    'filename': None,
                    'pdf_path': None,
                    'error': None
                }
                results['total_processed'] += 1
                results['already_downloaded'] += 1
                results['details'].append(result)
                print("  ⊙ Already downloaded (skipping URL - this run)")
                continue
            
            # Download the permit
            result = self.download_permit_pdf(url, permit_id, state_code)
            
            results['total_processed'] += 1
            
            if result['status'] == 'success':
                results['successful'] += 1
                print(f"  ✓ Downloaded: {result['pdf_path']}")
                # Mark as completed in file
                self._mark_link_completed(url)
                if url:
                    with downloaded_lock:
                        downloaded_urls.add(url)
            elif result['status'] == 'already_exists':
                results['already_exists'] += 1
                print(f"  ⊙ Already exists: {result['pdf_path']}")
                # Mark as completed in file (file exists, so it's effectively completed)
                self._mark_link_completed(url)
                if url:
                    with downloaded_lock:
                        downloaded_urls.add(url)
            elif result['status'] == 'already_downloaded':
                results['already_downloaded'] += 1
                print("  ⊙ Already downloaded (skipping URL)")
            else:
                results['failed'] += 1
                print(f"  ✗ Failed: {result['error']}")
            
            results['details'].append(result)
            
            # Delay between requests
            time.sleep(self.delay_seconds)
        
        return results
    
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
            
            # Wait for the permit details content to render
            print("  Waiting for page content to load...")
            tables_found = self._wait_for_tables(min_tables=2, timeout=20)
            
            if not tables_found:
                result['error'] = 'Page content did not load (Angular rendering issue)'
                return result
            
            # First, try to find a "Download all files" button
            try:
                # Look for buttons with text/title containing "all" and "download" (case-insensitive)
                # Try various XPath patterns to find the "Download all" button
                download_all_patterns = [
                    "//button[contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') and contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'all')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'all')]",
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download all')]",
                    "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download all')]",
                ]
                
                download_all_button = None
                for pattern in download_all_patterns:
                    buttons = self.driver.find_elements(By.XPATH, pattern)
                    if buttons:
                        download_all_button = buttons[0]
                        break
                
                if download_all_button:
                    # Use "Download all files" button
                    print("  Found 'Download all files' button, using it...")
                    download_result = self._download_from_button(download_all_button, state_code=state_code, permit_id=permit_id)
                    
                    result['status'] = download_result['status']
                    result['filename'] = download_result.get('filename')
                    result['pdf_path'] = download_result.get('pdf_path')
                    result['download_mode'] = 'download_all_button'
                    
                    if download_result.get('error'):
                        result['error'] = download_result['error']
                    
                    return result
                
                # Fallback: Find "Permit" labeled documents; if none, download all documents
                print("  'Download all files' button not found, downloading individual documents...")
                permit_buttons_xpath = (
                    "//tr[.//td[contains("
                    "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                    " 'permit')]]"
                    "//button[contains(@title, 'Download')]"
                )
                download_buttons = self.driver.find_elements(By.XPATH, permit_buttons_xpath)
                mode = 'permit_labeled'
                
                if not download_buttons:
                    download_buttons = self.driver.find_elements(
                        By.XPATH,
                        "//button[contains(@title, 'Download')]"
                    )
                    mode = 'all_documents'
                
                if not download_buttons:
                    result['error'] = 'No download buttons found'
                    return result
                
                downloaded_paths = []
                filenames = []
                errors = []
                
                for button in download_buttons:
                    download_result = self._download_from_button(button, state_code=state_code)
                    if download_result.get('filename'):
                        filenames.append(download_result['filename'])
                    if download_result.get('pdf_path'):
                        downloaded_paths.append(download_result['pdf_path'])
                    
                    if download_result['status'] == 'success':
                        result['status'] = 'success'
                    elif download_result['status'] == 'already_exists' and result['status'] != 'success':
                        result['status'] = 'already_exists'
                    else:
                        errors.append(download_result.get('error'))
                
                result['filename'] = filenames[0] if filenames else None
                result['pdf_path'] = downloaded_paths[0] if downloaded_paths else None
                result['pdf_paths'] = downloaded_paths
                result['download_mode'] = mode
                
                if result['status'] == 'failed' and errors:
                    result['error'] = '; '.join([e for e in errors if e])
                
            except Exception as e:
                result['error'] = f'Error finding/clicking download button(s): {str(e)}'
        
        except Exception as e:
            result['error'] = f"Unexpected error: {str(e)}"
        
        return result
    
    def download_from_csv(self, csv_path, max_permits=None, resume_from=0, workers=1):
        """
        Download Final Permit PDFs from URLs in a CSV file.
        
        Args:
            csv_path (str): Path to CSV file with permit URLs
            max_permits (int): Maximum number of permits to download (None for all)
            resume_from (int): Row index to resume from (0-based)
            workers (int): Number of parallel workers
        
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
        
        # Load completed links from persistent file
        completed_links = self._load_completed_links()
        print(f"Loaded {len(completed_links)} completed links from {self.completed_links_file.name}")
        
        # Also seed already-downloaded URLs from prior summary if available (for backward compatibility)
        summary_file = self.output_dir / "download_summary.json"
        downloaded_urls = set(completed_links)  # Start with completed links
        if summary_file.exists():
            try:
                with open(summary_file, 'r') as f:
                    previous_results = json.load(f)
                for detail in previous_results.get('details', []):
                    if detail.get('status') in {'success', 'already_exists', 'already_downloaded'}:
                        url = detail.get('permit_url')
                        if url:
                            downloaded_urls.add(url)
                additional_from_summary = len(downloaded_urls) - len(completed_links)
                if additional_from_summary > 0:
                    print(f"  Also found {additional_from_summary} additional URLs in download_summary.json")
            except Exception:
                print("Warning: Could not read existing download summary; using completed_links.txt only.")
        
        # Results tracking
        results = {
            'total_processed': 0,
            'successful': 0,
            'already_exists': 0,
            'already_downloaded': 0,
            'failed': 0,
            'details': []
        }
        
        downloaded_lock = threading.Lock()
        
        if workers <= 1 or len(df) <= 1:
            results = self._process_dataframe(df, downloaded_urls, downloaded_lock)
        else:
            # Split dataframe into chunks for workers
            chunk_size = max(1, len(df) // workers)
            chunks = [
                df.iloc[i:i + chunk_size]
                for i in range(0, len(df), chunk_size)
            ]
            
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for worker_id, chunk in enumerate(chunks, start=1):
                    temp_dir = str(self.output_dir / f"_temp_downloads_{worker_id}")
                    def _worker(local_chunk=chunk, local_id=worker_id, local_temp=temp_dir):
                        worker = EPAPermitPDFDownloader(
                            output_dir=str(self.output_dir),
                            delay_seconds=self.delay_seconds,
                            headless=self.headless,
                            temp_download_dir=local_temp
                        )
                        try:
                            return worker._process_dataframe(local_chunk, downloaded_urls, downloaded_lock)
                        finally:
                            try:
                                worker.driver.quit()
                            except Exception:
                                pass
                    
                    futures.append(executor.submit(_worker))
                
                for future in as_completed(futures):
                    chunk_results = future.result()
                    results['total_processed'] += chunk_results['total_processed']
                    results['successful'] += chunk_results['successful']
                    results['already_exists'] += chunk_results['already_exists']
                    results['already_downloaded'] += chunk_results['already_downloaded']
                    results['failed'] += chunk_results['failed']
                    results['details'].extend(chunk_results['details'])
        
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
        print(f"Already downloaded (skipped): {results['already_downloaded']}")
        print(f"Failed: {results['failed']}")
        print(f"\nOutput directory: {self.output_dir}")
        print(f"Completed links tracked in: {self.completed_links_file}")
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
    parser.add_argument(
        '--workers',
        type=int,
        help='Number of parallel workers (default: 1)',
        default=1
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
        resume_from=args.resume_from,
        workers=args.workers
    )


if __name__ == "__main__":
    main()

