import argparse
import json
from pathlib import Path
from typing import Any

from luban.config import (
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)
from luban.serialization import redact_secrets, to_jsonable


def main() -> None:
    parser = argparse.ArgumentParser(prog="luban")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one task or benchmark")
    run_parser.add_argument("--config", required=True, type=Path)

    health_parser = subparsers.add_parser("health", help="Check configured backends")
    health_parser.add_argument("--agent-config", required=True, type=Path)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Launch the read-only trajectory browser"
    )
    dashboard_parser.add_argument(
        "--runs-dir", type=Path, default=Path("runs"), help="runs 输出根目录"
    )
    dashboard_parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: localhost only)"
    )
    dashboard_parser.add_argument("--port", type=int, default=7860)

    serve_parser = subparsers.add_parser(
        "serve", help="Serve the core loop over HTTP for platform integration"
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument(
        "--run-config",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Expose a named run YAML through POST /v1/runs; repeat for more runs",
    )

    args = parser.parse_args()
    try:
        if args.command == "health":
            agent = build_agent(load_yaml(args.agent_config))
            try:
                result = agent.healthcheck()
            finally:
                agent.close()
            print(
                json.dumps(
                    redact_secrets(to_jsonable(result)), ensure_ascii=False, indent=2
                )
            )
            raise SystemExit(0 if result.get("healthy") else 1)

        if args.command == "serve":
            from luban.service import serve

            run_configs = {}
            for entry in args.run_config:
                name, separator, path = entry.partition("=")
                if not separator or not name or not path:
                    raise ValueError("--run-config requires NAME=PATH")
                if name in run_configs:
                    raise ValueError(f"Duplicate run config name: {name}")
                config_path = Path(path).resolve()
                if not config_path.is_file():
                    raise ValueError(f"Run config does not exist: {path}")
                run_configs[name] = config_path
            serve(args.host, args.port, run_configs)
            raise SystemExit(0)

        if args.command == "dashboard":
            try:
                from luban.dashboard.app import launch as launch_dashboard
            except ImportError as error:
                raise SystemExit(
                    "dashboard 需要 gradio,请安装可选依赖组:"
                    f" uv pip install --editable '.[dashboard]' ({error})"
                ) from error
            launch_dashboard(args.runs_dir, host=args.host, port=args.port)
            raise SystemExit(0)

        result = run_config(args.config)
        print(
            json.dumps(
                redact_secrets(to_jsonable(result)), ensure_ascii=False, indent=2
            )
        )
        summary = result.get("summary", {})
        resolved = result.get("resolved", {})
        if (
            result.get("exceptions")
            or result.get("termination_reason") in {"exception", "interrupted"}
            or (isinstance(summary, dict) and summary.get("exceptions"))
            or (isinstance(resolved, dict) and resolved.get("deferred_api_episodes"))
        ):
            raise SystemExit(1)
    except Exception as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "termination_reason": "exception",
                    "error_type": type(error).__name__,
                    "error": redact_secrets(str(error)),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def run_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    config = load_yaml(config_path)
    if "agent_config" not in config:
        raise ValueError("Run config requires agent_config")
    agent_path = resolve_config_path(config["agent_config"], config_path)
    agent = build_agent(load_yaml(agent_path))
    environment = None
    handed_off = False
    try:
        pipeline, runtime, environment = build_run_components(config)

        if "benchmark" in config:
            if environment is None:
                raise ValueError("Benchmark run requires environment")
            benchmark = instantiate(config["benchmark"], environment=environment)
            handed_off = True
            result = benchmark.run(agent, pipeline, runtime)
            if not isinstance(result, dict):
                raise TypeError("Benchmark run() must return a dict")
            return result
        if environment is None:
            raise ValueError("Run config requires environment")
        if "task" not in config:
            raise ValueError("Single run requires task")
        handed_off = True
        return runtime.run(agent, pipeline, environment, config["task"])
    finally:
        if not handed_off:
            for resource in (environment, agent):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass  # Preserve the configuration/construction failure.


if __name__ == "__main__":
    main()
