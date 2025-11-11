#!/usr/bin/env python3
"""
Download EPA Final Permits from the state permit links CSV.

This uses the V2 downloader that handles the EPA JavaScript website properly
by using Selenium to click download buttons.

Usage:
    # Download first 10 permits total (for testing)
    python download_epa_permits_v2.py --max 10
    
    # Download 10 permits from each state (better sampling)
    python download_epa_permits_v2.py --per-state 10
    
    # Download all permits
    python download_epa_permits_v2.py
    
    # Resume from row 100
    python download_epa_permits_v2.py --resume 100
    
    # Show browser (for debugging)
    python download_epa_permits_v2.py --no-headless --per-state 2
"""

import pandas as pd
from permit_data_extraction.epa_pdf_downloader_v2 import EPAPermitPDFDownloader

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Download EPA Final Permits from all_permit_links.csv'
    )
    parser.add_argument(
        '--max',
        type=int,
        help='Maximum number of permits to download total (for testing)',
        default=None
    )
    parser.add_argument(
        '--per-state',
        type=int,
        help='Download N permits from each state (better sampling)',
        default=None
    )
    parser.add_argument(
        '--resume',
        type=int,
        help='Row index to resume from (0-based). Only works with --max',
        default=0
    )
    parser.add_argument(
        '--delay',
        type=int,
        help='Delay between requests in seconds (default: 3)',
        default=3
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
    
    print(f"EPA Final Permit Downloader V2")
    print(f"CSV: {csv_path}")
    
    # Handle per-state sampling
    if args.per_state:
        print(f"Downloading {args.per_state} permits from each state")
        print()
        
        # Read CSV and sample per state
        df = pd.read_csv(csv_path)
        
        # Group by state and take N from each
        sampled_df = df.groupby('state_code').head(args.per_state)
        
        print(f"Total permits to download: {len(sampled_df)}")
        print(f"States: {sampled_df['state_code'].nunique()}")
        print("\nPermits per state:")
        print(sampled_df['state_code'].value_counts().sort_index())
        print()
        
        # Save to temp CSV
        temp_csv = 'temp_sampled_permits.csv'
        sampled_df.to_csv(temp_csv, index=False)
        
        # Download from temp CSV
        results = downloader.download_from_csv(temp_csv, max_permits=None, resume_from=0)
        
        # Clean up temp file
        import os
        os.remove(temp_csv)
        
    elif args.max:
        print(f"Downloading first {args.max} permits (starting from row {args.resume})")
        print()
        results = downloader.download_from_csv(
            csv_path,
            max_permits=args.max,
            resume_from=args.resume
        )
    else:
        print(f"Downloading all permits (starting from row {args.resume})")
        print()
        results = downloader.download_from_csv(
            csv_path,
            max_permits=None,
            resume_from=args.resume
        )
    
    return results


if __name__ == "__main__":
    main()

