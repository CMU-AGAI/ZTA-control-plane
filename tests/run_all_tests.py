#!/usr/bin/env python3
"""
Run All MCP Tests
=================
Runs tests against all three MCP servers.

Prerequisites:
    All backend services running:
        - Airline on :8001
        - Hotel on :8002  
        - Car Rental on :8003
    
    MCP server running on :8000 (one at a time, or different ports)

Usage:
    python run_all_tests.py <airline|hotel|car-rental|all>
"""

import subprocess
import sys


def run_test(name: str, script: str):
    """Run a test script and return success status."""
    print(f"\n{'#' * 70}")
    print(f"# Testing: {name}")
    print(f"{'#' * 70}")
    
    result = subprocess.run([sys.executable, script], cwd=".")
    return result.returncode == 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_all_tests.py <airline|hotel|car-rental|all>")
        print("\nNote: Make sure the appropriate backend and MCP servers are running.")
        sys.exit(1)
    
    target = sys.argv[1].lower()
    
    tests = {
        "airline": ("Airline MCP", "test_airline_mcp.py"),
        "hotel": ("Hotel MCP", "test_hotel_mcp.py"),
        "car-rental": ("Car Rental MCP", "test_car_rental_mcp.py"),
    }
    
    if target == "all":
        print("⚠️  Running all tests requires each MCP server on port 8000.")
        print("   Run each test separately with the corresponding MCP server running.\n")
        
        results = {}
        for key, (name, script) in tests.items():
            response = input(f"Run {name} test? (y/n/q): ").strip().lower()
            if response == 'q':
                break
            if response == 'y':
                results[name] = run_test(name, script)
        
        print("\n" + "=" * 70)
        print("📊 Final Results:")
        print("=" * 70)
        for name, passed in results.items():
            status = "✅ PASSED" if passed else "❌ FAILED"
            print(f"  {name}: {status}")
    
    elif target in tests:
        name, script = tests[target]
        success = run_test(name, script)
        sys.exit(0 if success else 1)
    
    else:
        print(f"Unknown target: {target}")
        print("Valid options: airline, hotel, car-rental, all")
        sys.exit(1)


if __name__ == "__main__":
    main()
