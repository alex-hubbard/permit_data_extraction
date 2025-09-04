"""
Integration tests for the complete permit data extraction pipeline.
"""
import pytest
import json
import tempfile
from pathlib import Path
from typing import Dict, List, Any
from unittest.mock import Mock, patch, MagicMock

from tests.test_data_generation import PermitDataGenerator, GroundTruthGenerator
from tests.test_data_validation import DataValidator, AccuracyChecker, HallucinationDetector
from tests.test_model_comparison import ModelComparisonTester


class IntegrationTestSuite:
    """Complete integration test suite for permit data extraction."""
    
    def __init__(self):
        self.data_generator = PermitDataGenerator(seed=42)
        self.gt_generator = GroundTruthGenerator(self.data_generator)
        self.validator = DataValidator({})  # Will be configured with rules
        self.accuracy_checker = AccuracyChecker()
        self.hallucination_detector = HallucinationDetector()
        self.model_comparison_tester = ModelComparisonTester()
    
    def run_complete_test_suite(self, num_test_permits: int = 5) -> Dict[str, Any]:
        """Run the complete test suite with generated data."""
        results = {
            "test_summary": {},
            "data_generation_results": {},
            "extraction_accuracy_results": {},
            "hallucination_detection_results": {},
            "model_comparison_results": {},
            "overall_score": 0.0
        }
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # 1. Generate test dataset
            print("Generating test dataset...")
            dataset = self.gt_generator.generate_test_dataset(
                num_permits=num_test_permits, 
                output_dir=temp_path
            )
            results["data_generation_results"] = {
                "permits_generated": len(dataset["permits"]),
                "text_files_created": len(dataset["text_files"]),
                "total_emission_units": sum(
                    len(p.get("Emission Units", [])) for p in dataset["permits"]
                )
            }
            
            # 2. Test extraction accuracy (mock both models)
            print("Testing extraction accuracy...")
            accuracy_results = self._test_extraction_accuracy(dataset)
            results["extraction_accuracy_results"] = accuracy_results
            
            # 3. Test hallucination detection
            print("Testing hallucination detection...")
            hallucination_results = self._test_hallucination_detection(dataset)
            results["hallucination_detection_results"] = hallucination_results
            
            # 4. Test model comparison
            print("Testing model comparison...")
            comparison_results = self._test_model_comparison(dataset)
            results["model_comparison_results"] = comparison_results
            
            # 5. Calculate overall score
            results["overall_score"] = self._calculate_overall_score(results)
            
            # 6. Generate test summary
            results["test_summary"] = self._generate_test_summary(results)
        
        return results
    
    def _test_extraction_accuracy(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        """Test extraction accuracy with mock model responses."""
        accuracy_results = {
            "gpt4_accuracy": [],
            "gemini_accuracy": [],
            "average_accuracy": 0.0,
            "field_accuracy_breakdown": {}
        }
        
        for i, (permit_data, text_file) in enumerate(zip(dataset["permits"], dataset["text_files"])):
            # Mock GPT-4 extraction (perfect accuracy for testing)
            gpt4_result = permit_data.copy()
            
            # Mock Gemini extraction (introduce some errors)
            gemini_result = permit_data.copy()
            if i % 2 == 0:  # Introduce errors in half the cases
                gemini_result["Facility Name"] = f"Modified {permit_data['Facility Name']}"
                if gemini_result.get("Emission Units"):
                    gemini_result["Emission Units"][0]["Unit Description"] = "Modified Unit"
            
            # Calculate accuracy
            gpt4_accuracy = self.accuracy_checker.compare_extractions(
                gpt4_result, permit_data
            )
            gemini_accuracy = self.accuracy_checker.compare_extractions(
                gemini_result, permit_data
            )
            
            accuracy_results["gpt4_accuracy"].append(gpt4_accuracy["overall_accuracy"])
            accuracy_results["gemini_accuracy"].append(gemini_accuracy["overall_accuracy"])
        
        # Calculate averages
        accuracy_results["average_accuracy"] = {
            "gpt4": sum(accuracy_results["gpt4_accuracy"]) / len(accuracy_results["gpt4_accuracy"]),
            "gemini": sum(accuracy_results["gemini_accuracy"]) / len(accuracy_results["gemini_accuracy"])
        }
        
        return accuracy_results
    
    def _test_hallucination_detection(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        """Test hallucination detection capabilities."""
        hallucination_results = {
            "detection_tests": [],
            "overall_detection_rate": 0.0,
            "false_positive_rate": 0.0
        }
        
        for i, (permit_data, text_file) in enumerate(zip(dataset["permits"], dataset["text_files"])):
            # Read the original text
            with open(text_file, 'r') as f:
                original_text = f.read()
            
            # Test 1: Clean extraction (should have low hallucination risk)
            clean_result = permit_data.copy()
            clean_detection = self.hallucination_detector.detect_hallucinations(
                clean_result, original_text
            )
            
            # Test 2: Extraction with hallucinations (should have high risk)
            hallucinated_result = permit_data.copy()
            hallucinated_result["Facility Name"] = "Hallucinated Facility Name"
            hallucinated_result["Permit Number"] = "HALLUCINATED-001"
            
            hallucinated_detection = self.hallucination_detector.detect_hallucinations(
                hallucinated_result, original_text
            )
            
            hallucination_results["detection_tests"].append({
                "permit_index": i,
                "clean_risk": clean_detection["overall_risk"],
                "hallucinated_risk": hallucinated_detection["overall_risk"],
                "clean_suspicious_fields": len(clean_detection["suspicious_fields"]),
                "hallucinated_suspicious_fields": len(hallucinated_detection["suspicious_fields"])
            })
        
        # Calculate detection rates
        clean_low_risk = sum(1 for test in hallucination_results["detection_tests"] 
                           if test["clean_risk"] == "low")
        hallucinated_high_risk = sum(1 for test in hallucination_results["detection_tests"] 
                                   if test["hallucinated_risk"] in ["high", "medium"])
        
        hallucination_results["overall_detection_rate"] = hallucinated_high_risk / len(hallucination_results["detection_tests"])
        hallucination_results["false_positive_rate"] = 1 - (clean_low_risk / len(hallucination_results["detection_tests"]))
        
        return hallucination_results
    
    def _test_model_comparison(self, dataset: Dict[str, Any]) -> Dict[str, Any]:
        """Test model comparison functionality."""
        comparison_results = {
            "model_comparisons": [],
            "average_agreement_rate": 0.0,
            "recommendations": []
        }
        
        for i, permit_data in enumerate(dataset["permits"]):
            # Mock different model results
            gpt4_result = permit_data.copy()
            gemini_result = permit_data.copy()
            
            # Introduce some differences
            if i % 3 == 0:
                gemini_result["Facility Name"] = f"Gemini {permit_data['Facility Name']}"
            if i % 3 == 1:
                gemini_result["Permit Number"] = f"GEMINI-{permit_data['Permit Number']}"
            if i % 3 == 2 and gemini_result.get("Emission Units"):
                gemini_result["Emission Units"][0]["Unit Description"] = "Gemini Modified Unit"
            
            # Compare models
            comparison = self.model_comparison_tester.compare_models(
                gpt4_result, gemini_result, permit_data
            )
            
            comparison_results["model_comparisons"].append({
                "permit_index": i,
                "gpt4_accuracy": comparison["gpt4_metrics"]["accuracy"],
                "gemini_accuracy": comparison["gemini_metrics"]["accuracy"],
                "agreement_rate": comparison["model_comparison"]["agreement_rate"],
                "recommendations": comparison["recommendations"]
            })
        
        # Calculate average agreement rate
        agreement_rates = [comp["agreement_rate"] for comp in comparison_results["model_comparisons"]]
        comparison_results["average_agreement_rate"] = sum(agreement_rates) / len(agreement_rates)
        
        # Collect recommendations
        all_recommendations = []
        for comp in comparison_results["model_comparisons"]:
            all_recommendations.extend(comp["recommendations"])
        comparison_results["recommendations"] = list(set(all_recommendations))
        
        return comparison_results
    
    def _calculate_overall_score(self, results: Dict[str, Any]) -> float:
        """Calculate overall test suite score."""
        scores = []
        
        # Data generation score (should be 100% if successful)
        if results["data_generation_results"]["permits_generated"] > 0:
            scores.append(1.0)
        
        # Accuracy score
        accuracy_results = results["extraction_accuracy_results"]
        if accuracy_results["average_accuracy"]:
            avg_accuracy = (
                accuracy_results["average_accuracy"]["gpt4"] + 
                accuracy_results["average_accuracy"]["gemini"]
            ) / 2
            scores.append(avg_accuracy)
        
        # Hallucination detection score
        hallucination_results = results["hallucination_detection_results"]
        detection_score = (
            hallucination_results["overall_detection_rate"] + 
            (1 - hallucination_results["false_positive_rate"])
        ) / 2
        scores.append(detection_score)
        
        # Model comparison score
        comparison_results = results["model_comparison_results"]
        if comparison_results["average_agreement_rate"] > 0:
            scores.append(comparison_results["average_agreement_rate"])
        
        return sum(scores) / len(scores) if scores else 0.0
    
    def _generate_test_summary(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a summary of test results."""
        return {
            "total_tests_run": 4,
            "tests_passed": sum(1 for key in ["data_generation_results", "extraction_accuracy_results", 
                                            "hallucination_detection_results", "model_comparison_results"] 
                              if results.get(key)),
            "overall_score": results["overall_score"],
            "recommendations": self._generate_recommendations(results)
        }
    
    def _generate_recommendations(self, results: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on test results."""
        recommendations = []
        
        # Check accuracy
        accuracy_results = results.get("extraction_accuracy_results", {})
        if accuracy_results.get("average_accuracy"):
            avg_accuracy = (
                accuracy_results["average_accuracy"]["gpt4"] + 
                accuracy_results["average_accuracy"]["gemini"]
            ) / 2
            if avg_accuracy < 0.8:
                recommendations.append("Consider improving extraction accuracy - current average is below 80%")
        
        # Check hallucination detection
        hallucination_results = results.get("hallucination_detection_results", {})
        if hallucination_results.get("overall_detection_rate", 0) < 0.7:
            recommendations.append("Improve hallucination detection - detection rate is below 70%")
        
        if hallucination_results.get("false_positive_rate", 0) > 0.3:
            recommendations.append("Reduce false positive rate in hallucination detection")
        
        # Check model comparison
        comparison_results = results.get("model_comparison_results", {})
        if comparison_results.get("average_agreement_rate", 0) < 0.6:
            recommendations.append("Models show low agreement - consider standardizing extraction approach")
        
        if not recommendations:
            recommendations.append("All tests passed successfully - system is performing well")
        
        return recommendations


class TestIntegrationSuite:
    """Test the integration test suite."""
    
    def test_complete_test_suite(self):
        """Test the complete integration test suite."""
        suite = IntegrationTestSuite()
        results = suite.run_complete_test_suite(num_test_permits=3)
        
        # Check that all components are tested
        assert "test_summary" in results
        assert "data_generation_results" in results
        assert "extraction_accuracy_results" in results
        assert "hallucination_detection_results" in results
        assert "model_comparison_results" in results
        assert "overall_score" in results
        
        # Check that results are reasonable
        assert results["overall_score"] >= 0.0
        assert results["overall_score"] <= 1.0
        assert results["data_generation_results"]["permits_generated"] == 3
    
    def test_data_generation_integration(self):
        """Test data generation integration."""
        suite = IntegrationTestSuite()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            dataset = suite.gt_generator.generate_test_dataset(
                num_permits=2, 
                output_dir=temp_path
            )
            
            # Check that all files are created
            assert len(dataset["permits"]) == 2
            assert len(dataset["text_files"]) == 2
            assert (temp_path / "ground_truth.json").exists()
            assert (temp_path / "ground_truth.csv").exists()
    
    def test_accuracy_testing_integration(self):
        """Test accuracy testing integration."""
        suite = IntegrationTestSuite()
        
        # Generate test data
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            dataset = suite.gt_generator.generate_test_dataset(
                num_permits=2, 
                output_dir=temp_path
            )
            
            # Test accuracy
            accuracy_results = suite._test_extraction_accuracy(dataset)
            
            assert "gpt4_accuracy" in accuracy_results
            assert "gemini_accuracy" in accuracy_results
            assert "average_accuracy" in accuracy_results
            assert len(accuracy_results["gpt4_accuracy"]) == 2
    
    def test_hallucination_detection_integration(self):
        """Test hallucination detection integration."""
        suite = IntegrationTestSuite()
        
        # Generate test data
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            dataset = suite.gt_generator.generate_test_dataset(
                num_permits=2, 
                output_dir=temp_path
            )
            
            # Test hallucination detection
            hallucination_results = suite._test_hallucination_detection(dataset)
            
            assert "detection_tests" in hallucination_results
            assert "overall_detection_rate" in hallucination_results
            assert "false_positive_rate" in hallucination_results
            assert len(hallucination_results["detection_tests"]) == 2
    
    def test_model_comparison_integration(self):
        """Test model comparison integration."""
        suite = IntegrationTestSuite()
        
        # Generate test data
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            dataset = suite.gt_generator.generate_test_dataset(
                num_permits=2, 
                output_dir=temp_path
            )
            
            # Test model comparison
            comparison_results = suite._test_model_comparison(dataset)
            
            assert "model_comparisons" in comparison_results
            assert "average_agreement_rate" in comparison_results
            assert "recommendations" in comparison_results
            assert len(comparison_results["model_comparisons"]) == 2
    
    def test_overall_score_calculation(self):
        """Test overall score calculation."""
        suite = IntegrationTestSuite()
        
        # Mock results
        mock_results = {
            "data_generation_results": {"permits_generated": 3},
            "extraction_accuracy_results": {
                "average_accuracy": {"gpt4": 0.9, "gemini": 0.8}
            },
            "hallucination_detection_results": {
                "overall_detection_rate": 0.8,
                "false_positive_rate": 0.2
            },
            "model_comparison_results": {
                "average_agreement_rate": 0.7
            }
        }
        
        score = suite._calculate_overall_score(mock_results)
        assert score > 0.0
        assert score <= 1.0
    
    def test_recommendation_generation(self):
        """Test recommendation generation."""
        suite = IntegrationTestSuite()
        
        # Test with good results
        good_results = {
            "extraction_accuracy_results": {
                "average_accuracy": {"gpt4": 0.9, "gemini": 0.9}
            },
            "hallucination_detection_results": {
                "overall_detection_rate": 0.9,
                "false_positive_rate": 0.1
            },
            "model_comparison_results": {
                "average_agreement_rate": 0.8
            }
        }
        
        recommendations = suite._generate_recommendations(good_results)
        assert len(recommendations) > 0
        assert "All tests passed successfully" in recommendations[0]
        
        # Test with poor results
        poor_results = {
            "extraction_accuracy_results": {
                "average_accuracy": {"gpt4": 0.5, "gemini": 0.5}
            },
            "hallucination_detection_results": {
                "overall_detection_rate": 0.3,
                "false_positive_rate": 0.5
            },
            "model_comparison_results": {
                "average_agreement_rate": 0.3
            }
        }
        
        recommendations = suite._generate_recommendations(poor_results)
        assert len(recommendations) > 1  # Should have multiple recommendations
