#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0",
# ]
# ///
"""
Run all DOSBox-X remote debugging integration tests.

Prerequisites:
    - DOSBox-X built with ./build-debug --enable-remotedebug

Run with:
    uv run tests/integration/run_all.py [pytest args...]

Examples:
    uv run tests/integration/run_all.py              # Run all tests
    uv run tests/integration/run_all.py -v           # Verbose output
    uv run tests/integration/run_all.py -k gdb       # Only GDB tests
    uv run tests/integration/run_all.py -x           # Stop on first failure
    uv run tests/integration/run_all.py --tb=long    # Full tracebacks
"""

import sys
from pathlib import Path

import pytest


def main():
    print("DOSBox-X Remote Debugging Integration Tests")
    print("=" * 50)
    print()
    print("Running tests...")
    print("-" * 50)

    # Get the directory containing this script
    test_dir = Path(__file__).parent

    # Build pytest arguments
    pytest_args = [
        str(test_dir),
        "-v",
        "--tb=short",
    ]

    # Add any additional arguments passed to this script
    pytest_args.extend(sys.argv[1:])

    # Run pytest
    return pytest.main(pytest_args)


if __name__ == "__main__":
    sys.exit(main())
