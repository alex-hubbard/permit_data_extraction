#!/usr/bin/env python3
"""
Quick analysis of permit links data
"""

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path

def quick_analysis():
    """Quick analysis of the permit links data."""
    
    # Read the permit links data
    df = pd.read_csv('data/external/state_permit_links/all_permit_links.csv')
    
    print("=== EPA PERMIT LINKS ANALYSIS ===")
    print(f"Total permit links: {len(df)}")
    print(f"States with data: {df['state_code'].nunique()}")
    print(f"Unique permit IDs: {df['permit_id'].nunique()}")
    
    # Count by state
    state_counts = df['state_code'].value_counts()
    print(f"\nPermit links by state:")
    for state, count in state_counts.items():
        print(f"  {state}: {count} links")
    
    # Create a simple bar chart
    fig, ax = plt.subplots(1, 1, figsize=(10, 6))
    
    # Create bar plot
    bars = ax.bar(state_counts.index, state_counts.values, color='skyblue', edgecolor='navy')
    
    # Add value labels on bars
    for bar, count in zip(bars, state_counts.values):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, 
                str(count), ha='center', va='bottom', fontweight='bold')
    
    # Customize chart
    ax.set_title('EPA Permit Links Discovered by State', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('State', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Permit Links', fontsize=12, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)
    
    # Add summary text
    plt.figtext(
        0.02, 0.02,
        f'Total Permit Links: {len(df):,}\n'
        f'States with Data: {df["state_code"].nunique()}\n'
        f'Data Source: EPA Permit Hub\n'
        f'Generated: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=10,
        style='italic',
        color='gray'
    )
    
    plt.tight_layout()
    
    # Save the chart
    output_dir = Path("reports/figures")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / "permit_links_quick_analysis.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight', facecolor='white')
    
    print(f"\nChart saved to: {output_path}")
    
    # Create summary text
    summary_text = f"""
================================================================================
                    EPA PERMIT LINKS DISCOVERY SUMMARY
================================================================================

OVERALL STATISTICS:
• Total Permit Links Found: {len(df):,}
• States with Permit Data: {df['state_code'].nunique()}
• Unique Permit IDs: {df['permit_id'].nunique()}

PERMIT LINKS BY STATE:
"""
    
    for state, count in state_counts.items():
        percentage = (count / len(df)) * 100
        summary_text += f"• {state}: {count} links ({percentage:.1f}%)\n"
    
    summary_text += f"""
DATA QUALITY:
• Duplicate URLs: {len(df) - df['url'].nunique()}
• Pages Scraped: {df['page_number'].max() if 'page_number' in df.columns else 'N/A'}

NOTES:
• Currently only Connecticut (CT) has been fully scraped
• Additional states can be added by running the state scraper
• Each permit link represents a unique permit document

================================================================================
"""
    
    # Save summary text
    summary_text_path = output_dir / "permit_links_quick_summary.txt"
    with open(summary_text_path, 'w') as f:
        f.write(summary_text)
    
    print(summary_text)
    print(f"Summary saved to: {summary_text_path}")
    
    plt.show()

if __name__ == "__main__":
    quick_analysis() 