#!/usr/bin/env bash
# 启动一个 XR-1 policy worker。
#
# 用法：launch_xr1_worker.sh <物理GPU索引> <端口> <标签>
#
# 约定：
#   - 只用 CUDA_VISIBLE_DEVICES 选卡（实测 render_gpu_device_id 不决定物理卡）；
#   - 服务内固定 --device cuda:0，因为可见设备已被收窄为一张；
#   - 记录 PID 与 PGID，只允许停止本脚本启动的进程；
#   - 启动前探测端口，冲突即退出，不抢用别人的端口。
set -euo pipefail

GPU_INDEX="${1:?用法: launch_xr1_worker.sh <GPU索引> <端口> <标签>}"
PORT="${2:?缺少端口}"
TAG="${3:?缺少标签}"

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
XR1_PYTHON="${XR1_PYTHON:?Set XR1_PYTHON to the upstream policy interpreter}"
MODEL_PATH="${XR1_MODEL_PATH:?Set XR1_MODEL_PATH to the local checkpoint directory}"
RUN_DIR="${PROJECT}/runs/xr1_workers/${TAG}"
PID_FILE="${RUN_DIR}/worker.pid"
LOG_FILE="${RUN_DIR}/worker.log"

if [[ "${TAG}" == *"/"* || "${TAG}" == "." || "${TAG}" == ".." ]]; then
  echo "TAG must be a single directory name" >&2
  exit 2
fi
[[ -x "${XR1_PYTHON}" && -d "${MODEL_PATH}" ]] || { echo "Interpreter/checkpoint missing" >&2; exit 2; }
mkdir -p "${RUN_DIR}"

if [ -f "${PID_FILE}" ]; then
  OLD_PID="$(cat "${PID_FILE}")"
  if kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "已有 worker 在运行：PID=${OLD_PID}，先停止或换标签" >&2
    exit 3
  fi
fi

if "${XR1_PYTHON}" - "${PORT}" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket()
sock.settimeout(1.0)
in_use = sock.connect_ex(("127.0.0.1", port)) == 0
sock.close()
sys.exit(0 if in_use else 1)
PY
then
  echo "端口 ${PORT} 已被占用，拒绝启动" >&2
  exit 4
fi

cd "${PROJECT}"
export PYTHONPATH=src
export CUDA_VISIBLE_DEVICES="${GPU_INDEX}"
export TOKENIZERS_PARALLELISM=false

setsid nohup "${XR1_PYTHON}" scripts/serve_xiaomi_policy.py \
  --model-path "${MODEL_PATH}" \
  --host 127.0.0.1 \
  --port "${PORT}" \
  --device cuda:0 \
  --robot-type robocasa365 \
  --observation-history 4 \
  --observation-interval 2 \
  --instruction-mode task_graph \
  --policy-seed-mode episode_step \
  >> "${LOG_FILE}" 2>&1 &

SERVICE_PID=$!
echo "${SERVICE_PID}" > "${PID_FILE}"

PGID="$(ps -o pgid= -p "${SERVICE_PID}" | tr -d ' ')"
echo "${PGID}" > "${RUN_DIR}/worker.pgid"
echo "started pid=${SERVICE_PID} pgid=${PGID} gpu=${GPU_INDEX} port=${PORT} log=${LOG_FILE}"

for _ in $(seq 1 180); do
  if ! kill -0 "${SERVICE_PID}" 2>/dev/null; then
    echo "服务进程已退出，见 ${LOG_FILE}" >&2
    exit 5
  fi
  if grep -q '"status": "ready"' "${LOG_FILE}" 2>/dev/null; then
    echo "ready"
    exit 0
  fi
  sleep 2
done

echo "等待健康标记超时，见 ${LOG_FILE}" >&2
exit 6
