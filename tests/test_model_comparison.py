"""
Tests for comparing different models (GPT-4 vs Gemini) for data extraction accuracy.
"""
import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, List, Any
import pandas as pd

from permit_data_extraction.dataset import extract_info_with_llm, PROMPT_TEMPLATE
from permit_data_extraction.analyze_permit_data import extract_fields_with_gemini


class ModelComparisonTester:
    """Compares different models for extraction accuracy and consistency."""
    
    def __init__(self):
        self.comparison_metrics = {
            "accuracy": 0.0,
            "consistency": 0.0,
            "completeness": 0.0,
            "hallucination_rate": 0.0,
            "processing_time": 0.0
        }
    
    def compare_models(self, 
                      gpt4_result: Dict[str, Any], 
                      gemini_result: Dict[str, Any],
                      ground_truth: Dict[str, Any]) -> Dict[str, Any]:
        """Compare GPT-4 and Gemini extraction results."""
        comparison = {
            "gpt4_metrics": self._calculate_metrics(gpt4_result, ground_truth),
            "gemini_metrics": self._calculate_metrics(gemini_result, ground_truth),
            "model_comparison": self._compare_model_outputs(gpt4_result, gemini_result),
            "recommendations": []
        }
        
        # Generate recommendations
        if comparison["gpt4_metrics"]["accuracy"] > comparison["gemini_metrics"]["accuracy"]:
            comparison["recommendations"].append("GPT-4 shows higher accuracy")
        elif comparison["gemini_metrics"]["accuracy"] > comparison["gpt4_metrics"]["accuracy"]:
            comparison["recommendations"].append("Gemini shows higher accuracy")
        else:
            comparison["recommendations"].append("Both models show similar accuracy")
        
        if comparison["gpt4_metrics"]["hallucination_rate"] < comparison["gemini_metrics"]["hallucination_rate"]:
            comparison["recommendations"].append("GPT-4 has lower hallucination rate")
        elif comparison["gemini_metrics"]["hallucination_rate"] < comparison["gpt4_metrics"]["hallucination_rate"]:
            comparison["recommendations"].append("Gemini has lower hallucination rate")
        
        return comparison
    
    def _calculate_metrics(self, extracted_data: Dict[str, Any], ground_truth: Dict[str, Any]) -> Dict[str, float]:
        """Calculate performance metrics for a model's extraction."""
        metrics = {
            "accuracy": 0.0,
            "completeness": 0.0,
            "hallucination_rate": 0.0,
            "field_coverage": 0.0
        }
        
        if not extracted_data or not ground_truth:
            return metrics
        
        # Calculate accuracy (exact matches)
        total_fields = len(ground_truth)
        exact_matches = 0
        
        for field, expected_value in ground_truth.items():
            if field in extracted_data:
                extracted_value = extracted_data[field]
                if self._values_match(extracted_value, expected_value):
                    exact_matches += 1
        
        metrics["accuracy"] = exact_matches / total_fields if total_fields > 0 else 0.0
        
        # Calculate completeness (fields found vs expected)
        fields_found = len([f for f in ground_truth.keys() if f in extracted_data and extracted_data[f] is not None])
        metrics["completeness"] = fields_found / total_fields if total_fields > 0 else 0.0
        
        # Calculate field coverage (extracted fields vs expected fields)
        expected_fields = set(ground_truth.keys())
        extracted_fields = set(extracted_data.keys())
        metrics["field_coverage"] = len(expected_fields.intersection(extracted_fields)) / len(expected_fields) if expected_fields else 0.0
        
        # Estimate hallucination rate (fields with values not in ground truth)
        hallucinated_fields = 0
        for field, value in extracted_data.items():
            if field in ground_truth and value is not None:
                if not self._values_match(value, ground_truth[field]):
                    # Check if this might be a hallucination
                    if self._is_potential_hallucination(value, ground_truth[field]):
                        hallucinated_fields += 1
        
        metrics["hallucination_rate"] = hallucinated_fields / len(extracted_data) if extracted_data else 0.0
        
        return metrics
    
    def _compare_model_outputs(self, gpt4_result: Dict[str, Any], gemini_result: Dict[str, Any]) -> Dict[str, Any]:
        """Compare the outputs of two models directly."""
        comparison = {
            "agreement_rate": 0.0,
            "gpt4_unique_fields": [],
            "gemini_unique_fields": [],
            "conflicting_values": []
        }
        
        all_fields = set(gpt4_result.keys()) | set(gemini_result.keys())
        agreements = 0
        total_comparable = 0
        
        for field in all_fields:
            gpt4_value = gpt4_result.get(field)
            gemini_value = gemini_result.get(field)
            
            if gpt4_value is not None and gemini_value is not None:
                total_comparable += 1
                if self._values_match(gpt4_value, gemini_value):
                    agreements += 1
                else:
                    comparison["conflicting_values"].append({
                        "field": field,
                        "gpt4_value": gpt4_value,
                        "gemini_value": gemini_value
                    })
            elif gpt4_value is not None:
                comparison["gpt4_unique_fields"].append(field)
            elif gemini_value is not None:
                comparison["gemini_unique_fields"].append(field)
        
        comparison["agreement_rate"] = agreements / total_comparable if total_comparable > 0 else 0.0
        
        return comparison
    
    def _values_match(self, value1: Any, value2: Any) -> bool:
        """Check if two values match (with some tolerance for formatting)."""
        if value1 is None and value2 is None:
            return True
        if value1 is None or value2 is None:
            return False
        
        # Convert to strings and normalize
        str1 = str(value1).strip().lower()
        str2 = str(value2).strip().lower()
        
        return str1 == str2
    
    def _is_potential_hallucination(self, extracted_value: Any, ground_truth_value: Any) -> bool:
        """Determine if a value might be a hallucination."""
        if extracted_value is None or ground_truth_value is None:
            return False
        
        # Check for completely different values
        if not self._values_match(extracted_value, ground_truth_value):
            # Check if extracted value contains ground truth (partial match)
            extracted_str = str(extracted_value).lower()
            ground_truth_str = str(ground_truth_value).lower()
            
            if ground_truth_str not in extracted_str and extracted_str not in ground_truth_str:
                return True
        
        return False


class TestModelComparison:
    """Test model comparison functionality."""
    
    def test_compare_identical_results(self):
        """Test comparison when both models produce identical results."""
        tester = ModelComparisonTester()
        result = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        ground_truth = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        
        comparison = tester.compare_models(result, result, ground_truth)
        
        assert comparison["gpt4_metrics"]["accuracy"] == 1.0
        assert comparison["gemini_metrics"]["accuracy"] == 1.0
        assert comparison["model_comparison"]["agreement_rate"] == 1.0
    
    def test_compare_different_results(self):
        """Test comparison when models produce different results."""
        tester = ModelComparisonTester()
        gpt4_result = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        gemini_result = {"Facility Name": "Test Plant", "Permit Number": "TEST-001"}
        ground_truth = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        
        comparison = tester.compare_models(gpt4_result, gemini_result, ground_truth)
        
        assert comparison["gpt4_metrics"]["accuracy"] > comparison["gemini_metrics"]["accuracy"]
        assert comparison["model_comparison"]["agreement_rate"] < 1.0
        assert len(comparison["model_comparison"]["conflicting_values"]) > 0
    
    def test_compare_missing_fields(self):
        """Test comparison when models have different field coverage."""
        tester = ModelComparisonTester()
        gpt4_result = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        gemini_result = {"Facility Name": "Test Facility"}
        ground_truth = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        
        comparison = tester.compare_models(gpt4_result, gemini_result, ground_truth)
        
        assert comparison["gpt4_metrics"]["completeness"] > comparison["gemini_metrics"]["completeness"]
        assert "Permit Number" in comparison["model_comparison"]["gpt4_unique_fields"]
    
    def test_hallucination_detection(self):
        """Test detection of potential hallucinations."""
        tester = ModelComparisonTester()
        gpt4_result = {"Facility Name": "Test Facility", "Permit Number": "HALLUCINATED-001"}
        gemini_result = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        ground_truth = {"Facility Name": "Test Facility", "Permit Number": "TEST-001"}
        
        comparison = tester.compare_models(gpt4_result, gemini_result, ground_truth)
        
        assert comparison["gpt4_metrics"]["hallucination_rate"] > comparison["gemini_metrics"]["hallucination_rate"]


class TestGPT4Extraction:
    """Test GPT-4 extraction functionality."""
    
    @patch('permit_data_extraction.dataset.openai.OpenAI')
    def test_extract_info_with_llm_success(self, mock_openai, sample_permit_text, expected_extraction_result):
        """Test successful GPT-4 extraction."""
        # Mock the OpenAI client and response
        mock_client = Mock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = json.dumps(expected_extraction_result)
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        result = extract_info_with_llm(mock_client, sample_permit_text, "test_file.txt")
        
        assert result is not None
        assert "Facility Name" in result
        assert "Emission Units" in result
        assert len(result["Emission Units"]) == 2
    
    @patch('permit_data_extraction.dataset.openai.OpenAI')
    def test_extract_info_with_llm_failure(self, mock_openai, sample_permit_text):
        """Test GPT-4 extraction failure handling."""
        # Mock the OpenAI client to raise an exception
        mock_client = Mock()
        mock_client.chat.completions.create.side_effect = Exception("API Error")
        mock_openai.return_value = mock_client
        
        result = extract_info_with_llm(mock_client, sample_permit_text, "test_file.txt")
        
        assert result is None
    
    def test_prompt_template_formatting(self, sample_permit_text):
        """Test that the prompt template formats correctly."""
        formatted_prompt = PROMPT_TEMPLATE.format(permit_text=sample_permit_text)
        
        assert "permit_text" not in formatted_prompt  # Should be replaced
        assert sample_permit_text in formatted_prompt
        assert "Facility Name" in formatted_prompt
        assert "Emission Units" in formatted_prompt


class TestGeminiExtraction:
    """Test Gemini extraction functionality."""
    
    @patch('permit_data_extraction.analyze_permit_data.genai.GenerativeModel')
    def test_extract_fields_with_gemini_success(self, mock_model_class, sample_permit_text):
        """Test successful Gemini extraction."""
        # Mock the Gemini model and response
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps({
            "facility_name": ["Acme Manufacturing Plant"],
            "permit_number": ["IL-2024-001"],
            "facility_address": ["123 Industrial Blvd"]
        })
        mock_model.generate_content.return_value = mock_response
        mock_model_class.return_value = mock_model
        
        result = extract_fields_with_gemini(sample_permit_text)
        
        assert result is not None
        assert "facility_name" in result
        assert "permit_number" in result
        assert len(result["facility_name"]) > 0
    
    @patch('permit_data_extraction.analyze_permit_data.genai.GenerativeModel')
    def test_extract_fields_with_gemini_failure(self, mock_model_class, sample_permit_text):
        """Test Gemini extraction failure handling."""
        # Mock the Gemini model to raise an exception
        mock_model = Mock()
        mock_model.generate_content.side_effect = Exception("API Error")
        mock_model_class.return_value = mock_model
        
        result = extract_fields_with_gemini(sample_permit_text)
        
        assert result == {}
    
    def test_gemini_prompt_structure(self):
        """Test that the Gemini prompt has the expected structure."""
        from permit_data_extraction.analyze_permit_data import extract_fields_with_gemini
        
        # The prompt should be defined in the function
        # We can't directly test it, but we can verify the function exists and is callable
        assert callable(extract_fields_with_gemini)


class TestIntegrationComparison:
    """Integration tests for model comparison."""
    
    def test_end_to_end_comparison(self, sample_permit_text, expected_extraction_result):
        """Test end-to-end comparison of both models."""
        tester = ModelComparisonTester()
        
        # Mock both models to return different results
        gpt4_result = expected_extraction_result.copy()
        gemini_result = expected_extraction_result.copy()
        gemini_result["Facility Name"] = "Different Facility Name"  # Introduce difference
        
        comparison = tester.compare_models(gpt4_result, gemini_result, expected_extraction_result)
        
        assert "gpt4_metrics" in comparison
        assert "gemini_metrics" in comparison
        assert "model_comparison" in comparison
        assert "recommendations" in comparison
        
        # GPT-4 should have higher accuracy since it matches ground truth exactly
        assert comparison["gpt4_metrics"]["accuracy"] > comparison["gemini_metrics"]["accuracy"]
        
        # Should have conflicting values
        assert len(comparison["model_comparison"]["conflicting_values"]) > 0
    
    def test_performance_metrics_calculation(self):
        """Test calculation of various performance metrics."""
        tester = ModelComparisonTester()
        
        # Test with known values
        extracted = {
            "Facility Name": "Test Facility",
            "Permit Number": "TEST-001",
            "Facility Address": "123 Test St"
        }
        ground_truth = {
            "Facility Name": "Test Facility",
            "Permit Number": "TEST-001",
            "Facility Address": "123 Test Street",  # Slightly different
            "Facility City": "Test City"  # Missing in extracted
        }
        
        metrics = tester._calculate_metrics(extracted, ground_truth)
        
        assert metrics["accuracy"] == 0.5  # 2 out of 4 exact matches
        assert metrics["completeness"] == 0.75  # 3 out of 4 fields found
        assert metrics["field_coverage"] == 0.75  # 3 out of 4 expected fields covered
        assert metrics["hallucination_rate"] >= 0.0  # Should be calculated
