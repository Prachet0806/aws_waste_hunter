#!/usr/bin/env bash
# Run test suite for AWS Waste Hunter

set -e

# Ensure we're in project root
cd "$(dirname "$0")/.."

echo "=== AWS Waste Hunter Test Suite ==="
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "Error: pytest not found. Installing requirements..."
    pip install -r requirements.txt
fi

# Run tests with coverage
echo "Running tests with coverage..."
pytest --cov --cov-report=html --cov-report=term-missing

# Check coverage threshold
echo ""
echo "=== Coverage Summary ==="
coverage report --fail-under=80

echo ""
echo "✓ All tests passed!"
echo "✓ Coverage report generated in htmlcov/index.html"
