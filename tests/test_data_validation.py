"""
Tests for data validation and accuracy checking in permit data extraction.
"""
import pytest
import json
from typing import Dict, List, Any
from tests.conftest import validation_rules


class DataValidator:
    """Validates extracted permit data against defined rules."""
    
    def __init__(self, rules: Dict[str, Dict]):
        self.rules = rules
    
    def validate_field(self, field_name: str, value: Any) -> Dict[str, Any]:
        """Validate a single field against its rules."""
        if field_name not in self.rules:
            return {"valid": True, "message": "No validation rules defined"}
        
        rule = self.rules[field_name]
        errors = []
        
        # Check if required field is present
        if rule.get("required", False) and (value is None or value == ""):
            errors.append(f"Required field '{field_name}' is missing or empty")
            return {"valid": False, "errors": errors}
        
        # Skip further validation if field is empty and not required
        if value is None or value == "":
            return {"valid": True, "message": "Field is empty but not required"}
        
        # Type validation
        expected_type = rule.get("type")
        if expected_type and not isinstance(value, expected_type):
            errors.append(f"Field '{field_name}' should be of type {expected_type}, got {type(value)}")
        
        # String length validation
        if isinstance(value, str):
            min_length = rule.get("min_length")
            max_length = rule.get("max_length")
            if min_length and len(value) < min_length:
                errors.append(f"Field '{field_name}' is too short (min: {min_length})")
            if max_length and len(value) > max_length:
                errors.append(f"Field '{field_name}' is too long (max: {max_length})")
            
            # Pattern validation
            pattern = rule.get("pattern")
            if pattern:
                import re
                if not re.match(pattern, value):
                    errors.append(f"Field '{field_name}' does not match expected pattern")
        
        # Numeric validation
        if rule.get("numeric", False):
            try:
                float(value)
            except (ValueError, TypeError):
                errors.append(f"Field '{field_name}' should be numeric")
        
        return {"valid": len(errors) == 0, "errors": errors}
    
    def validate_extraction(self, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate entire extraction result."""
        validation_results = {}
        overall_valid = True
        
        # Validate general fields
        for field_name, value in extracted_data.items():
            if field_name != "Emission Units":
                result = self.validate_field(field_name, value)
                validation_results[field_name] = result
                if not result["valid"]:
                    overall_valid = False
        
        # Validate emission units
        emission_units = extracted_data.get("Emission Units", [])
        if emission_units:
            unit_validation_results = []
            for i, unit in enumerate(emission_units):
                unit_result = {}
                for field_name, value in unit.items():
                    result = self.validate_field(field_name, value)
                    unit_result[field_name] = result
                    if not result["valid"]:
                        overall_valid = False
                unit_validation_results.append(unit_result)
            validation_results["Emission Units"] = unit_validation_results
        
        return {
            "overall_valid": overall_valid,
            "field_validations": validation_results
        }


class AccuracyChecker:
    """Checks accuracy of extracted data against ground truth."""
    
    def __init__(self):
        self.tolerance = 0.1  # 10% tolerance for numeric comparisons
    
    def compare_extractions(self, extracted: Dict, ground_truth: Dict) -> Dict[str, Any]:
        """Compare extracted data with ground truth."""
        comparison_results = {
            "exact_matches": 0,
            "partial_matches": 0,
            "missing_fields": 0,
            "extra_fields": 0,
            "field_comparisons": {},
            "overall_accuracy": 0.0
        }
        
        all_fields = set(extracted.keys()) | set(ground_truth.keys())
        total_fields = len(all_fields)
        
        for field in all_fields:
            extracted_value = extracted.get(field)
            ground_truth_value = ground_truth.get(field)
            
            if field == "Emission Units":
                # Special handling for emission units
                unit_comparison = self._compare_emission_units(
                    extracted_value, ground_truth_value
                )
                comparison_results["field_comparisons"][field] = unit_comparison
            else:
                # Regular field comparison
                field_comparison = self._compare_field_values(
                    field, extracted_value, ground_truth_value
                )
                comparison_results["field_comparisons"][field] = field_comparison
                
                if field_comparison["match_type"] == "exact":
                    comparison_results["exact_matches"] += 1
                elif field_comparison["match_type"] == "partial":
                    comparison_results["partial_matches"] += 1
                elif field_comparison["match_type"] == "missing":
                    comparison_results["missing_fields"] += 1
                elif field_comparison["match_type"] == "extra":
                    comparison_results["extra_fields"] += 1
        
        # Calculate overall accuracy
        if total_fields > 0:
            comparison_results["overall_accuracy"] = (
                comparison_results["exact_matches"] + 
                comparison_results["partial_matches"] * 0.5
            ) / total_fields
        
        return comparison_results
    
    def _compare_field_values(self, field_name: str, extracted: Any, ground_truth: Any) -> Dict:
        """Compare individual field values."""
        if extracted is None and ground_truth is None:
            return {"match_type": "exact", "similarity": 1.0}
        elif extracted is None:
            return {"match_type": "missing", "similarity": 0.0}
        elif ground_truth is None:
            return {"match_type": "extra", "similarity": 0.0}
        
        # Convert to strings for comparison
        extracted_str = str(extracted).strip().lower()
        ground_truth_str = str(ground_truth).strip().lower()
        
        if extracted_str == ground_truth_str:
            return {"match_type": "exact", "similarity": 1.0}
        
        # Check for partial matches (substring or similar)
        similarity = self._calculate_similarity(extracted_str, ground_truth_str)
        if similarity > 0.8:
            return {"match_type": "partial", "similarity": similarity}
        else:
            return {"match_type": "different", "similarity": similarity}
    
    def _compare_emission_units(self, extracted_units: List, ground_truth_units: List) -> Dict:
        """Compare emission units lists."""
        if not extracted_units and not ground_truth_units:
            return {"match_type": "exact", "similarity": 1.0, "unit_comparisons": []}
        
        extracted_units = extracted_units or []
        ground_truth_units = ground_truth_units or []
        
        unit_comparisons = []
        max_units = max(len(extracted_units), len(ground_truth_units))
        
        for i in range(max_units):
            extracted_unit = extracted_units[i] if i < len(extracted_units) else None
            ground_truth_unit = ground_truth_units[i] if i < len(ground_truth_units) else None
            
            if extracted_unit and ground_truth_unit:
                unit_comparison = {}
                for field in set(extracted_unit.keys()) | set(ground_truth_unit.keys()):
                    field_comp = self._compare_field_values(
                        field, 
                        extracted_unit.get(field), 
                        ground_truth_unit.get(field)
                    )
                    unit_comparison[field] = field_comp
                unit_comparisons.append(unit_comparison)
            elif extracted_unit:
                unit_comparisons.append({"match_type": "extra", "similarity": 0.0})
            else:
                unit_comparisons.append({"match_type": "missing", "similarity": 0.0})
        
        # Calculate overall similarity for units
        if unit_comparisons:
            avg_similarity = sum(comp.get("similarity", 0) for comp in unit_comparisons) / len(unit_comparisons)
        else:
            avg_similarity = 0.0
        
        return {
            "match_type": "partial" if avg_similarity > 0.5 else "different",
            "similarity": avg_similarity,
            "unit_comparisons": unit_comparisons
        }
    
    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity between two strings."""
        if not str1 or not str2:
            return 0.0
        
        # Simple Jaccard similarity
        set1 = set(str1.split())
        set2 = set(str2.split())
        
        if not set1 and not set2:
            return 1.0
        if not set1 or not set2:
            return 0.0
        
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        
        return intersection / union if union > 0 else 0.0


class HallucinationDetector:
    """Detects potential hallucinations in extracted data."""
    
    def __init__(self):
        self.suspicious_patterns = [
            r"^(test|example|sample|demo|placeholder)",  # Test-like values
            r"^(lorem|ipsum)",  # Lorem ipsum text
            r"^(xxx|xxx|zzz)",  # Placeholder patterns
            r"^(n/a|na|none|null|undefined)",  # Generic null values
        ]
    
    def detect_hallucinations(self, extracted_data: Dict[str, Any], original_text: str) -> Dict[str, Any]:
        """Detect potential hallucinations in extracted data."""
        hallucinations = {
            "suspicious_fields": [],
            "confidence_scores": {},
            "overall_risk": "low"
        }
        
        # Check for suspicious patterns
        for field_name, value in extracted_data.items():
            if isinstance(value, str) and value.strip():
                risk_score = self._assess_field_risk(field_name, value, original_text)
                hallucinations["confidence_scores"][field_name] = risk_score
                
                if risk_score > 0.7:  # High risk threshold
                    hallucinations["suspicious_fields"].append({
                        "field": field_name,
                        "value": value,
                        "risk_score": risk_score,
                        "reason": "High risk of hallucination"
                    })
        
        # Determine overall risk
        if hallucinations["suspicious_fields"]:
            avg_risk = sum(f["risk_score"] for f in hallucinations["suspicious_fields"]) / len(hallucinations["suspicious_fields"])
            if avg_risk > 0.8:
                hallucinations["overall_risk"] = "high"
            elif avg_risk > 0.5:
                hallucinations["overall_risk"] = "medium"
        
        return hallucinations
    
    def _assess_field_risk(self, field_name: str, value: str, original_text: str) -> float:
        """Assess the risk of hallucination for a specific field."""
        risk_score = 0.0
        
        # Check if value appears in original text
        if value.lower() not in original_text.lower():
            risk_score += 0.3
        
        # Check for suspicious patterns
        import re
        for pattern in self.suspicious_patterns:
            if re.search(pattern, value.lower()):
                risk_score += 0.4
        
        # Check for generic values
        generic_values = ["unknown", "not specified", "tbd", "to be determined"]
        if value.lower() in generic_values:
            risk_score += 0.2
        
        # Check for unrealistic values
        if self._is_unrealistic_value(field_name, value):
            risk_score += 0.3
        
        return min(risk_score, 1.0)
    
    def _is_unrealistic_value(self, field_name: str, value: str) -> bool:
        """Check if a value seems unrealistic for its field type."""
        # Date validation
        if "date" in field_name.lower():
            import re
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                return True
        
        # Year validation
        if "year" in field_name.lower():
            try:
                year = int(value)
                if year < 1900 or year > 2030:
                    return True
            except ValueError:
                return True
        
        # Numeric validation for capacity
        if "capacity" in field_name.lower():
            try:
                num = float(value)
                if num <= 0 or num > 10000:  # Unrealistic capacity values
                    return True
            except ValueError:
                pass
        
        return False


# Test classes
class TestDataValidator:
    """Test the DataValidator class."""
    
    def test_validate_required_field_present(self, validation_rules):
        """Test validation of required fields that are present."""
        validator = DataValidator(validation_rules)
        result = validator.validate_field("Facility Name", "Test Facility")
        assert result["valid"] is True
        assert "errors" not in result or len(result["errors"]) == 0
    
    def test_validate_required_field_missing(self, validation_rules):
        """Test validation of required fields that are missing."""
        validator = DataValidator(validation_rules)
        result = validator.validate_field("Facility Name", None)
        assert result["valid"] is False
        assert "Required field 'Facility Name' is missing or empty" in result["errors"]
    
    def test_validate_field_pattern(self, validation_rules):
        """Test validation of field patterns."""
        validator = DataValidator(validation_rules)
        
        # Valid permit number
        result = validator.validate_field("Permit Number", "IL-2024-001")
        assert result["valid"] is True
        
        # Invalid permit number
        result = validator.validate_field("Permit Number", "invalid permit!")
        assert result["valid"] is False
    
    def test_validate_numeric_field(self, validation_rules):
        """Test validation of numeric fields."""
        validator = DataValidator(validation_rules)
        
        # Valid numeric value
        result = validator.validate_field("Capacity Value", "50")
        assert result["valid"] is True
        
        # Invalid numeric value
        result = validator.validate_field("Capacity Value", "not a number")
        assert result["valid"] is False
    
    def test_validate_extraction_complete(self, validation_rules, expected_extraction_result):
        """Test validation of complete extraction result."""
        validator = DataValidator(validation_rules)
        result = validator.validate_extraction(expected_extraction_result)
        assert result["overall_valid"] is True


class TestAccuracyChecker:
    """Test the AccuracyChecker class."""
    
    def test_exact_match_comparison(self, expected_extraction_result):
        """Test comparison with exact matches."""
        checker = AccuracyChecker()
        result = checker.compare_extractions(
            expected_extraction_result, 
            expected_extraction_result
        )
        assert result["overall_accuracy"] == 1.0
        assert result["exact_matches"] > 0
    
    def test_partial_match_comparison(self):
        """Test comparison with partial matches."""
        checker = AccuracyChecker()
        extracted = {"Facility Name": "Acme Manufacturing Plant"}
        ground_truth = {"Facility Name": "Acme Manufacturing"}
        
        result = checker.compare_extractions(extracted, ground_truth)
        assert result["overall_accuracy"] > 0.5
        assert result["partial_matches"] > 0
    
    def test_missing_field_comparison(self):
        """Test comparison with missing fields."""
        checker = AccuracyChecker()
        extracted = {"Facility Name": "Test Facility"}
        ground_truth = {
            "Facility Name": "Test Facility",
            "Permit Number": "TEST-001"
        }
        
        result = checker.compare_extractions(extracted, ground_truth)
        assert result["missing_fields"] > 0
        assert result["overall_accuracy"] < 1.0
    
    def test_emission_units_comparison(self):
        """Test comparison of emission units."""
        checker = AccuracyChecker()
        extracted = {
            "Emission Units": [
                {"Unit ID": "EU001", "Unit Description": "Boiler"}
            ]
        }
        ground_truth = {
            "Emission Units": [
                {"Unit ID": "EU001", "Unit Description": "Natural Gas Boiler"}
            ]
        }
        
        result = checker.compare_extractions(extracted, ground_truth)
        assert "Emission Units" in result["field_comparisons"]
        unit_comparison = result["field_comparisons"]["Emission Units"]
        assert unit_comparison["similarity"] > 0.0


class TestHallucinationDetector:
    """Test the HallucinationDetector class."""
    
    def test_detect_suspicious_patterns(self, sample_permit_text):
        """Test detection of suspicious patterns."""
        detector = HallucinationDetector()
        extracted_data = {
            "Facility Name": "Test Facility",  # Suspicious pattern
            "Permit Number": "TEST-001"  # Suspicious pattern
        }
        
        result = detector.detect_hallucinations(extracted_data, sample_permit_text)
        assert len(result["suspicious_fields"]) > 0
        assert result["overall_risk"] in ["low", "medium", "high"]
    
    def test_detect_missing_from_text(self, sample_permit_text):
        """Test detection of values not present in original text."""
        detector = HallucinationDetector()
        extracted_data = {
            "Facility Name": "Non-existent Facility",  # Not in original text
            "Permit Number": "IL-2024-001"  # Present in original text
        }
        
        result = detector.detect_hallucinations(extracted_data, sample_permit_text)
        facility_risk = result["confidence_scores"]["Facility Name"]
        permit_risk = result["confidence_scores"]["Permit Number"]
        
        assert facility_risk > permit_risk  # Higher risk for non-existent value
    
    def test_detect_unrealistic_values(self):
        """Test detection of unrealistic values."""
        detector = HallucinationDetector()
        extracted_data = {
            "Issuance Date": "invalid-date",
            "Year of Manufacture": "1800",  # Unrealistic year
            "Capacity Value": "999999"  # Unrealistic capacity
        }
        
        result = detector.detect_hallucinations(extracted_data, "Some text")
        assert len(result["suspicious_fields"]) > 0
