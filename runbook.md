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
2.  **Verify EventBridge:**
    * Ensure the Rule `sre-waste-hunter-weekly` triggered successfully.
3.  **Validate Permissions:**
    * Check CloudTrail for `AccessDenied` events related to the Lambda execution role.

**Common Failures Matrix:**

| Error Message | Probable Cause | Remediation |
| :--- | :--- | :--- |
| `AccessDenied` | IAM Role missing permissions | Add missing action (e.g., `ec2:DescribeVolumes`) to IAM Policy. |
| `Task timed out` | Scanning takes too long | Increase Lambda Timeout (Default 3s → 30s or 60s). |
| `Endpoint request timed out` | API Throttling | Implement exponential backoff in Boto3 or run in a different region. |
| `KeyError: 'Tags'` | Resource has no tags | Ensure code handles empty tag lists gracefully (Fixed in v1.1). |

---

### Alert 2: "S3 Access Denied"
**Symptom:** Report delivered via SNS but failed to archive to S3.
**Fix:**
1.  Check `REPORT_BUCKET` environment variable matches the actual bucket name.
2.  Verify Lambda Role has `s3:PutObject` permission for that specific bucket ARN.

---

## 🛠️ Remediation Playbooks (Waste Cleanup)
*Note: The bot **reports** waste. Humans (or future automation) must **resolve** it.*

### 💾 1. Unattached EBS Volumes
* **Check:** Is the volume state `available`?
* **Action:**
    1.  Create a final snapshot (tag: `waste-hunter-backup`).
    2.  Delete the volume.

### 🖥️ 2. Idle EC2 Instances
* **Check:** CPU < 2% for 7 days.
* **Verification:** Check `NetworkIn/Out`. If network traffic is high, it might be a proxy or relay server (do not delete).
* **Action:**
    1.  Stop the instance.
    2.  Notify the `Owner` tag email.
    3.  Terminate after 7 days if no response.

### 🗄️ 3. Stopped RDS Clusters
* **Check:** Storage costs continue even when stopped.
* **Action:**
    1.  Take a manual snapshot.
    2.  Delete the cluster.

---

## 📝 Postmortem Template
If the bot fails to run for > 2 weeks, perform a mini-postmortem.

* **Summary:** (e.g., Bot failed due to API throttling on large account)
* **Root Cause:** (e.g., Default boto3 retry limit reached)
* **Resolution:** (e.g., Added `config=Config(retries={'max_attempts': 10})`)
* **Prevention:** (e.g., Add monitoring on Lambda `Duration` metric)

---

## 📞 Ownership & Escalation
* **Primary On-Call:** SRE Intern / Junior SRE
* **Escalation:** Platform Team Lead
* **Repository:** `github.com/org/aws-waste-hunter`