# 📘 SRE Runbook: AWS Waste Hunter Bot

**Service Name:** `aws-waste-hunter-lambda`
**Service Owner:** Platform Engineering / SRE
**Severity:** SEV-4 (Internal Tooling / Cost Optimization)

---

## 🎯 Purpose
This operational playbook outlines the troubleshooting steps, alert responses, and remediation workflows for the **AWS Waste Hunter** automation bot. Use this guide when the automation fails or when high waste is detected.

---

## 🚨 Alerts & Troubleshooting

### Alert 1: Lambda Failure / No Report Received
**Symptom:** The weekly report was not delivered to SNS, or the Lambda `Errors` metric in CloudWatch > 0.
**Severity:** Medium (Degraded Cost Visibility)

**Diagnosis Workflow:**
1.  **Check CloudWatch Logs:**
    * Navigate to Log Group: `/aws/lambda/aws-waste-hunter`.
    * Filter for `ERROR`, `CRITICAL`, or `Traceback`.
    * Look for structured log entries with error details.
2.  **Verify EventBridge:**
    * Ensure the Rule `sre-waste-hunter-weekly` triggered successfully.
    * Check EventBridge Metrics for failed invocations.
3.  **Validate Permissions:**
    * Check CloudTrail for `AccessDenied` events related to the Lambda execution role.
4.  **Check Handler Response:**
    * Lambda returns status: `ok`, `partial`, or `failed` (`failed` = nothing estimated plus delivery errors)
    * Check `scan_errors` and `delivery_errors` counts in response
    * Check `no_metrics` count for resources skipped fail-open due to missing/errored CloudWatch data
    * Check `high_waste` flag (waste >= `WASTE_ALERT_THRESHOLD`) and `compliance_percent`

**Common Failures Matrix:**

| Error Message | Probable Cause | Remediation |
| :--- | :--- | :--- |
| `AccessDenied` | IAM Role missing permissions | Add missing action (e.g., `ec2:DescribeVolumes`, `ec2:DescribeAddresses`, `ec2:DescribeSnapshots`, `ec2:DescribeImages`, `rds:DescribeDBInstances`, `cloudwatch:GetMetricData`, `pricing:GetProducts`) to IAM Policy. |
| `Task timed out` / scanner `TimeoutError` | Scanning takes too long | Increase Lambda Timeout (Default 3s → 300s recommended). Increase Memory to 512MB. Scanners run in parallel with per-scanner timeouts and fail open. |
| `Endpoint request timed out` | API Throttling | Handled with adaptive retries (max 5 attempts). Check if retry limit reached. |
| `ValueError: SNS_TOPIC_ARN is not set` | Missing env var | Set required environment variables in Lambda configuration (see `env.example`). |
| `ValueError: REPORT_BUCKET ...` | Bucket misconfigured | `REPORT_BUCKET` must be a bucket name, not an ARN or path. |
| `Region parsing failed` / `region is None` | Unknown AZ format | Malformed AZs now resolve to `None` and are logged; estimator defaults pricing region to `us-east-1`. |
| High `no_metrics` count | Missing CloudWatch data/permissions | Resources are skipped fail-open and logged. Verify `cloudwatch:GetMetricData` / `GetMetricStatistics` permissions and detailed monitoring. |

---

### Alert 2: "S3 Access Denied"
**Symptom:** Report delivered via SNS but failed to archive to S3.
**Status:** `partial` (or `failed` if nothing was estimated) with `delivery_errors >= 1`

**Fix:**
1.  Check `REPORT_BUCKET` environment variable matches the actual bucket name (not ARN, no `/` path).
2.  Verify Lambda Role has `s3:PutObject` permission for that specific bucket ARN.
3.  Check S3 bucket policy doesn't deny Lambda role access.
4.  If the bucket enforces SSE-KMS/Object Lock, set `S3_SSE=aws:kms` + `S3_KMS_KEY_ID` (default archive uses `AES256`).

### Alert 3: Partial/Failed Scan Completion
**Symptom:** Handler returns `status: "partial"` or `"failed"` with non-zero error counts.
**Severity:** Low-Medium (Some data collected, but incomplete); `failed` means nothing usable was produced.

**Diagnosis:**
1.  **Check scan_errors count:**
    * Indicates individual scanner failures (EBS, EC2, ELB, stopped RDS, idle RDS, EIP, snapshots)
    * Review CloudWatch Logs for specific scanner errors (search `Fail-open skip` for metric skips)
2.  **Check delivery_errors count:**
    * Indicates SNS or S3 delivery failures
    * Report was generated but not delivered
3.  **Review error details:**
    * Errors are included in the report itself under "Scan/Delivery Errors" section
    * Check report in S3 if SNS failed, or logs if S3 failed

**Common Causes:**
- API throttling on one service
- Temporary network issues
- Missing permissions for specific service
- Resource in unsupported region

**Action:**
- If one scanner fails but others succeed, report is still useful
- Fix the root cause and wait for next run
- Manual rerun if immediate data needed: `aws lambda invoke --function-name aws-waste-hunter`

### Alert 4: High Monthly Waste Detected
**Symptom:** Report shows waste >= `$WASTE_ALERT_THRESHOLD`/month (default $1000), `high_waste: true`, CRITICAL log.
**Severity:** High (Cost Impact)

**Action:**
1.  Review waste breakdown by resource type
2.  Identify top 5 most expensive resources
3.  Contact resource owners via tags
4.  Create remediation tickets
5.  Set deadline for cleanup (e.g., 7 days)
6.  Follow up in next weekly report

### Alert 5: Pricing API Failures
**Symptom:** Logs show "Pricing API call failed" warnings.  
**Impact:** Falls back to static pricing

**Diagnosis:**
1.  Check if `PRICING_MODE=live` is set
2.  Verify IAM role has `pricing:GetProducts` permission
3.  Check Pricing API service health
4.  Review pricing cache hit rate in logs

**Fix:**
- Pricing API only available in `us-east-1` region for client
- Ensure Lambda has internet access (VPC with NAT if in VPC)
- Falls back to static pricing automatically, so reports still work
- Consider staying on static mode if live pricing unreliable

---

## 🛠️ Remediation Playbooks (Waste Cleanup)
*Note: The bot **reports** waste. Humans (or future automation) must **resolve** it.*

### 💾 1. Unattached EBS Volumes
**Risk:** Low (Data only, no compute impact)  
**Cost Impact:** $0.10/GB/month (gp3)

**Verification:**
```bash
aws ec2 describe-volumes --volume-ids vol-xxxxx --query 'Volumes[0].State'
# Should return: "available"
```

**Action:**
1.  Create final snapshot (tag: `waste-hunter-backup`, `original-volume-id`):
    ```bash
    aws ec2 create-snapshot --volume-id vol-xxxxx \
      --description "Backup before deletion by waste-hunter" \
      --tag-specifications 'ResourceType=snapshot,Tags=[{Key=waste-hunter-backup,Value=true}]'
    ```
2.  Wait for snapshot completion
3.  Delete the volume:
    ```bash
    aws ec2 delete-volume --volume-id vol-xxxxx
    ```

### 🖥️ 2. Idle EC2 Instances
**Risk:** Medium (Possible service impact)  
**Cost Impact:** Varies by instance type

**Verification:**
1.  Check CPU < 2% for 7 days (already done by scanner)
2.  Check network metrics to rule out proxies/relays:
    ```bash
    aws cloudwatch get-metric-statistics \
      --namespace AWS/EC2 \
      --metric-name NetworkIn \
      --dimensions Name=InstanceId,Value=i-xxxxx \
      --start-time $(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%S) \
      --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
      --period 86400 \
      --statistics Sum
    ```
3.  Review instance tags for purpose/owner
4.  Check if part of Auto Scaling Group

**Action:**
1.  Contact owner via `owner` tag
2.  Stop instance (test impact):
    ```bash
    aws ec2 stop-instances --instance-ids i-xxxxx
    ```
3.  Wait 7 days for feedback
4.  Terminate if confirmed unused:
    ```bash
    aws ec2 terminate-instances --instance-ids i-xxxxx
    ```

### 🌐 3. Unused Load Balancers
**Risk:** Low (No traffic detected)  
**Cost Impact:** $18-22/month per ALB/NLB

**Verification:**
1.  Confirm zero requests for 7 days (already done by scanner)
2.  Check target health (should be none or unhealthy):
    ```bash
    aws elbv2 describe-target-health --target-group-arn <tg-arn>
    ```
3.  Review DNS records pointing to LB

**Action:**
1.  Document any DNS records
2.  Delete load balancer:
    ```bash
    aws elbv2 delete-load-balancer --load-balancer-arn <lb-arn>
    ```
3.  Delete associated target groups if orphaned

### 🔌 4a. Unattached Elastic IPs
**Risk:** Low (no compute impact)
**Cost Impact:** ~$3.60/month per unassociated EIP

**Verification:**
```bash
aws ec2 describe-addresses --allocation-ids eipalloc-xxxxx \
  --query 'Addresses[0].AssociationId'
# Should return: null/empty (unassociated)
```

**Action:**
1.  Confirm no association (InstanceId/NetworkInterfaceId empty).
2.  Release the address:
    ```bash
    aws ec2 release-address --allocation-id eipalloc-xxxxx
    ```

### 📸 4b. Orphaned EBS Snapshots
**Risk:** Low (not referenced by any AMI, past `MIN_SNAPSHOT_AGE_DAYS`)
**Cost Impact:** ~$0.05/GB/month

**Verification:**
```bash
aws ec2 describe-snapshots --snapshot-ids snap-xxxxx \
  --query 'Snapshots[0].StartTime'
# Confirm age exceeds MIN_SNAPSHOT_AGE_DAYS and no AMI references it
```

**Action:**
1.  Confirm no AMI references the snapshot.
2.  Delete the snapshot:
    ```bash
    aws ec2 delete-snapshot --snapshot-id snap-xxxxx
    ```

### 😴 4c. Idle RDS Instances/Clusters
**Risk:** Medium (running workloads with low CPU/connections over 7 days)
**Cost Impact:** Full instance rate (see report)

**Verification:**
1.  Check avg CPU < `RDS_CPU_THRESHOLD` (default 5%) and peak connections <= `RDS_CONNECTIONS_THRESHOLD` (default 1) over 7 days.
2.  Confirm with application owners before stopping.

**Action:**
1.  Contact owner via tags.
2.  Stop or downsize after approval.

### 🗄️ 4d. Stopped RDS Clusters/Instances
**Risk:** Medium (Data preservation needed)  
**Cost Impact:** Storage costs continue when stopped

**Verification:**
```bash
aws rds describe-db-clusters --db-cluster-identifier <id> \
  --query 'DBClusters[0].Status'
# Should return: "stopped"
```

**Action:**
1.  Take final snapshot:
    ```bash
    aws rds create-db-cluster-snapshot \
      --db-cluster-snapshot-identifier waste-hunter-backup-<id> \
      --db-cluster-identifier <id>
    ```
2.  Wait for snapshot completion
3.  Delete cluster:
    ```bash
    aws rds delete-db-cluster \
      --db-cluster-identifier <id> \
      --skip-final-snapshot
    ```

### 🏷️ 5. Tagging Violations
**Risk:** Low (Governance only)  
**Impact:** Cost attribution unclear

**Action:**
1.  Contact resource owner if `owner` tag missing
2.  Add missing tags:
    ```bash
    aws ec2 create-tags \
      --resources <resource-id> \
      --tags Key=owner,Value=<email> Key=env,Value=prod Key=cost-center,Value=<code>
    ```
3.  Update resource creation templates to include required tags

---

## 📝 Postmortem Template
If the bot fails to run for > 2 weeks, perform a mini-postmortem.

* **Summary:** (e.g., Bot failed due to API throttling on large account)
* **Root Cause:** (e.g., Default boto3 retry limit reached)
* **Resolution:** (e.g., Tuned shared `BOTO3_CONFIG` adaptive retries, currently max 5 attempts with 5s connect / 10s read timeouts)
* **Prevention:** (e.g., Add monitoring on Lambda `Duration` metric)

---

## 📊 Monitoring & Observability

### CloudWatch Log Insights Queries

**1. Error Summary:**
```
fields @timestamp, @message
| filter @message like /ERROR/
| stats count() by @message
```

**2. Scanner Performance:**
```
fields @timestamp, @message
| filter @message like /Found.*resources/
| parse @message "Found * *" as count, type
| sort @timestamp desc
```

**3. Partial Failure Rate:**
```
fields @timestamp
| filter @message like /status.*partial/
| stats count() as partial_runs
```

**4. Monthly Waste Trend:**
```
fields @timestamp, monthly_waste
| filter @message like /monthly_waste/
| sort @timestamp desc
| limit 52
```

### Recommended CloudWatch Alarms

1. **Lambda Errors > 0**
2. **Lambda Duration > 240s** (approaching 300s timeout)
3. **Lambda Throttles > 0**
4. **Monthly Waste > $1000** (custom metric if implemented)

### Health Checks

Run weekly health check:
```bash
# Check Lambda function exists
aws lambda get-function --function-name aws-waste-hunter

# Check EventBridge rule
aws events describe-rule --name sre-waste-hunter-weekly

# Verify last invocation
aws lambda get-function --function-name aws-waste-hunter \
  --query 'Configuration.LastModified'

# Check recent logs
aws logs tail /aws/lambda/aws-waste-hunter --since 1w
```

## 🧪 Testing

### Integration Test

```bash
# Set test env vars
export SNS_TOPIC_ARN=arn:aws:sns:...
export REPORT_BUCKET=test-bucket
export PRICING_MODE=static
export CPU_THRESHOLD=2

# Run handler locally
python -c "import lambda_handler; print(lambda_handler.handler({}, {}))"
```

### Manual Invocation

```bash
aws lambda invoke \
  --function-name aws-waste-hunter \
  --log-type Tail \
  --payload '{}' \
  /tmp/response.json

cat /tmp/response.json
```

## 📞 Ownership & Escalation

* **Primary On-Call:** SRE Team
* **Escalation:** Platform Team Lead → Cloud Architect
* **Documentation:** See `README.md`, `tests/README.md`, `NEXT_STEPS_PLAN.md`
* **Full env reference:** `env.example` (copy to `.env`, never commit `.env`)
* **Slack Channel:** `#sre-finops`

## 📚 Additional Resources

- **Test Documentation:** `tests/README.md`
- **AWS Documentation:** 
  - [Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
  - [Pricing API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html)
  - [CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html)