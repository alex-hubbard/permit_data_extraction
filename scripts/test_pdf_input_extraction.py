#!/usr/bin/env python3
"""
Test script: send PDF input to the LLM instead of pre-extracted text.

Can you send the whole PDF to the LLM?
  Yes, in two ways:
  (1) File upload: some APIs (e.g. OpenAI with supported models) accept a PDF
      via the Files API and reference it in the request. Your proxy may or may not
      support this.
  (2) Page images (vision): render each PDF page to an image and send as vision
      content. This works with any vision-capable model and is the most reliable
      option when file upload is not available. Use --max-pages to limit cost/size.

Two approaches implemented:

1. **PDF file upload** (--method file): Upload via Files API and reference file_id.
   Not all OpenAI-compatible backends support this.

2. **PDF as page images** (--method images): Render pages with pdf2image, base64-
   encode, and send as image_url content. Requires: pip install pdf2image (and
   poppler-utils on the system for pdf2image).

Default (--method auto): try file upload, then fall back to page images.

Run from project root, e.g.:
  python scripts/test_pdf_input_extraction.py path/to/permit.pdf
  python scripts/test_pdf_input_extraction.py path/to/permit.pdf --max-pages 5
  python scripts/test_pdf_input_extraction.py path/to/permit.pdf --method images --out result.json
"""

import base64
import io
import json
import logging
import sys
from pathlib import Path

# Add project root for imports
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from permit_data_extraction.dataset import (
    GENERAL_TARGET_FIELDS,
    UNIT_DETAIL_FIELDS,
    configure_llm,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Model: use a vision-capable model for image path; file upload may work with same or different model
LLM_MODEL = "amazon/gpt-oss-120b"  # or e.g. "openai/gpt-4.1" if your endpoint has vision

# For image path: limit page size/count to avoid token limits and timeouts
VISION_DPI = 150
DEFAULT_MAX_PAGES = 15

# Prompt for document/images (same extraction task, no {permit_text} placeholder)
DOCUMENT_PROMPT = f"""Analyze the following document. It is an industrial air permit (either as attached file or as page images below). Your goal is to extract key permit information AND details about individual emission units.

**Instructions:**

1. **Extract General Information:** Identify the following general details for the permit:
   * {', '.join(GENERAL_TARGET_FIELDS)}

2. **Extract Emission Unit Details:** Identify each distinct permitted emission unit. For each unit, extract:
   * {', '.join(UNIT_DETAIL_FIELDS)}
   * Look for information in sections describing specific equipment, process lines, or in tables.

3. **Output Format:** Respond with a single, valid JSON object.
   * General information as top-level key-value pairs.
   * Emission unit details in a JSON array named "Emission Units". Each element is an object with the unit fields above.
   * Use the exact field names as keys. Use null or empty string when information is not found.
   * If NO emission units are clearly identified, use an empty array [] for "Emission Units".

**Example JSON structure:**

{{
  "Facility Name": "Example Plant",
  "Permit Number": "123-ABC",
  "Issuance Date": "YYYY-MM-DD",
  "Expiration Date": "YYYY-MM-DD",
  "Regulatory Authority": "State EPA",
  "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)": "Title V, 40 CFR 63",
  "Emission Units": [
    {{
      "Unit ID": "EU001",
      "Unit Description": "Natural Gas Boiler 1",
      "Pollutants": "NOx, CO, PM",
      "Emission Limits": "NOx: 0.05 lb/MMBtu",
      "Control Device(s)": "Low NOx Burner"
    }}
  ]
}}

Respond with only the JSON object, no other text."""


def pdf_to_images(pdf_path: Path, max_pages: int = DEFAULT_MAX_PAGES, dpi: int = VISION_DPI):
    """Render PDF pages to PNG images (base64). Returns list of (page_num, base64_string)."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        logger.error("pdf2image is required for --method images. Install with: pip install pdf2image")
        return []

    try:
        images = convert_from_path(pdf_path, dpi=dpi, first_page=1, last_page=max_pages)
    except Exception as e:
        logger.error("Failed to convert PDF to images: %s", e)
        return []

    out = []
    for i, img in enumerate(images):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        out.append((i + 1, b64))
    return out


def extract_with_pdf_file_upload(client, pdf_path: Path, model: str):
    """Try to upload PDF and send file_id to the model. Returns (data, error)."""
    try:
        with open(pdf_path, "rb") as f:
            file_response = client.files.create(file=f, purpose="user_data")
    except Exception as e:
        return None, f"File upload not supported or failed: {e}"

    file_id = file_response.id
    logger.info("Uploaded PDF as file_id=%s", file_id)

    # OpenAI Responses API style (input_file) – may not be supported by all backends
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert at extracting structured information from industrial air permit documents. Respond with valid JSON only."},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": file_id},
                        {"type": "text", "text": DOCUMENT_PROMPT},
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=8192,
            timeout=120,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        # Fallback: some APIs expect file in a different shape
        err_str = str(e).lower()
        if "input_file" in err_str or "content" in err_str or "file" in err_str:
            return None, f"API does not support PDF file in message: {e}"
        raise

    return _parse_response(response, pdf_path.name)


def extract_with_page_images(client, pdf_path: Path, model: str, max_pages: int):
    """Send each PDF page as an image (vision) and get extraction. Returns (data, error)."""
    page_images = pdf_to_images(pdf_path, max_pages=max_pages, dpi=VISION_DPI)
    if not page_images:
        return None, "No pages could be converted to images"

    logger.info("Sending %d page(s) as images to model %s", len(page_images), model)

    content = [{"type": "text", "text": DOCUMENT_PROMPT}]
    for page_num, b64 in page_images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert at extracting structured information from industrial air permit documents. Respond with valid JSON only."},
                {"role": "user", "content": content},
            ],
            temperature=0.1,
            max_tokens=8192,
            timeout=180,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        return None, str(e)

    return _parse_response(response, pdf_path.name)


def _parse_response(response, filename: str):
    """Parse JSON from response. Returns (data_dict, error_str)."""
    if not response or not response.choices or len(response.choices) == 0:
        return None, "Empty or malformed response"

    raw = response.choices[0].message.content.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(raw)
        if "Emission Units" not in data or not isinstance(data.get("Emission Units"), list):
            data["Emission Units"] = data.get("Emission Units") or []
        return data, None
    except json.JSONDecodeError as e:
        return None, f"JSON decode error: {e}"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test extraction using PDF input (file or page images)")
    parser.add_argument("pdf", type=Path, help="Path to permit PDF")
    parser.add_argument("--method", choices=["auto", "file", "images"], default="auto",
                        help="auto: try file upload then images; file: only file upload; images: only page images")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help="Max PDF pages to send when using images (default %s)" % DEFAULT_MAX_PAGES)
    parser.add_argument("--model", default=LLM_MODEL, help="Model name")
    parser.add_argument("--out", type=Path, help="Write extracted JSON to this file")
    args = parser.parse_args()

    pdf_path = args.pdf
    if not pdf_path.is_file():
        logger.error("Not a file: %s", pdf_path)
        sys.exit(1)


    client = configure_llm()
    if not client:
        logger.error("Failed to configure LLM client (check CBORG_API_KEY and network).")
        sys.exit(1)

    result = None
    err = None

    if args.method == "file":
        result, err = extract_with_pdf_file_upload(client, pdf_path, args.model)
    elif args.method == "images":
        result, err = extract_with_page_images(client, pdf_path, args.model, args.max_pages)
    else:
        result, err = extract_with_pdf_file_upload(client, pdf_path, args.model)
        if err and "not supported" in err.lower():
            logger.info("File upload not supported; falling back to page images.")
            result, err = extract_with_page_images(client, pdf_path, args.model, args.max_pages)

    if err:
        logger.error("Extraction failed: %s", err)
        sys.exit(1)

    logger.info("Extraction succeeded.")
    print(json.dumps(result, indent=2))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        logger.info("Wrote %s", args.out)


if __name__ == "__main__":
    main()
