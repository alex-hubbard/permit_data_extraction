import json
import logging
import os
import time  # To add delays between API calls if needed
import re
import shutil
import requests
from datetime import datetime
from urllib.parse import quote_plus

import openai
import pandas as pd
import PyPDF2  # Library for reading text from PDFs
import typer
from dotenv import dotenv_values
from loguru import logger
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR

app = typer.Typer()

OPENAI_API_KEY = dotenv_values()['CBORG_API_KEY']

TEXT_INPUT_DIR = INTERIM_DATA_DIR / 'extracted_text'
COMPLETED_DIR = TEXT_INPUT_DIR / 'completed'
FAILED_DIR = TEXT_INPUT_DIR / 'failed'

# Path for the output Excel file
OUTPUT_EXCEL_FILE = os.path.join(PROCESSED_DATA_DIR,
                                 'permit_data_extracted.xlsx')

# Feature flags
ENABLE_SPEC_SHEET_LOOKUP = False

# LLM model configuration
LLM_MODEL = "lbl/llama"

# General permit info
GENERAL_TARGET_FIELDS = [
    "Facility Name",
    "Facility Address",
    "Facility City",
    "Facility State Abbreviation",
    "Facility Zip Code",
    "Facility County",
    "NAICS Code",
    "Operating Hours",
    "Industry Description",
    "Permit Number",
    "Issuance Date",
    "Expiration Date",
    "Regulatory Authority",
    "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)"
]

# Specific fields for each emission unit
UNIT_DETAIL_FIELDS = [
    "Unit ID",
    "Unit Description",
    "Unit Quantity", 
    "Unit Make",
    "Unit Model",
    "Year of Manufacture",
    "Unit Type", # especially for boilers, furnaces, etc.
    "Pollutants",  # Could be a list or comma-separated string
    "Emission Limits",  # Could be complex; aim for text description for now
    "Control Device(s)",
    "Capacity Value",  # e.g., MMBtu/hr, tons/year
    "Capacity Unit",  # e.g., MMBtu/hr, tons/year
    "Fuel Type",  # e.g., Natural Gas, Coal, etc.
    "Rated Efficiency", # e.g., 90%
    "Annual Run Hours", # e.g., 8760 hours
    "Generation Capacity", # e.g., 100 MW
]
# All fields expected in the final Excel output
ALL_OUTPUT_FIELDS = GENERAL_TARGET_FIELDS + UNIT_DETAIL_FIELDS

# --- LLM Prompt Template ---
# Requesting JSON output makes parsing much easier.
PROMPT_TEMPLATE = f"""
Analyze the following text from an industrial air permit document. Your goal is to extract key permit information AND details about individual emission units.

**Instructions:**

1.  **Extract General Information:** Identify the following general details for the permit:
    * {', '.join(GENERAL_TARGET_FIELDS)}

2.  **Extract Emission Unit Details:** Identify each distinct permitted emission unit mentioned in the text. For each unit, extract the following details:
    * {', '.join(UNIT_DETAIL_FIELDS)}
    * **Important:** Look for information presented in sections describing specific equipment, process lines, or in tables summarizing emission sources.

3.  **Output Format:** Present the extracted information in a single, valid JSON object.
    * The general information should be top-level key-value pairs.
    * The emission unit details should be in a JSON array (list) named "Emission Units". Each element in the array should be an object containing the details for one unit.
    * Use the field names exactly as listed above as keys in the JSON.
    * If a specific piece of general information cannot be found, use `null` or an empty string for its value.
    * If specific details for a unit (e.g., Control Device) cannot be found, use `null` or an empty string for that unit's field.
    * If NO emission units are clearly identified, provide an empty array `[]` for "Emission Units".

**Example JSON Output Structure:**

{{{{
  "Facility Name": "Example Plant",
  "Permit Number": "123-ABC",
  "Issuance Date": "YYYY-MM-DD",
  "Expiration Date": "YYYY-MM-DD",
  "Regulatory Authority": "State EPA",
  "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)": "Title V, 40 CFR 63 Subpart XXXX",
  "Emission Units": [
    {{{{
      "Unit ID": "EU001",
      "Unit Description": "Natural Gas Boiler 1",
      "Pollutants": "NOx, CO, PM",
      "Emission Limits": "NOx: 0.05 lb/MMBtu, CO: 50 ppmvd",
      "Control Device(s)": "Low NOx Burner"
    }}}},
    {{{{
      "Unit ID": "EU002",
      "Unit Description": "Paint Booth A",
      "Pollutants": "VOC, HAPs",
      "Emission Limits": "VOC: 2.7 tons/year",
      "Control Device(s)": "Dry Filters"
    }}}},
    {{{{
      "Unit ID": "Tank01",
      "Unit Description": "Storage Tank",
      "Pollutants": "VOC",
      "Emission Limits": null,
      "Control Device(s)": null
    }}}}
  ]
}}}}

**Permit Text:**
--- START TEXT ---
{{permit_text}}
--- END TEXT ---

**JSON Output:**
"""


def setup_directories():
    """Creates the completed and failed directories if they don't exist."""
    COMPLETED_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    logging.info(f"Completed directory ready at: {COMPLETED_DIR}")
    logging.info(f"Failed directory ready at: {FAILED_DIR}")


def move_processed_file(file_path, success=True):
    """Move a processed file to the appropriate folder (completed or failed).
    Completed files are organized by date in subfolders (YYYY-MM-DD).
    """
    try:
        # Choose destination directory based on success
        if success:
            # Organize completed files by date
            current_date = datetime.now().strftime("%Y-%m-%d")
            destination_dir = COMPLETED_DIR / current_date
            folder_name = f"completed/{current_date}"
        else:
            destination_dir = FAILED_DIR
            folder_name = "failed"
        
        # Create destination directory if it doesn't exist
        destination_dir.mkdir(parents=True, exist_ok=True)
        
        # Create destination path
        destination = destination_dir / file_path.name
        
        # If file already exists in destination, add a timestamp
        if destination.exists():
            timestamp = int(time.time())
            stem = file_path.stem
            suffix = file_path.suffix
            destination = destination_dir / f"{stem}_{timestamp}{suffix}"
        
        # Move the file
        shutil.move(str(file_path), str(destination))
        
        status = "successfully processed" if success else "failed processing"
        logging.info(f"  Moved {file_path.name} to {folder_name} folder ({status})")
        print(f"  → Moved to {folder_name} folder")
        
    except Exception as e:
        logging.error(f"  Failed to move {file_path.name} to {folder_name} folder: {e}")
        print(f"  → Warning: Could not move file to {folder_name} folder")


def move_failed_files_back():
    """Move files from the failed directory back to the main input directory for retry."""
    if not FAILED_DIR.exists():
        return []
    
    failed_files = list(FAILED_DIR.glob('*.txt'))
    moved_files = []
    
    for failed_file in failed_files:
        try:
            # Handle timestamped files - extract original name
            name_parts = failed_file.stem.split('_')
            if len(name_parts) > 1 and name_parts[-1].isdigit():
                # This is a timestamped file, use the original name
                original_name = '_'.join(name_parts[:-1]) + failed_file.suffix
                destination = TEXT_INPUT_DIR / original_name
            else:
                destination = TEXT_INPUT_DIR / failed_file.name
            
            # If file already exists in input directory, add a timestamp to the failed file
            if destination.exists():
                timestamp = int(time.time())
                stem = failed_file.stem
                suffix = failed_file.suffix
                destination = TEXT_INPUT_DIR / f"{stem}_retry_{timestamp}{suffix}"
            
            # Move the file back
            shutil.move(str(failed_file), str(destination))
            moved_files.append(destination)
            logging.info(f"  Moved {failed_file.name} back to input directory for retry")
            
        except Exception as e:
            logging.error(f"  Failed to move {failed_file.name} back to input directory: {e}")
    
    return moved_files


def append_rows_to_excel(new_rows, llm_client=None):
    """
    Append new rows to the Excel file, creating it if it doesn't exist.
    Handles incremental saving to prevent data loss if script is interrupted.
    
    Args:
        new_rows (list): List of dictionaries, each representing a row to add
        llm_client: Optional LLM client for spec sheet lookup (not used in incremental saves)
    """
    if not new_rows:
        return
    
    try:
        # Add current date and model used to all new rows
        current_date = datetime.now().strftime("%Y-%m-%d")
        for row in new_rows:
            row["Processing Date"] = current_date
            row["Model Used"] = LLM_MODEL
        
        # Define all expected columns
        excel_columns = ["Filename", "Status", "Processing Date", "Model Used"] + ALL_OUTPUT_FIELDS + ["Spec Sheet Link"]
        
        # Ensure all columns exist in new rows
        for row in new_rows:
            for col in excel_columns:
                if col not in row:
                    row[col] = None
        
        # Create DataFrame from new rows
        new_df = pd.DataFrame(new_rows)
        new_df = new_df[excel_columns]
        
        # Check if Excel file exists
        if os.path.exists(OUTPUT_EXCEL_FILE):
            # Read existing file
            existing_df = pd.read_excel(OUTPUT_EXCEL_FILE, engine='openpyxl')
            
            # Ensure existing file has all columns
            for col in excel_columns:
                if col not in existing_df.columns:
                    existing_df[col] = None
            
            # Remove any rows that match the new rows (by Filename) to avoid duplicates when retrying
            # This handles the case where we're retrying failed files
            filenames_to_update = set(new_df['Filename'].unique())
            existing_df = existing_df[~existing_df['Filename'].isin(filenames_to_update)]
            
            # Combine existing and new data
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        else:
            # Create new file
            combined_df = new_df
        
        # Save to Excel
        combined_df.to_excel(OUTPUT_EXCEL_FILE, index=False, engine='openpyxl')
        logging.info(f"  Appended {len(new_rows)} row(s) to Excel file: {OUTPUT_EXCEL_FILE}")
        
    except Exception as e:
        logging.error(f"Error appending rows to Excel: {e}", exc_info=True)
        print(f"  Warning: Failed to save rows to Excel: {e}")


def configure_llm():
    """Configures the OpenAI client."""
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY,
                               base_url="https://api.cborg.lbl.gov")
        # Test the connection with a simple call
        client.models.list()
        print("OpenAI client configured successfully.")
        return client
    except Exception as e:
        print(f"Error configuring OpenAI client: {e}")
        print("Please ensure your OpenAI API key is correct and valid.")
        return None


def setup_selenium_driver():
    """Setup and configure a WebDriver for Selenium (Chrome preferred, Firefox fallback)."""
    
    # Try Chrome first
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')  # Run in background
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
        
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Successfully setup Chrome WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Chrome WebDriver: {e}")
    
    # Try Firefox as fallback
    try:
        from selenium.webdriver.firefox.options import Options as FirefoxOptions
        firefox_options = FirefoxOptions()
        firefox_options.add_argument('--headless')
        firefox_options.add_argument('--width=1920')
        firefox_options.add_argument('--height=1080')
        
        driver = webdriver.Firefox(options=firefox_options)
        logging.info("Successfully setup Firefox WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Firefox WebDriver: {e}")
    
    # Try Chromium as another fallback
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.binary_location = '/usr/bin/chromium-browser'  # Common path for Chromium
        
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Successfully setup Chromium WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Chromium WebDriver: {e}")
    
    logging.error("Failed to setup any WebDriver (Chrome, Firefox, or Chromium)")
    return None


def perform_google_search_selenium(search_query, max_results=10):
    """
    Perform a Google search using Selenium and return the top search results.
    
    Args:
        search_query (str): The search query
        max_results (int): Maximum number of results to return
        
    Returns:
        list: List of dictionaries with 'title', 'url', and 'snippet' keys
    """
    driver = None
    try:
        # Setup Chrome driver
        driver = setup_selenium_driver()
        if not driver:
            logging.error("Failed to setup WebDriver")
            return []
        
        # Create a Google search URL
        encoded_query = quote_plus(search_query)
        google_url = f"https://www.google.com/search?q={encoded_query}"
        
        # Navigate to Google
        driver.get(google_url)
        
        # Wait for search results to load
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.g")))
        
        # Extract search results
        search_results = []
        
        # Find search result containers
        result_elements = driver.find_elements(By.CSS_SELECTOR, "div.g")[:max_results]
        
        for element in result_elements:
            try:
                # Extract title
                title_elem = element.find_element(By.CSS_SELECTOR, "h3")
                title = title_elem.text.strip() if title_elem else ""
                
                # Extract link
                link_elem = element.find_element(By.CSS_SELECTOR, "a")
                url = link_elem.get_attribute('href') if link_elem else ""
                
                # Extract snippet
                snippet = ""
                try:
                    snippet_elem = element.find_element(By.CSS_SELECTOR, "span.st, div.VwiC3b")
                    snippet = snippet_elem.text.strip() if snippet_elem else ""
                except NoSuchElementException:
                    # Try alternative selectors for snippets
                    try:
                        snippet_elem = element.find_element(By.CSS_SELECTOR, "div.s3v9rd")
                        snippet = snippet_elem.text.strip() if snippet_elem else ""
                    except NoSuchElementException:
                        pass
                
                if title and url:
                    search_results.append({
                        'title': title,
                        'url': url,
                        'snippet': snippet
                    })
                    
            except Exception as e:
                logging.warning(f"Error parsing search result: {e}")
                continue
        
        return search_results
        
    except TimeoutException:
        logging.warning(f"Timeout waiting for Google search results for '{search_query}'")
        return []
    except Exception as e:
        logging.warning(f"Error performing Google search with Selenium for '{search_query}': {e}")
        return []
    finally:
        if driver:
            driver.quit()


def search_manufacturer_direct(make, model):
    """Search for spec sheets by constructing direct manufacturer URLs."""
    manufacturer_urls = {
        "caterpillar": "https://www.cat.com/en_US/products/new/power-systems/industrial-engines/",
        "ge": "https://www.ge.com/gas-power/products/gas-turbines/",
        "cleaver brooks": "https://www.cleaverbrooks.com/products/boilers/",
        "john deere": "https://www.deere.com/en/engines/",
        "cummins": "https://www.cummins.com/engines/",
        "detroit diesel": "https://www.detroitdiesel.com/engines/",
        "perkins": "https://www.perkins.com/en_US/products/engines/",
        "kohler": "https://www.kohlerpower.com/engines/",
        "briggs & stratton": "https://www.briggsandstratton.com/engines/",
        "honda": "https://engines.honda.com/",
        "yanmar": "https://www.yanmar.com/us/engines/",
        "kubota": "https://www.kubota.com/products/engines/",
        "isuzu": "https://www.isuzuengines.com/",
        "mitsubishi": "https://www.mitsubishi-engines.com/",
        "volvo": "https://www.volvopenta.com/en-us/engines/",
        "man": "https://www.man.eu/en/engines/",
        "deutz": "https://www.deutz.com/en/products/engines/",
        "mtu": "https://www.mtu-online.com/engines/",
        "rolls-royce": "https://www.rolls-royce.com/products-and-services/marine/engines/",
        "wärtsilä": "https://www.wartsila.com/energy/engines"
    }
    
    make_lower = make.lower().strip()
    
    if make_lower in manufacturer_urls:
        base_url = manufacturer_urls[make_lower]
        return [{
            "title": f"{make} {model} - Official Product Page",
            "url": base_url,
            "snippet": f"Official {make} product page where you can find specifications for {model}"
        }]
    
    return []


def perform_google_search(search_query, max_results=10):
    """
    Perform a search using multiple methods: Selenium, requests, and manufacturer direct.
    
    Args:
        search_query (str): The search query
        max_results (int): Maximum number of results to return
        
    Returns:
        list: List of dictionaries with 'title', 'url', and 'snippet' keys
    """
    # Try Selenium first
    results = perform_google_search_selenium(search_query, max_results)
    if results:
        logging.info(f"Selenium search successful for '{search_query}'")
        return results
    
    # Fallback to requests if Selenium fails
    logging.info("Selenium search failed, trying requests fallback...")
    try:
        # Create a Google search URL
        encoded_query = quote_plus(search_query)
        google_url = f"https://www.google.com/search?q={encoded_query}"
        
        # Set headers to mimic a real browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Perform the search
        response = requests.get(google_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Simple parsing for fallback (basic regex-based extraction)
        search_results = []
        content = response.text
        
        # Extract URLs and titles using regex (basic fallback)
        url_pattern = r'href="(/url\?q=|https://)([^"]+)"'
        title_pattern = r'<h3[^>]*>([^<]+)</h3>'
        
        urls = re.findall(url_pattern, content)[:max_results]
        titles = re.findall(title_pattern, content)[:max_results]
        
        for i, (url_prefix, url) in enumerate(urls):
            if i < len(titles):
                # Clean up the URL
                if url_prefix == '/url?q=':
                    url = url.split('&')[0]
                else:
                    url = url_prefix + url
                
                search_results.append({
                    'title': titles[i].strip(),
                    'url': url,
                    'snippet': ''  # Snippet extraction is complex, skip for fallback
                })
        
        if search_results:
            logging.info(f"Requests fallback successful for '{search_query}'")
            return search_results
        
    except Exception as e:
        logging.warning(f"Fallback requests search also failed for '{search_query}': {e}")
    
    # Final fallback: try to extract make/model from query and use manufacturer direct
    logging.info("All search methods failed, trying manufacturer direct search...")
    try:
        # Extract make and model from the search query
        # Look for quoted terms in the search query
        import re
        quoted_terms = re.findall(r'"([^"]+)"', search_query)
        if len(quoted_terms) >= 2:
            make = quoted_terms[0]
            model = quoted_terms[1]
            manufacturer_results = search_manufacturer_direct(make, model)
            if manufacturer_results:
                logging.info(f"Found manufacturer direct results for {make} {model}")
                return manufacturer_results
    except Exception as e:
        logging.warning(f"Manufacturer direct search failed: {e}")
    
    logging.warning(f"All search methods failed for '{search_query}'")
    return []


def analyze_search_results_with_llm(make, model, search_results, client):
    """
    Use LLM to analyze search results and identify the best spec sheet links.
    
    Args:
        make (str): Equipment manufacturer/make
        model (str): Equipment model
        search_results (list): List of search result dictionaries
        client: OpenAI client instance
        
    Returns:
        list: List of URLs identified as spec sheets/manuals
    """
    if not search_results or not client:
        return []
    
    # Prepare the search results for LLM analysis
    results_text = ""
    for i, result in enumerate(search_results, 1):
        results_text += f"{i}. Title: {result['title']}\n"
        results_text += f"   URL: {result['url']}\n"
        results_text += f"   Snippet: {result['snippet']}\n\n"
    
    prompt = f"""
You are an expert at identifying equipment specification sheets and technical manuals from search results.

Equipment: {make} {model}

Search Results:
{results_text}

Please analyze these search results and identify which URLs are most likely to be:
1. Official equipment specification sheets
2. Technical manuals
3. Datasheets
4. Product documentation

Return your analysis as a JSON object with this structure:
{{
    "spec_sheets": [
        {{
            "url": "URL_HERE",
            "title": "TITLE_HERE",
            "confidence": "high|medium|low",
            "reason": "Why this is likely a spec sheet"
        }}
    ]
}}

Only include URLs with "high" or "medium" confidence. If there are multiple good matches, include up to 2 best ones.
Focus on official manufacturer websites, technical documentation sites, and direct PDF links.
"""
    
    try:
        response = client.chat.completions.create(
            model="lbl/llama",
            messages=[
                {"role": "system", "content": "You are an expert at identifying technical documentation from search results. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2048,
            timeout=30,
            response_format={"type": "json_object"}
        )
        
        if response and response.choices and len(response.choices) > 0:
            json_text = response.choices[0].message.content.strip()
            
            # Clean up JSON response
            if '```json' in json_text:
                json_text = json_text.split('```json')[1].split('```')[0].strip()
            elif '```' in json_text:
                json_text = json_text.split('```')[1].split('```')[0].strip()
            
            analysis = json.loads(json_text)
            spec_sheets = analysis.get('spec_sheets', [])
            
            # Return only high and medium confidence results
            filtered_results = [sheet for sheet in spec_sheets if sheet.get('confidence') in ['high', 'medium']]
            return filtered_results
            
    except Exception as e:
        logging.warning(f"Error analyzing search results with LLM for {make} {model}: {e}")
    
    return []


def search_spec_sheet(make, model, client=None):
    """
    Search for equipment specification sheets based on make and model using Google search and LLM analysis.
    
    Args:
        make (str): Equipment manufacturer/make
        model (str): Equipment model
        client: OpenAI client instance for LLM analysis
        
    Returns:
        str: URLs to spec sheets (comma-separated if multiple), empty string if not found
    """
    if not make or not model or make.strip() == "" or model.strip() == "":
        return ""
    
    # Clean the make and model for better search results
    clean_make = make.strip()
    clean_model = model.strip()
    
    # Common equipment types that might have spec sheets
    equipment_keywords = ["boiler", "furnace", "engine", "turbine", "compressor", "generator", "motor"]
    
    # Check if this looks like industrial equipment
    is_equipment = any(keyword in clean_model.lower() or keyword in clean_make.lower() for keyword in equipment_keywords)
    
    if not is_equipment:
        # For non-equipment items, try a general search
        search_query = f'"{clean_make}" "{clean_model}" specifications manual datasheet'
    else:
        # For equipment, use more specific search terms
        search_query = f'"{clean_make}" "{clean_model}" specification sheet manual datasheet technical documentation'
    
    logging.info(f"Searching for spec sheets: {make} {model}")
    
    # Perform Google search
    search_results = perform_google_search(search_query, max_results=10)
    
    if not search_results:
        logging.info(f"No search results found for {make} {model}")
        return ""
    
    logging.info(f"Found {len(search_results)} search results for {make} {model}")
    
    # Analyze results with LLM if client is available
    if client:
        spec_sheets = analyze_search_results_with_llm(make, model, search_results, client)
        
        if spec_sheets:
            # Return up to 2 best matches, comma-separated
            urls = [sheet['url'] for sheet in spec_sheets[:2]]
            logging.info(f"LLM identified {len(urls)} spec sheet(s) for {make} {model}")
            return ", ".join(urls)
    
    # Fallback: return first result if LLM analysis failed
    logging.info(f"LLM analysis failed for {make} {model}, returning first result as fallback")
    return search_results[0]['url'] if search_results else ""


def add_spec_sheet_links(df, llm_client=None):
    """
    Add spec sheet links to a dataframe that has Unit Make and Unit Model columns.
    
    Args:
        df (pd.DataFrame): DataFrame with equipment data
        llm_client: OpenAI client instance for LLM analysis
        
    Returns:
        pd.DataFrame: DataFrame with added Spec Sheet Link column
    """
    if df.empty:
        return df
    
    # Initialize the spec sheet link column
    df['Spec Sheet Link'] = ""

    if not ENABLE_SPEC_SHEET_LOOKUP:
        logging.info("Spec sheet lookup disabled by configuration. Skipping search.")
        return df
    
    # Find rows that have both make and model
    has_make_model = df['Unit Make'].notna() & df['Unit Model'].notna() & (df['Unit Make'] != "") & (df['Unit Model'] != "")
    
    if has_make_model.any():
        logging.info(f"Searching for spec sheets for {has_make_model.sum()} equipment items...")
        
        for idx in df[has_make_model].index:
            make = df.loc[idx, 'Unit Make']
            model = df.loc[idx, 'Unit Model']
            
            if make and model:
                spec_link = search_spec_sheet(make, model, llm_client)
                df.loc[idx, 'Spec Sheet Link'] = spec_link
                
                if spec_link:
                    logging.info(f"  Found spec sheet link(s) for {make} {model}: {spec_link}")
                else:
                    logging.info(f"  No spec sheet found for {make} {model}")
                
                # Add a small delay to avoid overwhelming the search APIs
                time.sleep(1)
    
    return df


def extract_text_from_pdf(pdf_path):
    """Extracts text from a text-based PDF file."""
    text = ""
    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            num_pages = len(reader.pages)
            print(f"  Reading {num_pages} pages from {pdf_path.name}...")
            for page_num in range(num_pages):
                page = reader.pages[page_num]
                text += page.extract_text() or ""  # fallback for blank pages
            print(f"  Successfully extracted text from {pdf_path.name}.")
        return text
    except FileNotFoundError:
        print(f"  Error: File not found at {pdf_path}")
        return None
    except Exception as e:
        print(f"  Error reading PDF {pdf_path.name}: {e}")
        return None


def read_text_from_file(file_path):
    """Reads text content from a given file path."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        logging.info(f"  Successfully read text from {file_path.name} (approx {len(content)} chars).")
        return content
    except FileNotFoundError:
        logging.error(f"  Error: Text file not found at {file_path}")
        return None
    except Exception as e:
        logging.error(f"  Error reading text file {file_path.name}: {e}", exc_info=True)
        return None


def extract_info_with_llm(client, text_content, filename):
    """Sends text to the LLM and attempts to parse the JSON response."""
    if not client or not text_content:
        logging.warning(f"  Skipping LLM call for {filename} due to missing client or text.")
        return None

    try:
        prompt = PROMPT_TEMPLATE.format(permit_text=text_content)
    except Exception as format_e:
        logging.error(f"  Internal Error: An unexpected error occurred during prompt formatting: {format_e}", exc_info=True)
        return None

    logging.info(f"  Sending text from {filename} to LLM (approx {len(text_content)} chars)...")
    try:
        time.sleep(1.0)  # API call delay
        
        logging.info(f"  Making API call for {filename}...")
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert at extracting structured information from industrial air permit documents. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=8192,
            timeout=60,  # 60 second timeout
            response_format={"type": "json_object"}  # Ensures JSON response
        )
        
        logging.info(f"  API call completed for {filename}")
        extracted_data = None
        json_text_response = None  # For logging in case of error

        if response and response.choices and len(response.choices) > 0:
            try:
                json_text_response = response.choices[0].message.content.strip()
                
                # Since we're using response_format={"type": "json_object"}, the response should already be valid JSON
                # But we'll still clean up just in case
                if '```json' in json_text_response:
                    json_text_response = json_text_response.split('```json')[1].split('```')[0].strip()
                elif '```' in json_text_response:
                    json_text_response = json_text_response.split('```')[1].split('```')[0].strip()
                
                try:
                    extracted_data = json.loads(json_text_response)
                except json.JSONDecodeError as json_e:
                    logging.error(f"  Failed to decode JSON for {filename}. Error: {json_e}")
                    logging.error(f"  Raw response text:\n{json_text_response[:1000]}...")
                    return None
                
                logging.info(f"  Successfully extracted and parsed JSON from {filename}.")
                
                if "Emission Units" not in extracted_data or not isinstance(extracted_data.get("Emission Units"), list):
                    logging.warning(f"  LLM response for {filename} parsed, but 'Emission Units' key is missing or not a list. Treating as no units found.")
                    extracted_data["Emission Units"] = []
                return extracted_data
                
            except Exception as e:
                logging.error(f"  Error processing response for {filename}: {e}", exc_info=True)
                return None
        else:
            logging.error(f"  LLM response was empty or malformed for {filename}.")
            if response:
                logging.error(f"  Response object type: {type(response)}")
                logging.error(f"  Response choices: {response.choices if hasattr(response, 'choices') else 'No choices'}")
            return None
    except Exception as e:
        logging.error(f"  Error during LLM API call for {filename}: {e}", exc_info=True)
        if "API key not valid" in str(e):
            logging.error("  Hint: Double-check your OPENAI_API_KEY setting.")
        return None


@app.command()
def main(
    retry_failed: bool = typer.Option(False, "--retry-failed", "-r", help="Retry processing files that previously failed")
):
    print("Starting LLM Extraction Process from Text Files...")
    logging.info("Starting LLM Extraction Process from Text Files...")

    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        logging.critical("Exiting due to LLM configuration error.")
        return

    print(f"Checking for text files in: {TEXT_INPUT_DIR}")
    if not TEXT_INPUT_DIR.is_dir():
        print(f"ERROR: Text input directory not found at '{TEXT_INPUT_DIR}'")
        logging.critical(f"Error: Text input directory not found at '{TEXT_INPUT_DIR}'. This directory should contain .txt files from the ocr_processor.py script.")
        return

    # Setup completed and failed directories
    setup_directories()

    # If retry_failed is True, move failed files back to input directory
    if retry_failed:
        print("Retry mode: Moving failed files back to input directory...")
        logging.info("Retry mode enabled: Moving failed files back to input directory")
        moved_files = move_failed_files_back()
        if moved_files:
            print(f"  Moved {len(moved_files)} failed file(s) back to input directory for retry")
            logging.info(f"Moved {len(moved_files)} failed file(s) back to input directory for retry")
        else:
            print("  No failed files found to retry")
            logging.info("No failed files found to retry")

    # Get all .txt files from the main directory (excluding completed folder)
    all_txt_files = list(TEXT_INPUT_DIR.glob('*.txt'))
    
    # Get list of already processed files (only completed files, not failed if retry is enabled)
    processed_files = set()
    
    # Check completed files (recursively search in date subfolders)
    if COMPLETED_DIR.exists():
        for f in COMPLETED_DIR.rglob('*.txt'):  # rglob searches recursively
            processed_files.add(f.name)
            # Also handle timestamped files (remove timestamp suffix)
            name_parts = f.stem.split('_')
            if len(name_parts) > 1 and name_parts[-1].isdigit():
                original_name = '_'.join(name_parts[:-1]) + f.suffix
                processed_files.add(original_name)
    
    # Only exclude failed files if retry_failed is False
    if not retry_failed:
        if FAILED_DIR.exists():
            for f in FAILED_DIR.glob('*.txt'):
                processed_files.add(f.name)
                # Also handle timestamped files (remove timestamp suffix)
                name_parts = f.stem.split('_')
                if len(name_parts) > 1 and name_parts[-1].isdigit():
                    original_name = '_'.join(name_parts[:-1]) + f.suffix
                    processed_files.add(original_name)
    
    # Filter out already processed files
    text_files = [f for f in all_txt_files if f.name not in processed_files]
    
    completed_count = len(list(COMPLETED_DIR.rglob('*.txt'))) if COMPLETED_DIR.exists() else 0
    failed_count = len(list(FAILED_DIR.glob('*.txt'))) if FAILED_DIR.exists() else 0
    
    print(f"Found {len(all_txt_files)} total .txt files")
    print(f"  - {completed_count} previously completed successfully")
    print(f"  - {failed_count} previously failed")
    if retry_failed:
        print(f"  - Retry mode: Will retry failed files")
    print(f"Processing {len(text_files)} remaining files")
    if not text_files:
        print(f"No .txt files found in '{TEXT_INPUT_DIR}'.")
        logging.warning(f"No .txt files found in '{TEXT_INPUT_DIR}'.")
        return

    print(f"Processing {len(text_files)} .txt files...")
    logging.info(f"Found {len(text_files)} .txt files to process.")
    processed_data_rows = []

    # Limit to first 3 files for testing
    # test_files = text_files[:3]
    logging.info(f"Processing first {len(text_files)} files for testing...")

    for i, txt_file_path in enumerate(text_files, 1):
        print(f"\nProcessing text file {i}/{len(text_files)}: {txt_file_path.name}")
        logging.info(f"\nProcessing text file {i}/{len(text_files)}: {txt_file_path.name}")
        original_filename = txt_file_path.stem # Assumes .txt was added to original PDF stem
        
        # Collect rows for this file
        file_rows = []

        permit_text_content = read_text_from_file(txt_file_path)
        if not permit_text_content:
            logging.warning(f"  Skipping file {original_filename} due to text reading error or empty content.")
            file_rows.append({"Filename": original_filename, "Status": "Text Reading Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            # Move file to failed folder - failed to read
            move_processed_file(txt_file_path, success=False)
            # Save immediately
            append_rows_to_excel(file_rows)
            processed_data_rows.extend(file_rows)
            continue

        # MAX_CHARS check can still be useful here if there's a concern about LLM input limits
        MAX_CHARS = 1500000 # Example
        if len(permit_text_content) > MAX_CHARS:
            logging.warning(f"  Text from {original_filename} is very long ({len(permit_text_content)} chars). Processing may be slow/costly.")
            # permit_text_content = permit_text_content[:MAX_CHARS] # Optional truncation

        extracted_info = extract_info_with_llm(llm_client, permit_text_content, original_filename)

        # Process results (same logic as before)
        if extracted_info and isinstance(extracted_info, dict):
            general_info = {field: extracted_info.get(field) for field in GENERAL_TARGET_FIELDS}
            emission_units = extracted_info.get("Emission Units", [])

            if not isinstance(emission_units, list):
                logging.warning(f"  'Emission Units' field in response for {original_filename} was not a list. Treating as no units found. Value: {emission_units}")
                emission_units = []

            if emission_units:
                logging.info(f"  Extracted {len(emission_units)} emission units from {original_filename}.")
                for unit in emission_units:
                    if isinstance(unit, dict):
                        row_data = {"Filename": original_filename, "Status": "Success"}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS:
                            row_data[field] = unit.get(field)
                        file_rows.append(row_data)
                    else:
                        logging.warning(f"  Skipping invalid unit entry (not a dict) in {original_filename}: {unit}")
                        # ... (malformed unit data logging as before)
                        row_data = {"Filename": original_filename, "Status": "Malformed Unit Data"}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS: row_data[field] = "INVALID UNIT ENTRY"
                        file_rows.append(row_data)
                
                # Move file to completed folder - successful extraction with units
                move_processed_file(txt_file_path, success=True)

            else:
                logging.info(f"  No valid emission units extracted or found for {original_filename}.")
                # ... (no units found logging as before)
                row_data = {"Filename": original_filename, "Status": "Success (No Units Found)"}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS: row_data[field] = None
                file_rows.append(row_data)
                
                # Move file to completed folder - successful extraction but no units
                move_processed_file(txt_file_path, success=True)
        else:
            logging.error(f"  Failed to extract information from {original_filename} (LLM call returned None or invalid data).")
            # ... (LLM extraction failed logging as before)
            file_rows.append({"Filename": original_filename, "Status": "LLM Extraction Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            
            # Move file to failed folder - failed extraction
            move_processed_file(txt_file_path, success=False)
        
        # Save rows for this file immediately to prevent data loss
        if file_rows:
            append_rows_to_excel(file_rows)
            processed_data_rows.extend(file_rows)
            print(f"  ✓ Saved {len(file_rows)} row(s) to Excel")


    # Add spec sheet links if enabled (rows are already saved incrementally)
    if processed_data_rows and ENABLE_SPEC_SHEET_LOOKUP:
        logging.info(f"\nAdding spec sheet links to existing Excel file...")
        try:
            # Read the existing Excel file
            if os.path.exists(OUTPUT_EXCEL_FILE):
                df = pd.read_excel(OUTPUT_EXCEL_FILE, engine='openpyxl')
                
                # Add spec sheet links for rows with make and model data
                logging.info("Adding spec sheet links for equipment with make and model information...")
                df = add_spec_sheet_links(df, llm_client)
                
                # Ensure all columns are in the right order
                excel_columns = ["Filename", "Status", "Processing Date", "Model Used"] + ALL_OUTPUT_FIELDS + ["Spec Sheet Link"]
                for col in excel_columns:
                    if col not in df.columns:
                        df[col] = None
                df = df[excel_columns]
                
                # Save updated file with spec sheet links
                df.to_excel(OUTPUT_EXCEL_FILE, index=False, engine='openpyxl')
                logging.info(f"Successfully added spec sheet links to '{OUTPUT_EXCEL_FILE}'.")
            else:
                logging.warning("Excel file not found for spec sheet link addition.")
        except Exception as e:
            logging.error(f"Error adding spec sheet links to Excel: {e}", exc_info=True)
    elif processed_data_rows:
        print(f"\nProcessed {len(processed_data_rows)} row(s) (saved incrementally)")
        logging.info(f"Processed {len(processed_data_rows)} row(s) (saved incrementally)")
    else:
        print("No data was processed to save.")
        logging.warning("No data was processed to save.")

    print(f"\nLLM Extraction process finished!")
    print(f"Processed {len(text_files)} files")
    print(f"Successful files moved to: {COMPLETED_DIR}")
    print(f"Failed files moved to: {FAILED_DIR}")
    logging.info("\nLLM Extraction process finished.")


@app.command()
def add_spec_sheets():
    """Add spec sheet links to an existing Excel file."""
    print("Adding spec sheet links to existing Excel file...")
    logging.info("Starting spec sheet link addition process...")

    if not ENABLE_SPEC_SHEET_LOOKUP:
        print("Spec sheet lookup is currently disabled. Enable ENABLE_SPEC_SHEET_LOOKUP to use this command.")
        logging.info("Spec sheet lookup disabled; exiting without changes.")
        return
    
    # Configure LLM client for analysis
    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        logging.critical("Exiting due to LLM configuration error.")
        return
    
    # Check if the Excel file exists
    if not os.path.exists(OUTPUT_EXCEL_FILE):
        print(f"ERROR: Excel file not found at '{OUTPUT_EXCEL_FILE}'")
        print("Please run the main pipeline first to generate the Excel file.")
        logging.error(f"Excel file not found at '{OUTPUT_EXCEL_FILE}'")
        return
    
    try:
        # Read the existing Excel file
        logging.info(f"Reading existing Excel file: {OUTPUT_EXCEL_FILE}")
        df = pd.read_excel(OUTPUT_EXCEL_FILE)
        
        print(f"Found {len(df)} rows in the Excel file")
        logging.info(f"Found {len(df)} rows in the Excel file")
        
        # Add spec sheet links
        logging.info("Adding spec sheet links...")
        df = add_spec_sheet_links(df, llm_client)
        
        # Create backup of original file
        backup_file = OUTPUT_EXCEL_FILE.replace('.xlsx', '_backup.xlsx')
        shutil.copy2(OUTPUT_EXCEL_FILE, backup_file)
        logging.info(f"Created backup of original file: {backup_file}")
        
        # Save the updated file
        df.to_excel(OUTPUT_EXCEL_FILE, index=False, engine='openpyxl')
        print(f"Successfully updated Excel file with spec sheet links!")
        print(f"Original file backed up as: {backup_file}")
        logging.info(f"Successfully updated Excel file with spec sheet links")
        
        # Report statistics
        spec_links_added = (df['Spec Sheet Link'] != "").sum()
        print(f"Added spec sheet links for {spec_links_added} equipment items")
        logging.info(f"Added spec sheet links for {spec_links_added} equipment items")
        
    except Exception as e:
        print(f"ERROR: Failed to add spec sheet links: {e}")
        logging.error(f"Failed to add spec sheet links: {e}", exc_info=True)


if __name__ == "__main__":
    app()
