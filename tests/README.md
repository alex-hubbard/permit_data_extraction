# Permit Data Extraction Testing Framework

This directory contains a comprehensive testing framework for the permit data extraction system. The framework is designed to ensure data accuracy and detect model hallucination.

## Overview

The testing framework consists of several components:

1. **Data Validation Tests** (`test_data_validation.py`) - Validates extracted data against defined rules
2. **Model Comparison Tests** (`test_model_comparison.py`) - Compares GPT-4 vs Gemini extraction accuracy
3. **Data Generation Tests** (`test_data_generation.py`) - Generates synthetic test data and ground truth
4. **Integration Tests** (`test_integration.py`) - End-to-end testing of the complete pipeline
5. **Test Runner** (`test_runner.py`) - Main test execution and reporting

## Key Features

### 🎯 Accuracy Validation
- Compares extracted data against ground truth
- Calculates field-level accuracy metrics
- Identifies missing or incorrect extractions

### 🚫 Hallucination Detection
- Detects when models generate non-existent data
- Identifies suspicious patterns in extracted values
- Provides confidence scores for hallucination risk

### 📊 Model Comparison
- Compares GPT-4 and Gemini extraction results
- Measures agreement rates between models
- Provides recommendations for model selection

### 🏗️ Synthetic Data Generation
- Generates realistic permit data for testing
- Creates ground truth datasets
- Supports deterministic generation with seeds

### 🔍 Field Validation
- Validates data types, formats, and patterns
- Checks required fields and constraints
- Ensures data quality standards

## Quick Start

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

# Data generation tests
python tests/test_runner.py --test data_generation

# Integration tests
python tests/test_runner.py --test integration
```

### Run with pytest directly
```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_data_validation.py

# Run with verbose output
pytest tests/ -v

# Run specific test class
pytest tests/test_data_validation.py::TestDataValidator
```

## Test Structure

### Data Validation Tests
- **DataValidator**: Validates extracted data against rules
- **AccuracyChecker**: Compares extractions with ground truth
- **HallucinationDetector**: Detects potential hallucinations

### Model Comparison Tests
- **ModelComparisonTester**: Compares different models
- **GPT-4 Tests**: Tests GPT-4 extraction functionality
- **Gemini Tests**: Tests Gemini extraction functionality

### Data Generation Tests
- **PermitDataGenerator**: Generates synthetic permit data
- **GroundTruthGenerator**: Creates ground truth datasets
- **Integration Tests**: End-to-end data generation testing

### Integration Tests
- **IntegrationTestSuite**: Complete test suite
- **End-to-end Testing**: Full pipeline validation
- **Performance Metrics**: Overall system scoring

## Configuration

### Test Configuration (`conftest.py`)
- Shared fixtures for all tests
- Sample data and expected results
- Mock objects for external dependencies
- Validation rules and test cases

### Pytest Configuration (`pytest.ini`)
- Test discovery patterns
- Output formatting
- Warning filters
- Test markers

## Test Data

The framework generates synthetic test data including:

- **Facility Information**: Names, addresses, contact details
- **Permit Information**: Numbers, dates, regulatory authority
- **Emission Units**: Equipment details, pollutants, limits
- **Ground Truth**: Expected extraction results

## Metrics and Scoring

### Accuracy Metrics
- **Exact Matches**: Perfect field matches
- **Partial Matches**: Similar but not identical values
- **Field Coverage**: Percentage of expected fields found
- **Completeness**: Percentage of required fields present

### Hallucination Detection
- **Risk Scores**: Confidence in hallucination detection
- **Suspicious Patterns**: Identified problematic values
- **False Positive Rate**: Incorrect hallucination flags
- **Detection Rate**: Successfully identified hallucinations

### Model Comparison
- **Agreement Rate**: How often models agree
- **Accuracy Comparison**: Relative performance metrics
- **Field-level Analysis**: Detailed comparison by field
- **Recommendations**: Model selection guidance

## Output and Reporting

Test results are saved in multiple formats:

- **JSON Results**: Detailed test results and metrics
- **Summary Reports**: High-level test summaries
- **Recommendations**: Actionable improvement suggestions
- **Console Output**: Real-time test progress and results

## Best Practices

### Running Tests
1. **Start Small**: Begin with a few test permits
2. **Use Seeds**: For reproducible results
3. **Check Results**: Review test outputs regularly
4. **Monitor Performance**: Track accuracy over time

### Interpreting Results
1. **Overall Score**: Aim for >80% for production use
2. **Hallucination Rate**: Keep below 10%
3. **Model Agreement**: >70% agreement is good
4. **Field Coverage**: >90% coverage is ideal

### Improving Accuracy
1. **Review Failures**: Analyze failed test cases
2. **Update Prompts**: Refine extraction prompts
3. **Add Validation**: Strengthen field validation rules
4. **Model Tuning**: Adjust model parameters

## Troubleshooting

### Common Issues
- **Import Errors**: Ensure all dependencies are installed
- **API Failures**: Check API keys and rate limits
- **Memory Issues**: Reduce number of test permits
- **Timeout Errors**: Increase timeout values

### Debug Mode
```bash
# Run with verbose output
python tests/test_runner.py --test all --verbose

# Run specific test with debugging
pytest tests/test_data_validation.py::TestDataValidator::test_validate_required_field_present -v -s
```

## Contributing

When adding new tests:

1. **Follow Naming**: Use `test_` prefix for test functions
2. **Add Fixtures**: Use shared fixtures from `conftest.py`
3. **Document Tests**: Add docstrings explaining test purpose
4. **Update Runner**: Add new tests to test runner if needed

## Dependencies

Required packages for testing:
- `pytest` - Test framework
- `pytest-json-report` - JSON test reporting
- `faker` - Synthetic data generation
- `pandas` - Data manipulation
- `unittest.mock` - Mocking external dependencies

Install with:
```bash
pip install pytest pytest-json-report faker pandas
```
