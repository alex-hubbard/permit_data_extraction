import json
import logging
import os
import time  # To add delays between API calls if needed

import google.generativeai as genai
import pandas as pd
import PyPDF2  # Library for reading text from PDFs
import typer
from dotenv import dotenv_values
from loguru import logger
from tqdm import tqdm

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR

app = typer.Typer()

API_KEY = dotenv_values()['API_KEY']

TEXT_INPUT_DIR = INTERIM_DATA_DIR / 'extracted_text'

# Path for the output Excel file
OUTPUT_EXCEL_FILE = os.path.join(PROCESSED_DATA_DIR,
                                 'permit_data_extracted.xlsx')

# General permit info
GENERAL_TARGET_FIELDS = [
    "Facility Name",
    "Facility Address",
    "Facility City",
    "Facility State",
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
    "Unit Type", # especially for boilers, furnaces, etc.
    "Pollutants",  # Could be a list or comma-separated string
    "Emission Limits",  # Could be complex; aim for text description for now
    "Control Device(s)",
    "Capacity",  # e.g., MMBtu/hr, tons/year
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


def configure_llm():
    """Configures the Google Generative AI client."""
    try:
        genai.configure(api_key=API_KEY)
        # Choose the Gemini model - check availability and suitability
        model = genai.GenerativeModel('gemini-2.0-flash')  # Can switch model
        print("Google Generative AI configured successfully.")
        return model
    except Exception as e:
        print(f"Error configuring Google Generative AI: {e}")
        print("Please ensure your API key is correct and valid.")
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


def extract_info_with_llm(model, text_content, filename):
    """Sends text to the LLM and attempts to parse the JSON response."""
    if not model or not text_content:
        logging.warning(f"  Skipping LLM call for {filename} due to missing model or text.")
        return None

    try:
        prompt = PROMPT_TEMPLATE.format(permit_text=text_content)
    except Exception as format_e:
        logging.error(f"  Internal Error: An unexpected error occurred during prompt formatting: {format_e}", exc_info=True)
        return None

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    ]
    generation_config = {
        "temperature": 0.1,
        "top_p": 1.0,
        "top_k": 1,
        "max_output_tokens": 8192,
    }

    logging.info(f"  Sending text from {filename} to LLM (approx {len(text_content)} chars)...")
    try:
        time.sleep(1.5) # API call delay

        response = model.generate_content(
            prompt,
            generation_config=generation_config,
            safety_settings=safety_settings
            )

        extracted_data = None
        json_text_response = None # For logging in case of error

        if response and hasattr(response, 'text'):
            try:
                json_text_response = response.text.strip()
                # Try to clean up the response if it contains markdown code blocks
                if '```json' in json_text_response:
                    json_text_response = json_text_response.split('```json')[1].split('```')[0].strip()
                elif '```' in json_text_response:
                    json_text_response = json_text_response.split('```')[1].split('```')[0].strip()
                
                extracted_data = json.loads(json_text_response)
                logging.info(f"  Successfully extracted and parsed JSON from {filename}.")
                
                if "Emission Units" not in extracted_data or not isinstance(extracted_data.get("Emission Units"), list):
                    logging.warning(f"  LLM response for {filename} parsed, but 'Emission Units' key is missing or not a list. Treating as no units found.")
                    extracted_data["Emission Units"] = []
                return extracted_data
            except json.JSONDecodeError as json_e:
                logging.error(f"  Failed to decode JSON from response for {filename}. Error: {json_e}")
                logging.error(f"  Raw response text:\n{json_text_response[:1000]}...")
                return None
            except Exception as e:
                logging.error(f"  Error processing response for {filename}: {e}", exc_info=True)
                return None
        else:
            logging.error(f"  LLM response was empty or malformed for {filename}.")
            if response:
                logging.error(f"  Response object type: {type(response)}")
                logging.error(f"  Response attributes: {dir(response)}")
            return None
    except Exception as e:
        logging.error(f"  Error during LLM API call for {filename}: {e}", exc_info=True)
        if "API key not valid" in str(e):
            logging.error("  Hint: Double-check your GOOGLE_API_KEY setting.")
        return None


@app.command()
def main():
    logging.info("Starting LLM Extraction Process from Text Files...")

    llm_model = configure_llm()
    if not llm_model:
        logging.critical("Exiting due to LLM configuration error.")
        return

    if not TEXT_INPUT_DIR.is_dir():
        logging.critical(f"Error: Text input directory not found at '{TEXT_INPUT_DIR}'. This directory should contain .txt files from the ocr_processor.py script.")
        return

    text_files = list(TEXT_INPUT_DIR.glob('*.txt'))
    if not text_files:
        logging.warning(f"No .txt files found in '{TEXT_INPUT_DIR}'.")
        return

    logging.info(f"Found {len(text_files)} .txt files to process.")
    processed_data_rows = []

    for txt_file_path in text_files:
        logging.info(f"\nProcessing text file: {txt_file_path.name}")
        original_filename = txt_file_path.stem # Assumes .txt was added to original PDF stem

        permit_text_content = read_text_from_file(txt_file_path)
        if not permit_text_content:
            logging.warning(f"  Skipping file {original_filename} due to text reading error or empty content.")
            processed_data_rows.append({"Filename": original_filename, "Status": "Text Reading Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            continue

        # MAX_CHARS check can still be useful here if there's a concern about LLM input limits
        MAX_CHARS = 1500000 # Example
        if len(permit_text_content) > MAX_CHARS:
            logging.warning(f"  Text from {original_filename} is very long ({len(permit_text_content)} chars). Processing may be slow/costly.")
            # permit_text_content = permit_text_content[:MAX_CHARS] # Optional truncation

        extracted_info = extract_info_with_llm(llm_model, permit_text_content, original_filename)

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

            else:
                logging.info(f"  No valid emission units extracted or found for {original_filename}.")
                # ... (no units found logging as before)
                row_data = {"Filename": original_filename, "Status": "Success (No Units Found)"}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS: row_data[field] = None
                processed_data_rows.append(row_data)
        else:
            logging.error(f"  Failed to extract information from {original_filename} (LLM call returned None or invalid data).")
            # ... (LLM extraction failed logging as before)
            processed_data_rows.append({"Filename": original_filename, "Status": "LLM Extraction Failed", **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})


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
        logging.warning("No data was processed to save.")

    logging.info("\nLLM Extraction process finished.")


if __name__ == "__main__":
    app()
