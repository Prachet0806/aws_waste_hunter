# AWS Waste Hunter — Cloud FinOps Automation Bot

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![AWS](https://img.shields.io/badge/AWS-Lambda%20%7C%20EventBridge-orange)

## 📖 Overview
**AWS Waste Hunter** is a serverless SRE/FinOps automation framework that proactively reduces cloud spend and operational toil.  
It detects wasted AWS resources, estimates their monthly cost, enforces tagging compliance, and delivers actionable optimization reports via SNS (Email/Slack) — on a schedule.

This moves cost optimization from a **manual, ad-hoc audit** to an **automated, reliable weekly operation**.

---

## Key Capabilities
- **Waste Detection**
  - Unattached EBS Volumes (plus in-use volumes on stopped instances)
  - Idle EC2 Instances (avg CPU < 2% and negligible network over 7 days, ASG-aware)
  - Unused Load Balancers (ALB, NLB, Classic; Gateway LBs skipped)
  - Stopped RDS Clusters and Instances (deduplicated)
  - Idle RDS Instances/Clusters (low CPU + low connections over 7 days)
  - Unattached Elastic IPs
  - Orphaned EBS Snapshots (unreferenced by AMIs, past minimum age)

- **Financial Estimation** — maps resources to pricing to estimate monthly waste ($)

- **Governance** — enforces tagging standards: `owner`, `env`, `cost-center`

- **Reporting** — generates a Markdown report (Jinja2) and delivers via SNS, archives to S3

- **Serverless Automation** — EventBridge cron → Lambda → report

---

## Architecture

```

EventBridge (weekly cron)
        ↓
AWS Lambda (lambda_handler.py)
        ↓
Scanners (boto3, parallel via ThreadPoolExecutor)
├── ebs_scanner.py
├── ec2_scanner.py
├── elb_scanner.py
├── rds_scanner.py
├── rds_idle_scanner.py
├── eip_scanner.py
└── snapshot_scanner.py
        ↓
Cost Engine (cost_engine/estimator.py)
        ↓
Compliance (compliance/tag_checker.py)
        ↓
Reporting (reporting/report_builder.py)
        ↓
Delivery (SNS + S3)

```

**Data Flow:**
1.  **Trigger:** EventBridge Rule triggers the Lambda function weekly (Cron).
2.  **Scan:** Boto3 scanners query AWS APIs (EC2, CloudWatch, ELB, RDS) to identify idle resources.
3.  **Analyze:**
    * **Cost Engine:** Maps resources to pricing data to estimate waste.
    * **Compliance Engine:** Checks resource tags against the policy.
4.  **Report:** A Jinja2 template renders a formatted Markdown summary.
5.  **Deliver:**
    * **SNS:** Pushes the report to subscribers (Email/Slack).
    * **S3:** Archives the report for audit history.

### 📂 Project Structure
```text
aws-waste-hunter/
├── scanner/              # Resource discovery logic (Boto3)
│   ├── ebs_scanner.py       # Unattached EBS + volumes on stopped instances
│   ├── ec2_scanner.py       # Idle EC2 instances
│   ├── elb_scanner.py       # Unused ALB/NLB/Classic LBs
│   ├── rds_scanner.py       # Stopped RDS clusters & instances (deduplicated)
│   ├── rds_idle_scanner.py  # Idle running RDS (CPU + connections)
│   ├── eip_scanner.py       # Unattached Elastic IPs
│   └── snapshot_scanner.py  # Orphaned EBS snapshots
├── cost_engine/          # Financial estimation logic
│   └── estimator.py     # Cost calculation, live-primary + static fallback
├── compliance/           # Tagging governance checks
│   └── tag_checker.py   # Configurable tag policy enforcement
├── reporting/            # Report generation
│   └── report_builder.py # Jinja2 Markdown templates
├── delivery/             # Report delivery
│   ├── sns_sender.py    # SNS notifications (256KB truncation)
│   └── s3_archiver.py   # S3 archival (UUID keys, SSE/KMS)
├── utils/                # Shared utilities
│   ├── aws_helpers.py   # Region parsing, batching, fail-open counter
│   ├── aws_factory.py   # Central boto3 client factory
│   ├── schema.py        # Normalized resource schema
│   └── logging_config.py # Logging setup (LOG_LEVEL aware)
├── tests/                # Test suite (152 tests, incl. conftest fixtures)
├── scripts/              # Helper scripts
│   ├── run_tests.sh     # Unix test runner
│   ├── run_tests.ps1    # Windows test runner
│   └── live_test.sh     # Staging integration testing
├── lambda_handler.py     # Main orchestrator (parallel scans, ok/partial/failed)
├── requirements.txt      # Pinned production dependencies
├── setup.py              # Development installation
├── pytest.ini            # Test discovery (coverage config lives in .coveragerc)
├── .coveragerc           # Coverage source/omit/report settings
├── env.example           # Full environment variable reference (copy to .env)
├── README.md             # This file
└── runbook.md            # Operational playbook
```
---

## ⚙️ Setup & Deployment

### Prerequisites
- Python 3.9+  
- AWS CLI configured  
- boto3, jinja2  

### 1. Infrastructure Setup

Create:

**IAM Role for Lambda** with permissions:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "elasticloadbalancing:Describe*",
        "rds:Describe*",
        "rds:ListTagsForResource",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:GetMetricData",
        "pricing:GetProducts",
        "sns:Publish",
        "s3:PutObject",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Decrypt", "kms:Encrypt"],
      "Resource": "*",
      "Condition": {"StringEquals": {"kms:ViaService": ["sns.us-east-1.amazonaws.com", "s3.us-east-1.amazonaws.com"]}}
    }
  ]
}
```

**SNS Topic** — subscribe email or Slack  
**S3 Bucket** — for report archival  

---

### 2. Deploy Lambda

```bash
pip install -r requirements.txt -t .
zip -r deployment_package.zip .
aws lambda update-function-code --function-name aws-waste-hunter --zip-file fileb://deployment_package.zip
```

### 3. Configure Environment Variables

Set the following environment variables in your Lambda configuration:

```bash
# Required
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:sre-cost-alerts
REPORT_BUCKET=sre-finops-reports

# Optional configuration
CPU_THRESHOLD=2
NETWORK_THRESHOLD=1000000      # max bytes (NetworkIn+NetworkOut) over 7d before considered active
RDS_CPU_THRESHOLD=5            # idle-RDS CPU threshold (%)
RDS_CONNECTIONS_THRESHOLD=1    # idle-RDS peak-connections threshold
MIN_SNAPSHOT_AGE_DAYS=7        # orphaned snapshots younger than this are ignored
WASTE_ALERT_THRESHOLD=1000     # CRITICAL log + high_waste flag when estimated waste >= this ($)
REQUIRED_TAGS=owner,env,cost-center
PRICING_MODE=static  # or 'live': live Pricing API primary, static fallback (default is 'static')
MAX_SCANNER_WORKERS=4          # parallel scanners, clamped to 1-10
AWS_REGION=us-east-1
# LOG_LEVEL=INFO               # DEBUG/INFO/WARNING/ERROR
# REPORT_SCHEDULE=Weekly       # label used in the SNS subject
# REPORT_PREFIX=reports        # S3 key prefix for archived reports
# S3_SSE=AES256                # or aws:kms with S3_KMS_KEY_ID
# SNS_TOPIC_REGION=us-east-1   # defaults to region parsed from SNS_TOPIC_ARN

# Optional pricing overrides (deep-merged over built-in defaults):
# PRICING_JSON='{"EBS":0.1,"EC2":{"t3.micro":8.5},"ELB":18,"RDS":120}'
# PRICING_FILE=/var/task/pricing.json

```

### 4. Schedule with EventBridge

Create an EventBridge (CloudWatch Events) Rule to trigger the function weekly:

```text
cron(0 14 ? * MON *)   # Every Monday 14:00 UTC

```

---

## 📊 Example Output

**Subject:** AWS Waste Hunter — Weekly Cost Optimization Report

> **Total Monthly Waste:** $42.30
> **🚨 Wasted Resources**
> * `vol-0abc123` (EBS): $10.00/mo
> * `i-0xyz789` (EC2): $32.30/mo
> 
> 
> **🏷️ Tagging Violations**
> * `i-0xyz789` missing: `owner`, `cost-center`
> 
> 

---

## 📈 Impact

* **Reduces Toil:** Eliminates manual cloud audits.
* **Improves Attribution:** Ensures every dollar has an owner.
* **Improves Reliability:** Prevents cost-driven incidents (e.g., limits reached).
* **Builds SRE Discipline:** Cost optimization becomes an operational habit.

## 🔧 Recent Improvements

### Reliability & Robustness
- **Retry/backoff:** Adaptive retries and timeouts on all AWS API calls
- **Lazy client initialization:** Boto3 clients initialized on-demand to avoid cold start issues
- **Comprehensive logging:** Structured logging throughout for debugging and monitoring
- **Error isolation:** Individual scanner failures don't crash the entire run
- **Input validation:** Environment variables validated with defaults and warnings

### Accuracy
- **Region parsing:** Handles Local Zones, Wavelength, and Outposts AZ formats
- **Safe array access:** Prevents crashes on empty availability zone lists
- **Datapoint validation:** Distinguishes between missing metrics and zero usage
- **Deduplication:** Removes duplicate resources before cost estimation
- **No mutation:** Cost estimator returns new objects, doesn't modify input

### Performance
- **Batch tag fetching:** ALB/NLB tags fetched in batches (up to 20 at a time); Classic ELB tags batch-fetched by name
- **Pricing cache with TTL:** Live prices cached 1h and static fallbacks 7d under separate keys, with size limits
- **Pagination:** All scanners use paginators to handle large resource counts
- **Failed lookups not cached:** Only successful pricing lookups are cached

### Coverage
- **Network Load Balancers:** Added NLB support alongside ALB
- **Classic Load Balancers:** Added Classic ELB support, including tag fetching
- **RDS instances:** Scans both clusters and standalone instances
- **Multiple EBS types:** Live pricing distinguishes gp2, gp3, io1, io2, st1, sc1 via `volumeApiName`
- **Per-type ELB pricing:** ALB, NLB, and Classic ELB use their own live rates
- **RDS class pricing:** Stopped instances estimated by instance class instead of a flat rate
- **Unattached Elastic IPs:** New `eip_scanner` flags unassociated EIPs (flat $3.60/mo default)
- **Orphaned snapshots:** New `snapshot_scanner` flags self-owned snapshots not referenced by any AMI and past `MIN_SNAPSHOT_AGE_DAYS`
- **Compliance score:** Report and result include a compliance percentage with per-type and missing-tag breakdown
- **Live pricing:** Optional AWS Pricing API integration for current rates
- **Region support:** Explicit `AWS_REGION` config; AZ parsing handles standard, GovCloud/ISO, Local Zone, Wavelength, and Outposts formats

### Performance
- **Batch metric fetch:** Idle-EC2 detection uses CloudWatch `GetMetricData` (up to 500 queries/call) instead of one `get_metric_statistics` per instance, with 6-hour aggregation periods over the 7-day window

### Accuracy
- **Network cross-check:** EC2 instances with low CPU but meaningful network traffic are no longer flagged as idle (`NETWORK_THRESHOLD`)
- **ASG awareness:** Instances managed by an Auto Scaling Group (tag `aws:autoscaling:groupName`) are skipped

### Alerting
- **High-waste alert:** When estimated monthly waste reaches `WASTE_ALERT_THRESHOLD`, a CRITICAL log is emitted and `high_waste` is set in the handler result

### Configuration
- **CPU threshold:** Configurable via `CPU_THRESHOLD` env var
- **Network threshold:** Configurable via `NETWORK_THRESHOLD` env var
- **Snapshot age:** Configurable via `MIN_SNAPSHOT_AGE_DAYS` env var
- **Waste alert threshold:** Configurable via `WASTE_ALERT_THRESHOLD` env var
- **Required tags:** Configurable via `REQUIRED_TAGS` env var
- **Pricing modes:** Toggle between static and live pricing
- **Pricing overrides:** Partial `PRICING_JSON`/`PRICING_FILE` configs deep-merge over built-in defaults

## 🧪 Testing

Test suite with **152 tests passing** (~73% total coverage; live Pricing API and real-AWS paths are fail-open and not fully coverable with mocks).

### Run Tests

**Quick Start:**
```bash
# Unix/Mac
./scripts/run_tests.sh

# Windows
.\scripts\run_tests.ps1
```

**Manual:**
```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests with coverage (settings in .coveragerc)
pytest --cov=. --cov-report=html --cov-report=term-missing

# View coverage report
open htmlcov/index.html  # Mac
start htmlcov/index.html # Windows
```

### Test Coverage

- **Unit Tests:** Scanner modules, cost estimation, compliance
- **Integration Tests:** Full handler workflows, partial failures
- **Edge Cases:** Empty results, missing metrics, API throttling
- **Configuration:** Environment variable validation

See `tests/README.md` for detailed testing documentation.

## 📘 Operations

See [runbook.md](runbook.md) for on-call playbooks, alerts, and remediation workflows.

## 🆘 Support

- **Documentation:** See `tests/README.md`
- **Issues:** Report bugs or request features via GitHub Issues
- **Runbook:** See `runbook.md` for operational guidance

## 🎯 Roadmap

- [ ] CloudWatch Metrics integration for custom metrics
- [ ] Parallel pricing API lookups with ThreadPoolExecutor
- [ ] Automated remediation workflows
- [ ] Multi-account support via AWS Organizations
- [ ] Enhanced reporting (HTML, dashboards)
- [ ] Cost trend analysis over time
- [ ] Slack bot integration for interactive queries

## 📊 Project Stats

- **Test Count:** 152 tests passing
- **Test Coverage:** ~73% total (mocks; live-AWS paths excluded)
- **Supported Resources:** EBS, EC2, ALB, NLB, Classic ELB, RDS (stopped + idle), EIP, EBS Snapshots
- **Pricing Modes:** Static + Live (AWS Pricing API, live-primary with cached static fallback)
- **Python Version:** 3.9+