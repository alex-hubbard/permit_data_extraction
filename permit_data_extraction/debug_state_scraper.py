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

# Define RAW_DATA_DIR locally
RAW_DATA_DIR = Path("data/raw")

class DebugStateScraper:
    def __init__(self, headless=False):  # Set headless=False for debugging
        """
        Initialize the Debug State Scraper with Selenium.
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
        chrome_options.add_argument("--user-data-dir=/tmp/chrome_debug")
        chrome_options.add_argument("--remote-debugging-port=9222")
        
        # Initialize the driver
        self.driver = webdriver.Chrome(options=chrome_options)
        self.wait = WebDriverWait(self.driver, 10)
    
    def __del__(self):
        """Clean up the driver when the object is destroyed."""
        if hasattr(self, 'driver'):
            self.driver.quit()
    
    def construct_state_search_url(self, state_code):
        """Construct EPA Permit Hub search URL for a specific state."""
        search_url = f"{self.base_url}?media=air&type=TITLE_V&state={state_code}"
        return search_url
    
    def debug_state_page(self, state_code):
        """
        Debug a specific state page to see what's happening.
        
        Args:
            state_code (str): Two-letter state code
        """
        try:
            search_url = self.construct_state_search_url(state_code)
            print(f"\n=== DEBUGGING {state_code} ===")
            print(f"URL: {search_url}")
            
            # Navigate to the page
            self.driver.get(search_url)
            
            # Wait for the page to load
            time.sleep(5)
            
            # Get current URL (in case of redirects)
            current_url = self.driver.current_url
            print(f"Current URL: {current_url}")
            
            # Get page title
            page_title = self.driver.title
            print(f"Page Title: {page_title}")
            
            # Check for common elements
            try:
                # Look for permit links
                permit_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/permit/']")
                print(f"Permit links found: {len(permit_links)}")
                
                # Look for search results
                search_results = self.driver.find_elements(By.CSS_SELECTOR, ".search-results, .results")
                print(f"Search results containers: {len(search_results)}")
                
                # Look for no results message
                no_results = self.driver.find_elements(By.CSS_SELECTOR, ".no-results, .no-data")
                print(f"No results messages: {len(no_results)}")
                
                # Look for pagination
                pagination = self.driver.find_elements(By.CSS_SELECTOR, ".pagination, .pager")
                print(f"Pagination elements: {len(pagination)}")
                
                # Get page source for analysis
                page_source = self.driver.page_source
                
                # Save page source for debugging
                debug_file = f"debug_{state_code}_page.html"
                with open(debug_file, 'w', encoding='utf-8') as f:
                    f.write(page_source)
                print(f"Page source saved to: {debug_file}")
                
                # Check for specific text patterns
                if "no results" in page_source.lower():
                    print("Found 'no results' text in page")
                if "no data" in page_source.lower():
                    print("Found 'no data' text in page")
                if "no permits" in page_source.lower():
                    print("Found 'no permits' text in page")
                
                # Look for any links on the page
                all_links = self.driver.find_elements(By.TAG_NAME, "a")
                print(f"Total links on page: {len(all_links)}")
                
                # Show first few links
                for i, link in enumerate(all_links[:10]):
                    href = link.get_attribute('href')
                    text = link.text.strip()
                    print(f"  Link {i+1}: {text[:50]} -> {href}")
                
                return {
                    'state_code': state_code,
                    'url': search_url,
                    'current_url': current_url,
                    'page_title': page_title,
                    'permit_links_count': len(permit_links),
                    'search_results_count': len(search_results),
                    'no_results_count': len(no_results),
                    'pagination_count': len(pagination),
                    'total_links': len(all_links)
                }
                
            except Exception as e:
                print(f"Error analyzing page elements: {e}")
                return {'state_code': state_code, 'error': str(e)}
            
        except Exception as e:
            print(f"Error debugging {state_code}: {e}")
            return {'state_code': state_code, 'error': str(e)}

def main():
    """Main function to debug state scraping."""
    print("EPA Permit Hub State Debugger")
    print("=" * 30)
    
    # Initialize scraper
    scraper = DebugStateScraper(headless=False)  # Not headless for debugging
    
    try:
        # Test states that work and don't work
        test_states = ['CT', 'CA', 'NY']
        
        results = []
        for state in test_states:
            result = scraper.debug_state_page(state)
            results.append(result)
            
            # Add delay between states
            time.sleep(3)
        
        # Print summary
        print("\n" + "=" * 50)
        print("DEBUG SUMMARY")
        print("=" * 50)
        for result in results:
            print(f"\n{result['state_code']}:")
            print(f"  Permit links: {result.get('permit_links_count', 0)}")
            print(f"  Search results: {result.get('search_results_count', 0)}")
            print(f"  No results: {result.get('no_results_count', 0)}")
            print(f"  Total links: {result.get('total_links', 0)}")
            if 'error' in result:
                print(f"  Error: {result['error']}")
        
        # Save debug results
        with open('debug_results.json', 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nDebug results saved to: debug_results.json")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Keep browser open for manual inspection
        input("Press Enter to close browser...")
        if hasattr(scraper, 'driver'):
            scraper.driver.quit()

if __name__ == "__main__":
    main() 