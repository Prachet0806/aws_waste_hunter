#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_INTEGRATION_ENABLED:?explicit staging opt-in required}"
: "${STAGING_ACCOUNT_ID:?designated staging account required}"
: "${STAGING_REGION:?designated staging region required}"
: "${STARTER_FUNCTION_NAME:?staging starter function required}"
python -m pytest -m staging -o addopts=
