#!/usr/bin/env python3
"""
Simple demonstration of the permit data extraction testing framework.

This script shows the core testing capabilities without requiring
the full permit data extraction dependencies.
"""

import sys
from pathlib import Path
import json

# Add the project root to the path
sys.path.append(str(Path(__file__).parent))

from tests.test_data_validation import DataValidator, AccuracyChecker, HallucinationDetector


def demo_extraction_accuracy():
    """Demonstrate extraction accuracy testing with sample data."""
    print("DEMO: Extraction Accuracy Testing")
    print("=" * 50)
    
    # Create sample permit data (ground truth)
    ground_truth = {
        "Facility Name": "Acme Manufacturing Plant",
        "Facility Address": "123 Industrial Blvd",
        "Facility City": "Springfield",
        "Facility State Abbreviation": "IL",
        "Permit Number": "IL-2024-001",
        "Issuance Date": "2024-01-15",
        "Emission Units": [
            {
                "Unit ID": "EU001",
                "Unit Description": "Natural Gas Boiler #1",
                "Unit Make": "Cleaver Brooks",
                "Pollutants": "NOx, CO, PM",
                "Capacity Value": "50",
                "Capacity Unit": "MMBtu/hr"
            }
        ]
    }
    
    # Simulate model extraction with some errors
    extracted_data = {
        "Facility Name": "Acme Manufacturing Plant",  # Perfect match
        "Facility Address": "123 Industrial Boulevard",  # Slight variation
        "Facility City": "Springfield",  # Perfect match
        "Facility State Abbreviation": "IL",  # Perfect match
        "Permit Number": "IL-2024-001",  # Perfect match
        "Issuance Date": "2024-01-15",  # Perfect match
        "Emission Units": [
            {
                "Unit ID": "EU001",  # Perfect match
                "Unit Description": "Natural Gas Boiler",  # Missing "#1"
                "Unit Make": "Cleaver Brooks",  # Perfect match
                "Pollutants": "NOx, CO, PM",  # Perfect match
                "Capacity Value": "50",  # Perfect match
                "Capacity Unit": "MMBtu/hr"  # Perfect match
            }
        ]
    }
    
    print(f"Ground Truth Facility: {ground_truth['Facility Name']}")
    print(f"Extracted Facility: {extracted_data['Facility Name']}")
    print(f"Ground Truth Address: {ground_truth['Facility Address']}")
    print(f"Extracted Address: {extracted_data['Facility Address']}")
    print(f"Number of emission units: {len(ground_truth['Emission Units'])}")
    
    return ground_truth, extracted_data


def demo_data_validation():
    """Demonstrate data validation."""
    print("\nDEMO: Data Validation")
    print("=" * 50)
    
    # Use sample permit data
    permit = {
        "Facility Name": "Acme Manufacturing Plant",
        "Facility Address": "123 Industrial Blvd",
        "Facility City": "Springfield",
        "Facility State Abbreviation": "IL",
        "Permit Number": "IL-2024-001",
        "Issuance Date": "2024-01-15"
    }
    
    # Create validator with some basic rules
    validation_rules = {
        "Facility Name": {
            "required": True,
            "type": str,
            "min_length": 1,
            "max_length": 200
        },
        "Permit Number": {
            "required": True,
            "type": str,
            "pattern": r"^[A-Z0-9\-]+$"
        },
        "Issuance Date": {
            "required": True,
            "type": str,
            "pattern": r"^\d{4}-\d{2}-\d{2}$"
        }
    }
    
    validator = DataValidator(validation_rules)
    
    # Validate the permit
    validation_result = validator.validate_extraction(permit)
    
    print(f"Overall validation result: {'✅ PASS' if validation_result['overall_valid'] else '❌ FAIL'}")
    
    # Show field-level validation
    for field, result in validation_result['field_validations'].items():
        if field != "Emission Units":
            status = "✅" if result['valid'] else "❌"
            print(f"  {field}: {status}")
            if not result['valid'] and 'errors' in result:
                for error in result['errors']:
                    print(f"    - {error}")


def demo_accuracy_checking():
    """Demonstrate accuracy checking."""
    print("\nDEMO: Accuracy Checking")
    print("=" * 50)
    
    # Use the sample data from extraction accuracy demo
    ground_truth, extracted_data = demo_extraction_accuracy()
    
    # Check accuracy
    checker = AccuracyChecker()
    accuracy_result = checker.compare_extractions(extracted_data, ground_truth)
    
    print(f"Overall accuracy: {accuracy_result['overall_accuracy']:.2%}")
    print(f"Exact matches: {accuracy_result['exact_matches']}")
    print(f"Partial matches: {accuracy_result['partial_matches']}")
    print(f"Missing fields: {accuracy_result['missing_fields']}")
    print(f"Extra fields: {accuracy_result['extra_fields']}")


def demo_hallucination_detection():
    """Demonstrate hallucination detection."""
    print("\nDEMO: Hallucination Detection")
    print("=" * 50)
    
    # Use sample permit data
    permit = {
        "Facility Name": "Acme Manufacturing Plant",
        "Facility Address": "123 Industrial Blvd",
        "Permit Number": "IL-2024-001"
    }
    
    # Create sample permit text
    permit_text = f"""
    AIR PERMIT APPLICATION
    
    Facility Name: {permit['Facility Name']}
    Facility Address: {permit['Facility Address']}
    Permit Number: {permit['Permit Number']}
    """
    
    # Create a version with hallucinations
    hallucinated_data = permit.copy()
    hallucinated_data["Facility Name"] = "Hallucinated Facility Name"
    hallucinated_data["Permit Number"] = "HALLUCINATED-001"
    
    # Detect hallucinations
    detector = HallucinationDetector()
    detection_result = detector.detect_hallucinations(hallucinated_data, permit_text)
    
    print(f"Overall risk level: {detection_result['overall_risk'].upper()}")
    print(f"Suspicious fields found: {len(detection_result['suspicious_fields'])}")
    
    for field_info in detection_result['suspicious_fields']:
        print(f"  - {field_info['field']}: {field_info['value']} (risk: {field_info['risk_score']:.2f})")


def demo_extraction_testing():
    """Demonstrate extraction testing capabilities."""
    print("\nDEMO: Extraction Testing Capabilities")
    print("=" * 50)
    
    # Show what the framework can test
    print("The testing framework can validate:")
    print("  - Data extraction accuracy")
    print("  - Field-level validation")
    print("  - Hallucination detection")
    print("  - Model comparison")
    print("  - Error detection and reporting")
    
    print("\nSample test scenarios:")
    print("  - Perfect extraction: 100% accuracy")
    print("  - Partial extraction: 90% accuracy (some fields missing/modified)")
    print("  - Poor extraction: <70% accuracy (many errors)")
    print("  - Hallucination: Model generates non-existent data")
    
    print("\nValidation rules tested:")
    print("  - Required fields present")
    print("  - Data types correct")
    print("  - Format patterns valid")
    print("  - Field lengths appropriate")


def main():
    """Run all demonstrations."""
    print("PERMIT DATA EXTRACTION TESTING FRAMEWORK DEMO")
    print("=" * 60)
    print("This demo shows the key features of the testing framework:")
    print("- Extraction accuracy testing")
    print("- Data validation and field checking")
    print("- Hallucination detection")
    print("- Model performance evaluation")
    print("=" * 60)
    
    try:
        # Run demonstrations
        demo_extraction_accuracy()
        demo_data_validation()
        demo_accuracy_checking()
        demo_hallucination_detection()
        demo_extraction_testing()
        
        print("\n" + "=" * 60)
        print("All demonstrations completed successfully!")
        print("=" * 60)
        
        print("\nNext Steps:")
        print("1. Install the full permit data extraction dependencies")
        print("2. Run the complete test suite: python tests/test_runner.py --test all")
        print("3. Integrate testing into your development workflow")
        
    except Exception as e:
        print(f"\nDemo failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
