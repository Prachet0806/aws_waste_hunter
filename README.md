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
  - Unattached EBS Volumes  
  - Idle EC2 Instances (avg CPU < 2% over 7 days)  
  - Unused Load Balancers (ALB, NLB, Classic)
  - Stopped RDS Clusters and Instances  

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
Scanners (boto3)
├── ebs_scanner.py
├── ec2_scanner.py
├── elb_scanner.py
└── rds_scanner.py
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
4.  **Report:** Jinja2 templates generate a formatted Markdown summary.
5.  **Deliver:**
    * **SNS:** Pushes the report to subscribers (Email/Slack).
    * **S3:** Archives the report for audit history.

### 📂 Project Structure
```text
aws-waste-hunter/
├── scanner/              # Resource discovery logic (Boto3)
│   ├── ebs_scanner.py   # Unattached EBS volumes
│   ├── ec2_scanner.py   # Idle EC2 instances
│   ├── elb_scanner.py   # Unused ALB/NLB/Classic LBs
│   └── rds_scanner.py   # Stopped RDS clusters & instances
├── cost_engine/          # Financial estimation logic
│   └── estimator.py     # Cost calculation with live/static pricing
├── compliance/           # Tagging governance checks
│   └── tag_checker.py   # Configurable tag policy enforcement
├── reporting/            # Report generation
│   └── report_builder.py # Jinja2 Markdown templates
├── delivery/             # Report delivery
│   ├── sns_sender.py    # SNS notifications
│   └── s3_archiver.py   # S3 archival
├── utils/                # Shared utilities
│   ├── aws_helpers.py   # Region parsing, batching, safe access
│   └── logging_config.py # Structured logging setup
├── tests/                # Comprehensive test suite (88+ tests)
├── scripts/              # Helper scripts
│   ├── run_tests.sh     # Unix test runner
│   ├── run_tests.ps1    # Windows test runner
│   └── live_test.sh     # Integration testing
├── lambda_handler.py     # Main orchestrator with error handling
├── requirements.txt      # Production dependencies
├── setup.py              # Development installation
├── pytest.ini            # Test configuration
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
        "pricing:GetProducts",
        "sns:Publish",
        "s3:PutObject",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "*"
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
REQUIRED_TAGS=owner,env,cost-center
PRICING_MODE=live  # or 'static' (default)

# Optional pricing overrides (only used if PRICING_MODE=static):
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
- **Batch tag fetching:** ALB/NLB/RDS tags fetched in batches (up to 20 at a time)
- **Pricing cache with TTL:** Live pricing cached for 1 hour with size limits
- **Pagination:** All scanners use paginators to handle large resource counts
- **Failed lookups not cached:** Only successful pricing lookups are cached

### Coverage
- **Network Load Balancers:** Added NLB support alongside ALB
- **Classic Load Balancers:** Added Classic ELB support
- **RDS instances:** Scans both clusters and standalone instances
- **Multiple EBS types:** Pricing accounts for gp2, gp3, io1, io2, st1, sc1
- **Live pricing:** Optional AWS Pricing API integration for current rates

### Configuration
- **CPU threshold:** Configurable via `CPU_THRESHOLD` env var
- **Required tags:** Configurable via `REQUIRED_TAGS` env var
- **Pricing modes:** Toggle between static and live pricing
- **Region support:** Explicit region configuration via `AWS_REGION`

## 🧪 Testing

Comprehensive test suite with **88+ tests** and **>85% code coverage**.

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

# Run all tests with coverage
pytest --cov --cov-report=html --cov-report=term

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

- **Lines of Code:** ~2,500
- **Test Coverage:** >85%
- **Test Count:** 88+ tests
- **Supported Resources:** EBS, EC2, ALB, NLB, Classic ELB, RDS
- **Pricing Modes:** Static + Live (AWS Pricing API)
- **Python Version:** 3.9+