# AWS Waste Hunter - Detailed Next Steps Plan

## Step 1: Update Test Suite to Cover New Code Paths — ✅ COMPLETE

All test-suite work from this step has been implemented and is passing
(**152 tests**, **~73% total coverage**, see `tests/README.md`):

### 1.1 Updated Existing Tests
- [x] **test_ebs_scanner.py** - volume_type/region mocks, region extraction, empty tags, stopped-instance join
- [x] **test_ec2_scanner.py** - CPU/network threshold validation, pagination, no-metrics tracking, ASG skip
- [x] **test_elb_scanner.py** - NLB, Classic ELB (incl. tag fetching), batch tag chunking, fail-open skip on missing datapoints, gateway skip, empty AZs, dimension extraction, brand-new LB skip
- [x] **test_rds_scanner.py** - clusters + instances, stopped cluster/instance dedupe, tag fetching, empty AZs, pagination
- [x] **test_rds_idle_scanner.py** - idle instances + clusters, busy/stopped skips, thresholds, no-metrics skip, GetMetricData batching
- [x] **test_eip_scanner.py** - association checks, namespaced fallback IDs, pagination, error propagation
- [x] **test_snapshot_scanner.py** - AMI references, min-age filtering, missing StartTime skip
- [x] **test_report_builder.py** - empty sections, error rendering
- [x] **test_delivery.py** - SNS/S3 missing-env failure paths
- [x] **test_lambda_handler.py / test_lambda_handler_integration.py** - partial/failed statuses, delivery/estimator/compliance failures, empty results, error details

### 1.2 New Test Files Added
- [x] **test_aws_helpers.py** - region parsing (standard/Local Zone/Wavelength/GovCloud/ISO/Outposts), safe_get_first, chunk_list, normalize_tags, malformed-input handling
- [x] **test_estimator_advanced.py** - cache TTL, size limits, dedup, no-mutation, mode validation, failed-lookup handling, per-type ELB pricing, RDS class pricing, pricing override merge
- [x] **test_estimator_pricing.py** - PRICING_JSON / PRICING_FILE overrides, deep-merge behavior
- [x] **test_compliance_advanced.py** - REQUIRED_TAGS parsing, empty-override fallback, compliance stats
- [x] **test_logging.py** - setup, format, idempotency, LOG_LEVEL
- [x] **test_delivery_happy.py** - SNS happy path, 256KB truncation, S3 archival
- [x] **test_phase_fixes.py** - fail-open ELB/gateway skips, live→static fallback without cache poisoning, report no-mutation/truncation, threshold validation, status mapping

### 1.3 Shared Fixtures
- [x] **tests/conftest.py** - project-root path guard plus autouse fixtures resetting scanner thresholds, no-metrics state, and price cache between tests

### 1.4 Run Tests
```bash
pytest --cov=. --cov-report=html --cov-report=term-missing
# Result: 152 passed, ~73% total (live Pricing API / real-AWS paths excluded from mocks)
```

---

## Step 2: Deploy to Staging Lambda and Run Integration Tests

### 2.1 Pre-Deployment Checklist
- [ ] Review all environment variables needed
- [ ] Ensure IAM role has correct permissions
- [ ] Create staging SNS topic and S3 bucket
- [ ] Set up test resources in staging account

### 2.2 Create Staging Resources
```bash
# Create staging SNS topic
aws sns create-topic --name aws-waste-hunter-staging

# Create staging S3 bucket
aws s3 mb s3://aws-waste-hunter-reports-staging

# Tag test resources
aws ec2 create-tags --resources i-test123 --tags Key=test,Value=waste-hunter-staging
```

### 2.3 Package and Deploy
```bash
# Option A: Using AWS CLI
cd C:\Users\prach\Documents\aws_waste_hunter

# Install dependencies
pip install -r requirements.txt -t .

# Create deployment package
zip -r deployment_package.zip . -x "*.git*" -x "*__pycache__*" -x "*.pytest_cache*" -x "tests/*"

# Update or create Lambda function
aws lambda update-function-code \
  --function-name aws-waste-hunter-staging \
  --zip-file fileb://deployment_package.zip

# Update configuration
aws lambda update-function-configuration \
  --function-name aws-waste-hunter-staging \
  --timeout 300 \
  --memory-size 512 \
  --environment Variables="{
    SNS_TOPIC_ARN=arn:aws:sns:REGION:ACCOUNT:aws-waste-hunter-staging,
    REPORT_BUCKET=aws-waste-hunter-reports-staging,
    CPU_THRESHOLD=2,
    NETWORK_THRESHOLD=1000000,
    RDS_CPU_THRESHOLD=5,
    RDS_CONNECTIONS_THRESHOLD=1,
    MIN_SNAPSHOT_AGE_DAYS=7,
    WASTE_ALERT_THRESHOLD=1000,
    PRICING_MODE=static,
    REQUIRED_TAGS=owner,env,cost-center,
    MAX_SCANNER_WORKERS=4,
    AWS_REGION=us-east-1
  }"
```

### 2.4 Create Test Resources in Staging
```bash
# Create unattached EBS volume
aws ec2 create-volume --size 10 --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=test,Value=waste-hunter}]'

# Launch idle EC2 instance
aws ec2 run-instances --image-id ami-xxxxx --instance-type t3.micro \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=test,Value=waste-hunter}]'

# Create unused ALB (optional - more complex)
# Stop RDS instance/cluster (optional - expensive)
```

### 2.5 Run Integration Tests
```bash
# Test 1: Manual invocation
aws lambda invoke \
  --function-name aws-waste-hunter-staging \
  --log-type Tail \
  --payload '{}' \
  response.json

# Check response
cat response.json

# Test 2: Check CloudWatch Logs
aws logs tail /aws/lambda/aws-waste-hunter-staging --follow

# Test 3: Verify SNS delivery
# Check email/Slack for report

# Test 4: Verify S3 archive
aws s3 ls s3://aws-waste-hunter-reports-staging/reports/

# Test 5: Download and review report
aws s3 ls s3://aws-waste-hunter-reports-staging/reports/
latest_report=$(aws s3 ls s3://aws-waste-hunter-reports-staging/reports/ | sort | tail -1 | awk '{print $4}')
aws s3 cp "s3://aws-waste-hunter-reports-staging/reports/$latest_report" ./report.md
cat report.md
```

### 2.6 Test Error Scenarios
```bash
# Test with missing env var
aws lambda update-function-configuration \
  --function-name aws-waste-hunter-staging \
  --environment Variables="{REPORT_BUCKET=aws-waste-hunter-reports-staging}"
# Invoke - should handle missing SNS_TOPIC_ARN gracefully

# Test with invalid pricing mode
aws lambda update-function-configuration \
  --function-name aws-waste-hunter-staging \
  --environment Variables="{...,PRICING_MODE=invalid}"
# Should fall back to static with warning

# Test with throttling (reduce IAM permissions temporarily)
# Should retry and continue with partial results
```

### 2.7 Test Live Pricing Mode
```bash
# Enable live pricing
aws lambda update-function-configuration \
  --function-name aws-waste-hunter-staging \
  --environment Variables="{...,PRICING_MODE=live}"

# Invoke and verify pricing API calls in logs
aws lambda invoke --function-name aws-waste-hunter-staging response.json
aws logs tail /aws/lambda/aws-waste-hunter-staging --filter-pattern "Pricing"
```

**Estimated Time:** 2-3 hours

---

## Step 3: Monitor CloudWatch Logs for Runtime Issues

### 3.1 Set Up Log Insights Queries
```bash
# Create CloudWatch Log Insights queries

# Query 1: Error summary
fields @timestamp, @message
| filter @message like /ERROR/
| stats count() by @message

# Query 2: Scanner performance
fields @timestamp, scanner, duration
| filter @message like /Starting scan/ or @message like /Completed scan/
| sort @timestamp desc

# Query 3: Resource counts
fields @timestamp, @message
| filter @message like /Found.*resources/
| parse @message "Found * *" as count, type

# Query 4: Pricing cache hits
fields @timestamp, @message
| filter @message like /cache/
| stats count()

# Query 5: API throttling
fields @timestamp, @message
| filter @message like /throttl/i or @message like /TooManyRequests/
```

### 3.2 Create CloudWatch Alarms
```bash
# Alarm 1: Lambda errors
aws cloudwatch put-metric-alarm \
  --alarm-name waste-hunter-staging-errors \
  --alarm-description "Alert on Lambda errors" \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=FunctionName,Value=aws-waste-hunter-staging

# Alarm 2: Lambda duration approaching timeout
aws cloudwatch put-metric-alarm \
  --alarm-name waste-hunter-staging-duration \
  --metric-name Duration \
  --namespace AWS/Lambda \
  --statistic Average \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 240000 \
  --comparison-operator GreaterThanThreshold \
  --dimensions Name=FunctionName,Value=aws-waste-hunter-staging

# Alarm 3: Lambda throttles
aws cloudwatch put-metric-alarm \
  --alarm-name waste-hunter-staging-throttles \
  --metric-name Throttles \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanThreshold
```

### 3.3 Monitor for 1 Week
- [ ] Run daily for 7 days via EventBridge scheduled rule
- [ ] Review logs daily for:
  - Errors and warnings
  - API throttling
  - Duration trends
  - Memory usage
  - Pricing cache behavior
  
### 3.4 Create Monitoring Dashboard
```bash
aws cloudwatch put-dashboard \
  --dashboard-name waste-hunter-staging \
  --dashboard-body file://dashboard.json
```

**dashboard.json:**
```json
{
  "widgets": [
    {
      "type": "metric",
      "properties": {
        "metrics": [
          ["AWS/Lambda", "Invocations", {"stat": "Sum"}],
          [".", "Errors", {"stat": "Sum"}],
          [".", "Duration", {"stat": "Average"}]
        ],
        "period": 300,
        "stat": "Average",
        "region": "us-east-1",
        "title": "Lambda Health"
      }
    }
  ]
}
```

**Estimated Time:** 1 hour setup + 7 days monitoring

---

## Step 4: Add CloudWatch Metrics for Scan Metrics (FUTURE — not yet implemented)

> Note: `utils/metrics.py` does not exist yet. The snippets below are a
> proposal, not current behavior.

### 4.1 Create Metrics Module
```python
# File: utils/metrics.py (proposed)
import boto3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
_cloudwatch = None

def _get_cloudwatch():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client('cloudwatch')
    return _cloudwatch

def put_scan_metrics(namespace, metrics_data):
    """
    Publish custom metrics to CloudWatch.
    
    Args:
        namespace: CloudWatch namespace (e.g., "WasteHunter/Scans")
        metrics_data: List of dicts with MetricName, Value, Unit
    """
    cw = _get_cloudwatch()
    try:
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=metrics_data
        )
        logger.info(f"Published {len(metrics_data)} metrics to {namespace}")
    except Exception as e:
        logger.warning(f"Failed to publish metrics: {e}")
```

### 4.2 Update Lambda Handler
```python
# Add to lambda_handler.py
from utils.metrics import put_scan_metrics
import time

def handler(event, context):
    start_time = time.time()
    
    # ... existing code ...
    
    # Publish metrics
    metrics_data = [
        {
            'MetricName': 'ResourcesScanned',
            'Value': len(resources),
            'Unit': 'Count',
            'Timestamp': datetime.utcnow()
        },
        {
            'MetricName': 'MonthlyWaste',
            'Value': total,
            'Unit': 'None',  # Dollars
            'Timestamp': datetime.utcnow()
        },
        {
            'MetricName': 'ScanErrors',
            'Value': len(scan_errors),
            'Unit': 'Count',
            'Timestamp': datetime.utcnow()
        },
        {
            'MetricName': 'TagViolations',
            'Value': len(violations),
            'Unit': 'Count',
            'Timestamp': datetime.utcnow()
        },
        {
            'MetricName': 'ScanDuration',
            'Value': time.time() - start_time,
            'Unit': 'Seconds',
            'Timestamp': datetime.utcnow()
        }
    ]
    
    put_scan_metrics('WasteHunter/Scans', metrics_data)
    
    return result
```

### 4.3 Add IAM Permission
```json
{
  "Effect": "Allow",
  "Action": "cloudwatch:PutMetricData",
  "Resource": "*"
}
```

### 4.4 Create Metric Alarms
```bash
# High waste detected
aws cloudwatch put-metric-alarm \
  --alarm-name waste-hunter-high-waste \
  --metric-name MonthlyWaste \
  --namespace WasteHunter/Scans \
  --statistic Maximum \
  --period 86400 \
  --evaluation-periods 1 \
  --threshold 1000 \
  --comparison-operator GreaterThanThreshold

# High tag violation rate
aws cloudwatch put-metric-alarm \
  --alarm-name waste-hunter-tag-violations \
  --metric-name TagViolations \
  --namespace WasteHunter/Scans \
  --statistic Maximum \
  --period 86400 \
  --evaluation-periods 1 \
  --threshold 50 \
  --comparison-operator GreaterThanThreshold
```

**Estimated Time:** 2-3 hours

---

## Step 5: Evaluate ThreadPoolExecutor for Parallel Pricing Lookups (FUTURE — not yet implemented)

> Note: scanners already run in parallel via ThreadPoolExecutor in
> `lambda_handler.py`; only pricing lookups remain sequential. The benchmark
> script below does not exist yet.

### 5.1 Analyze Current Performance
```python
# Add timing to cost_engine/estimator.py
import time

def estimate_monthly_waste(resources):
    pricing_start = time.time()
    
    # ... existing code ...
    
    pricing_duration = time.time() - pricing_start
    logger.info(f"Pricing lookup took {pricing_duration:.2f}s for {len(resources)} resources")
```

### 5.2 Benchmark Sequential vs Parallel
```python
# File: benchmark_pricing.py
import time
from concurrent.futures import ThreadPoolExecutor
from cost_engine.estimator import _get_ec2_hourly_price

test_cases = [
    ("t3.micro", "us-east-1"),
    ("t3.small", "us-west-2"),
    ("t3.medium", "eu-west-1"),
    # ... 20 more cases
]

# Sequential
start = time.time()
for instance_type, region in test_cases:
    _get_ec2_hourly_price(instance_type, region)
sequential_time = time.time() - start

# Parallel
start = time.time()
with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [
        executor.submit(_get_ec2_hourly_price, inst, reg)
        for inst, reg in test_cases
    ]
    results = [f.result() for f in futures]
parallel_time = time.time() - start

print(f"Sequential: {sequential_time:.2f}s")
print(f"Parallel: {parallel_time:.2f}s")
print(f"Speedup: {sequential_time/parallel_time:.2f}x")
```

### 5.3 Implement Parallel Pricing (If Beneficial)
```python
# Update cost_engine/estimator.py
from concurrent.futures import ThreadPoolExecutor
import os

MAX_WORKERS = int(os.environ.get("PRICING_WORKERS", "10"))

def _fetch_price_parallel(resource):
    """Fetch price for a single resource."""
    if resource.get("type") == "EBS":
        return _safe_price(
            _get_ebs_gb_month_price,
            resource.get("volume_type"),
            resource.get("region")
        )
    elif resource.get("type") == "EC2":
        return _safe_price(
            _get_ec2_hourly_price,
            resource.get("instance_type"),
            resource.get("region")
        )
    # ... etc
    return None

def estimate_monthly_waste(resources):
    pricing_mode = os.environ.get("PRICING_MODE", "static").lower()
    
    if pricing_mode == "live":
        # Parallel pricing lookups
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            price_futures = {
                executor.submit(_fetch_price_parallel, r): r
                for r in unique_resources
            }
            # Process results as they complete
            for future in as_completed(price_futures):
                resource = price_futures[future]
                try:
                    price = future.result()
                    # Apply price to resource
                except Exception as e:
                    logger.warning(f"Pricing lookup failed: {e}")
    else:
        # Sequential static pricing (fast, no need for threads)
        # ... existing code ...
```

### 5.4 Test and Compare
- [ ] Run benchmark with 100+ resources
- [ ] Measure impact on Lambda duration
- [ ] Check for pricing API throttling
- [ ] Verify cache hit rates
- [ ] Test error handling

### 5.5 Decision Criteria
**Implement parallel if:**
- Speedup > 2x with live pricing
- No increase in API throttling
- Lambda duration reduced by >10s
- Cache miss rate > 30%

**Keep sequential if:**
- Speedup < 1.5x
- Causes API throttling
- Cache hit rate > 90%
- Added complexity not worth gains

**Estimated Time:** 3-4 hours

---

## Summary Timeline

| Step | Task | Time Estimate | Priority | Status |
|------|------|---------------|----------|--------|
| 1 | Update test suite | 4-6 hours | HIGH | ✅ DONE (152 tests) |
| 2 | Deploy to staging | 2-3 hours | HIGH | ⏳ |
| 3 | Monitor logs | 1h + 7 days | MEDIUM | ⏳ |
| 4 | Add CloudWatch metrics | 2-3 hours | MEDIUM | ⏳ (proposal only) |
| 5 | Parallel pricing eval | 3-4 hours | LOW | ⏳ (proposal only) |

**Total Active Work:** 12-19 hours
**Total Calendar Time:** ~10 days (including monitoring)

## Execution Order

### Week 1
- **Day 1-2:** ~~Complete Step 1 (tests)~~ ✅ Done
- **Day 3:** Complete Step 2 (staging deployment)
- **Day 3-10:** Step 3 monitoring (passive)

### Week 2
- **Day 11:** Review monitoring results
- **Day 12:** Complete Step 4 (metrics)
- **Day 13:** Complete Step 5 (parallel pricing)
- **Day 14:** Final review and production deployment

## Success Criteria

- [x] All tests pass (152/152, ~73% total coverage)
- [ ] Coverage > 80% (blocked on live-AWS paths; currently ~73%)
- [ ] No errors in staging for 7 days
- [ ] Scan duration < 2 minutes for typical account
- [ ] Custom metrics publishing successfully
- [ ] Decision made on parallel pricing (implement or defer)
- [x] Documentation updated
- [ ] Production deployment successful
