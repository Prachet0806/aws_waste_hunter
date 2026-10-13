# AWS Waste Hunter

A read-only AWS discovery and FinOps reporting pipeline. Version 3 uses a version 2
result schema. It identifies review candidates, records evidence gaps, estimates a
priced list-price subtotal, archives complete findings, and sends an SNS summary.
It never deletes, stops, or retags scanned resources.

## Production architecture

EventBridge -> starter Lambda -> Step Functions Standard -> isolated scanner Lambda tasks
-> S3 attempt artifacts -> finalizer Lambda -> complete JSON/Markdown archive -> SNS summary
-> final manifest and conditional DynamoDB acknowledgment.

The workflow runs seven scanner tasks with concurrency four. Workflow definitions pin
immutable scanner/finalizer Lambda versions, so in-flight executions retain their code. Each scanner has a
270-second Lambda timeout and a 300-second workflow timeout. The finalizer has a
separate 240-second Lambda timeout. Workflow task timeout does not cancel a running
Lambda; the function timeout is the hard execution boundary. Local threaded scans
use a shared deadline and isolated evidence but cannot kill arbitrary blocked threads.

## Supported discovery and limitations

- EC2: seven-day CPU plus both network metrics; recent/incomplete histories are skipped.
- EBS: available volumes and volumes whose every attachment is on a stopped instance.
- Elastic IPs: unassociated addresses using the nonpageable DescribeAddresses API.
- ALB/NLB/Classic LB: corroborated traffic or structural review candidates; sparse data
  alone is insufficient. DNS/dependency review and owner approval remain necessary.
- RDS: stopped clusters/instances and idle running instances. Cluster compute is never
  counted again alongside its members.
- Snapshots: old snapshots unreferenced by owned AMIs, including disabled/deprecated AMIs.
  Backup/DLM/protection tags and shared snapshots are excluded. This is not proof that
  a snapshot is disposable; retention, recovery and cross-account dependencies require review.

Scope is one account and configured region. Inventory compliance includes active resources
returned by these discovery operations, not all AWS services. Failed tag retrieval is
unassessed rather than a missing-tag violation. Discovery completeness and unknown costs
are always separate from zero findings.

## Financial interpretation

`monthly_waste` is a legacy field name for the priced monthly list-price subtotal.
It is not an AWS invoice or realized savings. Unknown prices are null on individual rows;
`unpriced_resources` and `pricing_complete` disclose gaps. A subtotal below the alert
threshold cannot establish low waste when coverage or pricing is incomplete; `high_waste`
is then null. No usable scan/estimation produces a null subtotal.

Static defaults are illustrative approximations, not dated current regional quotes.
Unknown EC2 types are unpriced. Live catalog prices require an unambiguous SKU/unit match.
Linux/shared/on-demand EC2 and supported single-AZ RDS engines form the compute baseline.
Aurora, Multi-AZ and unsupported configurations remain explicitly unpriced until a
supported deployment-specific rate is supplied/implemented. Commitments and discounts
are excluded. LB base-rate estimates exclude reserved capacity and usage charges.

Stopped RDS compute is excluded. Optional `RDS_STORAGE`/`RDS_IOPS` configuration can price
known provisioned storage; backup attribution remains incomplete. Snapshot VolumeSize is
never used as billed incremental storage. Snapshot costs remain unknown unless
`billed_storage_gb` is supplied with trustworthy billing evidence.

Pricing overrides support scalar legacy EBS rates or per-type rates, plus optional
`EBS_IOPS`, `EBS_THROUGHPUT`, `RDS_STORAGE` and `RDS_IOPS` tables.
Exact deployment overrides can use `EC2_CONFIGS` keys
`region|platform|tenancy|purchasing|instance_type` and `RDS_CONFIGS` keys
`region|engine|instance_class|Single-AZ` (or `Multi-AZ`); their values are verified
USD/month compute rates and are labeled configured-deployment-rate. Quantities, units and
finite nonnegative rates are validated. Example (illustrative, operator must verify):

```json
{"EBS":{"gp3":0.08},"EBS_IOPS":{"gp3":0.005},"EBS_THROUGHPUT":{"gp3":0.04}}
```

## Development

Use Python 3.12. Install into a virtual environment rather than the source directory:

```bash
python -m venv .venv
# Activate .venv for your shell
python -m pip install -r requirements-dev.txt
python -m pytest --cov --cov-branch --cov-report=term-missing --cov-fail-under=80
```

The production dependency graph is pinned in `requirements.lock`; test dependencies
are separate. Unit tests block network access. SDK-contract tests use botocore Stubber.
Staging tests are skipped unless explicitly enabled. The test scripts run the same
coverage gate; they do not silently install dependencies.

## Build and deploy

Install Docker and AWS SAM CLI. Build native dependencies inside the Lambda Linux image:

```bash
sam validate --lint --template-file infra/template.yaml
sam build --use-container --template-file infra/template.yaml
python scripts/validate_artifact.py .aws-sam/build/FinalizerFunction
sam deploy --guided --template-file .aws-sam/build/template.yaml
```

The allowlisted builder excludes .env, Git metadata, AWS credentials, tests and coverage.
Never install dependencies into the source tree or zip the entire checkout.
The stack defaults to a disabled schedule. Use a distinct staging stack and configure
`ReportEmail` and `HealthEmail`; confirm the SNS subscriptions. `KmsKeyArn` optionally
selects a customer-managed S3 key. `PricingJson`, `RequiredTags` and
`ExcludedTagsJson` configure verified rates and governance policy. Reports and run state are retained on stack deletion.
Review retention requirements before deployment; default artifact retention is 365 days.

`AWS_REGION` and `AWS_DEFAULT_REGION` are runtime-provided reserved Lambda variables.
Use `SCAN_REGION` for application configuration. Infrastructure grants reporting writes
only to this stack's bucket prefix, table and report topic. Necessary describe/list
operations use wildcard resources where AWS requires them. No cleanup permissions exist.

## Configuration

See `env.example`. Production values are provisioned by SAM; local .env files are not
loaded automatically. Missing or invalid configuration is an operational failure.
The deployed workflow snapshots validated scan settings at execution start.
`PRICING_MODE` selects `static` or `live`. `MIN_METRIC_COVERAGE` defaults to 0.9;
`MIN_OBSERVATION_DAYS` defaults to seven. Exemptions are explicit tag/value rules.
Inventory has a configurable cap and exceeding it marks incomplete coverage.

## Results and artifacts

Version 2 results preserve compatible legacy field names while correcting their semantics:

- status: ok, partial or failed, based on useful stages and actual delivery outcomes.
- resources: discovered candidate count; priced_resources is separate.
- inventory_resources and compliance assessed/unassessed counts describe the denominator.
- no_metrics covers missing metric evidence; skipped_resources includes intentional exclusions.
- scanner_outcomes records every requested scanner.
- archive_uri points to the actual full report; delivery records archive/notification outcomes.

Scheduled invocation returns `accepted`, run_id and execution_arn, not a final report.
Manual `lambda_handler.handler` remains synchronous. This is an intentional interface
change; migrate consumers to workflow output or the final manifest.

Artifacts are under `reports/<account>/<region>/<run_id>/`:

- attempts/<scanner>/<lease>.json: immutable evidence for each attempt.
- findings.json: full scanner outputs, priced findings and compliance.
- report.md: complete ranked report, never truncated at 500 rows.
- manifest.json: final delivery/application outcomes known when it was written.

SNS receives a bounded summary with an exact S3 reference. Reports are generated before
notification; their own future delivery outcome lives in the final manifest, not the report.
Standard SNS delivery is at least once. A publish can succeed before acknowledgment is
persisted; run IDs make duplicates recognizable, not impossible.

## Staging and release

See `runbook.md` and `NEXT_STEPS_PLAN.md`. Opt-in deployed tests invoke a named staging
stack and can send reports; they create no discovery fixtures or delete resources:

```bash
export AWS_INTEGRATION_ENABLED=true
export STAGING_ACCOUNT_ID=<approved-account>
export STAGING_REGION=<region>
export STARTER_FUNCTION_NAME=<stack-containing-staging>-StarterFunction
python -m pytest -m staging -o addopts=
```

Live deployment, workload-specific pricing, Linux packaging and a complete weekly
observation cycle must be verified before production promotion. Source tests do not
prove AWS permissions, subscriber receipt, real workload classification or realized savings.
Slack is not a supported direct transport in this release; SNS email is supported.
