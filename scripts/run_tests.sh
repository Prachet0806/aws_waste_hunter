#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest --cov --cov-branch --cov-report=term-missing --cov-fail-under=80
