#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
scanner="${GITLEAKS_BIN:-gitleaks}"
command -v "$scanner" >/dev/null 2>&1 || { echo "Install Gitleaks or set GITLEAKS_BIN" >&2; exit 2; }
mkdir -p .release
"$scanner" git --redact=100 --report-format json --report-path .release/gitleaks-history.json
# Public route IDs can match generic-api-key; inspect redacted reports.
