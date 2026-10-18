# Remediation implementation and release evidence

The source implements the audit remediation: structured scan evidence, timestamped metric
aggregation, conservative snapshot/EBS/LB classification, explicit unknown costs, complete
archives, conditional stage leases, isolated Standard workflow tasks, scoped infrastructure,
locked production dependencies and regression/SDK-contract tests.

## Verified local evidence (2026-10-14)

- Python 3.12.14 with the pinned runtime/development dependencies: 291 tests passed,
  one explicitly opt-in deployed staging test skipped. Combined statement/branch
  coverage was 83.21%, above the enforced 80% gate.
- Essential Ruff correctness checks and git diff whitespace checks passed.
- cfn-lint passed for the SAM template, including immutable task versions and alarms.
- pip-audit found no known vulnerabilities in the full production dependency lock.
- All deployed handlers imported in an isolated process with site packages disabled,
  using an allowlisted source bundle and exactly pinned native Windows dependencies.
  Missing dependencies were also confirmed to fail artifact validation.

The Windows import check does not validate Linux native wheels. Docker's daemon is
unavailable locally, so the Linux SAM build remains pending along with live AWS checks.
CI defines those build/import checks; its remote execution has not been verified here.

## Follow-up review items

- Failed duplicate executions now report their terminal state. A fresh retry requires
  the original idempotency key plus a new unique redrive key.
- Finalizer reads have a 2 MiB aggregate cap; oversized artifacts are explicit failed
  scanner outcomes and are not loaded into memory.
- Each attempted discovery API records status, attempts and incomplete reasons. Missing
  required calls and incomplete metric coverage mark the scanner incomplete.
- Deterministic S3 writes, conditional DynamoDB stage acknowledgments, stable SNS run IDs,
  and side-effect failure tests cover replay paths. Standard SNS remains at least once.
- An operations dashboard tracks run outcomes, scan coverage, costs and scanner failures.
- Human recommendation acceptance/rejection and realized savings lack an input workflow,
  so outcome tracking remains pending.
- Local tests and static checks pass. Linux SAM build, remote CI and AWS staging remain
  pending; the user requested local checks only.

Local source checks must be recorded from the current checkout, not inherited test counts.
Run scripts/run_tests.ps1 or scripts/run_tests.sh, cfn-lint/SAM validation, dependency audit,
artifact validation and the CI pipeline before release. Deployment is intentionally separate
from source implementation. SAM defaults scheduling to disabled.

## Required AWS release checklist (pending until verified against an account)

- [ ] Linux Python 3.12 SAM container build and artifact import smoke test.
- [ ] Deploy an approved staging account/region with separate destinations.
- [ ] Confirm SNS report and health subscriptions and actual subscriber receipt.
- [ ] Verify least-privilege discovery, pricing, report-prefix, table and KMS access.
- [ ] Confirm incomplete metrics cannot create idle findings; validate CloudWatch dimensions.
- [ ] Verify protected/shared snapshots and failed AMI discovery suppress recommendations.
- [ ] Verify active Multi-Attach volumes and Aurora members do not overlap financially.
- [ ] Review actual pricing SKUs/configurations and unpriced/fallback disclosures.
- [ ] Exercise scanner timeout, denied discovery, report rendering, SNS and S3 failure cases.
- [ ] Verify every finding survives beyond 500 rows and manifest status matches the execution.
- [ ] Exercise duplicate events and stage/publish acknowledgment retry cases.
- [ ] Test fleet size limits, API throttling, memory and finalization headroom.
- [ ] Exercise operational alarms, missing completion and maintenance suppression.
- [ ] Observe a weekly cycle and compare results with independent resource review.
- [ ] Approve production cutover and retain rollback artifacts/evidence.

## Explicit limitations

Unsupported Aurora/Multi-AZ/platform pricing remains unpriced, not an invented rate.
Billing-backed snapshot storage, commitments and realized savings are outside this baseline.
Compliance covers discovered supported resources in one account/region, not every AWS service.
The local threaded runner cannot cancel arbitrary blocked work. Production uses hard Lambda
boundaries. SNS delivery is at least once; subscriber acknowledgment is not inferred.
Slack support is not claimed. Discovery APIs may be repeated by isolated scanner tasks;
optimization must preserve their independent completeness and failure boundaries.
