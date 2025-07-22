import pandas as pd
import time
import re
import os
from urllib.parse import urljoin, urlparse, quote
from bs4 import BeautifulSoup
from pathlib import Path
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import re

from permit_data_extraction.config import RAW_DATA_DIR, EXTERNAL_DATA_DIR

class EPAPermitScraper:
    def __init__(self, headless=True):
        """
        Initialize the EPA Permit Scraper with Selenium.
        
        Args:
            headless (bool): Whether to run browser in headless mode
        """
        self.base_url = "https://permitsearch.epa.gov/oms-permit-hub/"
        
        # Set up Chrome options
        chrome_options = Options()
        if headless:
            chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
        
        # Initialize the driver
        self.driver = webdriver.Chrome(options=chrome_options)
        self.wait = WebDriverWait(self.driver, 10)
    
    def __del__(self):
        """Clean up the driver when the object is destroyed."""
        if hasattr(self, 'driver'):
            self.driver.quit()
    
    def load_permit_data(self, csv_path=None):
        """
        Load permit data from the CSV file.
        
        Args:
            csv_path (str): Path to the permit hub CSV file
            
        Returns:
            pd.DataFrame: DataFrame containing permit data
        """
        if csv_path is None:
            # Use the default permit hub report
            csv_path = EXTERNAL_DATA_DIR / "permit-hub-report-2025-07-21.csv"
        
        print(f"Loading permit data from: {csv_path}")
        
        # Try different encodings to handle the UTF-8 issue
        encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
        
        for encoding in encodings:
            try:
                df = pd.read_csv(csv_path, encoding=encoding)
                print(f"Successfully loaded with {encoding} encoding")
                break
            except UnicodeDecodeError:
                print(f"Failed with {encoding} encoding, trying next...")
                continue
        else:
            # If all encodings fail, try with error handling
            try:
                df = pd.read_csv(csv_path, encoding='utf-8', errors='replace')
                print("Loaded with UTF-8 encoding and error replacement")
            except Exception as e:
                print(f"Failed to load CSV file: {e}")
                return pd.DataFrame()
        
        print(f"Loaded {len(df)} permit records")
        return df
    
    def construct_search_url(self, permit_title):
        """
        Construct EPA Permit Hub search URL by appending the permit title.
        
        Args:
            permit_title (str): The permit title to search for
            
        Returns:
            str: Complete EPA Permit Hub search URL
        """
        # URL encode the permit title
        encoded_title = quote(permit_title)
        
        # Construct the EPA Permit Hub search URL
        search_url = f"{self.base_url}?media=air&title={encoded_title}"
        
        return search_url
    
    def scrape_search_results(self, search_url):
        """
        Scrape data from EPA Permit Hub search results page using Selenium.
        
        Args:
            search_url (str): The EPA Permit Hub search URL
            
        Returns:
            dict: Scraped data including permit links and details
        """
        try:
            print(f"Scraping EPA Permit Hub with Selenium: {search_url}")
            
            # Navigate to the page
            self.driver.get(search_url)
            
            # Wait for the page to load
            time.sleep(3)
            
            # Wait for content to be loaded (adjust selector based on actual page structure)
            try:
                # Wait for either search results or a loading indicator to disappear
                self.wait.until(
                    EC.any_of(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/permit/']")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".search-results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".results")),
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner"))
                    )
                )
            except TimeoutException:
                print("  Warning: Timeout waiting for page content to load")
            
            # Get the page source after JavaScript has rendered
            page_source = self.driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')
            
            # Extract data from the page
            results = {
                'search_url': search_url,
                'page_title': self.extract_page_title(soup),
                'permit_links': self.extract_permit_links(soup),
                'search_results_count': self.extract_results_count(soup),
                'pagination_info': self.extract_pagination_info(soup),
                'raw_html': page_source  # Save raw HTML for debugging
            }
            
            print(f"  Found {len(results['permit_links'])} permit links")
            print(f"  Results count: {results['search_results_count']}")
            
            return results
            
        except Exception as e:
            print(f"Error scraping EPA Permit Hub: {e}")
            return {
                'search_url': search_url,
                'error': str(e),
                'permit_links': [],
                'search_results_count': 0
            }
    
    def extract_page_title(self, soup):
        """Extract the page title."""
        title_tag = soup.find('title')
        return title_tag.get_text().strip() if title_tag else "No title found"
    
    def extract_permit_links(self, soup):
        """
        Extract permit detail page links from the search results.
        
        Args:
            soup (BeautifulSoup): Parsed HTML content
            
        Returns:
            list: List of permit link dictionaries
        """
        permit_links = []
        
        try:
            # Look for links that contain '/permit/' in the href
            permit_pattern = r'/permit/[a-f0-9\-]+'
            
            # Find all links on the page
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link.get('href')
                text = link.get_text().strip()
                
                if href and re.search(permit_pattern, href):
                    # Convert relative URLs to absolute URLs
                    if href.startswith('/'):
                        href = urljoin(self.base_url, href)
                    elif not href.startswith('http'):
                        href = urljoin(self.base_url, href)
                    
                    permit_links.append({
                        'url': href,
                        'text': text,
                        'permit_id': self.extract_permit_id(href)
                    })
            
            # Remove duplicates
            seen_urls = set()
            unique_links = []
            for link in permit_links:
                if link['url'] not in seen_urls:
                    seen_urls.add(link['url'])
                    unique_links.append(link)
            
            return unique_links
            
        except Exception as e:
            print(f"Error extracting permit links: {e}")
            return []
    
    def extract_permit_id(self, url):
        """Extract permit ID from permit detail URL."""
        match = re.search(r'/permit/([a-f0-9\-]+)', url)
        return match.group(1) if match else None
    
    def extract_results_count(self, soup):
        """Extract the number of search results."""
        try:
            # Look for common patterns that indicate result count
            # This might need adjustment based on the actual page structure
            result_patterns = [
                r'(\d+)\s+results?',
                r'(\d+)\s+permits?',
                r'(\d+)\s+items?'
            ]
            
            page_text = soup.get_text()
            for pattern in result_patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
            
            return 0
            
        except Exception as e:
            print(f"Error extracting results count: {e}")
            return 0
    
    def extract_pagination_info(self, soup):
        """Extract pagination information if available."""
        try:
            pagination = {}
            
            # Look for pagination elements
            pagination_elements = soup.find_all(['nav', 'div'], class_=re.compile(r'pagination|pager'))
            
            if pagination_elements:
                pagination['has_pagination'] = True
                # Extract page numbers if available
                page_links = soup.find_all('a', href=re.compile(r'page|p='))
                if page_links:
                    pagination['page_links'] = [link.get('href') for link in page_links]
            else:
                pagination['has_pagination'] = False
            
            return pagination
            
        except Exception as e:
            print(f"Error extracting pagination info: {e}")
            return {'has_pagination': False}
    
    def scrape_permit_detail_page(self, permit_url):
        """
        Scrape detailed information from a permit detail page using Selenium.
        
        Args:
            permit_url (str): URL of the permit detail page
            
        Returns:
            dict: Detailed permit information
        """
        try:
            print(f"  Scraping permit detail with Selenium: {permit_url}")
            
            # Navigate to the permit detail page
            self.driver.get(permit_url)
            
            # Wait for the page to load
            time.sleep(3)
            
            # Wait for content to be loaded
            try:
                self.wait.until(
                    EC.any_of(
                        EC.presence_of_element_located((By.TAG_NAME, "h1")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".permit-details")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".content")),
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner"))
                    )
                )
            except TimeoutException:
                print("    Warning: Timeout waiting for permit detail page to load")
            
            # Get the page source after JavaScript has rendered
            page_source = self.driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')
            
            # Extract permit details
            details = {
                'permit_url': permit_url,
                'permit_id': self.extract_permit_id(permit_url),
                'page_title': self.extract_page_title(soup),
                'permit_title': self.extract_permit_title(soup),
                'facility_name': self.extract_facility_name(soup),
                'permit_number': self.extract_permit_number(soup),
                'download_links': self.extract_download_links(soup),
                'permit_details': self.extract_permit_details(soup),
                'raw_html': page_source  # Save raw HTML for debugging
            }
            
            return details
            
        except Exception as e:
            print(f"  Error scraping permit detail: {e}")
            return {
                'permit_url': permit_url,
                'error': str(e)
            }
    
    def extract_permit_title(self, soup):
        """Extract permit title from detail page."""
        # Look for common title patterns
        title_selectors = ['h1', '.permit-title', '.title', '[class*="title"]']
        
        for selector in title_selectors:
            element = soup.select_one(selector)
            if element:
                return element.get_text().strip()
        
        return "No title found"
    
    def extract_facility_name(self, soup):
        """Extract facility name from detail page."""
        # Look for facility name patterns
        facility_selectors = ['.facility-name', '.facility', '[class*="facility"]']
        
        for selector in facility_selectors:
            element = soup.select_one(selector)
            if element:
                return element.get_text().strip()
        
        return "No facility name found"
    
    def extract_permit_number(self, soup):
        """Extract permit number from detail page."""
        # Look for permit number patterns
        permit_number_patterns = [
            r'Permit\s+Number[:\s]*([A-Z0-9\-]+)',
            r'Permit\s+ID[:\s]*([A-Z0-9\-]+)',
            r'ID[:\s]*([A-Z0-9\-]+)'
        ]
        
        page_text = soup.get_text()
        for pattern in permit_number_patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        
        return "No permit number found"
    
    def extract_download_links(self, soup):
        """Extract download links from permit detail page."""
        download_links = []
        
        try:
            # Look for links that might be downloads
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link.get('href')
                text = link.get_text().strip().lower()
                
                # Check if it looks like a download link
                if any(keyword in text for keyword in ['download', 'pdf', 'permit', 'final', 'document']):
                    if href.startswith('/'):
                        href = urljoin(self.base_url, href)
                    elif not href.startswith('http'):
                        href = urljoin(self.base_url, href)
                    
                    download_links.append({
                        'url': href,
                        'text': link.get_text().strip(),
                        'filename': self.extract_filename(href)
                    })
            
            return download_links
            
        except Exception as e:
            print(f"Error extracting download links: {e}")
            return []
    
    def extract_filename(self, url):
        """Extract filename from URL."""
        parsed = urlparse(url)
        return os.path.basename(parsed.path) or "unknown"
    
    def extract_permit_details(self, soup):
        """Extract general permit details from the page."""
        details = {}
        
        try:
            # Look for key-value pairs in the page
            # This is a general approach - might need refinement based on actual page structure
            detail_elements = soup.find_all(['div', 'span', 'p'], class_=re.compile(r'detail|info|field'))
            
            for element in detail_elements:
                text = element.get_text().strip()
                if ':' in text:
                    key, value = text.split(':', 1)
                    details[key.strip()] = value.strip()
            
            return details
            
        except Exception as e:
            print(f"Error extracting permit details: {e}")
            return {}
    
    def process_permit_records(self, df, output_dir=None, max_permits=None):
        """
        Process permit records and scrape EPA Permit Hub search results.
        
        Args:
            df (pd.DataFrame): DataFrame with permit records
            output_dir (str): Output directory for results
            max_permits (int): Maximum number of permits to process
            
        Returns:
            dict: Summary of processing results
        """
        if output_dir is None:
            output_dir = RAW_DATA_DIR / "epa_permit_scraped"
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Limit number of permits if specified
        if max_permits:
            df = df.head(max_permits)
        
        results = {
            'total_processed': len(df),
            'permits_with_links': 0,
            'permits_without_links': 0,
            'total_links_found': 0,
            'permit_details': []
        }
        
        print(f"Processing {len(df)} permit records via EPA Permit Hub search...")
        print(f"Output directory: {output_dir}")
        print("-" * 60)
        
        for idx, row in df.iterrows():
            permit_title = row['Permit Title']
            facility_name = row.get('Facility Name', '')
            permit_number = row.get('Permit Number', '')
            
            print(f"\nProcessing {idx + 1}/{len(df)}: {permit_title[:80]}...")
            
            # Construct the search URL
            search_url = self.construct_search_url(permit_title)
            print(f"  Search URL: {search_url}")
            
            # Scrape the search results
            search_results = self.scrape_search_results(search_url)
            
            if search_results.get('permit_links'):
                results['permits_with_links'] += 1
                results['total_links_found'] += len(search_results['permit_links'])
                
                # Add permit details to results
                for link in search_results['permit_links']:
                    results['permit_details'].append({
                        'permit_title': permit_title,
                        'facility_name': facility_name,
                        'permit_number': permit_number,
                        'search_url': search_url,
                        'permit_detail_url': link['url'],
                        'permit_id': link['permit_id'],
                        'link_text': link['text']
                    })
                
                print(f"  Found {len(search_results['permit_links'])} EPA permit hub links")
            else:
                results['permits_without_links'] += 1
                print(f"  No EPA permit hub links found for: {permit_title}")
            
            # Add delay between searches to be respectful
            time.sleep(3)
        
        # Save results to CSV
        if results['permit_details']:
            results_df = pd.DataFrame(results['permit_details'])
            csv_file = output_dir / "permit_detail_links.csv"
            results_df.to_csv(csv_file, index=False)
            print(f"\nSaved {len(results_df)} permit detail links to: {csv_file}")
        
        # Save summary to JSON
        summary_file = output_dir / "processing_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        
        # Print summary
        print("\n" + "=" * 60)
        print("PROCESSING SUMMARY")
        print("=" * 60)
        print(f"Total permits processed: {results['total_processed']}")
        print(f"Permits with EPA links: {results['permits_with_links']}")
        print(f"Permits without EPA links: {results['permits_without_links']}")
        print(f"Total EPA permit hub links found: {results['total_links_found']}")
        if results['total_processed'] > 0:
            success_rate = results['permits_with_links']/results['total_processed']*100
            print(f"Success rate: {success_rate:.1f}%")
        
        print(f"\nResults saved to: {output_dir}")
        
        return results
    
    def save_results(self, results, output_dir=None):
        """
        Save scraped results to files.
        
        Args:
            results (dict): Scraped results
            output_dir (str): Output directory
        """
        if output_dir is None:
            output_dir = RAW_DATA_DIR / "epa_permit_scraped"
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Save permit links to CSV
        if results.get('permit_links'):
            links_df = pd.DataFrame(results['permit_links'])
            links_file = output_dir / "permit_links.csv"
            links_df.to_csv(links_file, index=False)
            print(f"Saved {len(links_df)} permit links to: {links_file}")
        
        # Save full results to JSON
        results_file = output_dir / "scraping_results.json"
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        
        print(f"Saved full results to: {results_file}")

def main():
    """Main function to run the EPA permit scraper."""
    print("EPA Permit Hub Scraper (Selenium)")
    print("=" * 35)
    
    # Initialize scraper
    scraper = EPAPermitScraper(headless=True)
    
    try:
        # Load permit data
        df = scraper.load_permit_data()
        
        # Ask user for processing options
        max_permits = input("Enter maximum number of permits to process (or press Enter for all): ").strip()
        max_permits = int(max_permits) if max_permits else None
        
        # Process permits
        results = scraper.process_permit_records(df, max_permits=max_permits)
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Clean up
        if hasattr(scraper, 'driver'):
            scraper.driver.quit()

if __name__ == "__main__":
    main() 