# Verification

Unit and SDK-contract tests are offline. An autouse fixture blocks sockets, supplies dummy
credentials and resets compatibility/cache state. botocore Stubber validates actual operation
models. Metric fixtures carry real timestamped histories; short/missing histories are tested
as insufficient evidence. Regression tests cover the audit's classification, pricing, status,
archive, size-accounting, identity, lease and workflow failures.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest --cov --cov-branch --cov-report=term-missing --cov-fail-under=80
```

Coverage includes runtime modules and orchestration. It excludes dependency/tool directories,
not difficult live-pricing code. Price catalog behavior is verified with controlled product
responses; real regional catalog/IAM behavior remains a staging gate.

Deployed tests require AWS_INTEGRATION_ENABLED=true, STAGING_ACCOUNT_ID, STAGING_REGION and
STARTER_FUNCTION_NAME containing staging. They verify caller identity before invocation and
can write S3/run records and publish SNS through the deployed pipeline. They do not create
workload fixtures or delete resources. Run explicitly with `python -m pytest -m staging`.
The staging marker bypasses dummy credential/socket fixtures only for opt-in tests.

Infrastructure checks: cfn-lint infra/template.yaml and sam validate --lint. Container build:
sam build --use-container --template-file infra/template.yaml. Inspect the resulting artifact
with scripts/validate_artifact.py. Production source smoke builds alone do not validate native
Linux dependencies or deployed permissions.
