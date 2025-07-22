import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from state_permit_scraper import StatePermitScraper
import time

def test_updated_scraper():
    """Test the updated state scraper with better JavaScript handling."""
    
    print("Testing Updated State Scraper")
    print("=" * 30)
    
    # Initialize scraper with headless=False for debugging
    scraper = StatePermitScraper(headless=False)
    
    try:
        # Test with Connecticut first (known to work)
        print("\nTesting Connecticut...")
        ct_results = scraper.scrape_state_permit_links('CT', max_pages=1)
        
        print(f"\nCT Results:")
        print(f"  Permit links found: {len(ct_results.get('permit_links', []))}")
        print(f"  Pages scraped: {ct_results.get('total_pages_scraped', 0)}")
        
        if 'error' in ct_results:
            print(f"  Error: {ct_results['error']}")
        
        # Wait a bit before testing another state
        time.sleep(5)
        
        # Test with California
        print("\nTesting California...")
        ca_results = scraper.scrape_state_permit_links('CA', max_pages=1)
        
        print(f"\nCA Results:")
        print(f"  Permit links found: {len(ca_results.get('permit_links', []))}")
        print(f"  Pages scraped: {ca_results.get('total_pages_scraped', 0)}")
        
        if 'error' in ca_results:
            print(f"  Error: {ca_results['error']}")
        
        # Wait a bit before testing another state
        time.sleep(5)
        
        # Test with New York
        print("\nTesting New York...")
        ny_results = scraper.scrape_state_permit_links('NY', max_pages=1)
        
        print(f"\nNY Results:")
        print(f"  Permit links found: {len(ny_results.get('permit_links', []))}")
        print(f"  Pages scraped: {ny_results.get('total_pages_scraped', 0)}")
        
        if 'error' in ny_results:
            print(f"  Error: {ny_results['error']}")
        
        # Summary
        print("\n" + "=" * 40)
        print("SUMMARY")
        print("=" * 40)
        print(f"Connecticut: {len(ct_results.get('permit_links', []))} links")
        print(f"California: {len(ca_results.get('permit_links', []))} links")
        print(f"New York: {len(ny_results.get('permit_links', []))} links")
        
    except Exception as e:
        print(f"Error during testing: {e}")
    finally:
        # Keep browser open for manual inspection
        input("Press Enter to close browser...")
        if hasattr(scraper, 'driver'):
            scraper.driver.quit()

if __name__ == "__main__":
    test_updated_scraper() 