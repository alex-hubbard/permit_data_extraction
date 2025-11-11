#!/usr/bin/env python3
"""Test the fixed EPA PDF downloader."""

from permit_data_extraction.epa_pdf_downloader_v2 import EPAPermitPDFDownloader

# Test URL
test_url = "https://permitsearch.epa.gov/oms-permit-hub/permit/8a6a70b0-19bc-ef11-b8e8-001dd8001877"
permit_id = "8a6a70b0-19bc-ef11-b8e8-001dd8001877"
state_code = "AL"

print("Testing fixed EPA PDF downloader with better Angular wait logic...")
print(f"URL: {test_url}")
print("=" * 80)

downloader = EPAPermitPDFDownloader(
    output_dir="data/raw/epa_test_fixed",
    headless=True
)

result = downloader.download_permit_pdf(test_url, permit_id, state_code)

print("\n" + "=" * 80)
print("RESULT:")
print("=" * 80)
print(f"Status: {result['status']}")
print(f"Filename: {result['filename']}")
print(f"PDF Path: {result['pdf_path']}")
if result['error']:
    print(f"Error: {result['error']}")

