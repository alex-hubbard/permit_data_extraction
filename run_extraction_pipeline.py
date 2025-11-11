#!/usr/bin/env python3
"""
Run the full extraction pipeline on all downloaded PDF permits.

This script:
1. Finds all PDF files in data/raw/
2. Extracts text from each PDF (skips if already done)
3. Prepares for LLM extraction

Usage:
    python run_extraction_pipeline.py
"""

from pathlib import Path
import sys

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from permit_data_extraction.ocr import extract_text_from_single_pdf
from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR


def find_all_pdfs(raw_dir):
    """Find all PDF files in the raw data directory."""
    raw_path = Path(raw_dir)
    
    # Find PDFs in multiple locations
    pdf_files = []
    
    # EPA final permits
    epa_permits_dir = raw_path / 'epa_final_permits'
    if epa_permits_dir.exists():
        pdf_files.extend(epa_permits_dir.glob('**/*.pdf'))
    
    # Downloaded PDFs
    downloaded_dir = raw_path / 'downloaded_pdfs'
    if downloaded_dir.exists():
        pdf_files.extend(downloaded_dir.glob('**/*.pdf'))
    
    # Any other PDFs in raw
    pdf_files.extend(raw_path.glob('*.pdf'))
    
    # Remove duplicates and sort
    pdf_files = sorted(set(pdf_files))
    
    return pdf_files


def extract_text_from_all_pdfs(pdf_files, output_dir):
    """Extract text from all PDF files."""
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*80}")
    print(f"PDF TEXT EXTRACTION")
    print(f"{'='*80}")
    print(f"Found {len(pdf_files)} PDF files to process")
    print(f"Output directory: {output_path}")
    print(f"{'='*80}\n")
    
    results = {
        'total': len(pdf_files),
        'successful': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }
    
    for i, pdf_path in enumerate(pdf_files, 1):
        # Create output filename
        output_filename = pdf_path.stem + '.txt'
        output_file = output_path / output_filename
        
        # Skip if already extracted
        if output_file.exists():
            print(f"[{i}/{len(pdf_files)}] ⊙ SKIP: {pdf_path.name} (already extracted)")
            results['skipped'] += 1
            continue
        
        print(f"[{i}/{len(pdf_files)}] Processing: {pdf_path.name}")
        
        try:
            # Extract text
            text, method = extract_text_from_single_pdf(pdf_path)
            
            if text:
                # Save to file
                output_file.write_text(text, encoding='utf-8')
                char_count = len(text)
                print(f"  ✓ SUCCESS: {char_count:,} characters ({method})")
                print(f"  Saved to: {output_filename}")
                results['successful'] += 1
            else:
                print(f"  ✗ FAILED: No text extracted ({method})")
                results['failed'] += 1
                results['errors'].append({
                    'file': str(pdf_path),
                    'error': f'No text extracted ({method})'
                })
        
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results['failed'] += 1
            results['errors'].append({
                'file': str(pdf_path),
                'error': str(e)
            })
    
    # Print summary
    print(f"\n{'='*80}")
    print(f"EXTRACTION SUMMARY")
    print(f"{'='*80}")
    print(f"Total PDFs found:        {results['total']}")
    print(f"Successfully extracted:  {results['successful']}")
    print(f"Already existed:         {results['skipped']}")
    print(f"Failed:                  {results['failed']}")
    print(f"\nText files location: {output_path}")
    print(f"{'='*80}\n")
    
    if results['errors']:
        print(f"\nErrors encountered:")
        for error in results['errors'][:10]:  # Show first 10 errors
            print(f"  - {Path(error['file']).name}: {error['error']}")
        if len(results['errors']) > 10:
            print(f"  ... and {len(results['errors']) - 10} more")
    
    return results


def main():
    """Run the extraction pipeline."""
    
    print("\n" + "="*80)
    print("PERMIT DATA EXTRACTION PIPELINE")
    print("="*80)
    
    # Step 1: Find all PDFs
    print("\nStep 1: Finding PDF files...")
    pdf_files = find_all_pdfs(RAW_DATA_DIR)
    
    if not pdf_files:
        print("ERROR: No PDF files found in data/raw/")
        print(f"Searched in: {RAW_DATA_DIR}")
        return 1
    
    print(f"Found {len(pdf_files)} PDF files in:")
    # Group by directory
    directories = {}
    for pdf in pdf_files:
        dir_name = pdf.parent.relative_to(RAW_DATA_DIR) if RAW_DATA_DIR in pdf.parents else pdf.parent
        directories[str(dir_name)] = directories.get(str(dir_name), 0) + 1
    
    for dir_name, count in sorted(directories.items()):
        print(f"  - {dir_name}: {count} files")
    
    # Step 2: Extract text
    print("\nStep 2: Extracting text from PDFs...")
    text_output_dir = INTERIM_DATA_DIR / 'extracted_text'
    results = extract_text_from_all_pdfs(pdf_files, text_output_dir)
    
    # Step 3: Next steps
    total_text_files = results['successful'] + results['skipped']
    
    if total_text_files > 0:
        print(f"\nStep 3: LLM Extraction (Next Step)")
        print(f"{'='*80}")
        print(f"\nYou now have {total_text_files} text files ready for LLM extraction.")
        print(f"\nTo run the LLM extraction and create the Excel output, run:")
        print(f"\n    python -m permit_data_extraction.dataset main")
        print(f"\nThis will:")
        print(f"  - Read all text files from {text_output_dir}")
        print(f"  - Extract structured data using LLM (GPT-4)")
        print(f"  - Save results to data/processed/permit_data_extracted.xlsx")
        print(f"\n{'='*80}\n")
    else:
        print("\nNo text files were successfully extracted.")
        print("Cannot proceed to LLM extraction.")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

