#!/usr/bin/env bash
set -euo pipefail

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

if [[ -z "${SNS_TOPIC_ARN:-}" ]]; then
  echo "SNS_TOPIC_ARN is not set" >&2
  exit 1
fi

if [[ -z "${REPORT_BUCKET:-}" ]]; then
  echo "REPORT_BUCKET is not set" >&2
  exit 1
fi

python -c "import lambda_handler; print(lambda_handler.handler({}, {}))"
