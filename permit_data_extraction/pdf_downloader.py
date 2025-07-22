import requests
from bs4 import BeautifulSoup
import os
from urllib.parse import urljoin, urlparse
import time
import mimetypes
import re
import json
import pandas as pd
from typing import List, Tuple, Optional

from permit_data_extraction.config import RAW_DATA_DIR

# LLM Configuration
LLM_API_KEY = os.getenv('API_KEY')  # Set your Google API key as environment variable
LLM_MODEL = "gemini-2.0-flash"  # You can change this to gemini-1.5-pro for better accuracy
LLM_ENABLED = LLM_API_KEY is not None

def clean_filename(filename):
    """
    Clean the filename by removing invalid characters and extra spaces.
    
    Args:
        filename (str): The filename to clean
    
    Returns:
        str: Cleaned filename
    """
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Replace multiple spaces with single space
    filename = re.sub(r'\s+', ' ', filename)
    # Strip leading/trailing spaces
    filename = filename.strip()
    return filename

def is_pdf_link(url, response=None):
    """
    Check if a URL points to a PDF file by checking both the URL and content type.
    
    Args:
        url (str): The URL to check
        response (requests.Response, optional): Response object if already fetched
    
    Returns:
        bool: True if the URL points to a PDF, False otherwise
    """
    # Check URL extension
    if url.lower().endswith('.pdf'):
        return True
    
    # If we have a response, check content type
    if response:
        content_type = response.headers.get('content-type', '').lower()
        if 'application/pdf' in content_type:
            return True
    
    return False

def is_permit_related_link_keywords(link_text, href):
    """
    Check if a link contains the word "permit" in the link text or URL.
    
    Args:
        link_text (str): The text content of the link
        href (str): The URL of the link
    
    Returns:
        bool: True if the link contains "permit", False otherwise
    """
    # Convert to lowercase for case-insensitive matching
    link_text_lower = link_text.lower()
    href_lower = href.lower()
    
    # Check for "permit" (including plural "permits")
    return 'permit' in link_text_lower or 'permit' in href_lower

def evaluate_links_with_llm(links_data: List[Tuple[str, str, str, bool]], page_context: str = "") -> List[Tuple[str, str, float, str, bool]]:
    """
    Use Gemini to evaluate which links are likely to contain permits for specific facilities.
    
    Args:
        links_data: List of tuples (url, link_text, surrounding_context, is_table_link)
        page_context: Additional context about the current page
    
    Returns:
        List of tuples (url, link_text, confidence_score, facility_name, is_table_link) for likely facility-specific permit links
    """
    if not LLM_ENABLED:
        return []
    
    if not links_data:
        return []
    
    try:
        # Prepare the prompt
        links_for_analysis = []
        for i, (url, link_text, context, is_table_link) in enumerate(links_data):
            links_for_analysis.append({
                "id": i,
                "url": url,
                "link_text": link_text,
                "context": context,
                "in_table": is_table_link
            })
        
        prompt = f"""
You are analyzing links from a government or municipal website to identify which ones are likely to contain permits for SPECIFIC FACILITIES (like power plants, refineries, hospitals, manufacturing plants, etc.) rather than general permit information.

Page Context: {page_context[:500]}

Links to analyze:
{json.dumps(links_for_analysis, indent=2)}

For each link, determine if it's likely to lead to permits or documents for a SPECIFIC NAMED FACILITY rather than general permit information, databases, or application forms.

Look for:
- Links that mention specific facility names (e.g., "Central Power Plant", "Memorial Hospital", "ABC Manufacturing")
- Permits for specific projects or locations
- Document repositories for particular facilities
- Links that suggest facility-specific regulatory documents

IMPORTANT: Give higher priority to links that are in tables (in_table: true), as these often contain structured permit data.

DO NOT select links that are:
- General permit application forms
- Permit databases or search pages
- General planning documents
- Municipal codes or regulations
- Generic permit information

Respond with a JSON array where each object has:
- "id": the link id number
- "likely_facility_specific": boolean (true if likely to contain permits for a specific facility)
- "confidence": float between 0.0 and 1.0
- "facility_name": string (name of the facility if identifiable, or "Unknown Facility" if not clear)
- "reasoning": brief explanation

Only return the JSON array, no other text.
"""

        # Call Google Gemini API
        headers = {
            'Content-Type': 'application/json'
        }
        
        data = {
            'contents': [
                {
                    'parts': [
                        {
                            'text': prompt
                        }
                    ]
                }
            ],
            'generationConfig': {
                'temperature': 0.3,
                'maxOutputTokens': 1000,
                'candidateCount': 1
            }
        }
        
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{LLM_MODEL}:generateContent?key={LLM_API_KEY}'
        
        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'candidates' in result and len(result['candidates']) > 0:
                content = result['candidates'][0]['content']['parts'][0]['text'].strip()
            else:
                print(f"  Unexpected Gemini response format: {result}")
                return []
            
            # Parse the JSON response
            try:
                evaluations = json.loads(content)
                permit_links = []
                
                for eval_result in evaluations:
                    if eval_result.get('likely_facility_specific', False):
                        link_id = eval_result['id']
                        confidence = eval_result.get('confidence', 0.5)
                        facility_name = eval_result.get('facility_name', 'Unknown Facility')
                        if link_id < len(links_data):
                            url, link_text, _, is_table_link = links_data[link_id]
                            permit_links.append((url, link_text, confidence, facility_name, is_table_link))
                
                return permit_links
                
            except json.JSONDecodeError as e:
                print(f"  Error parsing LLM response: {e}")
                return []
        else:
            print(f"  Gemini API error: {response.status_code} - {response.text}")
            return []
            
    except Exception as e:
        print(f"  Error calling Gemini: {e}")
        return []

def get_link_context(link_element, soup):
    """
    Get surrounding context for a link to help Gemini evaluation.
    
    Args:
        link_element: BeautifulSoup link element
        soup: BeautifulSoup object of the page
    
    Returns:
        str: Surrounding context text
    """
    context_parts = []
    
    # Get parent element text
    parent = link_element.parent
    if parent:
        parent_text = parent.get_text().strip()
        if parent_text and parent_text != link_element.get_text().strip():
            context_parts.append(parent_text[:200])
    
    # Get preceding and following siblings
    prev_sibling = link_element.previous_sibling
    if prev_sibling and hasattr(prev_sibling, 'get_text'):
        prev_text = prev_sibling.get_text().strip()
        if prev_text:
            context_parts.append(prev_text[-100:])
    
    next_sibling = link_element.next_sibling
    if next_sibling and hasattr(next_sibling, 'get_text'):
        next_text = next_sibling.get_text().strip()
        if next_text:
            context_parts.append(next_text[:100])
    
    return " | ".join(context_parts)

def download_pdf_from_page(url, output_dir, visited_urls, depth=0, max_depth=2, use_llm=True):
    """
    Download PDF files from a given URL and optionally follow facility-specific permit links.
    
    Args:
        url (str): The URL of the website to scrape for PDFs
        output_dir (str): Directory where PDFs will be saved
        visited_urls (set): Set of URLs already visited to avoid duplicates
        depth (int): Current recursion depth
        max_depth (int): Maximum depth to follow permit-related links
        use_llm (bool): Whether to use Gemini for enhanced link identification
    
    Returns:
        int: Number of PDFs downloaded from this page and its sub-pages
    """
    if url in visited_urls:
        return 0
    
    visited_urls.add(url)
    downloaded_count = 0
    
    print(f"{'  ' * depth}Processing: {url}")
    
    try:
        # Get the page content
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Parse the HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Remove navigation elements that might contain irrelevant links
        navigation_selectors = [
            'nav', '.nav', '.navigation', '.menu', '.navbar',
            '.header', '.footer', '.sidebar', '.breadcrumb',
            '.panel-sidebar', '.rail', '.left-rail', '.right-rail',
            '.quick-links', '.related-links', '.see-also', '.in-this-section'
        ]
        
        # Remove these elements from the soup
        for selector in navigation_selectors:
            for element in soup.select(selector):
                element.decompose()
        
        # Find all links in the remaining content (main body)
        all_links = soup.find_all('a')
        
        # Separate table links from other links (prioritize table links)
        table_links = []
        other_links = []
        
        for link in all_links:
            # Check if link is inside a table
            if link.find_parent('table'):
                table_links.append(link)
            else:
                other_links.append(link)
        
        # Process table links first (higher priority), then other links
        links = table_links + other_links
        
        permit_links = []
        llm_candidate_links = []
        
        print(f"{'  ' * depth}Found {len(table_links)} table links and {len(other_links)} other content links")
        
        # Process each link (table links processed first due to ordering above)
        for i, link in enumerate(links):
            is_table_link = i < len(table_links)  # First portion are table links
            href = link.get('href')
            if href:
                # Convert relative URL to absolute URL
                full_url = urljoin(url, href)
                
                # Skip external domains to avoid crawling the entire internet
                if urlparse(full_url).netloc != urlparse(url).netloc:
                    continue
                
                try:
                    # Get the file
                    file_response = requests.get(full_url, stream=True, timeout=30)
                    file_response.raise_for_status()
                    
                    # Check if it's a PDF
                    if is_pdf_link(full_url, file_response):
                        # Get the link text and clean it
                        link_text = link.get_text().strip()
                        if link_text:
                            filename = clean_filename(link_text)
                        else:
                            # If no link text, try to get filename from URL or Content-Disposition
                            filename = os.path.basename(urlparse(full_url).path)
                            if not filename or filename == '':
                                content_disposition = file_response.headers.get('content-disposition')
                                if content_disposition:
                                    filename_match = re.findall("filename=(.+)", content_disposition)
                                    if filename_match:
                                        filename = filename_match[0].strip('"')
                            
                            # If still no filename, use a default name
                            if not filename:
                                filename = f"document_{len(os.listdir(output_dir)) + 1}"
                        
                        # Ensure filename ends with .pdf
                        if not filename.lower().endswith('.pdf'):
                            filename += '.pdf'
                        
                        # Create unique filename if file already exists
                        counter = 1
                        original_filename = filename
                        while os.path.exists(os.path.join(output_dir, filename)):
                            name, ext = os.path.splitext(original_filename)
                            filename = f"{name}_{counter}{ext}"
                            counter += 1
                        
                        # Save the file
                        filepath = os.path.join(output_dir, filename)
                        with open(filepath, 'wb') as f:
                            for chunk in file_response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        
                        table_indicator = " [TABLE]" if is_table_link else ""
                        print(f"{'  ' * depth}Downloaded: {filename}{table_indicator}")
                        downloaded_count += 1
                        
                        # Add a small delay to be nice to the server
                        time.sleep(1)
                    
                    # If we haven't reached max depth and this link might lead to permits
                    elif depth < max_depth and full_url not in visited_urls:
                        link_text = link.get_text().strip()
                        
                        # First pass: keyword-based detection
                        if is_permit_related_link_keywords(link_text, href):
                            table_indicator = " [TABLE]" if is_table_link else ""
                            permit_links.append((full_url, link_text, is_table_link))
                            print(f"{'  ' * depth}Found permit link: {link_text[:40]}{'...' if len(link_text) > 40 else ''}{table_indicator}")
                        # Second pass: collect for Gemini evaluation if enabled
                        elif use_llm and LLM_ENABLED:
                            context = get_link_context(link, soup)
                            llm_candidate_links.append((full_url, link_text, context, is_table_link))
                    
                except Exception as e:
                    print(f"{'  ' * depth}Error accessing {full_url}: {str(e)}")
        
        # Use Gemini to evaluate remaining links if enabled
        if use_llm and LLM_ENABLED and llm_candidate_links and depth < max_depth:
            print(f"{'  ' * depth}Using Gemini to evaluate {len(llm_candidate_links)} additional links...")
            page_title = soup.title.string if soup.title else ""
            page_context = f"Page title: {page_title}"
            
            llm_permit_links = evaluate_links_with_llm(llm_candidate_links, page_context)
            
            # Add high-confidence Gemini suggestions to permit_links
            for url_link, link_text, confidence, facility_name, is_table_link in llm_permit_links:
                if confidence >= 0.6:  # Only follow links with 60%+ confidence
                    table_indicator = " [TABLE]" if is_table_link else ""
                    permit_links.append((url_link, link_text, is_table_link))
                    print(f"{'  ' * depth}Gemini identified facility: {facility_name} - {link_text[:30]}{'...' if len(link_text) > 30 else ''}{table_indicator} (confidence: {confidence:.2f})")
        
        # Follow permit-related links if we haven't reached max depth
        if depth < max_depth and permit_links:
            # Sort permit_links to prioritize table links
            permit_links.sort(key=lambda x: (not x[2], x[0]))  # Sort by (not is_table_link, url) - table links first
            
            print(f"{'  ' * depth}Found {len(permit_links)} potential permit-related links to explore...")
            for permit_url, link_text, is_table_link in permit_links:
                print(f"{'  ' * depth}Following permit link: {link_text[:50]}{'...' if len(link_text) > 50 else ''}")
                downloaded_count += download_pdf_from_page(
                    permit_url, output_dir, visited_urls, depth + 1, max_depth, use_llm
                )
                # Add delay between page requests
                time.sleep(2)
        
    except Exception as e:
        print(f"{'  ' * depth}Error accessing {url}: {str(e)}")
    
    return downloaded_count

def download_pdf(url, output_dir=f'{RAW_DATA_DIR}/downloaded_pdfs', max_depth=2, use_llm=True):
    """
    Download PDF files from a given URL and follow facility-specific permit links.
    
    Args:
        url (str): The URL of the website to scrape for PDFs
        output_dir (str): Directory where PDFs will be saved
        max_depth (int): Maximum depth to follow permit-related links (0 = no following, 1 = one level, etc.)
        use_llm (bool): Whether to use Gemini for enhanced link identification
    """
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    visited_urls = set()
    
    print(f"Starting PDF download from: {url}")
    print(f"Maximum link depth: {max_depth}")
    print("Target: Facility-specific permit documents only")
    if use_llm and LLM_ENABLED:
        print(f"Gemini-enhanced facility detection: ENABLED (using {LLM_MODEL})")
    elif use_llm and not LLM_ENABLED:
        print("Gemini-enhanced facility detection: DISABLED (no API key found)")
        use_llm = False
    else:
        print("Gemini-enhanced facility detection: DISABLED")
    print("-" * 50)
    
    total_downloaded = download_pdf_from_page(url, output_dir, visited_urls, 0, max_depth, use_llm)
    
    print("-" * 50)
    print(f"Download complete! Downloaded {total_downloaded} PDF files to {output_dir}")
    print(f"Visited {len(visited_urls)} unique URLs")
    
    return total_downloaded

def download_pdfs_from_csv(csv_path, output_dir=f'{RAW_DATA_DIR}/downloaded_pdfs', max_depth=2, use_llm=True, url_column='url'):
    """
    Download PDFs from multiple URLs specified in a CSV file.
    
    Args:
        csv_path (str): Path to the CSV file containing URLs
        output_dir (str): Directory where PDFs will be saved
        max_depth (int): Maximum depth to follow permit-related links
        use_llm (bool): Whether to use Gemini for enhanced link identification
        url_column (str): Name of the column containing URLs in the CSV
    
    Returns:
        dict: Summary of downloads for each URL
    """
    try:
        # Read the CSV file
        df = pd.read_csv(csv_path)
        
        if url_column not in df.columns:
            print(f"Error: Column '{url_column}' not found in CSV. Available columns: {list(df.columns)}")
            return {}
        
        # Get unique URLs (remove duplicates)
        urls = df[url_column].dropna().unique()
        
        print(f"Found {len(urls)} unique URLs in CSV file: {csv_path}")
        print(f"URL column: '{url_column}'")
        print("-" * 50)
        
        # Create output directory if it doesn't exist
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Process each URL
        results = {}
        total_downloaded = 0
        
        for i, url in enumerate(urls, 1):
            print(f"\nProcessing URL {i}/{len(urls)}: {url}")
            print("=" * 60)
            
            # Create a subdirectory for each URL to organize downloads
            url_clean = re.sub(r'[<>:"/\\|?*]', '', url.replace('://', '_').replace('/', '_'))
            url_output_dir = os.path.join(output_dir, url_clean)
            
            try:
                downloaded_count = download_pdf(url, url_output_dir, max_depth, use_llm)
                results[url] = {
                    'status': 'success',
                    'downloaded': downloaded_count,
                    'output_dir': url_output_dir
                }
                total_downloaded += downloaded_count
                
            except Exception as e:
                print(f"Error processing {url}: {str(e)}")
                results[url] = {
                    'status': 'error',
                    'error': str(e),
                    'downloaded': 0,
                    'output_dir': url_output_dir
                }
            
            # Add delay between different websites to be respectful
            if i < len(urls):
                print(f"\nWaiting 5 seconds before processing next URL...")
                time.sleep(5)
        
        # Print summary
        print("\n" + "=" * 60)
        print("BATCH DOWNLOAD SUMMARY")
        print("=" * 60)
        print(f"Total URLs processed: {len(urls)}")
        print(f"Total PDFs downloaded: {total_downloaded}")
        print(f"Output directory: {output_dir}")
        
        successful = sum(1 for r in results.values() if r['status'] == 'success')
        failed = len(results) - successful
        
        print(f"Successful downloads: {successful}")
        print(f"Failed downloads: {failed}")
        
        if failed > 0:
            print("\nFailed URLs:")
            for url, result in results.items():
                if result['status'] == 'error':
                    print(f"  - {url}: {result['error']}")
        
        return results
        
    except Exception as e:
        print(f"Error reading CSV file {csv_path}: {str(e)}")
        return {}

if __name__ == "__main__":
    # Ask user for input method
    print("PDF Downloader - Choose input method:")
    print("1. Single URL")
    print("2. CSV file with URLs")
    
    choice = input("Enter your choice (1 or 2): ").strip()
    
    if choice == "2":
        # CSV file input
        csv_path = input("Enter the path to your CSV file: ").strip()
        if not csv_path:
            print("No CSV path provided. Exiting.")
            exit()
        
        # Ask for URL column name
        url_column = input("Enter the name of the column containing URLs (default: 'url'): ").strip()
        if not url_column:
            url_column = 'url'
        
        # Ask user for maximum depth
        try:
            max_depth = int(input("Enter maximum link depth to follow (0=no following, 1=one level, 2=two levels, etc.): ") or "2")
        except ValueError:
            max_depth = 2
            print("Invalid input, using default depth of 2")
        
        # Ask user about Gemini usage
        if LLM_ENABLED:
            use_llm_input = input("Use Gemini to identify facility-specific permits? (y/n, default=y): ").lower()
            use_llm = use_llm_input != 'n'
        else:
            print("Gemini not available (set API_KEY environment variable to enable)")
            use_llm = False
        
        # Process CSV file
        download_pdfs_from_csv(csv_path, max_depth=max_depth, use_llm=use_llm, url_column=url_column)
        
    else:
        # Single URL input (original functionality)
        website_url = input("Enter the website URL to download PDFs from: ")
        
        # Ask user for maximum depth
        try:
            max_depth = int(input("Enter maximum link depth to follow (0=no following, 1=one level, 2=two levels, etc.): ") or "2")
        except ValueError:
            max_depth = 2
            print("Invalid input, using default depth of 2")
        
        # Ask user about Gemini usage
        if LLM_ENABLED:
            use_llm_input = input("Use Gemini to identify facility-specific permits? (y/n, default=y): ").lower()
            use_llm = use_llm_input != 'n'
        else:
            print("Gemini not available (set API_KEY environment variable to enable)")
            use_llm = False
        
        download_pdf(website_url, max_depth=max_depth, use_llm=use_llm) 
