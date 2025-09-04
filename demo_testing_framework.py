#!/usr/bin/env python3
"""
Demonstration script for the permit data extraction testing framework.

This script shows how to use the testing framework to validate data extraction
accuracy and detect model hallucination.
"""

import sys
from pathlib import Path
import json

# Add the project root to the path
sys.path.append(str(Path(__file__).parent))

from tests.test_runner import TestRunner
from tests.test_data_generation import PermitDataGenerator, GroundTruthGenerator
from tests.test_data_validation import DataValidator, AccuracyChecker, HallucinationDetector
from tests.test_model_comparison import ModelComparisonTester


def demo_data_generation():
    """Demonstrate synthetic data generation."""
    print("🏗️  DEMO: Synthetic Data Generation")
    print("=" * 50)
    
    # Create a data generator
    generator = PermitDataGenerator(seed=42)
    
    # Generate a complete permit
    permit = generator.generate_complete_permit(num_units=3)
    
    print(f"Generated permit for: {permit['Facility Name']}")
    print(f"Permit Number: {permit['Permit Number']}")
    print(f"Number of emission units: {len(permit['Emission Units'])}")
    
    # Generate human-readable text
    permit_text = generator.generate_permit_text(permit)
    print(f"\nGenerated text length: {len(permit_text)} characters")
    print(f"Text preview: {permit_text[:200]}...")
    
    return permit, permit_text


def demo_data_validation():
    """Demonstrate data validation."""
    print("\n🔍 DEMO: Data Validation")
    print("=" * 50)
    
    # Generate test data
    generator = PermitDataGenerator(seed=42)
    permit = generator.generate_complete_permit(num_units=2)
    
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
    print("\n📊 DEMO: Accuracy Checking")
    print("=" * 50)
    
    # Generate ground truth and "extracted" data
    generator = PermitDataGenerator(seed=42)
    ground_truth = generator.generate_complete_permit(num_units=2)
    
    # Simulate extraction with some errors
    extracted_data = ground_truth.copy()
    extracted_data["Facility Name"] = f"Modified {ground_truth['Facility Name']}"
    extracted_data["Emission Units"][0]["Unit Description"] = "Modified Unit Description"
    
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
    print("\n🚫 DEMO: Hallucination Detection")
    print("=" * 50)
    
    # Generate test data
    generator = PermitDataGenerator(seed=42)
    permit = generator.generate_complete_permit(num_units=1)
    permit_text = generator.generate_permit_text(permit)
    
    # Create a version with hallucinations
    hallucinated_data = permit.copy()
    hallucinated_data["Facility Name"] = "Hallucinated Facility Name"
    hallucinated_data["Permit Number"] = "HALLUCINATED-001"
    hallucinated_data["Emission Units"][0]["Unit Description"] = "Non-existent Unit"
    
    # Detect hallucinations
    detector = HallucinationDetector()
    detection_result = detector.detect_hallucinations(hallucinated_data, permit_text)
    
    print(f"Overall risk level: {detection_result['overall_risk'].upper()}")
    print(f"Suspicious fields found: {len(detection_result['suspicious_fields'])}")
    
    for field_info in detection_result['suspicious_fields']:
        print(f"  - {field_info['field']}: {field_info['value']} (risk: {field_info['risk_score']:.2f})")


def demo_model_comparison():
    """Demonstrate model comparison."""
    print("\n⚖️  DEMO: Model Comparison")
    print("=" * 50)
    
    # Generate test data
    generator = PermitDataGenerator(seed=42)
    ground_truth = generator.generate_complete_permit(num_units=2)
    
    # Simulate different model results
    gpt4_result = ground_truth.copy()
    gemini_result = ground_truth.copy()
    gemini_result["Facility Name"] = f"Gemini {ground_truth['Facility Name']}"
    gemini_result["Emission Units"][0]["Unit Description"] = "Gemini Modified Unit"
    
    # Compare models
    comparison_tester = ModelComparisonTester()
    comparison_result = comparison_tester.compare_models(gpt4_result, gemini_result, ground_truth)
    
    print("Model Performance Comparison:")
    print(f"  GPT-4 Accuracy: {comparison_result['gpt4_metrics']['accuracy']:.2%}")
    print(f"  Gemini Accuracy: {comparison_result['gemini_metrics']['accuracy']:.2%}")
    print(f"  Model Agreement Rate: {comparison_result['model_comparison']['agreement_rate']:.2%}")
    
    print("\nRecommendations:")
    for rec in comparison_result['recommendations']:
        print(f"  - {rec}")


def demo_integration_testing():
    """Demonstrate integration testing."""
    print("\n🔗 DEMO: Integration Testing")
    print("=" * 50)
    
    # Run a small integration test
    from permit_data_extraction.tests.test_integration import IntegrationTestSuite
    
    suite = IntegrationTestSuite()
    results = suite.run_complete_test_suite(num_test_permits=2)
    
    print(f"Integration Test Results:")
    print(f"  Overall Score: {results['overall_score']:.2%}")
    print(f"  Data Generation: {results['data_generation_results']['permits_generated']} permits")
    print(f"  Average Accuracy: {results['extraction_accuracy_results']['average_accuracy']}")
    print(f"  Hallucination Detection Rate: {results['hallucination_detection_results']['overall_detection_rate']:.2%}")
    
    print("\nTest Summary:")
    summary = results['test_summary']
    print(f"  Status: {summary['status']}")
    print(f"  Tests Passed: {summary['tests_passed']}/{summary['total_tests_run']}")
    
    if summary['recommendations']:
        print("  Recommendations:")
        for rec in summary['recommendations']:
            print(f"    - {rec}")


def demo_full_test_suite():
    """Demonstrate running the full test suite."""
    print("\n🚀 DEMO: Full Test Suite")
    print("=" * 50)
    
    # Create test runner
    runner = TestRunner(Path("demo_test_results"))
    
    # Run all tests
    results = runner.run_all_tests(num_test_permits=3, verbose=False)
    
    print("Full test suite completed!")
    print(f"Results saved to: demo_test_results/")


def main():
    """Run all demonstrations."""
    print("🧪 PERMIT DATA EXTRACTION TESTING FRAMEWORK DEMO")
    print("=" * 60)
    print("This demo shows the key features of the testing framework:")
    print("- Synthetic data generation")
    print("- Data validation and accuracy checking")
    print("- Hallucination detection")
    print("- Model comparison")
    print("- Integration testing")
    print("=" * 60)
    
    try:
        # Run demonstrations
        demo_data_generation()
        demo_data_validation()
        demo_accuracy_checking()
        demo_hallucination_detection()
        demo_model_comparison()
        demo_integration_testing()
        
        print("\n" + "=" * 60)
        print("✅ All demonstrations completed successfully!")
        print("=" * 60)
        
        # Ask if user wants to run full test suite
        response = input("\nWould you like to run the full test suite? (y/n): ").lower().strip()
        if response in ['y', 'yes']:
            demo_full_test_suite()
        
    except Exception as e:
        print(f"\n❌ Demo failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
