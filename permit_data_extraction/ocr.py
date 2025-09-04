import PyPDF2
import os
from pathlib import Path
import logging
import gc  # Import the garbage collection module

# OCR specific imports
try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import pytesseract
except ImportError:
    pytesseract = None
try:
    from pdf2image import convert_from_path, pdfinfo_from_path
except ImportError:
    convert_from_path = None
    pdfinfo_from_path = None

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Directory containing your PDF permit files
PDF_INPUT_DIR = Path(RAW_DATA_DIR)  # Example: Path('./raw_permits')

# Directory where extracted .txt files will be saved
TEXT_OUTPUT_DIR = Path(INTERIM_DATA_DIR / 'extracted_text')

# --- Tesseract Configuration ---
TESSERACT_CMD = None
TESSDATA_PREFIX_CONFIG = '/usr/share/tesseract-ocr/4.00/tessdata'

# --- Poppler Configuration ---
POPPLER_PATH_CONFIG = None  # Example: r'C:\poppler-23.08.0\Library\bin'

# --- OCR Performance Configuration ---
OCR_CHUNK_SIZE = 5  # Number of pages to process in one OCR batch
OCR_DPI = 200       # DPI for converting PDF pages to images for OCR

# --- Function Definitions ---


def get_pdf_page_count_pdf2image(pdf_path, poppler_path_to_use=None):
    """Gets PDF page count using pdfinfo_from_path to avoid PyPDF2 issues with some PDFs."""
    if not pdfinfo_from_path:
        logging.warning("pdfinfo_from_path (from pdf2image) not available. Cannot get page count for OCR chunking robustly.")
        return 0 # Fallback, though OCR chunking might not work as expected
    try:
        info = pdfinfo_from_path(pdf_path, poppler_path=poppler_path_to_use)
        return info.get('Pages', 0)
    except Exception as e:
        logging.error(f"    Error getting page count with pdfinfo_from_path for {pdf_path.name}: {e}")
        return 0


def ocr_pdf_pages(pdf_path, total_pages, poppler_path_to_use=None):
    """
    Performs OCR on PDF pages in chunks to save memory.
    """
    if not pytesseract or not convert_from_path or not Image:
        logging.error("OCR libraries (pytesseract, pdf2image, Pillow) not available. Skipping OCR.")
        return None
    if total_pages == 0:
        logging.warning(f"    Skipping OCR for {pdf_path.name} as it has 0 pages or page count couldn't be determined.")
        return ""


    ocr_text_content = ""
    logging.info(f"    Attempting OCR for {pdf_path.name} ({total_pages} pages) in chunks of {OCR_CHUNK_SIZE} at {OCR_DPI} DPI...")

    for page_start_num in range(1, total_pages + 1, OCR_CHUNK_SIZE):
        page_end_num = min(page_start_num + OCR_CHUNK_SIZE - 1, total_pages)
        logging.info(f"      Processing pages {page_start_num} to {page_end_num} of {total_pages} for {pdf_path.name}...")

        images = [] # Initialize images list for the current chunk
        try:
            images = convert_from_path(
                pdf_path,
                dpi=OCR_DPI,
                first_page=page_start_num,
                last_page=page_end_num,
                poppler_path=poppler_path_to_use
            )

            for i, image in enumerate(images):
                current_page_for_log = page_start_num + i
                logging.info(f"        OCRing page {current_page_for_log} (image {i+1}/{len(images)} in chunk)...")
                try:
                    page_text_content = pytesseract.image_to_string(image, lang='eng')
                    ocr_text_content += page_text_content + "\n\n--- Page Break ---\n\n"
                except pytesseract.TesseractNotFoundError:
                    logging.error("Tesseract executable not found. Set TESSERACT_CMD or ensure it's in PATH.")
                    return None # Critical error
                except pytesseract.TesseractError as te:
                    logging.error(f"        Tesseract error on page {current_page_for_log}: {te}. Check TESSDATA_PREFIX_CONFIG.")
                    # Optionally continue to next page/chunk or return None
                except Exception as ocr_page_e:
                    logging.warning(f"        Error OCRing page {current_page_for_log}: {ocr_page_e}")
                finally:
                    # Explicitly close image to free memory if possible (Pillow images are auto-closed by GC usually)
                    if hasattr(image, 'close'):
                        image.close()
            
            # Clear the list of images for the current chunk and suggest garbage collection
            del images 
            gc.collect()

        except Exception as e:
            logging.error(f"      Error during PDF to image conversion for pages {page_start_num}-{page_end_num} of {pdf_path.name}: {e}", exc_info=True)
            if "Unable to get page count" in str(e) or "pdfinfo" in str(e) or "pdftoppm" in str(e):
                logging.error("      This error often means Poppler utilities are not installed/found or POPPLER_PATH_CONFIG is incorrect.")
            # Decide if you want to stop for the whole file or try the next chunk
            # For now, we'll stop if a chunk fails conversion.
            return None # Or you could return partial ocr_text_content

    logging.info(f"    OCR completed for {pdf_path.name}. Extracted approx {len(ocr_text_content)} chars.")
    return ocr_text_content

def extract_text_from_single_pdf(pdf_path):
    """
    Extracts text from a single PDF file.
    Uses PyPDF2 first, then falls back to chunked OCR if needed.
    """
    direct_text_content = ""
    pypdf2_num_pages = 0
    extraction_method_used = "PyPDF2 (Direct)"

    # Try to get page count first using pdf2image's pdfinfo for robustness in OCR step
    # This is preferred over PyPDF2's page count if OCR is likely
    pdf2image_page_count = get_pdf_page_count_pdf2image(pdf_path, poppler_path_to_use=POPPLER_PATH_CONFIG)

    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            pypdf2_num_pages = len(reader.pages) # PyPDF2's perspective on page count

            if pypdf2_num_pages == 0 and pdf2image_page_count == 0:
                 logging.warning(f"  {pdf_path.name} reported as 0 pages by both PyPDF2 and pdfinfo. Skipping.")
                 return None, "No Pages Found"
            
            # If PyPDF2 says 0 pages but pdfinfo found pages, trust pdfinfo for OCR path
            if pypdf2_num_pages == 0 and pdf2image_page_count > 0:
                logging.warning(f"  {pdf_path.name} has 0 pages by PyPDF2, but {pdf2image_page_count} by pdfinfo. Attempting OCR.")
                ocr_text = ocr_pdf_pages(pdf_path, total_pages=pdf2image_page_count, poppler_path_to_use=POPPLER_PATH_CONFIG)
                if ocr_text is not None: # Check for None in case OCR itself fails
                    return ocr_text, "OCR (PyPDF2 reported 0 pages)"
                return None, "OCR Failed (PyPDF2 reported 0 pages)"

            logging.info(f"  Reading {pypdf2_num_pages} pages from {pdf_path.name} using PyPDF2...")
            for page_num in range(pypdf2_num_pages):
                try:
                    page = reader.pages[page_num]
                    page_text_content = page.extract_text()
                    if page_text_content:
                        direct_text_content += page_text_content + "\n\n--- Page Break ---\n\n"
                except Exception as page_e:
                    logging.warning(f"    Could not extract text from page {page_num + 1} with PyPDF2 in {pdf_path.name}: {page_e}")
        logging.info(f"  PyPDF2 extracted approx {len(direct_text_content)} chars from {pdf_path.name}.")

        min_chars_threshold = pypdf2_num_pages * 30 # Reduced threshold slightly
        # Use pdf2image_page_count for OCR if available and PyPDF2 text is minimal
        effective_pages_for_ocr = pdf2image_page_count if pdf2image_page_count > 0 else pypdf2_num_pages

        if effective_pages_for_ocr > 0 and (not direct_text_content or len(direct_text_content.replace("--- Page Break ---", "").strip()) < min_chars_threshold):
            logging.info(f"  Direct text from {pdf_path.name} is minimal. Attempting OCR fallback using {effective_pages_for_ocr} pages.")
            ocr_text_content = ocr_pdf_pages(pdf_path, total_pages=effective_pages_for_ocr, poppler_path_to_use=POPPLER_PATH_CONFIG)
            if ocr_text_content is not None: # Check for None
                logging.info(f"  Using OCR text for {pdf_path.name}.")
                extraction_method_used = "OCR (Fallback)"
                return ocr_text_content, extraction_method_used
            else:
                logging.warning(f"  OCR fallback failed for {pdf_path.name}. Using PyPDF2 text (if any).")
                return direct_text_content if direct_text_content else None, extraction_method_used if direct_text_content else "PyPDF2 (OCR Failed)"
        else:
            logging.info(f"  Using direct text extraction (PyPDF2) for {pdf_path.name}.")
            return direct_text_content, extraction_method_used

    except FileNotFoundError:
        logging.error(f"  File not found at {pdf_path}")
        return None, "File Not Found"
    except PyPDF2.errors.PdfReadError as pdf_err:
        logging.error(f"  Error reading PDF {pdf_path.name} with PyPDF2: {pdf_err}. Attempting OCR.")
        # Use pdf2image_page_count if available, otherwise attempt OCR without knowing total pages (ocr_pdf_pages handles total_pages=0)
        effective_pages_for_ocr = pdf2image_page_count if pdf2image_page_count > 0 else 0 # ocr_pdf_pages will try to get count if 0
        if effective_pages_for_ocr == 0 and pdfinfo_from_path: # Try one last time to get page count if not already done
            logging.info(f"  PyPDF2 failed, attempting to get page count via pdfinfo for {pdf_path.name} before OCR.")
            effective_pages_for_ocr = get_pdf_page_count_pdf2image(pdf_path, poppler_path_to_use=POPPLER_PATH_CONFIG)

        ocr_text_content = ocr_pdf_pages(pdf_path, total_pages=effective_pages_for_ocr, poppler_path_to_use=POPPLER_PATH_CONFIG)
        if ocr_text_content is not None:
            extraction_method_used = "OCR (PyPDF2 ReadError)"
            return ocr_text_content, extraction_method_used
        return None, "PyPDF2 ReadError (OCR Failed)"
    except Exception as e:
        logging.error(f"  Unexpected error processing PDF {pdf_path.name}: {e}", exc_info=True)
        return None, "Unexpected Error"


def save_text_to_file(text_content, output_path):
    """Saves the given text content to a file."""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text_content)
        logging.info(f"    Successfully saved extracted text to {output_path}")
    except Exception as e:
        logging.error(f"    Error saving text to {output_path}: {e}", exc_info=True)

# --- Main Execution ---
def main():
    logging.info("Starting PDF to Text Conversion Process...")

    # --- Apply Tesseract Configurations ---
    if TESSERACT_CMD and pytesseract:
        logging.info(f"Setting pytesseract.tesseract_cmd to: {TESSERACT_CMD}")
        pytesseract.tesseract_cmd = TESSERACT_CMD
    # ... (rest of Tesseract config as before) ...
    if TESSDATA_PREFIX_CONFIG:
        tessdata_path = Path(TESSDATA_PREFIX_CONFIG)
        if tessdata_path.is_dir():
            logging.info(f"Setting TESSDATA_PREFIX environment variable to: {TESSDATA_PREFIX_CONFIG}")
            os.environ['TESSDATA_PREFIX'] = str(tessdata_path.resolve())
            eng_traineddata_file = tessdata_path / 'eng.traineddata'
            if not eng_traineddata_file.is_file():
                logging.warning(f"'eng.traineddata' not found in TESSDATA_PREFIX_CONFIG: {eng_traineddata_file}.")
            else:
                logging.info(f"'eng.traineddata' found at {eng_traineddata_file}.")
        else:
            logging.warning(f"TESSDATA_PREFIX_CONFIG directory does not exist: {TESSDATA_PREFIX_CONFIG}.")
    else:
        logging.info("TESSDATA_PREFIX_CONFIG not set. Tesseract will try default paths for 'tessdata'.")

    # Log Poppler path
    if POPPLER_PATH_CONFIG:
        logging.info(f"Using configured Poppler path: {POPPLER_PATH_CONFIG}")
    else:
        logging.info("POPPLER_PATH_CONFIG not set. Relying on Poppler in system PATH.")

    if not PDF_INPUT_DIR.is_dir():
        logging.critical(f"PDF input directory not found: '{PDF_INPUT_DIR}'")
        return
    if not TEXT_OUTPUT_DIR.exists():
        TEXT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = list(PDF_INPUT_DIR.glob('*.pdf'))
    if not pdf_files:
        logging.warning(f"No PDF files found in '{PDF_INPUT_DIR}'.")
        return

    logging.info(f"Found {len(pdf_files)} PDF files to process.")
    summary = []

    for pdf_path in pdf_files:
        logging.info(f"\nProcessing file: {pdf_path.name}")
        # Ensure text_content is not None before trying to use it
        text_content, method = extract_text_from_single_pdf(pdf_path)

        if text_content is not None: # Check if text_content is not None
            output_filename = pdf_path.stem + ".txt"
            output_file_path = TEXT_OUTPUT_DIR / output_filename
            save_text_to_file(text_content, output_file_path)
            summary.append({"filename": pdf_path.name, "status": "Success", "method": method, "output_file": str(output_file_path)})
        else:
            logging.warning(f"  No text extracted from {pdf_path.name}. Method: {method}")
            summary.append({"filename": pdf_path.name, "status": "Failed", "method": method, "output_file": None})
        
        # Aggressive garbage collection after each file
        gc.collect()


    logging.info("\n--- Processing Summary ---")
    for item in summary:
        logging.info(f"File: {item['filename']}, Status: {item['status']}, Method: {item['method']}, Output: {item['output_file']}")
    logging.info("PDF to Text conversion process finished.")

if __name__ == "__main__":
    # Dependency checks
    if not pytesseract or not convert_from_path or not Image:
        logging.warning("OCR dependencies (pytesseract, pdf2image, Pillow) not fully installed. OCR functionality will be limited.")
    if not convert_from_path and not pdfinfo_from_path: # pdfinfo_from_path also comes from pdf2image
         logging.warning("The 'pdf2image' library (for convert_from_path/pdfinfo_from_path) is not available. PDF to image conversion for OCR will fail.")
    main()
