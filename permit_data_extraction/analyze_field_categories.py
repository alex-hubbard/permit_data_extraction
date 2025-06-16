import pandas as pd
import json
from pathlib import Path
from collections import defaultdict
from loguru import logger
from permit_data_extraction.config import PROCESSED_DATA_DIR

# Define field categories and their associated keywords
FIELD_CATEGORIES = {
    'facility_identification': {
        'keywords': ['facility', 'building', 'site', 'location', 'address', 'name', 'number', 'id', 'business'],
        'description': 'Basic facility identification information'
    },
    'facility_characteristics': {
        'keywords': ['type', 'use', 'size', 'capacity', 'area', 'square', 'sqft', 'dimension', 'height', 'story', 'floor', 'level'],
        'description': 'Physical and operational characteristics of the facility'
    },
    'facility_operations': {
        'keywords': ['hours', 'operation', 'operating', 'schedule', 'time', 'capacity', 'occupancy', 'employee', 'staff', 'worker', 'production'],
        'description': 'Operational details of the facility'
    },
    'contact_information': {
        'keywords': ['contact', 'owner', 'operator', 'applicant', 'representative', 'agent', 'phone', 'email', 'manager', 'supervisor'],
        'description': 'Contact and ownership information'
    },
    'location_details': {
        'keywords': ['zoning', 'parcel', 'lot', 'block', 'district', 'zone', 'property', 'neighborhood', 'street', 'avenue', 'road'],
        'description': 'Location and zoning information'
    },
    # Equipment Categories
    'equipment_description': {
        'keywords': ['equipment', 'device', 'unit', 'system', 'apparatus', 'instrument', 'tool', 'machine', 'appliance', 'type', 'category', 'class', 'model', 'make', 'manufacturer', 'brand', 'name', 'description', 'purpose', 'function', 'use'],
        'description': 'Equipment identification and description'
    },
    'equipment_capacity': {
        'keywords': ['capacity', 'rating', 'size', 'dimension', 'power', 'voltage', 'amperage', 'wattage', 'btu', 'cfm', 'gpm', 'psi', 'rpm', 'horsepower', 'hp', 'ton', 'kw', 'kva', 'output', 'throughput', 'yield', 'production', 'load', 'demand'],
        'description': 'Equipment capacity and performance specifications'
    },
    'equipment_vintage': {
        'keywords': ['year', 'date', 'age', 'vintage', 'manufacture', 'production', 'install', 'installation', 'purchase', 'acquisition', 'original', 'new', 'used', 'secondhand', 'refurbished', 'rebuilt'],
        'description': 'Equipment age and installation information'
    },
    'permit_details': {
        'keywords': ['permit', 'application', 'status', 'type', 'category', 'class', 'number', 'date', 'expiration', 'renewal', 'fee', 'cost'],
        'description': 'Permit-specific information'
    },
    'inspection_details': {
        'keywords': ['inspection', 'inspect', 'check', 'verify', 'approve', 'certify', 'compliance', 'violation', 'citation'],
        'description': 'Inspection-related information'
    },
    'construction_details': {
        'keywords': ['construction', 'build', 'install', 'erect', 'modify', 'alter', 'renovate', 'remodel', 'repair', 'maintenance'],
        'description': 'Construction and installation details'
    },
    'safety_compliance': {
        'keywords': ['safety', 'code', 'standard', 'regulation', 'requirement', 'compliance', 'hazard', 'risk', 'emergency', 'fire', 'alarm', 'sprinkler'],
        'description': 'Safety and compliance information'
    },
    'environmental_details': {
        'keywords': ['environmental', 'emission', 'pollution', 'waste', 'disposal', 'recycling', 'conservation', 'sustainability', 'green', 'eco'],
        'description': 'Environmental impact and compliance'
    },
    'utility_connections': {
        'keywords': ['utility', 'power', 'electric', 'gas', 'water', 'sewer', 'drain', 'vent', 'plumbing', 'electrical', 'connection', 'service'],
        'description': 'Utility connections and services'
    },
    'accessibility_details': {
        'keywords': ['accessibility', 'ada', 'handicap', 'wheelchair', 'ramp', 'elevator', 'lift', 'access', 'entrance', 'exit'],
        'description': 'Accessibility and ADA compliance'
    },
    'security_details': {
        'keywords': ['security', 'access', 'control', 'surveillance', 'camera', 'alarm', 'lock', 'key', 'card', 'badge'],
        'description': 'Security systems and access control'
    },
    'other': {
        'keywords': [],
        'description': 'Uncategorized fields'
    }
}

def categorize_field(field_name: str) -> str:
    """
    Categorize a field name based on keywords.
    
    Args:
        field_name (str): The field name to categorize
        
    Returns:
        str: The category name
    """
    field_name_lower = field_name.lower()
    
    for category, info in FIELD_CATEGORIES.items():
        if any(keyword in field_name_lower for keyword in info['keywords']):
            return category
            
    return 'other'

def analyze_field_categories():
    """
    Analyze field frequencies and organize them into broader categories.
    """
    # Load the field frequency data
    freq_file = PROCESSED_DATA_DIR / 'analysis' / 'field_frequency.csv'
    if not freq_file.exists():
        logger.error(f"Field frequency file not found: {freq_file}")
        return
        
    field_freq_df = pd.read_csv(freq_file)
    
    # Add category information
    field_freq_df['category'] = field_freq_df['field_name'].apply(categorize_field)
    
    # Calculate total frequency for weighting
    total_frequency = field_freq_df['frequency'].sum()
    
    # Create category summary with weighted percentages
    category_summary = field_freq_df.groupby('category').agg({
        'field_name': 'count',
        'frequency': 'sum'
    }).rename(columns={
        'field_name': 'num_fields',
        'frequency': 'total_occurrences'
    })
    
    # Calculate weighted percentage
    category_summary['weighted_percentage'] = (category_summary['total_occurrences'] / total_frequency * 100).round(2)
    
    # Sort by total occurrences
    category_summary = category_summary.sort_values('total_occurrences', ascending=False)
    
    # Add category descriptions
    category_summary['description'] = category_summary.index.map(
        lambda x: FIELD_CATEGORIES[x]['description']
    )
    
    # Create detailed field mapping
    field_mapping = defaultdict(list)
    for _, row in field_freq_df.iterrows():
        field_mapping[row['category']].append({
            'field_name': row['field_name'],
            'frequency': row['frequency'],
            'percentage': row['percentage']
        })
    
    # Save results
    output_dir = PROCESSED_DATA_DIR / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save category summary
    category_summary.to_csv(output_dir / 'field_category_summary.csv')
    
    # Save detailed field mapping
    with open(output_dir / 'field_category_mapping.json', 'w') as f:
        json.dump(field_mapping, f, indent=2)
    
    # Print summary
    print("\nField Category Summary:")
    print("======================")
    print(category_summary.to_string())
    
    # Print top fields in each category
    print("\nTop Fields by Category:")
    print("======================")
    for category in category_summary.index:
        print(f"\n{category.upper()} ({FIELD_CATEGORIES[category]['description']}):")
        category_fields = field_freq_df[field_freq_df['category'] == category].sort_values('frequency', ascending=False)
        print(category_fields[['field_name', 'frequency', 'percentage']].head().to_string(index=False))
    
    return category_summary

if __name__ == "__main__":
    analyze_field_categories()