# Run test suite for AWS Waste Hunter
# PowerShell script for Windows

$ErrorActionPreference = "Stop"

Write-Host "=== AWS Waste Hunter Test Suite ===" -ForegroundColor Cyan
Write-Host ""

# Ensure we're in project root
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

# Check if pytest is installed
try {
    $pytestVersion = pytest --version 2>&1
    Write-Host "Found pytest: $pytestVersion" -ForegroundColor Green
} catch {
    Write-Host "Error: pytest not found. Installing requirements..." -ForegroundColor Yellow
    pip install -r requirements.txt
}

# Run tests with coverage
Write-Host "Running tests with coverage..." -ForegroundColor Cyan
pytest --cov --cov-report=html --cov-report=term-missing

if ($LASTEXITCODE -ne 0) {
    Write-Host "Tests failed!" -ForegroundColor Red
    exit $LASTEXITCODE
}

# Check coverage threshold
Write-Host ""
Write-Host "=== Coverage Summary ===" -ForegroundColor Cyan
coverage report --fail-under=80

if ($LASTEXITCODE -ne 0) {
    Write-Host "Coverage below 80% threshold!" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "✓ All tests passed!" -ForegroundColor Green
Write-Host "✓ Coverage report generated in htmlcov/index.html" -ForegroundColor Green
