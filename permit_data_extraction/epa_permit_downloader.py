import requests
import pandas as pd
import time
import re
import os
from urllib.parse import urljoin, urlparse, quote
from bs4 import BeautifulSoup
from pathlib import Path
import json
from googlesearch import search

from permit_data_extraction.config import EXTERNAL_DATA_DIR, RAW_DATA_DIR

class EPAPermitDownloader:
    def __init__(self):
        """
        Initialize the EPA Permit Downloader.
        """
        self.base_url = "https://permitsearch.epa.gov/oms-permit-hub/"
        self.session = requests.Session()
        
        # Set up session headers to mimic a browser
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
    
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
    
    def search_permit_on_google(self, permit_title):
        """
        Search for a permit on Google using googlesearch-python library.
        
        Args:
            permit_title (str): The permit title to search for
            
        Returns:
            dict: Search results with permit detail links
        """
        try:
            # Clean the permit title by removing special characters
            import re
            cleaned_title = re.sub(r'[^\w\s\-\.]', ' ', permit_title)
            # Remove extra whitespace
            cleaned_title = ' '.join(cleaned_title.split())
            
            # Construct the search query
            search_query = f"epa permit hub {cleaned_title}"
            
            print(f"  Searching Google: {cleaned_title[:60]}...")
            
            # Use googlesearch-python to get search results
            search_results = []
            try:
                # Get first 10 search results
                for url in search(search_query, num_results=10, lang="en"):
                    if "epa.gov/oms-permit-hub/permit" in url:
                        search_results.append(url)
            except Exception as e:
                print(f"    Google search error: {e}")
                return {
                    'search_query': search_query,
                    'permit_detail_links': [],
                    'permit_title': permit_title,
                    'error': str(e)
                }
            
            # Look for EPA permit hub links in the search results
            permit_detail_links = self.extract_epa_links_from_results(search_results)
            
            return {
                'search_query': search_query,
                'search_results': search_results,
                'permit_detail_links': permit_detail_links,
                'permit_title': permit_title
            }
            
        except Exception as e:
            print(f"  Error searching Google: {e}")
            return {
                'search_query': search_query if 'search_query' in locals() else None,
                'permit_detail_links': [],
                'permit_title': permit_title,
                'error': str(e)
            }
    
    def extract_epa_links_from_results(self, search_results):
        """
        Extract EPA permit hub links from Google search results.
        
        Args:
            search_results (list): List of URLs from Google search
            
        Returns:
            list: List of permit detail link dictionaries
        """
        permit_links = []
        
        try:
            # Look for links that match the EPA permit hub pattern
            # Pattern: permitsearch.epa.gov/oms-permit-hub/permit/{uuid}
            epa_permit_pattern = r'permitsearch\.epa\.gov/oms-permit-hub/permit/[a-f0-9\-]+'
            
            for url in search_results:
                # Check if it matches the EPA permit hub pattern
                if re.search(epa_permit_pattern, url):
                    permit_links.append({
                        'url': url,
                        'text': 'EPA Permit Hub Link',
                        'permit_id': self.extract_permit_id(url)
                    })
            
            # Remove duplicates
            seen_urls = set()
            unique_links = []
            for link in permit_links:
                if link['url'] not in seen_urls:
                    seen_urls.add(link['url'])
                    unique_links.append(link)
            
            print(f"    Found {len(unique_links)} EPA permit hub links in Google results")
            return unique_links
            
        except Exception as e:
            print(f"Error extracting EPA links from results: {e}")
            return []
    
    def extract_permit_id(self, url):
        """
        Extract permit ID from permit detail URL.
        
        Args:
            url (str): Permit detail URL
            
        Returns:
            str: Permit ID
        """
        match = re.search(r'/permit/([a-f0-9\-]+)', url)
        return match.group(1) if match else None
    
    def process_permit_records(self, df, output_dir=None, max_permits=None):
        """
        Process permit records and find permit detail page links via Google search.
        
        Args:
            df (pd.DataFrame): DataFrame with permit records
            output_dir (str): Output directory for results
            max_permits (int): Maximum number of permits to process
            
        Returns:
            dict: Summary of processing results
        """
        if output_dir is None:
            output_dir = RAW_DATA_DIR / "epa_permit_links"
        
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
        
        print(f"Processing {len(df)} permit records via Google search...")
        print(f"Output directory: {output_dir}")
        print("-" * 60)
        
        for idx, row in df.iterrows():
            permit_title = row['Permit Title']
            facility_name = row.get('Facility Name', '')
            permit_number = row.get('Permit Number', '')
            
            print(f"\nProcessing {idx + 1}/{len(df)}: {permit_title[:80]}...")
            
            # Search for the permit on Google
            search_results = self.search_permit_on_google(permit_title)
            
            if search_results.get('permit_detail_links'):
                results['permits_with_links'] += 1
                results['total_links_found'] += len(search_results['permit_detail_links'])
                
                # Add permit details to results
                for link in search_results['permit_detail_links']:
                    results['permit_details'].append({
                        'permit_title': permit_title,
                        'facility_name': facility_name,
                        'permit_number': permit_number,
                        'google_search_query': search_results.get('search_query'),
                        'permit_detail_url': link['url'],
                        'permit_id': link['permit_id'],
                        'link_text': link['text']
                    })
                
                print(f"  Found {len(search_results['permit_detail_links'])} EPA permit hub links")
            else:
                results['permits_without_links'] += 1
                print(f"  No EPA permit hub links found for: {permit_title}")
            
            # Add delay between searches to be respectful to Google
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

def main():
    """Main function to run the EPA permit downloader."""
    print("EPA Permit Hub Link Finder (Google Search)")
    print("=" * 50)
    
    # Initialize downloader
    downloader = EPAPermitDownloader()
    
    try:
        # Load permit data
        df = downloader.load_permit_data()
        
        # Ask user for processing options
        max_permits = input("Enter maximum number of permits to process (or press Enter for all): ").strip()
        max_permits = int(max_permits) if max_permits else None
        
        # Process permits
        results = downloader.process_permit_records(df, max_permits=max_permits)
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main() 