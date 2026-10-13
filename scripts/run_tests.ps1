$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python -m pytest --cov --cov-branch --cov-report=term-missing --cov-fail-under=80
exit $LASTEXITCODE
