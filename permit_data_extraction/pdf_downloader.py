import requests
from bs4 import BeautifulSoup
import os
from urllib.parse import urljoin, urlparse
import time
import mimetypes
import re

from permit_data_extraction.config import RAW_DATA_DIR

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

def download_pdf(url, output_dir=f'{RAW_DATA_DIR}/downloaded_pdfs'):
    """
    Download PDF files from a given URL.
    
    Args:
        url (str): The URL of the website to scrape for PDFs
        output_dir (str): Directory where PDFs will be saved
    """
    # Create output directory if it doesn't exist
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    try:
        # Send GET request to the URL
        response = requests.get(url)
        response.raise_for_status()
        
        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all links
        links = soup.find_all('a')
        
        # Counter for downloaded files
        downloaded_count = 0
        
        # Process each link
        for link in links:
            href = link.get('href')
            if href:
                # Convert relative URL to absolute URL
                full_url = urljoin(url, href)
                
                try:
                    # Get the file
                    file_response = requests.get(full_url, stream=True)
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
                                filename = f"document_{downloaded_count + 1}"
                        
                        # Ensure filename ends with .pdf
                        if not filename.lower().endswith('.pdf'):
                            filename += '.pdf'
                        
                        # Save the file
                        filepath = os.path.join(output_dir, filename)
                        with open(filepath, 'wb') as f:
                            for chunk in file_response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        
                        print(f"Downloaded: {filename}")
                        downloaded_count += 1
                        
                        # Add a small delay to be nice to the server
                        time.sleep(1)
                    
                except Exception as e:
                    print(f"Error downloading {full_url}: {str(e)}")
        
        print(f"\nDownload complete! Downloaded {downloaded_count} PDF files to {output_dir}")
        
    except Exception as e:
        print(f"Error accessing {url}: {str(e)}")

if __name__ == "__main__":
    # Example usage
    website_url = input("Enter the website URL to download PDFs from: ")
    download_pdf(website_url) 