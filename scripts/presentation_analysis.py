#!/usr/bin/env python3
"""
Presentation Analysis Script
Generate comprehensive statistics and metrics for the POC presentation
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from collections import Counter
import sys

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent))
from permit_data_extraction.config import PROCESSED_DATA_DIR

# File paths
EXCEL_FILE = PROCESSED_DATA_DIR / 'permit_data_extracted.xlsx'
FIELD_CATEGORY_FILE = PROCESSED_DATA_DIR / 'analysis' / 'field_category_summary.csv'
OUTPUT_DIR = Path(__file__).parent.parent / 'presentation'
OUTPUT_DIR.mkdir(exist_ok=True)

def load_data():
    """Load the extracted permit data."""
    print("Loading extracted permit data...")
    df = pd.read_excel(EXCEL_FILE)
    print(f"  Loaded {len(df)} records")
    return df

def load_field_categories():
    """Load field category analysis."""
    print("Loading field category analysis...")
    df = pd.read_csv(FIELD_CATEGORY_FILE)
    print(f"  Loaded {len(df)} field categories")
    return df

def calculate_performance_metrics(df):
    """Calculate overall performance metrics."""
    print("\nCalculating performance metrics...")
    
    # Basic counts
    total_records = len(df)
    unique_permits = df['Filename'].nunique()
    
    # Status breakdown
    status_counts = df['Status'].value_counts()
    successful = status_counts.get('Success', 0)
    failed = status_counts.get('LLM Extraction Failed', 0)
    no_units = status_counts.get('Success (No Units Found)', 0)
    
    success_rate = (successful / total_records * 100) if total_records > 0 else 0
    
    # Estimate processing time (conservative estimate: 30 seconds per permit average)
    avg_processing_time = 30  # seconds
    total_processing_time_minutes = (unique_permits * avg_processing_time) / 60
    
    # Estimate API costs (GPT-4: ~$0.03 per 1K input tokens, ~$0.06 per 1K output tokens)
    # Average permit: ~10K input tokens, ~2K output tokens
    avg_input_tokens = 10000
    avg_output_tokens = 2000
    cost_per_permit = (avg_input_tokens / 1000 * 0.03) + (avg_output_tokens / 1000 * 0.06)
    total_api_cost = unique_permits * cost_per_permit
    
    # Manual extraction time estimate (2-4 hours per permit)
    manual_hours_per_permit = 3
    manual_total_hours = unique_permits * manual_hours_per_permit
    time_saved_hours = manual_total_hours - (total_processing_time_minutes / 60)
    
    metrics = {
        'total_records': int(total_records),
        'unique_permits': int(unique_permits),
        'successful_extractions': int(successful),
        'failed_extractions': int(failed),
        'no_units_found': int(no_units),
        'success_rate_percent': round(success_rate, 2),
        'avg_processing_time_seconds': avg_processing_time,
        'total_processing_time_minutes': round(total_processing_time_minutes, 2),
        'cost_per_permit_usd': round(cost_per_permit, 3),
        'total_api_cost_usd': round(total_api_cost, 2),
        'manual_hours_per_permit': manual_hours_per_permit,
        'manual_total_hours': manual_total_hours,
        'automated_total_hours': round(total_processing_time_minutes / 60, 2),
        'time_saved_hours': round(time_saved_hours, 2),
        'time_saved_percent': round((time_saved_hours / manual_total_hours * 100), 2)
    }
    
    print(f"  Total records: {metrics['total_records']}")
    print(f"  Unique permits: {metrics['unique_permits']}")
    print(f"  Success rate: {metrics['success_rate_percent']}%")
    
    return metrics

def calculate_quality_metrics(df):
    """Calculate data quality metrics."""
    print("\nCalculating quality metrics...")
    
    # Field completeness
    field_columns = [col for col in df.columns if col not in ['Filename', 'Status']]
    
    field_completeness = {}
    for field in field_columns:
        non_null = df[field].notna().sum()
        non_empty = df[field].apply(lambda x: x != '' if isinstance(x, str) else True).sum()
        completeness = (non_empty / len(df) * 100) if len(df) > 0 else 0
        field_completeness[field] = round(completeness, 2)
    
    # Sort by completeness
    field_completeness = dict(sorted(field_completeness.items(), 
                                    key=lambda x: x[1], 
                                    reverse=True))
    
    # Calculate average completeness by field category
    general_fields = [
        "Facility Name", "Facility Address", "Facility City", 
        "Facility State Abbreviation", "Facility Zip Code", "Facility County",
        "NAICS Code", "Operating Hours", "Industry Description",
        "Permit Number", "Issuance Date", "Expiration Date",
        "Regulatory Authority", "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)"
    ]
    
    unit_fields = [
        "Unit ID", "Unit Description", "Unit Make", "Unit Model",
        "Year of Manufacture", "Unit Type", "Pollutants", "Emission Limits",
        "Control Device(s)", "Capacity Value", "Capacity Unit", "Fuel Type",
        "Rated Efficiency"
    ]
    
    general_completeness = np.mean([field_completeness.get(f, 0) for f in general_fields if f in field_completeness])
    unit_completeness = np.mean([field_completeness.get(f, 0) for f in unit_fields if f in field_completeness])
    
    metrics = {
        'field_completeness': field_completeness,
        'avg_general_completeness_percent': round(general_completeness, 2),
        'avg_unit_completeness_percent': round(unit_completeness, 2),
        'overall_completeness_percent': round(np.mean(list(field_completeness.values())), 2)
    }
    
    print(f"  General field completeness: {metrics['avg_general_completeness_percent']}%")
    print(f"  Unit field completeness: {metrics['avg_unit_completeness_percent']}%")
    
    return metrics

def calculate_coverage_metrics(df):
    """Calculate coverage metrics."""
    print("\nCalculating coverage metrics...")
    
    # State distribution
    state_counts = df['Facility State Abbreviation'].value_counts()
    
    # Facility distribution
    facility_counts = df['Facility Name'].value_counts()
    
    # Equipment types
    unit_types = df['Unit Type'].value_counts()
    
    # Pollutants (need to parse comma-separated values)
    all_pollutants = []
    for pollutant_str in df['Pollutants'].dropna():
        if isinstance(pollutant_str, str):
            pollutants = [p.strip() for p in pollutant_str.split(',')]
            all_pollutants.extend(pollutants)
    pollutant_counts = Counter(all_pollutants)
    
    # Fuel types
    fuel_counts = df['Fuel Type'].value_counts()
    
    # Control devices
    control_device_counts = df['Control Device(s)'].value_counts()
    
    metrics = {
        'num_states': len(state_counts),
        'state_distribution': state_counts.head(10).to_dict(),
        'num_unique_facilities': len(facility_counts),
        'top_facilities': facility_counts.head(10).to_dict(),
        'num_unit_types': len(unit_types),
        'top_unit_types': unit_types.head(10).to_dict(),
        'num_unique_pollutants': len(pollutant_counts),
        'top_pollutants': dict(pollutant_counts.most_common(10)),
        'num_fuel_types': len(fuel_counts),
        'top_fuel_types': fuel_counts.head(10).to_dict(),
        'num_control_devices': len(control_device_counts),
        'top_control_devices': control_device_counts.head(10).to_dict()
    }
    
    print(f"  States covered: {metrics['num_states']}")
    print(f"  Unique facilities: {metrics['num_unique_facilities']}")
    print(f"  Unique pollutants: {metrics['num_unique_pollutants']}")
    
    return metrics

def get_sample_extractions(df):
    """Get sample extraction examples."""
    print("\nGetting sample extractions...")
    
    # Get a successful extraction with complete data
    successful_df = df[df['Status'] == 'Success']
    
    # Find records with high field completeness
    field_columns = [col for col in df.columns if col not in ['Filename', 'Status']]
    successful_df['completeness'] = successful_df[field_columns].notna().sum(axis=1)
    
    # Get top 5 most complete records
    top_complete = successful_df.nlargest(5, 'completeness')
    
    samples = []
    for idx, row in top_complete.iterrows():
        sample = {
            'filename': row['Filename'],
            'facility_name': row['Facility Name'],
            'permit_number': row['Permit Number'],
            'unit_id': row['Unit ID'],
            'unit_description': row['Unit Description'],
            'pollutants': row['Pollutants'],
            'completeness_score': int(row['completeness'])
        }
        samples.append(sample)
    
    print(f"  Found {len(samples)} sample extractions")
    
    return samples

def generate_visualization_data(df, field_categories):
    """Generate data exports for visualizations."""
    print("\nGenerating visualization data...")
    
    viz_data = {
        'status_breakdown': df['Status'].value_counts().to_dict(),
        'field_categories': field_categories.to_dict('records'),
        'state_distribution': df['Facility State Abbreviation'].value_counts().to_dict(),
        'facility_distribution': df['Facility Name'].value_counts().head(20).to_dict(),
    }
    
    print(f"  Generated visualization data")
    
    return viz_data

def main():
    """Main analysis function."""
    print("=" * 60)
    print("PERMIT DATA EXTRACTION - PRESENTATION ANALYSIS")
    print("=" * 60)
    
    # Load data
    df = load_data()
    field_categories = load_field_categories()
    
    # Calculate metrics
    performance_metrics = calculate_performance_metrics(df)
    quality_metrics = calculate_quality_metrics(df)
    coverage_metrics = calculate_coverage_metrics(df)
    sample_extractions = get_sample_extractions(df)
    viz_data = generate_visualization_data(df, field_categories)
    
    # Combine all metrics
    all_metrics = {
        'performance': performance_metrics,
        'quality': quality_metrics,
        'coverage': coverage_metrics,
        'samples': sample_extractions,
        'visualization_data': viz_data
    }
    
    # Save to JSON
    output_file = OUTPUT_DIR / 'presentation_metrics.json'
    with open(output_file, 'w') as f:
        json.dump(all_metrics, f, indent=2)
    
    print(f"\n{'=' * 60}")
    print(f"Analysis complete! Results saved to:")
    print(f"  {output_file}")
    print(f"{'=' * 60}")
    
    # Print summary
    print("\nKEY METRICS SUMMARY:")
    print(f"  Permits Processed: {performance_metrics['unique_permits']}")
    print(f"  Emission Units Extracted: {performance_metrics['total_records']}")
    print(f"  Success Rate: {performance_metrics['success_rate_percent']}%")
    print(f"  States Covered: {coverage_metrics['num_states']}")
    print(f"  Time Saved: {performance_metrics['time_saved_hours']} hours ({performance_metrics['time_saved_percent']}%)")
    print(f"  Total API Cost: ${performance_metrics['total_api_cost_usd']}")
    print(f"  Overall Field Completeness: {quality_metrics['overall_completeness_percent']}%")
    
    return all_metrics

if __name__ == "__main__":
    metrics = main()

