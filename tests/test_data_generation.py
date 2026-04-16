"""
Test data generation utilities for creating synthetic permit data and ground truth datasets.
"""
import pytest
import json
import random
from typing import Dict, List, Any, Optional
from pathlib import Path
import pandas as pd
from faker import Faker

from permit_data_extraction.dataset import GENERAL_TARGET_FIELDS, UNIT_DETAIL_FIELDS


class PermitDataGenerator:
    """Generates synthetic permit data for testing purposes."""
    
    def __init__(self, seed: Optional[int] = None):
        self.fake = Faker()
        if seed:
            Faker.seed(seed)
            random.seed(seed)
    
    def generate_facility_info(self) -> Dict[str, str]:
        """Generate synthetic facility information."""
        return {
            "Facility Name": f"{self.fake.company()} {random.choice(['Plant', 'Facility', 'Manufacturing', 'Industrial Center'])}",
            "Owner/Operator Name": f"{self.fake.company()} LLC",
            "Facility Address": self.fake.street_address(),
            "Facility City": self.fake.city(),
            "Facility State Abbreviation": self.fake.state_abbr(),
            "Facility Zip Code": self.fake.zipcode(),
            "Facility County": f"{self.fake.last_name()} County",
            "NAICS Code": f"{random.randint(100000, 999999)}",
            "SIC Code": f"{random.randint(2000, 3999)}",
            "Operating Hours": random.choice(["24/7", "8:00 AM - 5:00 PM", "6:00 AM - 10:00 PM", "Monday-Friday 8:00 AM - 5:00 PM"]),
            "Industry Description": random.choice([
                "Food Manufacturing", "Chemical Manufacturing", "Metal Fabrication",
                "Plastics Manufacturing", "Textile Manufacturing", "Paper Manufacturing"
            ])
        }
    
    def generate_permit_info(self) -> Dict[str, str]:
        """Generate synthetic permit information."""
        state = self.fake.state_abbr()
        year = random.randint(2020, 2024)
        permit_num = f"{state}-{year}-{random.randint(100, 999):03d}"
        
        from datetime import date
        
        return {
            "Permit Number": permit_num,
            "Permit Type": random.choice(["Title V", "State Only Operating Permit", "Synthetic Minor", "PSD"]),
            "Issuance Date": self.fake.date_between(start_date=date(year, 1, 1), end_date=date(year, 12, 31)).strftime("%Y-%m-%d"),
            "Expiration Date": self.fake.date_between(start_date=date(year+5, 1, 1), end_date=date(year+5, 12, 31)).strftime("%Y-%m-%d"),
            "Regulatory Authority": f"{state} EPA",
            "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)": random.choice([
                "Title V, 40 CFR 63 Subpart DDDD",
                "PSD, 40 CFR 52",
                "NESHAP Subpart MMMM",
                "Title V, 40 CFR 63 Subpart ZZZZ"
            ])
        }
    
    def generate_emission_unit(self, unit_id: str) -> Dict[str, str]:
        """Generate synthetic emission unit information."""
        unit_types = [
            "Natural Gas Boiler", "Coal Boiler", "Paint Booth", "Coating Line",
            "Storage Tank", "Process Heater", "Emergency Generator", "Dryer",
            "Furnace", "Incinerator", "Crusher", "Grinder"
        ]
        
        unit_type = random.choice(unit_types)
        make_models = {
            "Natural Gas Boiler": [("Cleaver Brooks", "CB-100"), ("Babcock & Wilcox", "BW-200")],
            "Coal Boiler": [("Foster Wheeler", "FW-300"), ("Riley Power", "RP-150")],
            "Paint Booth": [("Custom", "PB-200"), ("Nordson", "ND-100")],
            "Storage Tank": [("Custom", "ST-500"), ("API", "API-1000")]
        }
        
        make, model = random.choice(make_models.get(unit_type, [("Custom", "CUSTOM-001")]))
        
        pollutants = {
            "Natural Gas Boiler": "NOx, CO, PM",
            "Coal Boiler": "NOx, CO, PM, SO2",
            "Paint Booth": "VOC, HAPs",
            "Storage Tank": "VOC",
            "Process Heater": "NOx, CO",
            "Emergency Generator": "NOx, CO, PM"
        }
        
        neshap_subparts = {
            "Natural Gas Boiler": "40 CFR 63 Subpart DDDDD",
            "Coal Boiler": "40 CFR 63 Subpart DDDDD",
            "Emergency Generator": "40 CFR 63 Subpart ZZZZ",
            "Process Heater": "40 CFR 63 Subpart DDDDD",
        }
        throughput_limits = {
            "Paint Booth": "500 gallons/day",
            "Coating Line": "1000 gallons/day",
            "Crusher": "200 tons/hr",
            "Grinder": "50 tons/hr",
        }
        return {
            "Unit ID": unit_id,
            "Unit Description": f"{unit_type} #{random.randint(1, 10)}",
            "Unit Make": make,
            "Unit Model": model,
            "Year of Manufacture": str(random.randint(1990, 2023)),
            "Unit Type": unit_type,
            "Pollutants": pollutants.get(unit_type, "Various"),
            "Emission Limits": self._generate_emission_limits(unit_type),
            "Opacity Limit": random.choice(["20%", "10% (3 min/hr); 30% (any time)", None]),
            "Throughput/Production Limit": throughput_limits.get(unit_type, None),
            "Control Device(s)": self._generate_control_device(unit_type),
            "Capacity Value": str(random.randint(10, 1000)),
            "Capacity Unit": self._get_capacity_unit(unit_type),
            "Fuel Type": self._get_fuel_type(unit_type),
            "Rated Efficiency": f"{random.randint(70, 95)}%",
            "Applicable NESHAP/NSPS Subpart": neshap_subparts.get(unit_type, None),
        }
    
    def _generate_emission_limits(self, unit_type: str) -> str:
        """Generate realistic emission limits based on unit type."""
        limits = {
            "Natural Gas Boiler": "NOx: 0.05 lb/MMBtu, CO: 50 ppmvd, PM: 0.01 lb/MMBtu",
            "Coal Boiler": "NOx: 0.15 lb/MMBtu, CO: 100 ppmvd, PM: 0.03 lb/MMBtu, SO2: 0.2 lb/MMBtu",
            "Paint Booth": "VOC: 2.7 tons/year",
            "Storage Tank": "VOC: 0.1 lb/hr",
            "Process Heater": "NOx: 0.08 lb/MMBtu, CO: 75 ppmvd",
            "Emergency Generator": "NOx: 0.3 lb/MMBtu, CO: 200 ppmvd, PM: 0.05 lb/MMBtu"
        }
        return limits.get(unit_type, "As specified in permit")
    
    def _generate_control_device(self, unit_type: str) -> str:
        """Generate realistic control devices based on unit type."""
        devices = {
            "Natural Gas Boiler": "Low NOx Burner",
            "Coal Boiler": "SCR, ESP",
            "Paint Booth": "Dry Filters",
            "Storage Tank": "Vapor Recovery Unit",
            "Process Heater": "Low NOx Burner",
            "Emergency Generator": "Oxidation Catalyst"
        }
        return devices.get(unit_type, "As specified in permit")
    
    def _get_capacity_unit(self, unit_type: str) -> str:
        """Get appropriate capacity unit for unit type."""
        units = {
            "Natural Gas Boiler": "MMBtu/hr",
            "Coal Boiler": "MMBtu/hr",
            "Paint Booth": "cfm",
            "Storage Tank": "gallons",
            "Process Heater": "MMBtu/hr",
            "Emergency Generator": "kW"
        }
        return units.get(unit_type, "units")
    
    def _get_fuel_type(self, unit_type: str) -> str:
        """Get appropriate fuel type for unit type."""
        fuels = {
            "Natural Gas Boiler": "Natural Gas",
            "Coal Boiler": "Coal",
            "Paint Booth": "N/A",
            "Storage Tank": "N/A",
            "Process Heater": "Natural Gas",
            "Emergency Generator": "Diesel"
        }
        return fuels.get(unit_type, "N/A")
    
    def generate_complete_permit(self, num_units: int = None) -> Dict[str, Any]:
        """Generate a complete permit with facility info, permit info, and emission units."""
        if num_units is None:
            num_units = random.randint(1, 5)
        
        permit = {}
        permit.update(self.generate_facility_info())
        permit.update(self.generate_permit_info())
        
        # Generate emission units
        emission_units = []
        for i in range(num_units):
            unit_id = f"EU{random.randint(1, 999):03d}"
            unit = self.generate_emission_unit(unit_id)
            emission_units.append(unit)
        
        permit["Emission Units"] = emission_units
        return permit
    
    def generate_permit_text(self, permit_data: Dict[str, Any]) -> str:
        """Generate human-readable permit text from structured data."""
        text = "AIR PERMIT APPLICATION\n\n"
        
        # Add facility information
        text += "FACILITY INFORMATION:\n"
        for field in GENERAL_TARGET_FIELDS:
            if field in permit_data and permit_data[field]:
                text += f"{field}: {permit_data[field]}\n"
        
        text += "\nEMISSION UNITS:\n\n"
        
        # Add emission units
        for i, unit in enumerate(permit_data.get("Emission Units", []), 1):
            text += f"Unit {i}:\n"
            for field in UNIT_DETAIL_FIELDS:
                if field in unit and unit[field]:
                    text += f"  {field}: {unit[field]}\n"
            text += "\n"
        
        return text


class GroundTruthGenerator:
    """Generates ground truth datasets for testing."""
    
    def __init__(self, generator: PermitDataGenerator):
        self.generator = generator
    
    def generate_test_dataset(self, num_permits: int, output_dir: Path) -> Dict[str, Any]:
        """Generate a complete test dataset with permits, text, and ground truth."""
        dataset = {
            "permits": [],
            "text_files": [],
            "ground_truth": [],
            "metadata": {
                "num_permits": num_permits,
                "generated_at": self.generator.fake.date_time().isoformat(),
                "seed": getattr(self.generator.fake, '_seed', None)
            }
        }
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        for i in range(num_permits):
            # Generate permit data
            permit_data = self.generator.generate_complete_permit()
            permit_text = self.generator.generate_permit_text(permit_data)
            
            # Save text file
            text_file = output_dir / f"permit_{i+1:03d}.txt"
            with open(text_file, 'w', encoding='utf-8') as f:
                f.write(permit_text)
            
            # Store in dataset
            dataset["permits"].append(permit_data)
            dataset["text_files"].append(str(text_file))
            dataset["ground_truth"].append(permit_data)  # Ground truth is the same as generated data
        
        # Save dataset metadata
        with open(output_dir / "dataset_metadata.json", 'w') as f:
            json.dump(dataset["metadata"], f, indent=2)
        
        # Save ground truth as JSON
        with open(output_dir / "ground_truth.json", 'w') as f:
            json.dump(dataset["ground_truth"], f, indent=2)
        
        # Save ground truth as CSV for easy analysis
        self._save_ground_truth_csv(dataset["ground_truth"], output_dir)
        
        return dataset
    
    def _save_ground_truth_csv(self, ground_truth: List[Dict], output_dir: Path):
        """Save ground truth data as CSV files for analysis."""
        # Flatten the data for CSV export
        flattened_data = []
        
        for i, permit in enumerate(ground_truth):
            # Add general permit info
            row = {"permit_index": i}
            for field in GENERAL_TARGET_FIELDS:
                row[field] = permit.get(field, "")
            
            # Add emission units (one row per unit)
            emission_units = permit.get("Emission Units", [])
            if emission_units:
                for unit in emission_units:
                    unit_row = row.copy()
                    for field in UNIT_DETAIL_FIELDS:
                        unit_row[field] = unit.get(field, "")
                    flattened_data.append(unit_row)
            else:
                # No emission units, just add the general info
                for field in UNIT_DETAIL_FIELDS:
                    row[field] = ""
                flattened_data.append(row)
        
        # Save as CSV
        df = pd.DataFrame(flattened_data)
        df.to_csv(output_dir / "ground_truth.csv", index=False)
        
        # Also save a summary
        summary = {
            "total_permits": len(ground_truth),
            "total_emission_units": sum(len(p.get("Emission Units", [])) for p in ground_truth),
            "fields_covered": list(set(GENERAL_TARGET_FIELDS + UNIT_DETAIL_FIELDS)),
            "facility_types": list(set(p.get("Industry Description", "") for p in ground_truth)),
            "states": list(set(p.get("Facility State Abbreviation", "") for p in ground_truth))
        }
        
        with open(output_dir / "dataset_summary.json", 'w') as f:
            json.dump(summary, f, indent=2)


class TestPermitDataGenerator:
    """Test the PermitDataGenerator class."""
    
    def test_generate_facility_info(self):
        """Test facility information generation."""
        generator = PermitDataGenerator(seed=42)
        facility_info = generator.generate_facility_info()
        
        assert "Facility Name" in facility_info
        assert "Facility Address" in facility_info
        assert "Facility City" in facility_info
        assert "Facility State Abbreviation" in facility_info
        assert len(facility_info["Facility State Abbreviation"]) == 2
        assert len(facility_info["Facility Zip Code"]) == 5
    
    def test_generate_permit_info(self):
        """Test permit information generation."""
        generator = PermitDataGenerator(seed=42)
        permit_info = generator.generate_permit_info()
        
        assert "Permit Number" in permit_info
        assert "Issuance Date" in permit_info
        assert "Expiration Date" in permit_info
        assert "Regulatory Authority" in permit_info
        
        # Check date format
        assert len(permit_info["Issuance Date"]) == 10  # YYYY-MM-DD
        assert len(permit_info["Expiration Date"]) == 10  # YYYY-MM-DD
    
    def test_generate_emission_unit(self):
        """Test emission unit generation."""
        generator = PermitDataGenerator(seed=42)
        unit = generator.generate_emission_unit("EU001")
        
        assert "Unit ID" in unit
        assert "Unit Description" in unit
        assert "Unit Make" in unit
        assert "Unit Model" in unit
        assert "Pollutants" in unit
        assert "Emission Limits" in unit
        assert unit["Unit ID"] == "EU001"
    
    def test_generate_complete_permit(self):
        """Test complete permit generation."""
        generator = PermitDataGenerator(seed=42)
        permit = generator.generate_complete_permit(num_units=3)
        
        assert "Facility Name" in permit
        assert "Permit Number" in permit
        assert "Emission Units" in permit
        assert len(permit["Emission Units"]) == 3
        
        # Check that all required fields are present
        for field in GENERAL_TARGET_FIELDS:
            assert field in permit
    
    def test_generate_permit_text(self):
        """Test permit text generation."""
        generator = PermitDataGenerator(seed=42)
        permit_data = generator.generate_complete_permit(num_units=2)
        text = generator.generate_permit_text(permit_data)
        
        assert "AIR PERMIT APPLICATION" in text
        assert "FACILITY INFORMATION:" in text
        assert "EMISSION UNITS:" in text
        assert permit_data["Facility Name"] in text
        assert permit_data["Permit Number"] in text
    
    def test_deterministic_generation(self):
        """Test that generation is deterministic with same seed."""
        generator1 = PermitDataGenerator(seed=42)
        generator2 = PermitDataGenerator(seed=42)
        
        permit1 = generator1.generate_complete_permit(num_units=2)
        permit2 = generator2.generate_complete_permit(num_units=2)
        
        assert permit1["Facility Name"] == permit2["Facility Name"]
        assert permit1["Permit Number"] == permit2["Permit Number"]


class TestGroundTruthGenerator:
    """Test the GroundTruthGenerator class."""
    
    def test_generate_test_dataset(self, temp_data_dir):
        """Test test dataset generation."""
        generator = PermitDataGenerator(seed=42)
        gt_generator = GroundTruthGenerator(generator)
        
        dataset = gt_generator.generate_test_dataset(num_permits=3, output_dir=temp_data_dir)
        
        assert len(dataset["permits"]) == 3
        assert len(dataset["text_files"]) == 3
        assert len(dataset["ground_truth"]) == 3
        
        # Check that files were created
        assert (temp_data_dir / "permit_001.txt").exists()
        assert (temp_data_dir / "permit_002.txt").exists()
        assert (temp_data_dir / "permit_003.txt").exists()
        assert (temp_data_dir / "ground_truth.json").exists()
        assert (temp_data_dir / "ground_truth.csv").exists()
        assert (temp_data_dir / "dataset_metadata.json").exists()
    
    def test_ground_truth_csv_format(self, temp_data_dir):
        """Test that ground truth CSV is properly formatted."""
        generator = PermitDataGenerator(seed=42)
        gt_generator = GroundTruthGenerator(generator)
        
        gt_generator.generate_test_dataset(num_permits=2, output_dir=temp_data_dir)
        
        # Read the CSV
        df = pd.read_csv(temp_data_dir / "ground_truth.csv")
        
        # Check that it has the expected columns
        expected_columns = ["permit_index"] + GENERAL_TARGET_FIELDS + UNIT_DETAIL_FIELDS
        for col in expected_columns:
            assert col in df.columns
        
        # Check that we have data
        assert len(df) > 0
    
    def test_dataset_metadata(self, temp_data_dir):
        """Test that dataset metadata is properly generated."""
        generator = PermitDataGenerator(seed=42)
        gt_generator = GroundTruthGenerator(generator)
        
        dataset = gt_generator.generate_test_dataset(num_permits=5, output_dir=temp_data_dir)
        
        metadata = dataset["metadata"]
        assert metadata["num_permits"] == 5
        assert "generated_at" in metadata
        assert "seed" in metadata


class TestDataGenerationIntegration:
    """Integration tests for data generation."""
    
    def test_end_to_end_generation(self, temp_data_dir):
        """Test end-to-end data generation and validation."""
        generator = PermitDataGenerator(seed=42)
        gt_generator = GroundTruthGenerator(generator)
        
        # Generate dataset
        dataset = gt_generator.generate_test_dataset(num_permits=3, output_dir=temp_data_dir)
        
        # Validate that generated data matches ground truth
        for i, (permit, ground_truth) in enumerate(zip(dataset["permits"], dataset["ground_truth"])):
            assert permit == ground_truth, f"Permit {i} doesn't match ground truth"
        
        # Validate that text files contain the data
        for i, text_file in enumerate(dataset["text_files"]):
            with open(text_file, 'r') as f:
                text = f.read()
            
            permit_data = dataset["permits"][i]
            assert permit_data["Facility Name"] in text
            assert permit_data["Permit Number"] in text
    
    def test_data_quality_checks(self):
        """Test that generated data meets quality requirements."""
        generator = PermitDataGenerator(seed=42)
        
        for _ in range(10):  # Generate multiple permits
            permit = generator.generate_complete_permit()
            
            # Check that all required fields are present
            for field in GENERAL_TARGET_FIELDS:
                assert field in permit
                assert permit[field] is not None
                assert permit[field] != ""
            
            # Check emission units
            emission_units = permit.get("Emission Units", [])
            assert len(emission_units) > 0
            
            for unit in emission_units:
                for field in UNIT_DETAIL_FIELDS:
                    assert field in unit
                    # Some fields can be None or empty, but they should be present
