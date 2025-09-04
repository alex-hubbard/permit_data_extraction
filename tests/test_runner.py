"""
Test runner for the permit data extraction testing framework.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any, List
import pytest
from datetime import datetime

# Import will be handled dynamically to avoid circular imports


class TestRunner:
    """Main test runner for the permit data extraction testing framework."""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or Path("test_results")
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def run_all_tests(self, num_test_permits: int = 5, verbose: bool = False) -> Dict[str, Any]:
        """Run all tests in the framework."""
        print("🚀 Starting Permit Data Extraction Test Suite")
        print("=" * 60)
        
        results = {
            "test_run_metadata": {
                "timestamp": datetime.now().isoformat(),
                "num_test_permits": num_test_permits,
                "test_framework_version": "1.0.0"
            },
            "unit_tests": {},
            "integration_tests": {},
            "overall_summary": {}
        }
        
        # Run unit tests
        print("\n📋 Running Unit Tests...")
        unit_test_results = self._run_unit_tests(verbose)
        results["unit_tests"] = unit_test_results
        
        # Run integration tests
        print("\n🔗 Running Integration Tests...")
        try:
            # Import dynamically to avoid circular import issues
            from tests.test_integration import IntegrationTestSuite
            integration_suite = IntegrationTestSuite()
            integration_results = integration_suite.run_complete_test_suite(num_test_permits)
            results["integration_tests"] = integration_results
        except ImportError as e:
            print(f"⚠️  Integration tests skipped due to missing dependencies: {e}")
            print("   Use standalone_test_runner.py for testing without full dependencies")
            results["integration_tests"] = {
                "error": "Integration tests skipped due to missing dependencies",
                "overall_score": 0.0
            }
        
        # Generate overall summary
        results["overall_summary"] = self._generate_overall_summary(results)
        
        # Save results
        self._save_results(results)
        
        # Print summary
        self._print_summary(results)
        
        return results
    
    def _run_unit_tests(self, verbose: bool = False) -> Dict[str, Any]:
        """Run unit tests using pytest."""
        test_files = [
            "tests/test_data_validation.py",
            "tests/test_model_comparison.py"
        ]
        
        # Build pytest arguments
        pytest_args = test_files.copy()
        if verbose:
            pytest_args.append("-v")
        else:
            pytest_args.append("-q")
        
        pytest_args.extend([
            "--tb=short",
            "--disable-warnings",
            "--json-report",
            "--json-report-file=unit_test_results.json"
        ])
        
        # Run pytest
        exit_code = pytest.main(pytest_args)
        
        # Load results if available
        results_file = Path("unit_test_results.json")
        if results_file.exists():
            with open(results_file, 'r') as f:
                pytest_results = json.load(f)
            results_file.unlink()  # Clean up
        else:
            pytest_results = {"summary": {"total": 0, "passed": 0, "failed": 0}}
        
        return {
            "exit_code": exit_code,
            "pytest_results": pytest_results,
            "success": exit_code == 0
        }
    
    def _generate_overall_summary(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Generate overall summary of all test results."""
        summary = {
            "total_tests_run": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "overall_score": 0.0,
            "recommendations": [],
            "status": "PASS"
        }
        
        # Unit test summary
        unit_tests = results.get("unit_tests", {})
        if unit_tests.get("pytest_results", {}).get("summary"):
            unit_summary = unit_tests["pytest_results"]["summary"]
            summary["total_tests_run"] += unit_summary.get("total", 0)
            summary["tests_passed"] += unit_summary.get("passed", 0)
            summary["tests_failed"] += unit_summary.get("failed", 0)
        
        # Integration test summary
        integration_tests = results.get("integration_tests", {})
        if integration_tests.get("test_summary"):
            integration_summary = integration_tests["test_summary"]
            summary["total_tests_run"] += integration_summary.get("total_tests_run", 0)
            summary["tests_passed"] += integration_summary.get("tests_passed", 0)
            summary["overall_score"] = integration_tests.get("overall_score", 0.0)
            summary["recommendations"].extend(integration_summary.get("recommendations", []))
        
        # Determine overall status
        if summary["tests_failed"] > 0:
            summary["status"] = "FAIL"
        elif summary["overall_score"] < 0.7:
            summary["status"] = "WARN"
        
        return summary
    
    def _save_results(self, results: Dict[str, Any]):
        """Save test results to files."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save full results as JSON
        results_file = self.output_dir / f"test_results_{timestamp}.json"
        with open(results_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        # Save summary as JSON
        summary_file = self.output_dir / f"test_summary_{timestamp}.json"
        with open(summary_file, 'w') as f:
            json.dump(results["overall_summary"], f, indent=2)
        
        # Save latest results (overwrite)
        latest_results = self.output_dir / "latest_test_results.json"
        latest_summary = self.output_dir / "latest_test_summary.json"
        
        with open(latest_results, 'w') as f:
            json.dump(results, f, indent=2)
        
        with open(latest_summary, 'w') as f:
            json.dump(results["overall_summary"], f, indent=2)
        
        print(f"\n💾 Results saved to:")
        print(f"   - {results_file}")
        print(f"   - {summary_file}")
        print(f"   - {latest_results}")
        print(f"   - {latest_summary}")
    
    def _print_summary(self, results: Dict[str, Any]):
        """Print a summary of test results."""
        summary = results["overall_summary"]
        
        print("\n" + "=" * 60)
        print("📊 TEST SUITE SUMMARY")
        print("=" * 60)
        
        # Status
        status_emoji = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
        print(f"Status: {status_emoji.get(summary['status'], '❓')} {summary['status']}")
        
        # Test counts
        print(f"Total Tests: {summary['total_tests_run']}")
        print(f"Passed: {summary['tests_passed']}")
        print(f"Failed: {summary['tests_failed']}")
        
        # Overall score
        print(f"Overall Score: {summary['overall_score']:.2%}")
        
        # Recommendations
        if summary["recommendations"]:
            print(f"\n💡 Recommendations:")
            for i, rec in enumerate(summary["recommendations"], 1):
                print(f"   {i}. {rec}")
        
        print("\n" + "=" * 60)
    
    def run_specific_test(self, test_name: str, verbose: bool = False) -> Dict[str, Any]:
        """Run a specific test or test category."""
        if test_name == "data_validation":
            return self._run_data_validation_tests(verbose)
        elif test_name == "model_comparison":
            return self._run_model_comparison_tests(verbose)
        elif test_name == "data_generation":
            return self._run_data_generation_tests(verbose)
        elif test_name == "integration":
            integration_suite = IntegrationTestSuite()
            return integration_suite.run_complete_test_suite()
        else:
            raise ValueError(f"Unknown test: {test_name}")
    
    def _run_data_validation_tests(self, verbose: bool) -> Dict[str, Any]:
        """Run data validation tests."""
        pytest_args = ["permit_data_extraction/tests/test_data_validation.py"]
        if verbose:
            pytest_args.append("-v")
        else:
            pytest_args.append("-q")
        
        exit_code = pytest.main(pytest_args)
        return {"exit_code": exit_code, "success": exit_code == 0}
    
    def _run_model_comparison_tests(self, verbose: bool) -> Dict[str, Any]:
        """Run model comparison tests."""
        pytest_args = ["tests/test_model_comparison.py"]
        if verbose:
            pytest_args.append("-v")
        else:
            pytest_args.append("-q")
        
        exit_code = pytest.main(pytest_args)
        return {"exit_code": exit_code, "success": exit_code == 0}
    
    def _run_data_generation_tests(self, verbose: bool) -> Dict[str, Any]:
        """Run data generation tests."""
        pytest_args = ["tests/test_data_generation.py"]
        if verbose:
            pytest_args.append("-v")
        else:
            pytest_args.append("-q")
        
        exit_code = pytest.main(pytest_args)
        return {"exit_code": exit_code, "success": exit_code == 0}


def main():
    """Main entry point for the test runner."""
    parser = argparse.ArgumentParser(description="Permit Data Extraction Test Runner")
    parser.add_argument("--test", choices=["all", "data_validation", "model_comparison", "data_generation", "integration"],
                       default="all", help="Which test to run")
    parser.add_argument("--num-permits", type=int, default=5,
                       help="Number of test permits to generate for integration tests")
    parser.add_argument("--output-dir", type=str, default="test_results",
                       help="Directory to save test results")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Verbose output")
    
    args = parser.parse_args()
    
    # Create test runner
    runner = TestRunner(Path(args.output_dir))
    
    try:
        if args.test == "all":
            results = runner.run_all_tests(args.num_permits, args.verbose)
        else:
            results = runner.run_specific_test(args.test, args.verbose)
        
        # Exit with appropriate code
        if args.test == "all":
            summary = results["overall_summary"]
            if summary["status"] == "FAIL":
                sys.exit(1)
            elif summary["status"] == "WARN":
                sys.exit(2)
            else:
                sys.exit(0)
        else:
            if not results.get("success", False):
                sys.exit(1)
            else:
                sys.exit(0)
    
    except Exception as e:
        print(f"❌ Test runner failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
