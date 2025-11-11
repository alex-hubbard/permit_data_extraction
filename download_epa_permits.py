#!/usr/bin/env python3
"""
Example script to download EPA Final Permits from the state permit links CSV.

Usage:
    # Download all permits
    python download_epa_permits.py
    
    # Download first 10 permits (for testing)
    python download_epa_permits.py --max 10
    
    # Resume from row 100
    python download_epa_permits.py --resume 100
"""

from permit_data_extraction.epa_pdf_downloader import EPAPermitPDFDownloader
from pathlib import Path

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Download EPA Final Permits from all_permit_links.csv'
    )
    parser.add_argument(
        '--max',
        type=int,
        help='Maximum number of permits to download (for testing)',
        default=None
    )
    parser.add_argument(
        '--resume',
        type=int,
        help='Row index to resume from (0-based)',
        default=0
    )
    parser.add_argument(
        '--delay',
        type=int,
        help='Delay between requests in seconds (default: 2)',
        default=2
    )
    parser.add_argument(
        '--no-headless',
        action='store_true',
        help='Run Chrome in visible mode (for debugging)',
        default=False
    )
    
    args = parser.parse_args()
    
    # Path to your CSV file
    csv_path = 'data/raw/state_permit_links/all_permit_links.csv'
    
    # Create downloader
    downloader = EPAPermitPDFDownloader(
        delay_seconds=args.delay,
        headless=not args.no_headless
    )
    
    print(f"EPA Final Permit Downloader")
    print(f"CSV: {csv_path}")
    if args.max:
        print(f"Downloading first {args.max} permits (starting from row {args.resume})")
    else:
        print(f"Downloading all permits (starting from row {args.resume})")
    print()
    
    # Download permits
    results = downloader.download_from_csv(
        csv_path,
        max_permits=args.max,
        resume_from=args.resume
    )
    
    return results


if __name__ == "__main__":
    main()

