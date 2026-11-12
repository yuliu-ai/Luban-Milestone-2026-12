#!/usr/bin/env bash
# Start the SAM3 perception service on a GPU and run the P01 evidence probe.
#
# 用法：scripts/study/start_sam3_and_probe.sh [GPU索引] [端口]
#
# 前提：本机已分配 GPU（无卡模式下容器内存上限 2 GB，装不下 3.4 GB 权重）。
# 该脚本只启动感知服务与只读探针，不修改任何契约状态、不写主实验目录。
set -euo pipefail

GPU_INDEX="${1:-0}"
PORT="${2:-18200}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SAM3_PY="${SAM3_PYTHON:?Set SAM3_PYTHON to the upstream perception interpreter}"
CHECKPOINT="${SAM3_CHECKPOINT:?Set SAM3_CHECKPOINT to the local checkpoint file}"
OMNI_PY="${LUBAN_PYTHON:-${REPO}/.venv/bin/python}"
IMAGE="${SAM3_INPUT_IMAGE:-${REPO}/catalogs/v2.2/evidence/sam3_probe/input_frame.png}"
LOG="${REPO}/runs/sam3_probe/server.log"
RUN_DIR="${REPO}/runs/sam3_probe"

cd "${REPO}"
[[ -x "${SAM3_PY}" && -x "${OMNI_PY}" && -f "${CHECKPOINT}" && -f "${IMAGE}" ]] || { echo "Interpreter/checkpoint/image missing" >&2; exit 2; }
mkdir -p "${RUN_DIR}"

if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  echo "没有可用 GPU：无卡模式下容器内存上限 2 GB，无法加载 3.4 GB 的 SAM3 权重。" >&2
  exit 2
fi

echo "GPU 列表："
nvidia-smi -L | head -4

echo "启动 SAM3 感知服务（GPU ${GPU_INDEX}，端口 ${PORT}）"
CUDA_VISIBLE_DEVICES="${GPU_INDEX}" setsid nohup "${SAM3_PY}" scripts/serve_perception.py \
  --checkpoint "${CHECKPOINT}" \
  --device cuda \
  --port "${PORT}" \
  --confidence-threshold 0.3 \
  > "${LOG}" 2>&1 < /dev/null &
SERVICE_PID=$!
echo "service pid=${SERVICE_PID} log=${LOG}"

echo "等待服务就绪（最多 10 分钟）"
for _ in $(seq 1 60); do
  if grep -q "perception server ready" "${LOG}" 2>/dev/null; then
    echo "服务已就绪"
    break
  fi
  if ! kill -0 "${SERVICE_PID}" 2>/dev/null; then
    echo "服务进程已退出，最后 20 行日志：" >&2
    tail -20 "${LOG}" >&2
    exit 3
  fi
  sleep 10
done

if ! grep -q "perception server ready" "${LOG}" 2>/dev/null; then
  echo "服务在 10 分钟内未就绪，最后 20 行日志：" >&2
  tail -20 "${LOG}" >&2
  exit 4
fi

echo "运行 P01 探针（真实 SAM3 推理 + 契约校验 + 独立验证器）"
PYTHONPATH="${REPO}/src" "${OMNI_PY}" scripts/study/probe_p01_sam3.py \
  --image "${IMAGE}" \
  --port "${PORT}" \
  --score-threshold 0.3 \
  --out-dir "${RUN_DIR}"

echo "证据已写入 ${RUN_DIR}/p01_sam3_summary.json"
echo "服务仍在运行（pid=${SERVICE_PID}）。停止：kill ${SERVICE_PID}"
