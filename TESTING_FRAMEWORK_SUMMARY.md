# Permit Data Extraction Testing Framework

## Overview

I've built a comprehensive testing framework for your permit data extraction system that addresses your key concerns about data accuracy and model hallucination. The framework provides robust validation, accuracy checking, and hallucination detection capabilities.

## 🎯 Key Features

### 1. **Accuracy Validation**
- **Field-level accuracy metrics** comparing extracted data against ground truth
- **Exact match detection** for perfect extractions
- **Partial match scoring** for similar but not identical values
- **Completeness checking** to ensure all required fields are extracted

### 2. **Hallucination Detection**
- **Pattern-based detection** identifying suspicious values not present in source text
- **Risk scoring** with confidence levels for each extracted field
- **False positive reduction** to minimize incorrect hallucination flags
- **Unrealistic value detection** for impossible dates, years, or capacity values

### 3. **Model Comparison**
- **GPT-4 vs Gemini comparison** to evaluate which model performs better
- **Agreement rate analysis** showing how often models produce similar results
- **Performance metrics** including accuracy, completeness, and hallucination rates
- **Recommendations** for model selection and improvement

### 4. **Synthetic Data Generation**
- **Realistic permit data** generation for comprehensive testing
- **Ground truth datasets** with known correct values
- **Deterministic generation** using seeds for reproducible results
- **Multiple permit types** covering various industrial facilities

### 5. **Field Validation**
- **Data type validation** ensuring correct field types
- **Format validation** checking patterns like dates, permit numbers
- **Required field checking** ensuring critical data is present
- **Range validation** for numeric values and string lengths

## 📁 Framework Structure

```
tests/
├── __init__.py                    # Package initialization
├── conftest.py                    # Shared fixtures and configuration
├── test_data_validation.py        # Data validation and accuracy tests
├── test_model_comparison.py       # Model comparison tests
├── test_data_generation.py        # Synthetic data generation tests
├── test_integration.py            # End-to-end integration tests
├── test_runner.py                 # Main test execution framework
└── README.md                      # Detailed documentation

pytest.ini                         # Pytest configuration
requirements-test.txt              # Testing dependencies
demo_testing_framework.py          # Demonstration script
```

## 🚀 Quick Start

### Install Dependencies
```bash
pip install -r requirements-test.txt
```

### Run All Tests
```bash
python tests/test_runner.py --test all --num-permits 10
```

### Run Specific Test Categories
```bash
# Data validation tests
python tests/test_runner.py --test data_validation

# Model comparison tests  
python tests/test_runner.py --test model_comparison

# Integration tests
python tests/test_runner.py --test integration
```

### Run Demo
```bash
python demo_testing_framework.py
```

## 📊 Testing Components

### 1. DataValidator Class
- Validates extracted data against defined rules
- Checks data types, formats, and patterns
- Ensures required fields are present
- Provides detailed error reporting

### 2. AccuracyChecker Class
- Compares extracted data with ground truth
- Calculates field-level accuracy metrics
- Handles emission units comparison
- Provides similarity scoring

### 3. HallucinationDetector Class
- Detects values not present in source text
- Identifies suspicious patterns
- Provides risk confidence scores
- Reduces false positives

### 4. ModelComparisonTester Class
- Compares GPT-4 and Gemini results
- Measures model agreement rates
- Provides performance recommendations
- Analyzes conflicting extractions

### 5. PermitDataGenerator Class
- Generates realistic synthetic permit data
- Creates ground truth datasets
- Supports deterministic generation
- Covers various permit types

## 🎯 Accuracy Metrics

### Field-Level Accuracy
- **Exact Matches**: Perfect field matches (100% accuracy)
- **Partial Matches**: Similar values (50% accuracy)
- **Missing Fields**: Fields not extracted (0% accuracy)
- **Extra Fields**: Fields not in ground truth (0% accuracy)

### Overall Scoring
- **Accuracy Score**: Percentage of correctly extracted fields
- **Completeness Score**: Percentage of required fields found
- **Hallucination Rate**: Percentage of potentially hallucinated fields
- **Model Agreement**: How often models produce similar results

## 🚫 Hallucination Detection

### Detection Methods
1. **Text Presence Check**: Values not found in original text
2. **Pattern Analysis**: Suspicious patterns like "test", "example"
3. **Unrealistic Values**: Impossible dates, years, or capacities
4. **Generic Values**: "unknown", "not specified", "tbd"

### Risk Scoring
- **Low Risk (0-0.3)**: Likely accurate extraction
- **Medium Risk (0.3-0.7)**: Potentially problematic
- **High Risk (0.7-1.0)**: Likely hallucination

## ⚖️ Model Comparison

### Comparison Metrics
- **Accuracy Comparison**: Which model extracts more accurately
- **Agreement Rate**: How often models produce similar results
- **Field Coverage**: Which model finds more fields
- **Hallucination Rate**: Which model hallucinates less

### Recommendations
- Model selection guidance based on performance
- Suggestions for prompt improvement
- Recommendations for hybrid approaches

## 🔧 Configuration

### Validation Rules
```python
validation_rules = {
    "Facility Name": {
        "required": True,
        "type": str,
        "min_length": 1,
        "max_length": 200,
        "pattern": r"^[a-zA-Z0-9\s\-\.&,()]+$"
    },
    "Permit Number": {
        "required": True,
        "type": str,
        "pattern": r"^[A-Z0-9\-]+$"
    }
}
```

### Test Configuration
- **Number of test permits**: Configurable for different test sizes
- **Seed values**: For reproducible results
- **Output directories**: Customizable result storage
- **Verbose mode**: Detailed test output

## 📈 Usage Examples

### Basic Accuracy Testing
```python
from tests.test_data_validation import AccuracyChecker

checker = AccuracyChecker()
result = checker.compare_extractions(extracted_data, ground_truth)
print(f"Accuracy: {result['overall_accuracy']:.2%}")
```

### Hallucination Detection
```python
from tests.test_data_validation import HallucinationDetector

detector = HallucinationDetector()
result = detector.detect_hallucinations(extracted_data, original_text)
print(f"Risk level: {result['overall_risk']}")
```

### Model Comparison
```python
from tests.test_model_comparison import ModelComparisonTester

tester = ModelComparisonTester()
result = tester.compare_models(gpt4_result, gemini_result, ground_truth)
print(f"GPT-4 accuracy: {result['gpt4_metrics']['accuracy']:.2%}")
```

## 🎯 Best Practices

### For Accuracy
1. **Use ground truth datasets** for validation
2. **Test with diverse permit types** to ensure robustness
3. **Monitor field-level accuracy** to identify problem areas
4. **Regular testing** to catch regressions

### For Hallucination Detection
1. **Set appropriate risk thresholds** for your use case
2. **Review suspicious fields** manually to validate detection
3. **Update patterns** based on new hallucination types
4. **Balance sensitivity** to avoid too many false positives

### For Model Comparison
1. **Test with sufficient data** for statistical significance
2. **Compare on multiple metrics** not just accuracy
3. **Consider use case requirements** when selecting models
4. **Monitor performance over time** as models evolve

## 📊 Expected Results

### Good Performance Indicators
- **Overall accuracy > 80%**: Good extraction performance
- **Hallucination rate < 10%**: Low hallucination risk
- **Model agreement > 70%**: Consistent model behavior
- **Field coverage > 90%**: Comprehensive data extraction

### Warning Signs
- **Accuracy < 60%**: Poor extraction performance
- **Hallucination rate > 20%**: High hallucination risk
- **Model agreement < 50%**: Inconsistent model behavior
- **Field coverage < 70%**: Missing important data

## 🔄 Continuous Improvement

### Regular Testing
- Run tests after model updates
- Test with new permit types
- Monitor performance trends
- Update validation rules as needed

### Framework Evolution
- Add new validation rules for new fields
- Improve hallucination detection patterns
- Enhance model comparison metrics
- Expand synthetic data generation

## 📝 Next Steps

1. **Install the framework** and run initial tests
2. **Generate test datasets** with your specific permit types
3. **Establish baseline metrics** for your current system
4. **Set up regular testing** in your development workflow
5. **Monitor and improve** based on test results

The testing framework provides a solid foundation for ensuring data extraction accuracy and detecting model hallucination. It's designed to be extensible and can be adapted to your specific needs and permit types.
