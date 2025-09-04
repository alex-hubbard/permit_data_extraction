#!/usr/bin/env python3
"""
Simple visualization of permit links by state
"""

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import sys
import os

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from permit_data_extraction.config import EXTERNAL_DATA_DIR, REPORTS_FIGURES_DIR

def analyze_permit_links():
    """Analyze and visualize permit links by state."""
    
    # Read the permit links data
    permit_links_path = Path(f"{EXTERNAL_DATA_DIR}/state_permit_links/all_permit_links.csv")
    
    if not permit_links_path.exists():
        print(f"Permit links file not found: {permit_links_path}")
        return
    
    # Read the permit links data
    df_permits = pd.read_csv(permit_links_path)
    
    print(f"Total permit links found: {len(df_permits)}")
    print(f"Unique states: {df_permits['state_code'].nunique()}")
    
    # Count permits by state
    state_counts = df_permits['state_code'].value_counts().reset_index()
    state_counts.columns = ['state_code', 'permit_count']
    
    print(f"\nPermit counts by state:")
    print(state_counts)
    
    # Create a bar chart
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 12))
    
    # Sort by permit count for better visualization
    state_counts_sorted = state_counts.sort_values('permit_count', ascending=False)
    
    # Create the bar plot
    bars = ax1.bar(range(len(state_counts_sorted)), state_counts_sorted['permit_count'], 
                   color=sns.color_palette("YlOrRd", len(state_counts_sorted)))
    
    # Add value labels on bars
    for i, (bar, count) in enumerate(zip(bars, state_counts_sorted['permit_count'])):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, 
                str(count), ha='center', va='bottom', fontsize=9, fontweight='bold')
    
    # Customize the first chart
    ax1.set_xlabel('State', fontsize=12, fontweight='bold')
    ax1.set_ylabel('Number of Permit Links', fontsize=12, fontweight='bold')
    ax1.set_title('EPA Permit Links Discovered by State', fontsize=16, fontweight='bold', pad=20)
    
    # Set x-axis labels
    ax1.set_xticks(range(len(state_counts_sorted)))
    ax1.set_xticklabels(state_counts_sorted['state_code'], rotation=45, ha='right')
    
    # Add grid
    ax1.grid(axis='y', alpha=0.3)
    
    # Create a pie chart for top states
    top_states = state_counts_sorted.head(10)
    other_count = state_counts_sorted.iloc[10:]['permit_count'].sum() if len(state_counts_sorted) > 10 else 0
    
    if other_count > 0:
        pie_data = pd.concat([top_states, pd.DataFrame({
            'state_code': ['Other States'],
            'permit_count': [other_count]
        })])
    else:
        pie_data = top_states
    
    # Create pie chart
    wedges, texts, autotexts = ax2.pie(
        pie_data['permit_count'], 
        labels=pie_data['state_code'],
        autopct='%1.1f%%',
        startangle=90,
        colors=sns.color_palette("YlOrRd", len(pie_data))
    )
    
    # Customize pie chart
    ax2.set_title('Distribution of Permit Links (Top 10 States)', fontsize=16, fontweight='bold', pad=20)
    
    # Add summary statistics
    total_permits = state_counts['permit_count'].sum()
    states_with_data = len(state_counts)
    avg_per_state = total_permits / states_with_data if states_with_data > 0 else 0
    
    plt.figtext(
        0.02, 0.02,
        f'Total Permit Links: {total_permits:,}\n'
        f'States with Data: {states_with_data}\n'
        f'Average per State: {avg_per_state:.1f}\n'
        f'Data Source: EPA Permit Hub\n'
        f'Generated: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=10,
        style='italic',
        color='gray'
    )
    
    plt.tight_layout()
    
    # Save the visualization
    output_dir = Path(REPORTS_FIGURES_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / "permit_links_analysis.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight', facecolor='white')
    
    print(f"\nVisualization saved to: {output_path}")
    
    # Save summary table to CSV
    summary_csv_path = output_dir / "permit_links_summary.csv"
    state_counts_sorted.to_csv(summary_csv_path, index=False)
    print(f"Summary table saved to: {summary_csv_path}")
    
    # Print summary
    print(f"\nSummary of permit links by state:")
    print("=" * 50)
    print(f"{'State':<4} {'Permit Links':<12} {'Percentage':<10}")
    print("-" * 50)
    for _, row in state_counts_sorted.iterrows():
        percentage = (row['permit_count'] / total_permits) * 100
        print(f"{row['state_code']:<4} {row['permit_count']:<12} {percentage:>6.1f}%")
    
    plt.show()
    
    return state_counts_sorted

def create_text_summary():
    """Create a text summary of the permit links data."""
    
    # Read the permit links data
    permit_links_path = Path(f"{EXTERNAL_DATA_DIR}/state_permit_links/all_permit_links.csv")
    
    if not permit_links_path.exists():
        print(f"Permit links file not found: {permit_links_path}")
        return
    
    # Read the permit links data
    df_permits = pd.read_csv(permit_links_path)
    
    # Count permits by state
    state_counts = df_permits['state_code'].value_counts().reset_index()
    state_counts.columns = ['state_code', 'permit_count']
    state_counts = state_counts.sort_values('permit_count', ascending=False)
    
    # Create summary text
    total_permits = len(df_permits)
    states_with_data = len(state_counts)
    avg_per_state = total_permits / states_with_data if states_with_data > 0 else 0
    
    summary_text = f"""
================================================================================
                    EPA PERMIT LINKS DISCOVERY SUMMARY
================================================================================

OVERALL STATISTICS:
• Total Permit Links Found: {total_permits:,}
• States with Permit Data: {states_with_data}
• Average Links per State: {avg_per_state:.1f}

TOP 10 STATES BY PERMIT LINKS:
"""
    
    for i, (_, row) in enumerate(state_counts.head(10).iterrows(), 1):
        percentage = (row['permit_count'] / total_permits) * 100
        summary_text += f"{i:2d}. {row['state_code']:<4} - {row['permit_count']:>4} links ({percentage:>5.1f}%)\n"
    
    summary_text += f"""
PERMIT LINK DISTRIBUTION:
• States with 100+ links: {len(state_counts[state_counts['permit_count'] >= 100])}
• States with 50-99 links: {len(state_counts[(state_counts['permit_count'] >= 50) & (state_counts['permit_count'] < 100)])}
• States with 10-49 links: {len(state_counts[(state_counts['permit_count'] >= 10) & (state_counts['permit_count'] < 50)])}
• States with 1-9 links: {len(state_counts[(state_counts['permit_count'] >= 1) & (state_counts['permit_count'] < 10)])}

DATA QUALITY:
• Unique Permit IDs: {df_permits['permit_id'].nunique()}
• Duplicate URLs: {len(df_permits) - df_permits['url'].nunique()}
• Pages Scraped: {df_permits['page_number'].max() if 'page_number' in df_permits.columns else 'N/A'}

================================================================================
"""
    
    # Save summary text
    output_dir = Path(REPORTS_FIGURES_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    summary_text_path = output_dir / "permit_links_summary.txt"
    with open(summary_text_path, 'w') as f:
        f.write(summary_text)
    
    print(summary_text)
    print(f"Summary text saved to: {summary_text_path}")

if __name__ == "__main__":
    print("Analyzing permit links data...")
    state_counts = analyze_permit_links()
    
    print("\nCreating text summary...")
    create_text_summary() 