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
  - Unused Application Load Balancers (ALBs)  
  - Stopped RDS Clusters  

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
├── cost_engine/          # Financial estimation logic
├── compliance/           # Tagging governance checks
├── reporting/            # Jinja2 report templates
├── delivery/             # SNS and S3 integration
├── lambda_handler.py     # Main Orchestrator
├── requirements.txt      # Dependencies
├── README.md             # Documentation
└── runbook.md            # Operational Playbook
```
---

## ⚙️ Setup & Deployment

### Prerequisites
- Python 3.9+  
- AWS CLI configured  
- boto3, jinja2  

### 1. Infrastructure Setup

Create:

**IAM Role for Lambda**  
Permissions:
ec2:Describe*
elasticloadbalancing:Describe*
rds:Describe*
cloudwatch:GetMetricStatistics
sns:Publish
s3:PutObject
logs:CreateLogGroup
logs:CreateLogStream
logs:PutLogEvents
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
SNS_TOPIC_ARN=arn:aws:sns:us-east-1:123456789012:sre-cost-alerts
REPORT_BUCKET=sre-finops-reports
CPU_THRESHOLD=2
# Optional pricing overrides:
# PRICING_MODE=live
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

## 📘 Operations

See [runbook.md](runbook.md) for on-call playbooks, alerts, and remediation workflows.