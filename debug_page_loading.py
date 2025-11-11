#!/usr/bin/env python3
"""
Debug script to check if EPA pages are loading correctly.
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

def test_page_loading():
    """Test if EPA permit pages load correctly."""
    
    test_url = "https://permitsearch.epa.gov/oms-permit-hub/permit/8a6a70b0-19bc-ef11-b8e8-001dd8001877"
    
    print("Setting up Chrome...")
    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    
    driver = webdriver.Chrome(options=chrome_options)
    
    try:
        print(f"\nLoading: {test_url}")
        driver.get(test_url)
        
        print("Waiting for page to load...")
        
        # Try different wait strategies
        print("\n1. Checking if page title loaded...")
        time.sleep(2)
        print(f"   Page title: {driver.title}")
        
        print("\n2. Waiting for tables to appear...")
        for i in range(10):
            time.sleep(1)
            tables = driver.find_elements(By.TAG_NAME, 'table')
            print(f"   After {i+1}s: Found {len(tables)} tables")
            
            if len(tables) >= 2:
                print("   ✓ Tables loaded!")
                break
        
        print("\n3. Checking for 'Final Permit' text...")
        page_source = driver.page_source
        if 'Final Permit' in page_source:
            print("   ✓ Found 'Final Permit' in page")
        else:
            print("   ✗ 'Final Permit' not found in page")
        
        print("\n4. Looking for download button...")
        try:
            final_permit_xpath = "//td[contains(text(), 'Final Permit')]/following-sibling::td//button[contains(@title, 'Download')]"
            download_button = driver.find_element(By.XPATH, final_permit_xpath)
            print(f"   ✓ Found download button!")
            print(f"   Button title: {download_button.get_attribute('title')}")
        except Exception as e:
            print(f"   ✗ Could not find download button: {e}")
            
            # Try to find Final Permit cell
            try:
                cells = driver.find_elements(By.XPATH, "//td[contains(text(), 'Final Permit')]")
                print(f"\n   Found {len(cells)} cells containing 'Final Permit'")
                for cell in cells:
                    print(f"   - Cell text: {cell.text}")
            except:
                print("   Could not find any cells with 'Final Permit'")
        
        print("\n5. Page load state...")
        print(f"   Document ready state: {driver.execute_script('return document.readyState')}")
        
        # Save page source for inspection
        print("\n6. Saving page source...")
        with open('debug_page_source.html', 'w', encoding='utf-8') as f:
            f.write(driver.page_source)
        print("   Saved to: debug_page_source.html")
        
        print("\n" + "=" * 60)
        print("DIAGNOSIS:")
        print("=" * 60)
        
        if len(tables) >= 2 and 'Final Permit' in page_source:
            print("✓ Page appears to be loading correctly")
            print("  The issue might be timing-related")
            print("  Recommendation: Increase wait time in the downloader")
        else:
            print("✗ Page is not loading properly")
            print("  Possible issues:")
            print("  - Network connectivity")
            print("  - EPA website is down or changed")
            print("  - Chrome/Selenium configuration issue")
            print("  - Need longer wait time")
        
    finally:
        driver.quit()
        print("\nBrowser closed.")


if __name__ == "__main__":
    test_page_loading()

