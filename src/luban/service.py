"""Minimal HTTP service for platform integration (milestone indicator 6.1).

Standard library only, so a clean ``uv sync`` is enough to deploy it. Endpoints:

* ``GET  /health``                 liveness and version
* ``POST /v1/episodes/run``        run the Planner-Verifier-Actor loop on the
                                   bundled offline environment (no GPU, no LLM)
* ``POST /v1/task-graph/validate`` validate (and optionally repair) a task graph
* ``POST /v1/runs``               execute a named server-side run configuration

Configured runs retain the runtime limits and artifacts declared in their YAML.
"""

import json
import tempfile
import threading
import time
import uuid
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from luban.agent_core import (
    DefaultAgent,
    EnvironmentVerifier,
    InMemoryMemory,
    TaskSkillPlanner,
)
from luban.agent_core.task_decomposer import TaskDecomposer, normalize_graph_payload
from luban.backends.skills import LanguageSkillBackend
from luban.examples.offline import CounterEnvironment
from luban.exceptions import LubanError
from luban.pipelines import DirectPipeline
from luban.runtimes import SyncRuntime
from luban.serialization import redact_secrets, to_jsonable

MAX_BODY_BYTES = 1_000_000
MAX_EPISODE_STEPS = 50
MAX_GRAPH_NODES = 64


class RequestError(ValueError):
    pass


def _package_version() -> str:
    try:
        return version("luban")
    except PackageNotFoundError:
        return "unknown"


def _require(body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise RequestError(f"unknown fields: {unknown}")


def run_episode(body: dict[str, Any]) -> dict[str, Any]:
    _require(body, {"task", "max_steps"})
    task = body.get("task", {"name": "increment"})
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise RequestError("task must be an object with a string name")
    max_steps = body.get("max_steps", 3)
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or not 1 <= max_steps <= MAX_EPISODE_STEPS
    ):
        raise RequestError(f"max_steps must be an integer in 1..{MAX_EPISODE_STEPS}")
    agent = DefaultAgent(
        TaskSkillPlanner(),
        EnvironmentVerifier(),
        InMemoryMemory(),
        LanguageSkillBackend(),
    )
    with tempfile.TemporaryDirectory(prefix="luban-service-") as output_dir:
        result = SyncRuntime(output_dir=Path(output_dir), max_steps=max_steps).run(
            agent, DirectPipeline(), CounterEnvironment(), task
        )
    keys = ("success", "termination_reason", "steps", "planner_calls", "verifier_calls")
    return {key: to_jsonable(result.get(key)) for key in keys if key in result}


def validate_graph(body: dict[str, Any]) -> dict[str, Any]:
    _require(body, {"graph", "max_nodes", "repair"})
    max_nodes = body.get("max_nodes", 8)
    if (
        isinstance(max_nodes, bool)
        or not isinstance(max_nodes, int)
        or not 1 <= max_nodes <= MAX_GRAPH_NODES
    ):
        raise RequestError(f"max_nodes must be an integer in 1..{MAX_GRAPH_NODES}")
    graph = body.get("graph")
    if graph is None:
        raise RequestError("graph is required")
    repair = body.get("repair", True)
    if not isinstance(repair, bool):
        raise RequestError("repair must be a boolean")
    try:
        return {
            "graph": TaskDecomposer.validate_graph(graph, max_nodes=max_nodes),
            "repaired": False,
        }
    except LubanError as strict_error:
        if not repair:
            raise RequestError(str(strict_error)) from strict_error
    try:
        normalized = normalize_graph_payload(graph, max_nodes)
        return {
            "graph": TaskDecomposer.validate_graph(normalized, max_nodes=max_nodes),
            "repaired": True,
        }
    except LubanError as error:
        raise RequestError(str(error)) from error


ROUTES = {
    "/v1/episodes/run": run_episode,
    "/v1/task-graph/validate": validate_graph,
}


def run_registered(
    body: dict[str, Any], *, configs: dict[str, Path], lock: Any
) -> dict[str, Any]:
    from luban.cli import run_config

    _require(body, {"config"})
    name = body.get("config")
    if not isinstance(name, str) or name not in configs:
        raise RequestError("config must name a run registered at server startup")
    # A simulator and its GPU context are owned by one run at a time.
    with lock:
        function_started = time.perf_counter()
        result = run_config(configs[name])
        function_seconds = time.perf_counter() - function_started
    result["service_function_seconds"] = function_seconds
    return dict(redact_secrets(to_jsonable(result)))


class LubanRequestHandler(BaseHTTPRequestHandler):
    server_version = "LubanService"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            stats = self.server.stats  # type: ignore[attr-defined]
            self._send(
                HTTPStatus.OK,
                {"status": "ok", "version": _package_version(), **stats.snapshot()},
            )
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        request_id = self.headers.get("X-Request-Id") or uuid.uuid4().hex
        started = time.perf_counter()
        stats = self.server.stats  # type: ignore[attr-defined]
        handler = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        try:
            if handler is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                raise RequestError("Content-Length is required")
            length = int(length_header)
            if length < 0 or length > MAX_BODY_BYTES:
                stats.record(ok=False)
                self.close_connection = True
                self._send(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": f"body must be at most {MAX_BODY_BYTES} bytes"},
                )
                return
            raw = self.rfile.read(length)
            if not raw.strip():
                raise RequestError("empty body")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise RequestError("body must be a JSON object")
            result = handler(body)
        except (
            RequestError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
        ) as error:
            stats.record(ok=False)
            self._send(
                HTTPStatus.BAD_REQUEST, {"error": str(error), "request_id": request_id}
            )
            return
        except Exception as error:  # the process must survive any single request
            stats.record(ok=False)
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": type(error).__name__, "request_id": request_id},
            )
            return
        stats.record(ok=True)
        self._send(
            HTTPStatus.OK,
            {
                "request_id": request_id,
                "server_seconds": time.perf_counter() - started,
                "result": result,
            },
        )


class ServiceStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.ok = 0
        self.rejected = 0

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.ok += 1
            else:
                self.rejected += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "uptime_seconds": round(time.time() - self.started, 3),
                "requests_ok": self.ok,
                "requests_rejected": self.rejected,
            }


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    run_configs: dict[str, Path] | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), LubanRequestHandler)
    server.daemon_threads = True
    server.stats = ServiceStats()  # type: ignore[attr-defined]
    server.routes = {  # type: ignore[attr-defined]
        **ROUTES,
        "/v1/runs": partial(
            run_registered, configs=dict(run_configs or {}), lock=threading.Lock()
        ),
    }
    return server


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    run_configs: dict[str, Path] | None = None,
) -> None:
    server = make_server(host, port, run_configs)
    print(json.dumps({"listening": f"http://{host}:{server.server_port}"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
