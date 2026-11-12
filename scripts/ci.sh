#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
"$PY" -m ruff check src tests scripts
"$PY" -m ruff format --check src tests
"$PY" -m mypy
REPORT="${PYTEST_REPORT:-runs/validation/pytest.xml}"
mkdir -p "$(dirname "$REPORT")"
"$PY" -m pytest -ra --junitxml="$REPORT"
if [[ "${REQUIRE_NO_SKIPS:-0}" == "1" ]]; then
  "$PY" - "$REPORT" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
skipped = root.findall(".//testcase/skipped")
if skipped:
    raise SystemExit(f"Full CPU validation requires zero skips; found {len(skipped)}")
PY
fi
"$PY" scripts/validate_configs.py
"$PY" scripts/check_docs.py
"$PY" scripts/check_catalogs.py
if [[ "${1:-}" != "--no-build" ]]; then
  uv build
fi
