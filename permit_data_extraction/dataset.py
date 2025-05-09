
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

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR

app = typer.Typer()

API_KEY = dotenv_values()['API_KEY']

PERMIT_DIR = RAW_DATA_DIR

# Path for the output Excel file
OUTPUT_EXCEL_FILE = os.path.join(INTERIM_DATA_DIR,
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
    "Pollutants",  # Could be a list or comma-separated string
    "Emission Limits",  # Could be complex; aim for text description for now
    "Control Device(s)",
    "Capacity",  # e.g., MMBtu/hr, tons/year
    "Fuel Type",  # e.g., Natural Gas, Coal, etc.
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
        model = genai.GenerativeModel('gemini-1.5-flash')  # Can switch model
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


def extract_info_with_llm(model, text, filename):
    """Sends text to the LLM and attempts to parse the JSON response."""
    if not model or not text:
        logging.warning(f"""  Skipping LLM call for {filename}
                         due to missing model or text.""")
        return None

    try:
        prompt = PROMPT_TEMPLATE.format(permit_text=text)
    except Exception as format_e:
        logging.error(f"""  Internal Error: An unexpected error occurred
                       during prompt formatting: {format_e}""",
                      exc_info=True)
        return None

    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
         "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    ]
    generation_config = {
        "temperature": 0.1,  # Very low temperature for fact-based extraction
        "top_p": 1.0,
        "top_k": 1,
        "max_output_tokens": 8192,  # Increased further for many units
        "response_mime_type": "application/json",
    }

    logging.info(f"""  Sending text from {filename} to LLM
                 (approx {len(text)} chars)...""")
    try:
        time.sleep(1.5)  # Slightly longer delay for more complex requests

        response = model.generate_content(
            prompt,
            generation_config=generation_config,
            safety_settings=safety_settings
            )

        extracted_data = None
        json_text = None  # Initialize json_text for except blocks

        if response and response.parts:
            try:
                # Extract the text content believed to be JSON
                json_text = response.parts[0].text
                # Attempt to parse the JSON
                extracted_data = json.loads(json_text)
                logging.info(f"""  Successfully extracted and parsed JSON
                             from {filename}.""")
                # Basic validation
                if "Emission Units" not in extracted_data or not isinstance(
                        extracted_data.get("Emission Units"), list):
                    logging.warning(f"""  LLM response for {filename} parsed,
                                    but 'Emission Units' key is missing or not
                                    a list. Treating as no units found.""")
                    extracted_data["Emission Units"] = []
                return extracted_data
            except json.JSONDecodeError as json_e:
                # *** MODIFICATION START ***
                # Log the specific error AND the problematic text
                # if parsing fails
                logging.error(f"""  Failed to decode JSON from response part
                              for {filename}. Error: {json_e}""")
                logging.error(f"""  Problematic JSON text received from LLM for
                              {filename}:\n--- START MALFORMED JSON ---
                              \n{json_text}\n--- END MALFORMED JSON ---""")
                # *** MODIFICATION END ***
                return None  # Continue returning None after logging
            except (IndexError, AttributeError, TypeError) as e:
                # Handle other potential errors accessing response parts
                logging.error(f"""  Failed to access or process response part
                              for {filename}. Error: {e}""")
                # Log the raw response if possible for debugging
                try:
                    logging.error(f"""  Raw response part content
                                  (if available):
                                  {response.parts[0].text[:1000]}...""")
                except:
                    logging.error("""  Could not retrieve raw response part
                                  content for logging.""")
                return None
        # Fallback attempt if response.parts wasn't the primary way
        # data was returned (less likely with mime_type=json)
        elif hasattr(response, 'text') and response.text:
            logging.info("  Attempting fallback parsing from response.text...")
            try:
                json_text = response.text.strip().lstrip('```json').rstrip(
                    '```').strip()
                extracted_data = json.loads(json_text)
                logging.info(f"""  Successfully extracted and parsed JSON from
                              {filename} using fallback.""")
                if "Emission Units" not in extracted_data or not isinstance(
                        extracted_data.get("Emission Units"), list):
                    logging.warning(f"""  LLM fallback response for {filename}
                                    parsed, but 'Emission Units' key invalid.
                                    Treating as no units found.""")
                    extracted_data["Emission Units"] = []
                return extracted_data
            except json.JSONDecodeError as json_e_fallback:
                # *** MODIFICATION START ***
                logging.error(f"""  Fallback JSON decoding failed for
                              {filename}. Error: {json_e_fallback}""")
                logging.error(f"""  Problematic JSON text received from LLM
                              (Fallback) for {filename}:\n--- START MALFORMED
                              JSON ---\n{json_text}\n--- END MALFORMED JSON ---
                              """)
                # *** MODIFICATION END ***
                return None
            except Exception as fallback_e:
                logging.error(f"""  Error during fallback parsing for
                              {filename}: {fallback_e}""",
                              exc_info=True)
                return None
        else:
            # Handle cases where the response structure is unexpected or empty
            raw_response_content = str(response)  # Attempt to get string
            logging.error(f"""  LLM response was empty or malformed for
                          {filename}.""")
            logging.debug(f"""  Raw Response object (stringified):
                          {raw_response_content[:1000]}...""")
            return None

    except Exception as e:
        # Catch other potential API errors (rate limits, auth, etc.)
        logging.error(f"  Error during LLM API call for {filename}: {e}",
                      exc_info=True)
        if "API key not valid" in str(e):
            logging.error("  Hint: Double-check your GOOGLE_API_KEY setting.")
        return None


@app.command()
def main():
    logger.info("Processing dataset...")
    for i in tqdm(range(10), total=10):
        if i == 5:
            logger.info("Something happened for iteration 5.")
    logger.success("Processing dataset complete.")

    logging.info("Starting Air Permit Extraction Process...")

    llm_model = configure_llm()
    if not llm_model:
        logging.critical("Exiting due to LLM configuration error.")
        return

    if not PERMIT_DIR.is_dir():
        logging.critical(f"""Error: Permit directory not found at
                         '{PERMIT_DIR}'.
                         Please create it and add PDF files.""")
        return

    pdf_files = list(PERMIT_DIR.glob('*.pdf'))
    if not pdf_files:
        logging.warning(f"""No PDF files found in '{PERMIT_DIR}'. Ensure files
                        end with '.pdf'.""")
        return

    logging.info(f"Found {len(pdf_files)} PDF files to process.")

    processed_data_rows = []  # Will store dicts, each representing a row

    for pdf_path in pdf_files:
        logging.info(f"\nProcessing file: {pdf_path.name}")
        filename = pdf_path.name  # Store filename for logging/reporting

        # 1. Extract Text
        permit_text = extract_text_from_pdf(pdf_path)
        if not permit_text:
            logging.warning(f"""  Skipping file {filename} due to text
                             extraction error or empty content.""")
            # Log basic failure info if needed, though handled later
            processed_data_rows.append({"Filename": filename,
                                        "Status": "Text Extraction Failed",
                                        **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})
            continue

        # Basic text length check
        MAX_CHARS = 1500000  # Adjust as needed, Gemini 1.5 has large context
        if len(permit_text) > MAX_CHARS:
            logging.warning(f"""  Text from {filename} is very long
                             ({len(permit_text)} chars). 
                             Processing may be slow/costly.""")
            # Consider truncation or chunking for extremely large files.
            # permit_text = permit_text[:MAX_CHARS]

        # 2. Extract Info using LLM
        extracted_info = extract_info_with_llm(llm_model,
                                               permit_text,
                                               filename)

        # 3. Process Results and Create Rows
        if extracted_info and isinstance(extracted_info, dict):
            general_info = {field: extracted_info.get(field) for field in GENERAL_TARGET_FIELDS}
            emission_units = extracted_info.get("Emission Units", [])

            if isinstance(emission_units, list) and emission_units:
                logging.info(f"""  Extracted {len(emission_units)}
                             emission units from {filename}.""")
                for unit in emission_units:
                    if isinstance(unit, dict): # Ensure unit is a dictionary
                        row_data = {"Filename": filename, "Status": "Success"}
                        row_data.update(general_info) # Add general permit info
                        # Add unit details, using .get() for safety
                        for field in UNIT_DETAIL_FIELDS:
                            row_data[field] = unit.get(field)
                        processed_data_rows.append(row_data)
                    else:
                        logging.warning(f"""  Skipping invalid unit
                                        entry in {filename}: {unit}""")
                        # Optionally log a row indicating a malformed unit
                        row_data = {"Filename": filename,
                                    "Status": "Malformed Unit Data"}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS:
                            row_data[field] = "INVALID UNIT ENTRY"
                        processed_data_rows.append(row_data)

            else:
                # Permit processed, but no units found or unit list was empty
                logging.info(f"""  No valid emission units extracted
                             or found for {filename}.""")
                row_data = {"Filename": filename, 
                            "Status": "Success (No Units Found)"}
                row_data.update(general_info)
                # Add null/empty placeholders for unit fields
                for field in UNIT_DETAIL_FIELDS: 
                    row_data[field] = None
                processed_data_rows.append(row_data)
        else:
            # LLM extraction failed completely for this file
            logging.error(f"  Failed to extract information from {filename}.")
            processed_data_rows.append({"Filename": filename,
                                        "Status": "LLM Extraction Failed",
                                        **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}})

    # 4. Save to Excel
    if processed_data_rows:
        logging.info(f"\nSaving extracted data to {OUTPUT_EXCEL_FILE}...")
        try:
            df = pd.DataFrame(processed_data_rows)
            # Define final column order including Filename and Status
            excel_columns = ["Filename", "Status"] + ALL_OUTPUT_FIELDS
            # Ensure all expected columns exist, adding missing ones with None
            for col in excel_columns:
                if col not in df.columns:
                    df[col] = None
            df = df[excel_columns] # Reorder/select columns
            df.to_excel(OUTPUT_EXCEL_FILE, index=False, engine='openpyxl')
            logging.info(f"Data successfully saved to '{OUTPUT_EXCEL_FILE}'.")
        except Exception as e:
            logging.error(f"Error saving data to Excel: {e}", exc_info=True)
    else:
        logging.warning("No data was processed to save.")

    logging.info("\nExtraction process finished.")


if __name__ == "__main__":
    app()
