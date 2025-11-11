#!/usr/bin/env python3
"""
Test the EPA PDF downloader v2 on a single permit.
"""

from permit_data_extraction.epa_pdf_downloader_v2 import EPAPermitPDFDownloader

def test_single_permit():
    # Test URL from the CSV
    test_url = "https://permitsearch.epa.gov/oms-permit-hub/permit/8a6a70b0-19bc-ef11-b8e8-001dd8001877"
    permit_id = "8a6a70b0-19bc-ef11-b8e8-001dd8001877"
    state_code = "AL"
    
    print("Testing EPA PDF Downloader V2 on a single permit...")
    print(f"URL: {test_url}")
    print(f"Permit ID: {permit_id}")
    print(f"State: {state_code}")
    print("=" * 80)
    
    # Create downloader with test output directory
    downloader = EPAPermitPDFDownloader(
        output_dir="data/raw/epa_test_v2",
        headless=True
    )
    
    # Try to download
    result = downloader.download_permit_pdf(test_url, permit_id, state_code)
    
    print("\n" + "=" * 80)
    print("RESULT:")
    print("=" * 80)
    print(f"Status: {result['status']}")
    print(f"Filename: {result['filename']}")
    print(f"PDF Path: {result['pdf_path']}")
    if result['error']:
        print(f"Error: {result['error']}")
    
    return result


if __name__ == "__main__":
    test_single_permit()

