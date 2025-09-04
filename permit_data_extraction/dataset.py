import json
import logging
import os
import time  # To add delays between API calls if needed
import re
import shutil

import openai
import pandas as pd
import PyPDF2  # Library for reading text from PDFs
import typer
from dotenv import dotenv_values
from loguru import logger
from tqdm import tqdm

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR

app = typer.Typer()

OPENAI_API_KEY = dotenv_values()['CBORG_API_KEY']

TEXT_INPUT_DIR = INTERIM_DATA_DIR / 'extracted_text'
COMPLETED_DIR = TEXT_INPUT_DIR / 'completed'
FAILED_DIR = TEXT_INPUT_DIR / 'failed'

# Path for the output Excel file
OUTPUT_EXCEL_FILE = os.path.join(PROCESSED_DATA_DIR,
                                 'permit_data_extracted.xlsx')

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
    """Move a processed file to the appropriate folder (completed or failed)."""
    try:
        # Choose destination directory based on success
        if success:
            destination_dir = COMPLETED_DIR
            folder_name = "completed"
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
            model="openai/gpt-4.1",  # Using GPT-4 for better JSON parsing
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
def main():
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

    # Get all .txt files from the main directory (excluding completed folder)
    all_txt_files = list(TEXT_INPUT_DIR.glob('*.txt'))
    
    # Get list of already processed files (both completed and failed)
    processed_files = set()
    
    # Check completed files
    if COMPLETED_DIR.exists():
        for f in COMPLETED_DIR.glob('*.txt'):
            processed_files.add(f.name)
            # Also handle timestamped files (remove timestamp suffix)
            name_parts = f.stem.split('_')
            if len(name_parts) > 1 and name_parts[-1].isdigit():
                original_name = '_'.join(name_parts[:-1]) + f.suffix
                processed_files.add(original_name)
    
    # Check failed files
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
    
    completed_count = len(list(COMPLETED_DIR.glob('*.txt'))) if COMPLETED_DIR.exists() else 0
    failed_count = len(list(FAILED_DIR.glob('*.txt'))) if FAILED_DIR.exists() else 0
    
    print(f"Found {len(all_txt_files)} total .txt files")
    print(f"  - {completed_count} previously completed successfully")
    print(f"  - {failed_count} previously failed")
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

        permit_text_content = read_text_from_file(txt_file_path)
        if not permit_text_content:
            logging.warning(f"  Skipping file {original_filename} due to text reading error or empty content.")
            processed_data_rows.append({"Filename": original_filename, "Status": "Text Reading Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            # Move file to failed folder - failed to read
            move_processed_file(txt_file_path, success=False)
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
                        processed_data_rows.append(row_data)
                    else:
                        logging.warning(f"  Skipping invalid unit entry (not a dict) in {original_filename}: {unit}")
                        # ... (malformed unit data logging as before)
                        row_data = {"Filename": original_filename, "Status": "Malformed Unit Data"}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS: row_data[field] = "INVALID UNIT ENTRY"
                        processed_data_rows.append(row_data)
                
                # Move file to completed folder - successful extraction with units
                move_processed_file(txt_file_path, success=True)

            else:
                logging.info(f"  No valid emission units extracted or found for {original_filename}.")
                # ... (no units found logging as before)
                row_data = {"Filename": original_filename, "Status": "Success (No Units Found)"}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS: row_data[field] = None
                processed_data_rows.append(row_data)
                
                # Move file to completed folder - successful extraction but no units
                move_processed_file(txt_file_path, success=True)
        else:
            logging.error(f"  Failed to extract information from {original_filename} (LLM call returned None or invalid data).")
            # ... (LLM extraction failed logging as before)
            processed_data_rows.append({"Filename": original_filename, "Status": "LLM Extraction Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            
            # Move file to failed folder - failed extraction
            move_processed_file(txt_file_path, success=False)


    # Save to Excel (same logic as before)
    if processed_data_rows:
        logging.info(f"\nSaving extracted data to {OUTPUT_EXCEL_FILE}...")
        try:
            df = pd.DataFrame(processed_data_rows)
            excel_columns = ["Filename", "Status"] + ALL_OUTPUT_FIELDS
            for col in excel_columns:
                if col not in df.columns:
                    df[col] = None
            df = df[excel_columns]
            df.to_excel(OUTPUT_EXCEL_FILE, index=False, engine='openpyxl')
            logging.info(f"Data successfully saved to '{OUTPUT_EXCEL_FILE}'.")
        except Exception as e:
            logging.error(f"Error saving data to Excel: {e}", exc_info=True)
    else:
        print("No data was processed to save.")
        logging.warning("No data was processed to save.")

    print(f"\nLLM Extraction process finished!")
    print(f"Processed {len(text_files)} files")
    print(f"Successful files moved to: {COMPLETED_DIR}")
    print(f"Failed files moved to: {FAILED_DIR}")
    logging.info("\nLLM Extraction process finished.")


if __name__ == "__main__":
    app()
