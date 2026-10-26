# AWS Waste Hunter operational runbook

## Run health

Use Step Functions execution status, the scoped S3 manifest, EMF application metrics
and Lambda errors. Function LastModified is deployment time, never last invocation.
The starter returns accepted/execution_arn. A successful invocation alone does not
establish a complete scan or delivered notification.

```bash
aws stepfunctions describe-execution --execution-arn <execution-arn>
aws s3 cp s3://<bucket>/reports/<account>/<region>/<run-id>/manifest.json -
aws logs tail /aws/lambda/<stack>-FinalizerFunction --since 1d
```

CloudWatch Logs Insights for structured application records:

```
fields @timestamp, result.run_id, result.status, result.monthly_waste,
       result.unpriced_resources, result.scan_errors, result.delivery_errors
| filter ispresent(result.run_id)
| sort @timestamp desc
```

The stack provisions failed/partial/delivery application alarms, workflow failure/timeout alarms,
Lambda error/throttle/duration alarms, and missing completion after eight daily periods.
Application metrics have Service and Deployment dimensions. Scanner metrics add a bounded
Scanner dimension; run/resource IDs are log fields, never metric dimensions.
The Operations dashboard shows those run, coverage, pricing and scanner-health metrics.
It does not show realized savings or recommendation acceptance because no human feedback
ingestion path exists yet.
Confirm HealthEmail and ReportEmail subscriptions and exercise each alarm in staging.
A disabled schedule can produce the missing-completion alarm; suppress that alarm during
approved maintenance rather than silently considering the pipeline healthy.

## Partial and failed runs

1. Inspect scanner_outcomes and errors in findings.json and immutable attempt artifacts.
2. Inspect discovery_complete and per-operation results. Zero candidates with denied
   discovery does not establish zero waste.
3. Missing CPU/network/connections or incomplete coverage suppresses idle classification.
4. Tag-access failures are unassessed compliance, not missing-tag violations.
5. Missing prices remain visible; monthly_waste is a priced subtotal, not a complete bill.
6. Check IAM and throttling codes. Transient scanner errors retry with bounded backoff;
   permanent permission failures retain their evidence and reach finalization.

Do not increase timeouts without measuring supported fleet size and API costs. Each scanner
has a hard Lambda execution boundary; workflow timeout does not cancel the Lambda.
The synchronous local path cannot forcibly cancel blocked Python threads.

## Delivery failures and retries

Normal notification follows full JSON/Markdown archival. If archival fails, notification
explicitly states that no full archive exists. The final manifest records publication-time
outcomes. If manifest archival itself fails, the run fails and telemetry/history are authoritative.
A notification acknowledgment proves SNS publication, not subscriber receipt.

DynamoDB leases prevent overlapping stage ownership. Completed stages return cached results;
scanner attempts use unique artifact keys. Conditional writes prevent stale owners from
acknowledging another attempt. Scanner and finalizer exceptions release owned leases;
transient persistence errors retry with bounded backoff. Scanner analysis errors retain
attempt evidence before retry. A release failure preserves the original error and lease expiry.
An abrupt timeout can leave a lease until expiry; investigate before replaying.
SNS is standard at-least-once delivery and may duplicate after an ambiguous publish
acknowledgment. The stable run_id message attribute lets automated subscribers deduplicate;
human email subscribers should check the run ID before acting.

Repeating the same event identity returns its current outcome. A terminal failed execution
is reported as failed with redrive_required and is not acknowledged as a healthy duplicate.
For an intentional fresh retry, invoke the starter with the same idempotency_key and a
new unique redrive_key. That creates an independent run with new leases and artifacts;
reusing that redrive_key returns the same execution. A successful duplicate returns
completed and does not publish a second report.

The finalizer checks artifact size before reading and has a configurable aggregate budget
from 1 KiB to 2 MiB. Oversized artifacts become explicit failed scanner results while the
finalizer continues with the remaining bounded artifacts and produces a partial report.

## Financial review

Prices are list-price opportunities. Check provenance, supported deployment model, unit,
components, fallback notes and unpriced counts. Never use the subtotal as an invoice,
commitment-adjusted savings figure or proof that cleanup saves that exact amount.
Stopped RDS compute is excluded; storage/IOPS require verified rates and quantities.
Snapshot capacity is not billed incremental storage and deleting a snapshot may preserve
shared blocks. Verify costs against billing-backed data before financial commitments.

## Retained data and cleanup

The report bucket and run table use `Retain` deletion policies. Deleting or replacing the
stack therefore does not delete either resource. The bucket is versioned: current report
objects expire after the configured `RetentionDays` (365 by default), and noncurrent
versions expire after 30 days. Lifecycle expiration is asynchronous and must not be treated
as proof that every version or delete marker has disappeared.

For a planned cleanup, the service owner should first confirm the AWS account, region,
stack, bucket and table names; export any manifests and run records subject to audit or
legal retention; obtain the data owner's retention approval; and inspect the bucket's
current lifecycle rules and object versions. Review explicit run prefixes and version IDs
before any manual deletion. Do not use recursive bucket deletion or remove the table as a
shortcut. Confirm lifecycle completion and billing after the approved retention window,
then remove the retained bucket/table only through an approved change process. The runtime
has no S3 or DynamoDB deletion permissions; cleanup is a separate operator responsibility.

## Candidate review before human cleanup

- EC2/RDS: confirm a full observation window, production dependencies, scheduled jobs,
  recovery roles and owner approval. Observe replicas and members individually.
- EBS: verify every attachment and retention. Preserve required recovery copies.
- Snapshots: verify backup/lifecycle ownership, protection tags, sharing, disabled/deprecated
  AMIs, retention, recovery and cross-account dependencies. AMI absence is insufficient.
- LBs: verify protocol metrics, listeners, targets, DNS, fixed responses, rejected requests
  and owner approval. Structural emptiness is a review candidate, not a deletion authorization.
- EIPs: verify association and external allowlist dependencies before releasing addresses.

The application grants and performs no deletion, stop or retagging actions.
Backup snapshots created during a separately approved cleanup must carry a protected tag
and satisfy the organization's retention policy; do not remove them based on age alone.

## Staging, cutover and rollback

Use a distinct staging stack, disable its schedule initially, confirm report/health subscribers,
and run the documented failure/coverage/financial/delivery cases. Test at the declared maximum
fleet size and beyond it. Compare candidates to independently reviewed AWS resources.
Observe one weekly cycle plus targeted runs; verify actual alarms and subscription receipt.

Enable production scheduling only after the release checklist passes. Disable the old schedule
before enabling the new one to avoid duplicate weekly reporting. Keep prior deployment artifacts.
For rollback, disable the new schedule, restore the previously reviewed execution target, and
retain diagnostics. The old version has known classification/cost defects; keep warnings and
operational alarms rather than restoring misleading cleanup recommendations.
Reports and the run table have Retain policies; avoid destroying evidence during rollback.
