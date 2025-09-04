#!/usr/bin/env python3
"""
Map showing the number of permit links found in each state
"""

import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import sys
import os

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from permit_data_extraction.config import RAW_DATA_DIR, FIGURES_DIR, EXTERNAL_DATA_DIR

def create_permit_links_map():
    """Create a map showing the number of permit links found in each state."""
    
    # Read the permit links data
    permit_links_path = Path(f"{RAW_DATA_DIR}/state_permit_links/all_permit_links.csv")
    
    if not permit_links_path.exists():
        print(f"Permit links file not found: {permit_links_path}")
        return
    
    # Read the permit links data
    df_permits = pd.read_csv(permit_links_path)
    
    # Count permits by state
    state_counts = df_permits['state_code'].value_counts().reset_index()
    state_counts.columns = ['state_code', 'permit_count']
    
    print(f"Total permit links found: {len(df_permits)}")
    print(f"States with permit data: {len(state_counts)}")
    print("\nPermit counts by state:")
    print(state_counts)
    
    # Load US states shapefile
    states_path = Path(f"{EXTERNAL_DATA_DIR}/us_states/us_states.shp")
    
    if not states_path.exists():
        print(f"States shapefile not found: {states_path}")
        return
    
    # Read the states shapefile
    gdf_states = gpd.read_file(states_path)
    
    # Filter to only include the lower 48 states (exclude Alaska, Hawaii, and territories)
    exclude_states = ['AK', 'HI', 'AS', 'GU', 'MP', 'PR', 'VI']
    gdf_states = gdf_states[~gdf_states['ABBREV'].isin(exclude_states)]
    
    # Merge the permit counts with the states geodataframe
    gdf_states = gdf_states.merge(state_counts, left_on='ABBREV', right_on='state_code', how='left')
    
    # Fill NaN values with 0 for states with no permit data
    gdf_states['permit_count'] = gdf_states['permit_count'].fillna(0).astype(int)
    
    # Create the map
    fig, ax = plt.subplots(1, 1, figsize=(16, 10))
    
    # Set up the color palette
    colors = sns.color_palette("YlOrRd", n_colors=8)
    
    # Plot the states with permit counts
    gdf_states.plot(
        column='permit_count',
        ax=ax,
        legend=True,
        legend_kwds={
            'label': 'Number of Permit Links',
            'orientation': 'vertical',
            'shrink': 0.8,
            'aspect': 30
        },
        missing_kwds={'color': 'lightgray'},
        cmap='YlOrRd',
        edgecolor='black',
        linewidth=0.5
    )
    
    # Add state labels for states with permit data
    for idx, row in gdf_states[gdf_states['permit_count'] > 0].iterrows():
        ax.annotate(
            text=f"{row['ABBREV']}\n{row['permit_count']}",
            xy=row.geometry.centroid.coords[0],
            ha='center',
            va='center',
            fontsize=8,
            fontweight='bold',
            color='black',
            bbox=dict(boxstyle="round,pad=0.2", facecolor='white', alpha=0.8, edgecolor='gray')
        )
    
    # Add title and labels
    total_permits = gdf_states['permit_count'].sum()
    states_with_data = len(gdf_states[gdf_states['permit_count'] > 0])
    
    plt.title(
        f'EPA Permit Links Discovered by State\n'
        f'Total: {total_permits:,} permit links across {states_with_data} states',
        fontsize=16,
        fontweight='bold',
        pad=20
    )
    
    # Remove axes
    ax.set_axis_off()
    
    # Add a note about the data
    plt.figtext(
        0.02, 0.02,
        f'Data Source: EPA Permit Hub\n'
        f'Generated: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}\n'
        f'Note: Only states with permit data are labeled',
        fontsize=10,
        style='italic',
        color='gray'
    )
    
    # Save the map
    output_dir = Path(FIGURES_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / "permit_links_by_state_map.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight', facecolor='white')
    
    print(f"\nMap saved to: {output_path}")
    
    # Also create a summary table
    summary_table = gdf_states[['ABBREV', 'STATE', 'permit_count']].sort_values('permit_count', ascending=False)
    summary_table = summary_table[summary_table['permit_count'] > 0]
    
    print(f"\nSummary of permit links by state:")
    print("=" * 50)
    print(f"{'State':<4} {'State Name':<20} {'Permit Links':<12}")
    print("-" * 50)
    for _, row in summary_table.iterrows():
        print(f"{row['ABBREV']:<4} {row['STATE']:<20} {row['permit_count']:<12}")
    
    # Save summary table to CSV
    summary_csv_path = output_dir / "permit_links_summary.csv"
    summary_table.to_csv(summary_csv_path, index=False)
    print(f"\nSummary table saved to: {summary_csv_path}")
    
    plt.show()
    
    return gdf_states

def create_permit_links_chart():
    """Create a bar chart showing permit counts by state."""
    
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
    
    # Sort by permit count
    state_counts = state_counts.sort_values('permit_count', ascending=False)
    
    # Create the bar chart
    fig, ax = plt.subplots(1, 1, figsize=(14, 8))
    
    # Create the bar plot
    bars = ax.bar(range(len(state_counts)), state_counts['permit_count'], 
                  color=sns.color_palette("YlOrRd", len(state_counts)))
    
    # Add value labels on bars
    for i, (bar, count) in enumerate(zip(bars, state_counts['permit_count'])):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, 
                str(count), ha='center', va='bottom', fontsize=9, fontweight='bold')
    
    # Customize the chart
    ax.set_xlabel('State', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Permit Links', fontsize=12, fontweight='bold')
    ax.set_title('EPA Permit Links Discovered by State', fontsize=16, fontweight='bold', pad=20)
    
    # Set x-axis labels
    ax.set_xticks(range(len(state_counts)))
    ax.set_xticklabels(state_counts['state_code'], rotation=45, ha='right')
    
    # Add grid
    ax.grid(axis='y', alpha=0.3)
    
    # Add summary statistics
    total_permits = state_counts['permit_count'].sum()
    states_with_data = len(state_counts)
    avg_per_state = total_permits / states_with_data if states_with_data > 0 else 0
    
    plt.figtext(
        0.02, 0.02,
        f'Total Permit Links: {total_permits:,}\n'
        f'States with Data: {states_with_data}\n'
        f'Average per State: {avg_per_state:.1f}',
        fontsize=10,
        style='italic',
        color='gray'
    )
    
    # Save the chart
    output_dir = Path(FIGURES_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / "permit_links_by_state_chart.png"
    plt.savefig(output_path, dpi=300, bbox_inches='tight', facecolor='white')
    
    print(f"Chart saved to: {output_path}")
    
    plt.show()

if __name__ == "__main__":
    print("Creating permit links map...")
    gdf_states = create_permit_links_map()
    
    print("\nCreating permit links chart...")
    create_permit_links_chart() 