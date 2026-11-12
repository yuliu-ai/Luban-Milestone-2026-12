"""Validate every agent/run config by constructing all configured components.

Constructor-only validation: no service connections are made. Catches broken
class paths, missing imports, and invalid init_args across the whole config
surface. Mirrors the CLI: the benchmark evaluator is built with the configured
``environment`` injected.

Statuses:
- OK           every component constructed
- NEEDS_ENV    an optional heavy dependency module is absent (spatiallm,
               embodiedbench, isaacsim, clawvla, ... live in dedicated envs)
- NEEDS_ASSET  a weight/asset path is not configured on this machine
- NEEDS_SERVICE a constructor tried to reach a service
- FAIL         broken config or code — must be fixed

Exit code 1 only on FAIL.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Iterator
from pathlib import Path

from luban.config import (
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "configs" / "agents"
RUNS = ROOT / "configs" / "runs"

SERVICE_HINTS = (
    "connect",
    "connection",
    "refused",
    "timeout",
    "unreachable",
    "address",
    "grpc",
    "resolve",
)
ASSET_HINTS = (
    "model_path",
    "environment variable",
    "no such file",
    "not found",
)
OPTIONAL_MODULES = (
    "spatiallm",
    "embodiedbench",
    "isaacsim",
    "clawvla",
    "lerobot",
    "libero",
    "gradio",
    "grpc",
    "chemistry_bench_proto",
    "openpi_client",
    "torch",
    "zmq",
    "msgpack",
    "robocasa",
    "robosuite",
    "mujoco",
)


def _close(components: list[object]) -> None:
    for component in components:
        closer = getattr(component, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def _classify(error: Exception) -> str:
    chain: list[Exception] = [error]
    cause = error.__cause__
    while cause is not None and len(chain) < 8:
        chain.append(cause)
        cause = cause.__cause__
    for item in chain:
        if isinstance(item, ModuleNotFoundError) and item.name:
            if item.name.split(".")[0] in OPTIONAL_MODULES:
                return "NEEDS_ENV"
            if item.name.startswith("luban"):
                return "FAIL"
            return "FAIL"
    text = " ".join(str(item) for item in chain).lower()
    if any(hint in text for hint in ASSET_HINTS):
        return "NEEDS_ASSET"
    if any(hint in text for hint in SERVICE_HINTS):
        return "NEEDS_SERVICE"
    return "FAIL"


def _detail(error: Exception) -> str:
    detail = f"{type(error).__name__}: {error}"
    if error.__cause__ is not None:
        detail += f" | cause: {type(error.__cause__).__name__}: {error.__cause__}"
    return detail


def agent_configs() -> Iterator[Path]:
    yield from sorted(AGENTS.glob("*.yaml"))


def run_configs() -> Iterator[Path]:
    yield from sorted(RUNS.glob("*.yaml"))


def validate_agent(path: Path) -> tuple[str, str]:
    try:
        agent = build_agent(load_yaml(path))
        _close([agent])
        return "OK", ""
    except Exception as error:
        return _classify(error), _detail(error)


def validate_run(path: Path) -> tuple[str, str]:
    try:
        config = load_yaml(path)
        agent_spec = config.get("agent_config")
        notes: list[str] = []
        if isinstance(agent_spec, str):
            agent_path = resolve_config_path(agent_spec, path)
            if not agent_path.exists():
                return "FAIL", f"agent_config not found: {agent_path}"
            status, detail = validate_agent(agent_path)
            if status == "FAIL":
                return status, f"agent_config {agent_path.name}: {detail}"
            if status != "OK":
                notes.append(f"agent={agent_path.name}[{status}]")
            else:
                notes.append(f"agent={agent_path.name}")
        components: list[object] = []
        try:
            pipeline, runtime, environment = build_run_components(config)
            components = [pipeline, runtime, environment]
            benchmark = config.get("benchmark")
            if isinstance(benchmark, dict) and "class_path" in benchmark:
                instantiate(benchmark, environment=environment)
                notes.append("benchmark=built")
        finally:
            _close(components)
        if any("[NEEDS" in note for note in notes):
            return "NEEDS", " ".join(notes)
        return "OK", " ".join(notes)
    except Exception as error:
        return _classify(error), _detail(error)


def main() -> int:
    if not AGENTS.is_dir():
        print(f"missing config dir: {AGENTS}")
        return 1
    failures = 0
    total = 0
    print(f"{'status':<13} config")
    print("-" * 88)
    for path in agent_configs():
        total += 1
        status, detail = validate_agent(path)
        print(f"{status:<13} agents/{path.name} {detail}")
        failures += status == "FAIL"
    for path in run_configs():
        total += 1
        status, detail = validate_run(path)
        print(f"{status:<13} runs/{path.name} {detail}")
        failures += status == "FAIL"
    print("-" * 88)
    print(f"configs checked: {total}, hard failures: {failures}")
    if failures and "--debug" in sys.argv:
        traceback.print_exc()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
