#!/usr/bin/env python3
import argparse
import json
from collections.abc import Mapping
from typing import Any

from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.backends.skills.xiaomi import (
    XIAOMI_STATELESS_HISTORY_SCHEMA,
    XIAOMI_TASK_GRAPH_CONTRACT_VERSION,
    XiaomiRobotics1VLAPolicyBackend,
)

_MAX_TORCH_SEED = 2**63 - 1
_EPISODE_SEED_MULTIPLIER = 1_000_003


def _episode_step_seed(data: Mapping[str, Any]) -> int:
    task = data.get("task")
    if not isinstance(task, Mapping):
        raise TypeError("deterministic Xiaomi inference requires task metadata")
    episode_seed = task.get("seed")
    step = data.get("step")
    if (
        not isinstance(episode_seed, int)
        or isinstance(episode_seed, bool)
        or not isinstance(step, int)
        or isinstance(step, bool)
        or step < 0
    ):
        raise TypeError(
            "deterministic Xiaomi inference requires integer task.seed and step"
        )
    return (episode_seed * _EPISODE_SEED_MULTIPLIER + step) % _MAX_TORCH_SEED


def _predict(
    backend: XiaomiRobotics1VLAPolicyBackend,
    data: dict[str, Any],
    policy_seed_mode: str,
) -> tuple[dict[str, Any], int | None]:
    if data.get("request_schema") != XIAOMI_STATELESS_HISTORY_SCHEMA:
        raise TypeError("Xiaomi server requires a complete stateless history request")
    predict = backend.predict_stateless_history
    if policy_seed_mode == "unbound":
        return predict(data), None
    if policy_seed_mode != "episode_step":
        raise ValueError(f"Unsupported policy seed mode: {policy_seed_mode}")

    policy_seed = _episode_step_seed(data)
    torch = backend._torch
    if torch is None:
        raise RuntimeError("Xiaomi torch runtime is unavailable")
    cuda_devices = (
        [torch.cuda.current_device()] if str(backend.device).startswith("cuda") else []
    )
    with torch.random.fork_rng(devices=cuda_devices):
        torch.manual_seed(policy_seed)
        if cuda_devices:
            torch.cuda.manual_seed_all(policy_seed)
        return predict(data), policy_seed


def _response(
    endpoint: str,
    backend: XiaomiRobotics1VLAPolicyBackend,
    data: Any,
    policy_seed_mode: str,
) -> dict[str, Any]:
    if endpoint == "ping":
        return {
            "ok": True,
            "status": "ok",
            "server": {
                "backend": type(backend).__name__,
                "model_path": backend.model_path,
                "device": backend.device,
                "action_requests": backend.action_requests,
                "policy_seed_mode": policy_seed_mode,
                "instruction_mode": backend.instruction_mode,
                "instruction_contract_version": (
                    XIAOMI_TASK_GRAPH_CONTRACT_VERSION
                    if backend.instruction_mode == "task_graph"
                    else None
                ),
                "request_schema": XIAOMI_STATELESS_HISTORY_SCHEMA,
                "server_history_stateful": False,
            },
        }
    if endpoint != "predict":
        raise ValueError(f"Unsupported endpoint: {endpoint}")
    if not isinstance(data, dict):
        raise TypeError("predict data must be a dict")
    actions, policy_seed = _predict(backend, data, policy_seed_mode)
    inference = dict(backend.last_inference)
    inference["policy_seed_mode"] = policy_seed_mode
    if policy_seed is not None:
        inference["policy_seed"] = policy_seed
    return {
        "ok": True,
        "actions": actions,
        "inference": inference,
    }


def serve(args: argparse.Namespace) -> None:
    import zmq

    backend = XiaomiRobotics1VLAPolicyBackend(
        model_path=args.model_path,
        device=args.device,
        robot_type=args.robot_type,
        observation_history=args.observation_history,
        observation_interval=args.observation_interval,
        crop_ratio=args.crop_ratio,
        denoising_steps=args.denoising_steps,
        attention_backend=args.attention_backend,
        instruction_mode=args.instruction_mode,
    )
    health = backend.healthcheck()
    if not health.get("healthy", False):
        raise RuntimeError(f"Xiaomi backend failed healthcheck: {health}")

    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.setsockopt(zmq.LINGER, 0)
    socket.bind(f"tcp://{args.host}:{args.port}")
    print(
        json.dumps(
            {
                "status": "ready",
                "host": args.host,
                "port": args.port,
                "model_path": args.model_path,
                "device": args.device,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        while True:
            request = _RLDXMsgSerializer.from_bytes(socket.recv())
            try:
                if not isinstance(request, Mapping):
                    raise TypeError("request must be a mapping")
                response = _response(
                    str(request.get("endpoint", "")),
                    backend,
                    request.get("data"),
                    args.policy_seed_mode,
                )
            except Exception as error:
                response = {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            socket.send(_RLDXMsgSerializer.to_bytes(response))
    finally:
        socket.close(linger=0)
        context.term()
        backend.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve Xiaomi RoboCasa365 policy")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--robot-type", default="robocasa365")
    parser.add_argument("--observation-history", type=int, default=4)
    parser.add_argument("--observation-interval", type=int, default=2)
    parser.add_argument("--crop-ratio", type=float, default=0.95)
    parser.add_argument("--denoising-steps", type=int, default=5)
    parser.add_argument("--attention-backend", default="eager")
    parser.add_argument(
        "--instruction-mode",
        choices=(
            "task_description",
            "task_and_subtask",
            "planner_subtask",
            "task_graph",
        ),
        default="planner_subtask",
    )
    parser.add_argument(
        "--policy-seed-mode",
        choices=("unbound", "episode_step"),
        default="unbound",
    )
    return parser.parse_args()


if __name__ == "__main__":
    serve(parse_args())
