# AWS Waste Hunter - Test Suite

This directory contains comprehensive tests for the AWS Waste Hunter project.

## Test Structure

```
tests/
├── test_ebs_scanner.py          # EBS volume scanning tests
├── test_ec2_scanner.py          # EC2 instance scanning tests
├── test_elb_scanner.py          # Load balancer scanning tests (ALB/NLB/Classic)
├── test_rds_scanner.py          # RDS cluster/instance scanning tests
├── test_estimator.py            # Basic cost estimation tests
├── test_estimator_advanced.py   # Advanced estimator tests (cache, dedup, etc.)
├── test_tag_checker.py          # Basic tag compliance tests
├── test_compliance_advanced.py  # Advanced compliance tests
├── test_lambda_handler.py       # Basic handler tests
├── test_lambda_handler_integration.py  # Full integration tests
├── test_aws_helpers.py          # Helper function tests
├── test_logging.py              # Logging configuration tests
├── test_report_builder.py       # Report generation tests
└── test_delivery.py             # SNS/S3 delivery tests
```

## Running Tests

### Quick Start

**Linux/Mac:**
```bash
./scripts/run_tests.sh
```

**Windows:**
```powershell
.\scripts\run_tests.ps1
```

### Manual Execution

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run all tests:**
```bash
pytest
```

**Run with coverage:**
```bash
pytest --cov --cov-report=html --cov-report=term
```

**Run specific test file:**
```bash
pytest tests/test_ebs_scanner.py
```

**Run specific test:**
```bash
pytest tests/test_ebs_scanner.py::test_scan_unattached_ebs_basic
```

**Run tests matching a pattern:**
```bash
pytest -k "test_scan"
```

**Verbose output:**
```bash
pytest -v
```

**Stop on first failure:**
```bash
pytest -x
```

## Test Coverage

Target coverage: **>80%**

View coverage report:
```bash
# Generate HTML report
pytest --cov --cov-report=html

# Open in browser (Linux/Mac)
open htmlcov/index.html

# Open in browser (Windows)
start htmlcov/index.html
```

## Test Categories

### Unit Tests
Test individual functions and modules in isolation:
- `test_ebs_scanner.py`
- `test_ec2_scanner.py`
- `test_elb_scanner.py`
- `test_rds_scanner.py`
- `test_aws_helpers.py`
- `test_estimator.py`
- `test_tag_checker.py`

### Integration Tests
Test complete workflows:
- `test_lambda_handler_integration.py` - Full handler execution
- `test_delivery.py` - End-to-end delivery

### Advanced Tests
Test edge cases and complex scenarios:
- `test_estimator_advanced.py` - Caching, deduplication, TTL
- `test_compliance_advanced.py` - Custom tag policies

## Writing New Tests

### Test Structure
```python
import pytest
from module import function

class TestFeature:
    """Test feature X."""

    def test_basic_case(self, monkeypatch):
        """Test basic functionality."""
        # Arrange
        monkeypatch.setenv("VAR", "value")
        
        # Act
        result = function()
        
        # Assert
        assert result == expected
```

### Mocking AWS Services
```python
class FakeEC2Client:
    def __init__(self, data):
        self.data = data
    
    def get_paginator(self, name):
        return FakePaginator(self.data)

def test_with_mock(monkeypatch):
    fake_client = FakeEC2Client(test_data)
    monkeypatch.setattr(module, "_ec2_client", fake_client)
    
    result = module.scan()
    assert len(result) == expected_count
```

### Testing Error Handling
```python
def test_handles_error(monkeypatch):
    def mock_fail():
        raise RuntimeError("API error")
    
    monkeypatch.setattr(module, "api_call", mock_fail)
    
    result = module.safe_operation()
    assert result == []  # Should handle gracefully
```

## Common Issues

### Import Errors
If you see `ModuleNotFoundError: No module named 'scanner'` or similar:

**Solution 1: Use conftest.py (Automatic)**
The `tests/conftest.py` file automatically adds the project root to Python path. Just ensure you're running from the project root:
```bash
cd /path/to/aws_waste_hunter
pytest
```

**Solution 2: Install in development mode**
```bash
pip install -e .
```

**Solution 3: Set PYTHONPATH manually**
```bash
# Unix/Mac
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
pytest

# Windows PowerShell
$env:PYTHONPATH="$env:PYTHONPATH;$PWD"
pytest

# Windows Command Prompt
set PYTHONPATH=%PYTHONPATH%;%CD%
pytest
```

### Missing Dependencies
```bash
pip install -r requirements.txt
# or with dev dependencies
pip install -e .[dev]
```

### Boto3 Client Errors
Tests should mock boto3 clients, not make real AWS calls. If you see actual AWS errors, check your mocks.

### Environment Variables
Tests should set required env vars using `monkeypatch`:
```python
def test_feature(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "test-arn")
    monkeypatch.setenv("REPORT_BUCKET", "test-bucket")
```

## CI/CD Integration

### GitHub Actions Example
```yaml
- name: Run tests
  run: |
    pip install -r requirements.txt
    pytest --cov --cov-report=xml
    
- name: Upload coverage
  uses: codecov/codecov-action@v3
```

### Pre-commit Hook
```bash
#!/bin/bash
pytest --cov --cov-report=term --cov-fail-under=80
```

## Test Data

Tests use minimal, focused test data. Real AWS resources are NOT accessed.

### Example Test Data
```python
test_volumes = [{
    "Volumes": [{
        "VolumeId": "vol-123",
        "Size": 100,
        "VolumeType": "gp3",
        "AvailabilityZone": "us-east-1a",
        "Tags": [{"Key": "owner", "Value": "sre"}]
    }]
}]
```

## Performance Tests

For performance testing of pricing lookups:
```bash
python -m pytest tests/test_estimator_advanced.py::TestCacheTTL -v
```

## Debugging Tests

**Run with print statements:**
```bash
pytest -s
```

**Run with debugger:**
```bash
pytest --pdb
```

**Show local variables on failure:**
```bash
pytest -l
```

## Contributing

When adding new features:
1. Write tests first (TDD)
2. Ensure >80% coverage
3. Test both success and failure paths
4. Include edge cases
5. Update this README if adding new test files

## Questions?

See main project README or open an issue on GitHub.
