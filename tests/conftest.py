"""
Pytest configuration and shared fixtures for permit data extraction tests.
"""
import pytest
import json
import tempfile
from pathlib import Path
from typing import Dict, List, Any
import pandas as pd
from unittest.mock import Mock, patch

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR


@pytest.fixture
def temp_data_dir():
    """Create a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def sample_permit_text():
    """Sample permit text for testing."""
    return """
    AIR PERMIT APPLICATION
    
    Facility Name: Acme Manufacturing Plant
    Facility Address: 123 Industrial Blvd
    Facility City: Springfield
    Facility State: IL
    Facility Zip Code: 62701
    Facility County: Sangamon
    NAICS Code: 311111
    Operating Hours: 24/7
    Industry Description: Food Manufacturing
    
    Permit Number: IL-2024-001
    Issuance Date: 2024-01-15
    Expiration Date: 2029-01-15
    Regulatory Authority: Illinois EPA
    Primary Applicable Regulations: Title V, 40 CFR 63 Subpart DDDD
    
    EMISSION UNITS:
    
    Unit ID: EU001
    Unit Description: Natural Gas Boiler #1
    Unit Make: Cleaver Brooks
    Unit Model: CB-100
    Year of Manufacture: 2020
    Unit Type: Water Tube Boiler
    Pollutants: NOx, CO, PM
    Emission Limits: NOx: 0.05 lb/MMBtu, CO: 50 ppmvd, PM: 0.01 lb/MMBtu
    Control Device(s): Low NOx Burner
    Capacity Value: 50
    Capacity Unit: MMBtu/hr
    Fuel Type: Natural Gas
    Rated Efficiency: 85%
    
    Unit ID: EU002
    Unit Description: Paint Booth A
    Unit Make: Custom
    Unit Model: PB-200
    Year of Manufacture: 2019
    Unit Type: Paint Booth
    Pollutants: VOC, HAPs
    Emission Limits: VOC: 2.7 tons/year
    Control Device(s): Dry Filters
    Capacity Value: 1000
    Capacity Unit: cfm
    Fuel Type: N/A
    Rated Efficiency: 95%
    """


@pytest.fixture
def expected_extraction_result():
    """Expected extraction result for the sample permit text."""
    return {
        "Facility Name": "Acme Manufacturing Plant",
        "Facility Address": "123 Industrial Blvd",
        "Facility City": "Springfield",
        "Facility State Abbreviation": "IL",
        "Facility Zip Code": "62701",
        "Facility County": "Sangamon",
        "NAICS Code": "311111",
        "Operating Hours": "24/7",
        "Industry Description": "Food Manufacturing",
        "Permit Number": "IL-2024-001",
        "Issuance Date": "2024-01-15",
        "Expiration Date": "2029-01-15",
        "Regulatory Authority": "Illinois EPA",
        "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)": "Title V, 40 CFR 63 Subpart DDDD",
        "Emission Units": [
            {
                "Unit ID": "EU001",
                "Unit Description": "Natural Gas Boiler #1",
                "Unit Make": "Cleaver Brooks",
                "Unit Model": "CB-100",
                "Year of Manufacture": "2020",
                "Unit Type": "Water Tube Boiler",
                "Pollutants": "NOx, CO, PM",
                "Emission Limits": "NOx: 0.05 lb/MMBtu, CO: 50 ppmvd, PM: 0.01 lb/MMBtu",
                "Control Device(s)": "Low NOx Burner",
                "Capacity Value": "50",
                "Capacity Unit": "MMBtu/hr",
                "Fuel Type": "Natural Gas",
                "Rated Efficiency": "85%"
            },
            {
                "Unit ID": "EU002",
                "Unit Description": "Paint Booth A",
                "Unit Make": "Custom",
                "Unit Model": "PB-200",
                "Year of Manufacture": "2019",
                "Unit Type": "Paint Booth",
                "Pollutants": "VOC, HAPs",
                "Emission Limits": "VOC: 2.7 tons/year",
                "Control Device(s)": "Dry Filters",
                "Capacity Value": "1000",
                "Capacity Unit": "cfm",
                "Fuel Type": "N/A",
                "Rated Efficiency": "95%"
            }
        ]
    }


@pytest.fixture
def hallucination_test_cases():
    """Test cases designed to detect hallucination."""
    return [
        {
            "name": "missing_facility_name",
            "text": """
            AIR PERMIT APPLICATION
            
            Facility Address: 123 Industrial Blvd
            Facility City: Springfield
            Permit Number: IL-2024-001
            """,
            "expected": {
                "Facility Name": None,  # Should not hallucinate a name
                "Facility Address": "123 Industrial Blvd",
                "Facility City": "Springfield",
                "Permit Number": "IL-2024-001"
            }
        },
        {
            "name": "missing_emission_units",
            "text": """
            AIR PERMIT APPLICATION
            
            Facility Name: Test Facility
            Permit Number: TEST-001
            """,
            "expected": {
                "Facility Name": "Test Facility",
                "Permit Number": "TEST-001",
                "Emission Units": []  # Should not hallucinate units
            }
        },
        {
            "name": "partial_unit_info",
            "text": """
            AIR PERMIT APPLICATION
            
            Facility Name: Test Facility
            Permit Number: TEST-001
            
            Unit ID: EU001
            Unit Description: Boiler
            """,
            "expected": {
                "Facility Name": "Test Facility",
                "Permit Number": "TEST-001",
                "Emission Units": [
                    {
                        "Unit ID": "EU001",
                        "Unit Description": "Boiler",
                        "Unit Make": None,  # Should not hallucinate
                        "Unit Model": None,  # Should not hallucinate
                        "Pollutants": None,  # Should not hallucinate
                    }
                ]
            }
        }
    ]


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing."""
    mock_client = Mock()
    mock_response = Mock()
    mock_response.choices = [Mock()]
    mock_response.choices[0].message.content = '{"test": "response"}'
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


@pytest.fixture
def mock_gemini_model():
    """Mock Gemini model for testing."""
    mock_model = Mock()
    mock_response = Mock()
    mock_response.text = '{"test": "response"}'
    mock_model.generate_content.return_value = mock_response
    return mock_model


@pytest.fixture
def validation_rules():
    """Validation rules for permit data fields."""
    return {
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
            "min_length": 3,
            "max_length": 50,
            "pattern": r"^[A-Z0-9\-]+$"
        },
        "Facility Address": {
            "required": True,
            "type": str,
            "min_length": 5,
            "max_length": 200
        },
        "Issuance Date": {
            "required": True,
            "type": str,
            "pattern": r"^\d{4}-\d{2}-\d{2}$"
        },
        "Expiration Date": {
            "required": True,
            "type": str,
            "pattern": r"^\d{4}-\d{2}-\d{2}$"
        },
        "NAICS Code": {
            "required": False,
            "type": str,
            "pattern": r"^\d{6}$"
        },
        "Unit ID": {
            "required": True,
            "type": str,
            "min_length": 1,
            "max_length": 20,
            "pattern": r"^[A-Z0-9\-]+$"
        },
        "Capacity Value": {
            "required": False,
            "type": (str, int, float),
            "numeric": True
        },
        "Year of Manufacture": {
            "required": False,
            "type": (str, int),
            "pattern": r"^(19|20)\d{2}$"
        }
    }


@pytest.fixture
def test_permit_files(temp_data_dir):
    """Create test permit files for integration testing."""
    test_files = []
    
    # Create sample text files
    for i in range(3):
        file_path = temp_data_dir / f"test_permit_{i+1}.txt"
        with open(file_path, 'w') as f:
            f.write(f"""
            AIR PERMIT APPLICATION {i+1}
            
            Facility Name: Test Facility {i+1}
            Facility Address: {100+i} Test Street
            Permit Number: TEST-{i+1:03d}
            Issuance Date: 2024-01-{i+1:02d}
            Expiration Date: 2029-01-{i+1:02d}
            """)
        test_files.append(file_path)
    
    return test_files
