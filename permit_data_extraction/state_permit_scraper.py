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

# Define RAW_DATA_DIR locally to avoid import issues
RAW_DATA_DIR = Path(__file__).parent.parent / "data" / "external"

class StatePermitScraper:
    def __init__(self, headless=True):
        """
        Initialize the State Permit Scraper with Selenium.
        
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
        chrome_options.add_argument("--remote-debugging-port=9224")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        
        # Initialize the driver
        self.driver = webdriver.Chrome(options=chrome_options)
        self.wait = WebDriverWait(self.driver, 30)  # Increased timeout for regional scraping
        
        # Define EPA regions and their states
        self.regions = {
            'Region 1': {
                'name': 'New England',
                'states': ['CT', 'ME', 'MA', 'NH', 'RI', 'VT']
            },
            'Region 2': {
                'name': 'New York/New Jersey',
                'states': ['NJ', 'NY', 'PR', 'VI']
            },
            'Region 3': {
                'name': 'Mid-Atlantic',
                'states': ['DE', 'MD', 'PA', 'VA', 'WV', 'DC']
            },
            'Region 4': {
                'name': 'Southeast',
                'states': ['AL', 'FL', 'GA', 'KY', 'MS', 'NC', 'SC', 'TN']
            },
            'Region 5': {
                'name': 'Great Lakes',
                'states': ['IL', 'IN', 'MI', 'MN', 'OH', 'WI']
            },
            'Region 6': {
                'name': 'South Central',
                'states': ['AR', 'LA', 'NM', 'OK', 'TX']
            },
            'Region 7': {
                'name': 'Midwest',
                'states': ['IA', 'KS', 'MO', 'NE']
            },
            'Region 8': {
                'name': 'Mountains and Plains',
                'states': ['CO', 'MT', 'ND', 'SD', 'UT', 'WY']
            },
            'Region 9': {
                'name': 'Pacific Southwest',
                'states': ['AZ', 'CA', 'HI', 'NV', 'GU', 'AS', 'MP']
            },
            'Region 10': {
                'name': 'Pacific Northwest',
                'states': ['AK', 'ID', 'OR', 'WA']
            }
        }
        
        # Define all US states and territories (for legacy state scraping)
        self.states = {
            'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California',
            'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware', 'FL': 'Florida', 'GA': 'Georgia',
            'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa',
            'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
            'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri',
            'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey',
            'NM': 'New Mexico', 'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
            'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
            'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont',
            'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
            'DC': 'District of Columbia', 'PR': 'Puerto Rico', 'VI': 'U.S. Virgin Islands', 'GU': 'Guam',
            'AS': 'American Samoa', 'MP': 'Northern Mariana Islands'
        }
    
    def __del__(self):
        """Clean up the driver when the object is destroyed."""
        if hasattr(self, 'driver'):
            self.driver.quit()
    
    def construct_region_search_url(self, region_key):
        """
        Construct EPA Permit Hub search URL for a specific region.
        
        Args:
            region_key (str): Region key (e.g., 'Region 1', 'Region 2')
            
        Returns:
            str: Complete EPA Permit Hub search URL for the region
        """
        # Extract region number from region key (e.g., 'Region 1' -> '1')
        region_number = region_key.split()[-1]
        search_url = f"{self.base_url}?media=air&type=TITLE_V&region={region_number}"
        return search_url
    
    def construct_state_search_url(self, state_code):
        """
        Construct EPA Permit Hub search URL for a specific state.
        
        Args:
            state_code (str): Two-letter state code (e.g., 'CT', 'NY')
            
        Returns:
            str: Complete EPA Permit Hub search URL for the state
        """
        search_url = f"{self.base_url}?media=air&type=TITLE_V&state={state_code}"
        return search_url
    
    def scrape_region_permit_links(self, region_key, max_pages=None):
        """
        Scrape all permit detail page links for a specific region.
        
        Args:
            region_key (str): Region key (e.g., 'Region 1', 'Region 2')
            max_pages (int): Maximum number of pages to scrape (None for all)
            
        Returns:
            dict: Scraped data including permit links and pagination info
        """
        try:
            search_url = self.construct_region_search_url(region_key)
            region_name = self.regions[region_key]['name']
            region_states = self.regions[region_key]['states']
            
            print(f"\nScraping permits for {region_name} ({region_key}): {search_url}")
            print(f"  States in region: {', '.join(region_states)}")
            
            all_permit_links = []
            current_page = 1
            
            # Navigate to the first page
            print(f"  Navigating to first page...")
            self.driver.get(search_url)
            
            # Wait for the page to load
            time.sleep(3)
            
            # Try to set items per page to 100
            self.set_items_per_page_to_100()
            
            # Wait for content to be loaded
            try:
                self.wait.until(
                    EC.any_of(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/permit/']")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".search-results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".no-results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "permit-hub-root")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='permit']")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='result']")),
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner, [class*='loading']"))
                    )
                )
                
                # Additional wait for JavaScript to fully load
                time.sleep(5)
                
            except TimeoutException:
                print(f"    Warning: Timeout waiting for initial page content to load")
                # Still wait a bit more for JavaScript
                time.sleep(3)
            
            while True:
                print(f"  Scraping page {current_page}...")
                
                # Get the page source after JavaScript has rendered
                page_source = self.driver.page_source
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # Check if the page actually loaded content
                permit_root = soup.find('permit-hub-root')
                if permit_root and not permit_root.get_text().strip():
                    print(f"    Warning: permit-hub-root is empty - JavaScript may not have loaded content")
                    # Wait a bit more and try again
                    time.sleep(3)
                    page_source = self.driver.page_source
                    soup = BeautifulSoup(page_source, 'html.parser')
                
                # Extract permit links from this page
                page_links = self.extract_permit_links(soup)
                
                if page_links:
                    # Add region information to each link
                    for link in page_links:
                        link['region_key'] = region_key
                        link['region_name'] = region_name
                        link['region_states'] = region_states
                        link['page_number'] = current_page
                    
                    all_permit_links.extend(page_links)
                    print(f"    Found {len(page_links)} permit links on page {current_page}")
                    
                    # If we found fewer than 100 links, we've likely reached the end
                    if len(page_links) < 100:
                        print(f"    Found fewer than 100 links ({len(page_links)}) - likely reached end of results")
                        break
                else:
                    print(f"    No permit links found on page {current_page}")
                    break
                
                # Check if we've reached the max pages limit
                if max_pages and current_page >= max_pages:
                    print(f"    Reached maximum pages limit ({max_pages})")
                    break
                
                # Check if next button is disabled (we're on the last page)
                if self.is_next_button_disabled():
                    print(f"    Next button is disabled - reached last page")
                    break
                
                # Try to click the next button
                if self.find_and_click_next_button():
                    current_page += 1
                    # Wait for the new page to load
                    time.sleep(3)
                    
                    # Wait for content to load on the new page
                    try:
                        self.wait.until(
                            EC.any_of(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/permit/']")),
                                EC.presence_of_element_located((By.CSS_SELECTOR, ".search-results")),
                                EC.presence_of_element_located((By.CSS_SELECTOR, ".results")),
                                EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner, [class*='loading']"))
                            )
                        )
                        time.sleep(2)  # Additional wait for content
                    except TimeoutException:
                        print(f"    Warning: Timeout waiting for page {current_page} content to load")
                else:
                    print(f"    No next button found - reached last page")
                    break
            
            results = {
                'region_key': region_key,
                'region_name': region_name,
                'region_states': region_states,
                'search_url': search_url,
                'total_pages_scraped': current_page,
                'total_permit_links': len(all_permit_links),
                'permit_links': all_permit_links
            }
            
            print(f"  Completed {region_name}: {len(all_permit_links)} permit links from {current_page} pages")
            
            return results
            
        except Exception as e:
            print(f"Error scraping {region_key}: {e}")
            return {
                'region_key': region_key,
                'region_name': self.regions[region_key]['name'],
                'region_states': self.regions[region_key]['states'],
                'error': str(e),
                'permit_links': []
            }
    
    def scrape_state_permit_links(self, state_code, max_pages=None):
        """
        Scrape all permit detail page links for a specific state.
        
        Args:
            state_code (str): Two-letter state code
            max_pages (int): Maximum number of pages to scrape (None for all)
            
        Returns:
            dict: Scraped data including permit links and pagination info
        """
        try:
            search_url = self.construct_state_search_url(state_code)
            state_name = self.states.get(state_code, state_code)
            
            print(f"\nScraping permits for {state_name} ({state_code}): {search_url}")
            
            all_permit_links = []
            current_page = 1
            total_pages = None
            
            # Navigate to the first page
            print(f"  Navigating to first page...")
            self.driver.get(search_url)
            
            # Wait for the page to load
            time.sleep(3)
            
            # Try to set items per page to 100
            self.set_items_per_page_to_100()
            
            # Wait for content to be loaded
            try:
                self.wait.until(
                    EC.any_of(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/permit/']")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".search-results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".no-results")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "permit-hub-root")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='permit']")),
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='result']")),
                        EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner, [class*='loading']"))
                    )
                )
                
                # Additional wait for JavaScript to fully load
                time.sleep(5)
                
            except TimeoutException:
                print(f"    Warning: Timeout waiting for initial page content to load")
                # Still wait a bit more for JavaScript
                time.sleep(3)
            
            while True:
                print(f"  Scraping page {current_page}...")
                
                # Get the page source after JavaScript has rendered
                page_source = self.driver.page_source
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # Check if the page actually loaded content
                permit_root = soup.find('permit-hub-root')
                if permit_root and not permit_root.get_text().strip():
                    print(f"    Warning: permit-hub-root is empty - JavaScript may not have loaded content")
                    # Wait a bit more and try again
                    time.sleep(3)
                    page_source = self.driver.page_source
                    soup = BeautifulSoup(page_source, 'html.parser')
                
                # Extract permit links from this page
                page_links = self.extract_permit_links(soup)
                
                if page_links:
                    # Add state information to each link
                    for link in page_links:
                        link['state_code'] = state_code
                        link['state_name'] = state_name
                        link['page_number'] = current_page
                    
                    all_permit_links.extend(page_links)
                    print(f"    Found {len(page_links)} permit links on page {current_page}")
                    
                    # If we found fewer than 100 links, we've likely reached the end
                    if len(page_links) < 100:
                        print(f"    Found fewer than 100 links ({len(page_links)}) - likely reached end of results")
                        break
                else:
                    print(f"    No permit links found on page {current_page}")
                    break
                
                # Check if we've reached the max pages limit
                if max_pages and current_page >= max_pages:
                    print(f"    Reached maximum pages limit ({max_pages})")
                    break
                
                # Check if next button is disabled (we're on the last page)
                if self.is_next_button_disabled():
                    print(f"    Next button is disabled - reached last page")
                    break
                
                # Try to click the next button
                if self.find_and_click_next_button():
                    current_page += 1
                    # Wait for the new page to load
                    time.sleep(3)
                    
                    # Wait for content to load on the new page
                    try:
                        self.wait.until(
                            EC.any_of(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/permit/']")),
                                EC.presence_of_element_located((By.CSS_SELECTOR, ".search-results")),
                                EC.presence_of_element_located((By.CSS_SELECTOR, ".results")),
                                EC.invisibility_of_element_located((By.CSS_SELECTOR, ".loading, .spinner, [class*='loading']"))
                            )
                        )
                        time.sleep(2)  # Additional wait for content
                    except TimeoutException:
                        print(f"    Warning: Timeout waiting for page {current_page} content to load")
                else:
                    print(f"    No next button found - reached last page")
                    break
            
            results = {
                'state_code': state_code,
                'state_name': state_name,
                'search_url': search_url,
                'total_pages_scraped': current_page,
                'total_permit_links': len(all_permit_links),
                'permit_links': all_permit_links
            }
            
            print(f"  Completed {state_name}: {len(all_permit_links)} permit links from {current_page} pages")
            
            return results
            
        except Exception as e:
            print(f"Error scraping {state_code}: {e}")
            return {
                'state_code': state_code,
                'state_name': self.states.get(state_code, state_code),
                'error': str(e),
                'permit_links': []
            }
    
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
    
    def has_next_page(self, soup):
        """
        Check if there's a next page available.
        
        Args:
            soup (BeautifulSoup): Parsed HTML content
            
        Returns:
            bool: True if next page exists, False otherwise
        """
        try:
            # Look for next page indicators
            next_indicators = [
                'Next',
                'next',
                '>',
                '»',
                'Next Page',
                'next page'
            ]
            
            # Check for next page links
            for indicator in next_indicators:
                next_link = soup.find('a', string=re.compile(indicator, re.IGNORECASE))
                if next_link and next_link.get('href'):
                    return True
            
            # Check for pagination with page numbers
            pagination = soup.find_all(['nav', 'div'], class_=re.compile(r'pagination|pager'))
            if pagination:
                # Look for current page indicator and see if there's a higher page number
                page_numbers = soup.find_all('a', href=re.compile(r'page=\d+'))
                if page_numbers:
                    return True
            
            return False
            
        except Exception as e:
            print(f"Error checking for next page: {e}")
            return False
    
    def find_and_click_next_button(self):
        """
        Find and click the next button on the current page.
        
        Returns:
            bool: True if next button was found and clicked, False otherwise
        """
        try:
            # Look for next button with various selectors
            next_selectors = [
                "button[aria-label*='Next']",
                "button[title*='Next']",
                "a[aria-label*='Next']",
                "a[title*='Next']",
                "button:contains('Next')",
                "a:contains('Next')",
                "[class*='next']",
                "[class*='pagination'] button:last-child",
                "[class*='pagination'] a:last-child"
            ]
            
            for selector in next_selectors:
                try:
                    next_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if next_button.is_displayed() and next_button.is_enabled():
                        print(f"    Found next button with selector: {selector}")
                        if self.safe_click_element(next_button):
                            time.sleep(3)  # Wait for page to load
                            return True
                except NoSuchElementException:
                    continue
            
            # Try finding by text content
            try:
                next_button = self.driver.find_element(By.XPATH, "//*[contains(text(), 'Next') or contains(text(), 'next')]")
                if next_button.is_displayed() and next_button.is_enabled():
                    print(f"    Found next button by text: {next_button.text}")
                    if self.safe_click_element(next_button):
                        time.sleep(3)  # Wait for page to load
                        return True
            except NoSuchElementException:
                pass
            
            # Try finding by partial text match
            try:
                next_button = self.driver.find_element(By.XPATH, "//*[contains(translate(text(), 'NEXT', 'next'), 'next')]")
                if next_button.is_displayed() and next_button.is_enabled():
                    print(f"    Found next button by partial text: {next_button.text}")
                    if self.safe_click_element(next_button):
                        time.sleep(3)  # Wait for page to load
                        return True
            except NoSuchElementException:
                pass
            
            print("    No next button found")
            return False
            
        except Exception as e:
            print(f"    Error clicking next button: {e}")
            return False
    
    def safe_click_element(self, element):
        """
        Safely click an element using multiple strategies to handle click interception.
        
        Args:
            element: Selenium WebElement to click
            
        Returns:
            bool: True if click was successful, False otherwise
        """
        try:
            # Strategy 1: Regular click
            try:
                element.click()
                return True
            except Exception as e:
                print(f"      Regular click failed: {e}")
            
            # Strategy 2: JavaScript click
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception as e:
                print(f"      JavaScript click failed: {e}")
            
            # Strategy 3: Scroll to element and click
            try:
                self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
                time.sleep(1)
                element.click()
                return True
            except Exception as e:
                print(f"      Scroll and click failed: {e}")
            
            # Strategy 4: Move to element and click
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                actions = ActionChains(self.driver)
                actions.move_to_element(element).click().perform()
                return True
            except Exception as e:
                print(f"      Action chains click failed: {e}")
            
            # Strategy 5: Clear any overlays and try again
            try:
                # Try to remove any potential overlays
                self.driver.execute_script("""
                    var overlays = document.querySelectorAll('.overlay, .modal, .popup, [style*="z-index"]');
                    overlays.forEach(function(overlay) {
                        overlay.style.display = 'none';
                    });
                """)
                time.sleep(1)
                element.click()
                return True
            except Exception as e:
                print(f"      Overlay removal and click failed: {e}")
            
            # Strategy 6: Handle table header interception specifically
            try:
                # Hide table headers that might be intercepting clicks
                self.driver.execute_script("""
                    var headers = document.querySelectorAll('th[data-sortable], th[role="columnheader"]');
                    headers.forEach(function(header) {
                        header.style.pointerEvents = 'none';
                        header.style.zIndex = '-1';
                    });
                """)
                time.sleep(1)
                element.click()
                return True
            except Exception as e:
                print(f"      Table header removal and click failed: {e}")
            
            # Strategy 7: Scroll to element and use JavaScript click with offset
            try:
                # Scroll element into view and click with offset
                self.driver.execute_script("""
                    arguments[0].scrollIntoView({block: 'center', inline: 'center'});
                """, element)
                time.sleep(1)
                
                # Get element position and click with offset
                location = element.location
                size = element.size
                x_offset = location['x'] + (size['width'] // 2)
                y_offset = location['y'] + (size['height'] // 2)
                
                from selenium.webdriver.common.action_chains import ActionChains
                actions = ActionChains(self.driver)
                actions.move_by_offset(x_offset, y_offset).click().perform()
                return True
            except Exception as e:
                print(f"      Offset click failed: {e}")
            
            print("      All click strategies failed")
            return False
            
        except Exception as e:
            print(f"      Error in safe_click_element: {e}")
            return False
    
    def set_items_per_page_to_100(self):
        """
        Try to set the items per page selector to 100 to maximize permits per page.
        """
        try:
            print("    Attempting to set items per page to 100...")
            
            # Strategy 1: Look for common pagination selectors
            page_size_selectors = [
                "select[aria-label*='items per page']",
                "select[aria-label*='per page']",
                "select[title*='items per page']",
                "select[title*='per page']",
                "[class*='pagination'] select",
                "[class*='page-size'] select",
                "[class*='items-per-page'] select",
                "select[name*='pageSize']",
                "select[name*='page_size']",
                "select[id*='pageSize']",
                "select[id*='page_size']"
            ]
            
            for selector in page_size_selectors:
                try:
                    select_element = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if select_element.is_displayed():
                        print(f"      Found page size selector with: {selector}")
                        self.select_option_by_value(select_element, "100")
                        return True
                except NoSuchElementException:
                    continue
            
            # Strategy 2: Look for dropdown buttons that might open a page size selector
            dropdown_selectors = [
                "button[aria-label*='items per page']",
                "button[aria-label*='per page']",
                "[class*='pagination'] button",
                "[class*='page-size'] button",
                "[class*='items-per-page'] button"
            ]
            
            for selector in dropdown_selectors:
                try:
                    dropdown_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if dropdown_button.is_displayed():
                        print(f"      Found page size dropdown button with: {selector}")
                        if self.open_page_size_dropdown(dropdown_button):
                            return True
                except NoSuchElementException:
                    continue
            
            # Strategy 3: Look for text-based selectors
            try:
                select_element = self.driver.find_element(By.XPATH, "//select[contains(., 'items per page') or contains(., 'per page')]")
                if select_element.is_displayed():
                    print("      Found page size selector by text")
                    self.select_option_by_value(select_element, "100")
                    return True
            except NoSuchElementException:
                pass
            
            print("      No page size selector found - continuing with default")
            return False
            
        except Exception as e:
            print(f"      Error setting items per page: {e}")
            return False
    
    def select_option_by_value(self, select_element, value):
        """
        Select an option from a select element by value.
        
        Args:
            select_element: Selenium WebElement (select element)
            value: Value to select
            
        Returns:
            bool: True if selection was successful, False otherwise
        """
        try:
            from selenium.webdriver.support.ui import Select
            
            select = Select(select_element)
            
            # Try to select by value
            try:
                select.select_by_value(value)
                print(f"        Selected option with value: {value}")
                time.sleep(2)  # Wait for page to update
                return True
            except Exception as e:
                print(f"        Failed to select by value '{value}': {e}")
            
            # Try to select by visible text
            try:
                select.select_by_visible_text(value)
                print(f"        Selected option with text: {value}")
                time.sleep(2)  # Wait for page to update
                return True
            except Exception as e:
                print(f"        Failed to select by text '{value}': {e}")
            
            # Try to select by index (if 100 is the last option)
            try:
                options = select.options
                if len(options) > 0:
                    # Select the last option (often the highest number)
                    last_index = len(options) - 1
                    select.select_by_index(last_index)
                    selected_text = options[last_index].text
                    print(f"        Selected last option: {selected_text}")
                    time.sleep(2)  # Wait for page to update
                    return True
            except Exception as e:
                print(f"        Failed to select by index: {e}")
            
            return False
            
        except Exception as e:
            print(f"        Error in select_option_by_value: {e}")
            return False
    
    def open_page_size_dropdown(self, dropdown_button):
        """
        Open a page size dropdown and select 100.
        
        Args:
            dropdown_button: Selenium WebElement (dropdown button)
            
        Returns:
            bool: True if selection was successful, False otherwise
        """
        try:
            # Click the dropdown button
            if self.safe_click_element(dropdown_button):
                time.sleep(1)
                
                # Look for the option with value 100
                option_selectors = [
                    "option[value='100']",
                    "li[data-value='100']",
                    "[role='option'][data-value='100']",
                    "*[data-value='100']"
                ]
                
                for selector in option_selectors:
                    try:
                        option = self.driver.find_element(By.CSS_SELECTOR, selector)
                        if option.is_displayed():
                            print(f"        Found option 100 with selector: {selector}")
                            if self.safe_click_element(option):
                                time.sleep(2)  # Wait for page to update
                                return True
                    except NoSuchElementException:
                        continue
                
                # Try to find by text
                try:
                    option = self.driver.find_element(By.XPATH, "//*[contains(text(), '100')]")
                    if option.is_displayed():
                        print("        Found option 100 by text")
                        if self.safe_click_element(option):
                            time.sleep(2)  # Wait for page to update
                            return True
                except NoSuchElementException:
                    pass
                
                print("        Could not find option 100 in dropdown")
                return False
            else:
                print("        Failed to click dropdown button")
                return False
                
        except Exception as e:
            print(f"        Error opening page size dropdown: {e}")
            return False
    
    def is_next_button_disabled(self):
        """
        Check if the next button is disabled (indicating we're on the last page).
        
        Returns:
            bool: True if next button is disabled, False otherwise
        """
        try:
            # Look for disabled next button indicators
            disabled_selectors = [
                "button[aria-label*='Next'][disabled]",
                "button[title*='Next'][disabled]",
                "a[aria-label*='Next'][disabled]",
                "a[title*='Next'][disabled]",
                "[class*='next'][disabled]",
                "[class*='next'][aria-disabled='true']",
                "[class*='pagination'] button:last-child[disabled]",
                "[class*='pagination'] a:last-child[disabled]"
            ]
            
            for selector in disabled_selectors:
                try:
                    disabled_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if disabled_button.is_displayed():
                        print(f"    Found disabled next button with selector: {selector}")
                        return True
                except NoSuchElementException:
                    continue
            
            # Check for "disabled" class or similar
            try:
                disabled_button = self.driver.find_element(By.CSS_SELECTOR, "[class*='next'][class*='disabled']")
                if disabled_button.is_displayed():
                    print(f"    Found disabled next button with disabled class")
                    return True
            except NoSuchElementException:
                pass
            
            return False
            
        except Exception as e:
            print(f"    Error checking if next button is disabled: {e}")
            return False
    
    def scrape_all_regions(self, regions_to_scrape=None, max_pages_per_region=None, output_dir=None):
        """
        Scrape permit links for all regions or specified regions.
        
        Args:
            regions_to_scrape (list): List of region keys to scrape (None for all)
            max_pages_per_region (int): Maximum pages per region (None for all)
            output_dir (str): Output directory for results
            
        Returns:
            dict: Summary of all scraping results
        """
        if output_dir is None:
            output_dir = RAW_DATA_DIR / "region_permit_links"
        
        os.makedirs(output_dir, exist_ok=True)
        
        if regions_to_scrape is None:
            regions_to_scrape = list(self.regions.keys())
        
        all_results = {
            'total_regions_processed': 0,
            'total_permit_links': 0,
            'regions_with_links': 0,
            'regions_without_links': 0,
            'regions_with_no_permits': [],
            'region_results': []
        }
    
    def scrape_all_states(self, states_to_scrape=None, max_pages_per_state=None, output_dir=None):
        """
        Scrape permit links for all states or specified states.
        
        Args:
            states_to_scrape (list): List of state codes to scrape (None for all)
            max_pages_per_state (int): Maximum pages per state (None for all)
            output_dir (str): Output directory for results
            
        Returns:
            dict: Summary of all scraping results
        """
        if output_dir is None:
            output_dir = RAW_DATA_DIR / "state_permit_links"
        
        os.makedirs(output_dir, exist_ok=True)
        
        if states_to_scrape is None:
            states_to_scrape = list(self.states.keys())
        
        all_results = {
            'total_states_processed': 0,
            'total_permit_links': 0,
            'states_with_links': 0,
            'states_without_links': 0,
            'states_with_no_permits': [],  # New list for states with no permits
            'state_results': []
        }
        
        print(f"Starting state-by-state permit scraping...")
        print(f"States to process: {len(states_to_scrape)}")
        print(f"Max pages per state: {max_pages_per_state or 'All'}")
        print(f"Output directory: {output_dir}")
        print("=" * 60)
        
        for i, state_code in enumerate(states_to_scrape, 1):
            state_name = self.states.get(state_code, state_code)
            print(f"\nProcessing state {i}/{len(states_to_scrape)}: {state_name} ({state_code})")
            
            # Scrape permits for this state
            state_results = self.scrape_state_permit_links(state_code, max_pages_per_state)
            
            all_results['state_results'].append(state_results)
            all_results['total_states_processed'] += 1
            
            if state_results.get('permit_links'):
                all_results['states_with_links'] += 1
                all_results['total_permit_links'] += len(state_results['permit_links'])
            else:
                all_results['states_without_links'] += 1
                # Add state to the no permits list
                all_results['states_with_no_permits'].append({
                    'state_code': state_code,
                    'state_name': state_name,
                    'search_url': state_results.get('search_url'),
                    'error': state_results.get('error', None)
                })
            
            # Save individual state results
            state_file = output_dir / f"{state_code}_permit_links.json"
            with open(state_file, 'w') as f:
                json.dump(state_results, f, indent=2, default=str)
            
            # Add delay between states
            time.sleep(3)
        
        # Save combined results
        combined_file = output_dir / "all_states_permit_links.json"
        with open(combined_file, 'w') as f:
            json.dump(all_results, f, indent=2, default=str)
        
        # Save states with no permits to separate file
        if all_results['states_with_no_permits']:
            no_permits_file = output_dir / "states_with_no_permits.json"
            with open(no_permits_file, 'w') as f:
                json.dump(all_results['states_with_no_permits'], f, indent=2, default=str)
            print(f"\nSaved {len(all_results['states_with_no_permits'])} states with no permits to: {no_permits_file}")
            
            # Also save as CSV for easy viewing
            no_permits_df = pd.DataFrame(all_results['states_with_no_permits'])
            no_permits_csv = output_dir / "states_with_no_permits.csv"
            no_permits_df.to_csv(no_permits_csv, index=False)
            print(f"Saved states with no permits to: {no_permits_csv}")
        
        # Create combined CSV
        all_links = []
        for state_result in all_results['state_results']:
            for link in state_result.get('permit_links', []):
                all_links.append(link)
        
        if all_links:
            links_df = pd.DataFrame(all_links)
            csv_file = output_dir / "all_permit_links.csv"
            links_df.to_csv(csv_file, index=False)
            print(f"\nSaved {len(links_df)} total permit links to: {csv_file}")
        
        # Print summary
        print("\n" + "=" * 60)
        print("STATE-BY-STATE SCRAPING SUMMARY")
        print("=" * 60)
        print(f"Total states processed: {all_results['total_states_processed']}")
        print(f"States with permit links: {all_results['states_with_links']}")
        print(f"States without permit links: {all_results['states_without_links']}")
        print(f"Total permit links found: {all_results['total_permit_links']}")
        
        if all_results['total_states_processed'] > 0:
            success_rate = all_results['states_with_links']/all_results['total_states_processed']*100
            print(f"Success rate: {success_rate:.1f}%")
        
        # Print states with no permits
        if all_results['states_with_no_permits']:
            print(f"\nStates with no permits found ({len(all_results['states_with_no_permits'])}):")
            for state_info in all_results['states_with_no_permits']:
                error_msg = f" (Error: {state_info['error']})" if state_info.get('error') else ""
                print(f"  - {state_info['state_name']} ({state_info['state_code']}){error_msg}")
        
        print(f"\nResults saved to: {output_dir}")
        
        return all_results
        
        print(f"Starting state-by-state permit scraping...")
        print(f"States to process: {len(states_to_scrape)}")
        print(f"Max pages per state: {max_pages_per_state or 'All'}")
        print(f"Output directory: {output_dir}")
        print("=" * 60)
        
        for i, state_code in enumerate(states_to_scrape, 1):
            state_name = self.states.get(state_code, state_code)
            print(f"\nProcessing state {i}/{len(states_to_scrape)}: {state_name} ({state_code})")
            
            # Scrape permits for this state
            state_results = self.scrape_state_permit_links(state_code, max_pages_per_state)
            
            all_results['state_results'].append(state_results)
            all_results['total_states_processed'] += 1
            
            if state_results.get('permit_links'):
                all_results['states_with_links'] += 1
                all_results['total_permit_links'] += len(state_results['permit_links'])
            else:
                all_results['states_without_links'] += 1
                # Add state to the no permits list
                all_results['states_with_no_permits'].append({
                    'state_code': state_code,
                    'state_name': state_name,
                    'search_url': state_results.get('search_url'),
                    'error': state_results.get('error', None)
                })
            
            # Save individual state results
            state_file = output_dir / f"{state_code}_permit_links.json"
            with open(state_file, 'w') as f:
                json.dump(state_results, f, indent=2, default=str)
            
            # Add delay between states
            time.sleep(3)
        
        # Save combined results
        combined_file = output_dir / "all_states_permit_links.json"
        with open(combined_file, 'w') as f:
            json.dump(all_results, f, indent=2, default=str)
        
        # Save states with no permits to separate file
        if all_results['states_with_no_permits']:
            no_permits_file = output_dir / "states_with_no_permits.json"
            with open(no_permits_file, 'w') as f:
                json.dump(all_results['states_with_no_permits'], f, indent=2, default=str)
            print(f"\nSaved {len(all_results['states_with_no_permits'])} states with no permits to: {no_permits_file}")
            
            # Also save as CSV for easy viewing
            no_permits_df = pd.DataFrame(all_results['states_with_no_permits'])
            no_permits_csv = output_dir / "states_with_no_permits.csv"
            no_permits_df.to_csv(no_permits_csv, index=False)
            print(f"Saved states with no permits to: {no_permits_csv}")
        
        # Create combined CSV
        all_links = []
        for state_result in all_results['state_results']:
            for link in state_result.get('permit_links', []):
                all_links.append(link)
        
        if all_links:
            links_df = pd.DataFrame(all_links)
            csv_file = output_dir / "all_permit_links.csv"
            links_df.to_csv(csv_file, index=False)
            print(f"\nSaved {len(links_df)} total permit links to: {csv_file}")
        
        # Print summary
        print("\n" + "=" * 60)
        print("STATE-BY-STATE SCRAPING SUMMARY")
        print("=" * 60)
        print(f"Total states processed: {all_results['total_states_processed']}")
        print(f"States with permit links: {all_results['states_with_links']}")
        print(f"States without permit links: {all_results['states_without_links']}")
        print(f"Total permit links found: {all_results['total_permit_links']}")
        
        if all_results['total_states_processed'] > 0:
            success_rate = all_results['states_with_links']/all_results['total_states_processed']*100
            print(f"Success rate: {success_rate:.1f}%")
        
        # Print states with no permits
        if all_results['states_with_no_permits']:
            print(f"\nStates with no permits found ({len(all_results['states_with_no_permits'])}):")
            for state_info in all_results['states_with_no_permits']:
                error_msg = f" (Error: {state_info['error']})" if state_info.get('error') else ""
                print(f"  - {state_info['state_name']} ({state_info['state_code']}){error_msg}")
        
        print(f"\nResults saved to: {output_dir}")
        
        return all_results

def main():
    """Main function to run the state permit scraper."""
    print("EPA Permit Hub State-by-State Scraper")
    print("=" * 40)
    
    # Initialize scraper
    scraper = StatePermitScraper(headless=True)
    
    try:
        # Ask user for options
        print("\nOptions:")
        print("1. Scrape all states")
        print("2. Scrape specific states")
        print("3. Test with a few states")
        print("4. Scrape all regions (experimental)")
        
        choice = input("Enter your choice (1-4): ").strip()
        
        if choice == "1":
            states_to_scrape = None  # All states
            max_pages = input("Enter maximum pages per state (or press Enter for all): ").strip()
            max_pages = int(max_pages) if max_pages else None
            
            # Start scraping states
            results = scraper.scrape_all_states(
                states_to_scrape=states_to_scrape,
                max_pages_per_state=max_pages
            )
            
        elif choice == "2":
            print("\nAvailable states:")
            for i, (state_code, state_name) in enumerate(scraper.states.items(), 1):
                if i <= 20:  # Show first 20 states
                    print(f"{i}. {state_name} ({state_code})")
                elif i == 21:
                    print("... (and more)")
                    break
            
            state_input = input("Enter state codes separated by commas (e.g., CT,NY,CA): ").strip()
            states_to_scrape = [s.strip().upper() for s in state_input.split(',')]
            
            max_pages = input("Enter maximum pages per state (or press Enter for all): ").strip()
            max_pages = int(max_pages) if max_pages else None
            
            # Start scraping states
            results = scraper.scrape_all_states(
                states_to_scrape=states_to_scrape,
                max_pages_per_state=max_pages
            )
            
        elif choice == "3":
            states_to_scrape = ['CT', 'NY', 'CA']  # Test states
            max_pages = input("Enter maximum pages per state (or press Enter for all): ").strip()
            max_pages = int(max_pages) if max_pages else None
            
            # Start scraping states
            results = scraper.scrape_all_states(
                states_to_scrape=states_to_scrape,
                max_pages_per_state=max_pages
            )
            
        elif choice == "4":
            # Experimental region scraping
            print("\nRegion scraping options (experimental - may be slow):")
            print("1. Scrape all regions")
            print("2. Scrape specific regions")
            print("3. Test with a few regions")
            
            region_choice = input("Enter your choice (1-3): ").strip()
            
            if region_choice == "1":
                regions_to_scrape = None  # All regions
            elif region_choice == "2":
                print("\nAvailable regions:")
                for i, (region_key, region_info) in enumerate(scraper.regions.items(), 1):
                    print(f"{i}. {region_info['name']} ({region_key}) - States: {', '.join(region_info['states'])}")
                
                region_input = input("Enter region numbers separated by commas (e.g., 1,2,3): ").strip()
                region_numbers = [int(r.strip()) for r in region_input.split(',')]
                region_keys = list(scraper.regions.keys())
                regions_to_scrape = [region_keys[i-1] for i in region_numbers if 1 <= i <= len(region_keys)]
            elif region_choice == "3":
                regions_to_scrape = ['Region 1', 'Region 2', 'Region 9']  # Test regions
            else:
                print("Invalid choice. Using test regions.")
                regions_to_scrape = ['Region 1', 'Region 2', 'Region 9']
            
            max_pages = input("Enter maximum pages per region (or press Enter for all): ").strip()
            max_pages = int(max_pages) if max_pages else None
            
            # Start scraping regions
            results = scraper.scrape_all_regions(
                regions_to_scrape=regions_to_scrape,
                max_pages_per_region=max_pages
            )
        else:
            print("Invalid choice. Using test states.")
            states_to_scrape = ['CT', 'NY', 'CA']
            max_pages = 1
            
            # Start scraping states
            results = scraper.scrape_all_states(
                states_to_scrape=states_to_scrape,
                max_pages_per_state=max_pages
            )
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Clean up
        if hasattr(scraper, 'driver'):
            scraper.driver.quit()

if __name__ == "__main__":
    main() 