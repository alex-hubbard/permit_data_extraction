#!/usr/bin/env python3
"""
Convenience script to run the permit data extraction testing framework.

This script provides easy access to both the standalone and full test runners.
"""

import argparse
import sys
from pathlib import Path


def main():
    """Main entry point for running tests."""
    parser = argparse.ArgumentParser(description="Permit Data Extraction Test Runner")
    parser.add_argument("--mode", choices=["standalone", "full"], default="standalone",
                       help="Test mode: standalone (no external deps) or full (requires all deps)")
    parser.add_argument("--test", choices=["all", "data_validation", "data_generation", "integration"],
                       default="all", help="Which test to run")
    parser.add_argument("--num-permits", type=int, default=5,
                       help="Number of test permits to generate")
    parser.add_argument("--output-dir", type=str, default="test_results",
                       help="Directory to save test results")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Verbose output")
    parser.add_argument("--demo", action="store_true",
                       help="Run the simple demo instead of tests")
    
    args = parser.parse_args()
    
    if args.demo:
        print("🧪 Running Simple Demo...")
        print("=" * 50)
        try:
            import subprocess
            result = subprocess.run([sys.executable, "simple_demo.py"], 
                                  capture_output=False, text=True)
            return result.returncode
        except Exception as e:
            print(f"❌ Demo failed: {e}")
            return 1
    
    if args.mode == "standalone":
        print("🚀 Running Standalone Test Suite...")
        print("=" * 50)
        try:
            import subprocess
            cmd = [sys.executable, "standalone_test_runner.py"]
            if args.test != "all":
                cmd.extend(["--test", args.test])
            cmd.extend(["--num-permits", str(args.num_permits)])
            cmd.extend(["--output-dir", args.output_dir])
            if args.verbose:
                cmd.append("--verbose")
            
            result = subprocess.run(cmd, capture_output=False, text=True)
            return result.returncode
        except Exception as e:
            print(f"❌ Standalone test runner failed: {e}")
            return 1
    
    elif args.mode == "full":
        print("🚀 Running Full Test Suite...")
        print("=" * 50)
        print("Note: This requires all permit data extraction dependencies.")
        print("If you get import errors, use --mode standalone instead.")
        print()
        
        try:
            import subprocess
            cmd = [sys.executable, "tests/test_runner.py"]
            if args.test != "all":
                cmd.extend(["--test", args.test])
            cmd.extend(["--num-permits", str(args.num_permits)])
            cmd.extend(["--output-dir", args.output_dir])
            if args.verbose:
                cmd.append("--verbose")
            
            result = subprocess.run(cmd, capture_output=False, text=True)
            return result.returncode
        except Exception as e:
            print(f"❌ Full test runner failed: {e}")
            print("💡 Try using --mode standalone instead")
            return 1


if __name__ == "__main__":
    sys.exit(main())
