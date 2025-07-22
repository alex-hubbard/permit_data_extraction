import requests
from urllib.parse import urljoin
import time

def test_state_urls():
    """Test EPA Permit Hub URLs for different states."""
    
    base_url = "https://permitsearch.epa.gov/oms-permit-hub/"
    test_states = ['CT', 'CA', 'NY']
    
    # Set up session headers
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })
    
    print("Testing EPA Permit Hub State URLs")
    print("=" * 40)
    
    for state in test_states:
        print(f"\n--- Testing {state} ---")
        
        # Construct URL
        search_url = f"{base_url}?media=air&type=TITLE_V&state={state}"
        print(f"URL: {search_url}")
        
        try:
            # Make request
            response = session.get(search_url, timeout=30)
            print(f"Status Code: {response.status_code}")
            print(f"Response URL: {response.url}")
            
            # Check if redirected
            if response.url != search_url:
                print(f"REDIRECTED from {search_url} to {response.url}")
            
            # Check content length
            content_length = len(response.content)
            print(f"Content Length: {content_length} bytes")
            
            # Check for permit links in HTML
            html_content = response.text
            permit_links = html_content.count('/permit/')
            print(f"Permit links found in HTML: {permit_links}")
            
            # Check for common patterns
            if "no results" in html_content.lower():
                print("Found 'no results' text")
            if "no data" in html_content.lower():
                print("Found 'no data' text")
            if "no permits" in html_content.lower():
                print("Found 'no permits' text")
            if "search results" in html_content.lower():
                print("Found 'search results' text")
            
            # Save HTML for inspection
            with open(f"test_{state}_page.html", 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"HTML saved to: test_{state}_page.html")
            
        except Exception as e:
            print(f"Error: {e}")
        
        # Add delay between requests
        time.sleep(2)
    
    print("\n" + "=" * 40)
    print("Testing complete!")

if __name__ == "__main__":
    test_state_urls() 