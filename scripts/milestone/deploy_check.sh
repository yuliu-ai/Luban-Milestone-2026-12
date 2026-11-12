#!/usr/bin/env bash
# Indicator 6.1 / 1.7: anonymous clone, clean install and core-function check.
# Usage: bash deploy_check.sh <repository-url> <empty-work-dir> [git-ref]
# Writes <work-dir>/deploy_result.json; exit code 0 only when every step passed.
set -uo pipefail
REPO_URL=${1:?repository url}
WORK=${2:?work directory}
REF=${3:-main}
mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
LOG="$WORK/deploy.log"
RESULT="$WORK/deploy_result.json"
START=$(date +%s)
steps=()
fail=0

step() {
  local name=$1; shift
  local t0=$(date +%s)
  if "$@" >>"$LOG" 2>&1; then status=passed; else status=failed; fail=1; fi
  steps+=("{\"step\":\"$name\",\"status\":\"$status\",\"seconds\":$(( $(date +%s) - t0 ))}")
  echo "[$status] $name" | tee -a "$LOG"
}

export GIT_TERMINAL_PROMPT=0   # anonymous: never ask for credentials
command -v uv >/dev/null || step install_uv sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
export PATH="$HOME/.local/bin:$PATH"
step clone git clone --depth 1 --branch "$REF" "$REPO_URL" "$WORK/src"
cd "$WORK/src" || exit 1
COMMIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)
step install uv sync --frozen --group dev
step offline_example .venv/bin/python -m luban.examples.offline --output-dir "$WORK/offline"
step health .venv/bin/luban health --agent-config configs/agents/offline.yaml
step core_tests .venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_openvla_interface_compat.py tests/unit/test_service.py

PORT=$(.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
.venv/bin/luban serve --port "$PORT" >"$WORK/serve.log" 2>&1 &
SERVER=$!
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.2; done
step service_call sh -c "curl -sf -X POST -H 'Content-Type: application/json' -d '{\"task\":{\"name\":\"increment\"}}' http://127.0.0.1:$PORT/v1/episodes/run | grep -q '\"success\": true'"
step service_faults .venv/bin/python scripts/milestone/platform_check.py faults --url "http://127.0.0.1:$PORT" --output "$WORK/faults"
kill "$SERVER" 2>/dev/null; wait "$SERVER" 2>/dev/null

joined=$(IFS=,; echo "${steps[*]}")
cat >"$RESULT" <<JSON
{"repository": "$REPO_URL", "ref": "$REF", "commit": "$COMMIT", "host": "$(hostname)",
 "python": "$(.venv/bin/python -V 2>&1)", "seconds": $(( $(date +%s) - START )),
 "passed": $([ $fail -eq 0 ] && echo true || echo false), "steps": [$joined]}
JSON
cat "$RESULT"
exit $fail
