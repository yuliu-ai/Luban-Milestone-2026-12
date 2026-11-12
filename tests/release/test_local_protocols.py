"""Real loopback HTTP/ZeroMQ + fake-environment resume; no model calls."""

import importlib.util
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from luban.agent_core import (
    DefaultAgent,
    EnvironmentVerifier,
    InMemoryMemory,
    TaskSkillPlanner,
)
from luban.backends.llm import OpenAICompatibleLLMBackend
from luban.backends.skills.rldx import _RLDXMsgSerializer, _RLDXZMQClient
from luban.backends.skills.xiaomi_remote import _XiaomiZMQClient
from luban.evals.benchmarks.robocasa import RoboCasa365Evaluator
from luban.exceptions import BackendError, ConfigError
from luban.pipelines import SkillExecutionPipeline
from luban.runtimes import SyncRuntime


@contextmanager
def http_server(responses, *, delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.path, json.loads(body) if body else None))
            if delay:
                time.sleep(delay)
            status, payload = responses[min(len(requests) - 1, len(responses) - 1)]
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


GOOD = {"choices": [{"message": {"content": "拿起杯子"}, "finish_reason": "stop"}]}


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_real_http_base_url_and_unicode(suffix):
    with http_server([(200, {"data": [{"id": "audit-model"}]}), (200, GOOD)]) as (
        url,
        requests,
    ):
        backend = OpenAICompatibleLLMBackend(
            url + suffix, "audit-model", timeout_seconds=1
        )
        try:
            assert backend.healthcheck()["healthy"]
            r = backend.complete(
                {"messages": [{"role": "user", "content": "拿起杯子"}]}
            )
            assert r["choices"][0]["message"]["content"] == "拿起杯子"
            assert [r[0] for r in requests] == ["/v1/models", "/v1/chat/completions"]
            assert requests[1][1]["messages"][0]["content"] == "拿起杯子"
        finally:
            backend.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_real_http_retry_then_success(status):
    with http_server(
        [(status, {"error": {"message": "synthetic transient"}}), (200, GOOD)]
    ) as (url, requests):
        backend = OpenAICompatibleLLMBackend(
            url, "audit-model", max_retries=1, timeout_seconds=1
        )
        backend._retry_delay = lambda attempt: 0
        try:
            r = backend.complete({"messages": [{"role": "user", "content": "hi"}]})
            assert r["_backend"]["attempts"] == 2 and len(requests) == 2
        finally:
            backend.close()


def test_real_http_read_timeout_is_bounded():
    with http_server([(200, GOOD)], delay=0.2) as (url, requests):
        backend = OpenAICompatibleLLMBackend(
            url, "audit-model", max_retries=0, timeout_seconds=0.03
        )
        start = time.monotonic()
        try:
            with pytest.raises(BackendError, match="request failed"):
                backend.complete({"messages": [{"role": "user", "content": "hi"}]})
            assert time.monotonic() - start < 1
        finally:
            backend.close()


@contextmanager
def zmq_server():
    zmq = pytest.importorskip("zmq")
    pytest.importorskip("msgpack")

    ready, stop = threading.Event(), threading.Event()
    shared = {"requests": []}

    def serve():
        context = zmq.Context()
        server = context.socket(zmq.REP)
        server.setsockopt(zmq.RCVTIMEO, 100)
        server.setsockopt(zmq.LINGER, 0)
        shared["port"] = server.bind_to_random_port("tcp://127.0.0.1")
        ready.set()
        try:
            while not stop.is_set():
                try:
                    data = _RLDXMsgSerializer.from_bytes(server.recv())
                except zmq.Again:
                    continue
                shared["requests"].append(data)
                response = {"status": "ok", "ok": True}
                if data["endpoint"] in {"get_action", "predict"}:
                    response = {
                        "ok": True,
                        "actions": np.zeros((4, 12), dtype=np.float32),
                    }
                server.send(_RLDXMsgSerializer.to_bytes(response))
        finally:
            server.close(0)
            context.term()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    try:
        yield shared
    finally:
        stop.set()
        thread.join(2)
        assert not thread.is_alive()


def test_real_rldx_zmq_ping_reset_numpy_roundtrip():
    with zmq_server() as server:
        client = _RLDXZMQClient("127.0.0.1", server["port"], timeout_seconds=1)
        try:
            assert client.ping()
            assert client.reset({"seed": 7})["status"] == "ok"
            data = client.get_action({"image": np.zeros((8, 8, 3), dtype=np.uint8)}, {})
            assert data["actions"].shape == (4, 12)
            assert (
                server["requests"][-1]["data"]["observation"]["image"].dtype == np.uint8
            )
        finally:
            client.close()


def test_real_xr1_zmq_ping_numpy_roundtrip():
    with zmq_server() as server:
        client = _XiaomiZMQClient("127.0.0.1", server["port"], timeout_seconds=1)
        try:
            assert client.ping()["status"] == "ok"
            assert client.predict({"state": np.zeros(14, dtype=np.float32)})[
                "actions"
            ].shape == (4, 12)
        finally:
            client.close()


def test_xr1_unresponsive_socket_times_out_and_closes():
    pytest.importorskip("zmq")
    pytest.importorskip("msgpack")
    # Reserve a unique local TCP port without listening; never target a real service.
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    client = _XiaomiZMQClient(
        "127.0.0.1", reservation.getsockname()[1], timeout_seconds=0.03
    )
    start = time.monotonic()
    try:
        with pytest.raises(BackendError, match="timed out"):
            client.ping()
    finally:
        client.close()
        reservation.close()
    assert time.monotonic() - start < 1


def test_transport_rejects_pickle_object_arrays():
    pytest.importorskip("msgpack")
    with pytest.raises(ValueError):
        _RLDXMsgSerializer.to_bytes({"bad": np.array([{"object": True}], dtype=object)})


def fixtures():
    spec = importlib.util.spec_from_file_location(
        "audit_eval_fixtures", Path.cwd() / "tests/unit/test_robocasa_evaluation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate(root, *, resume, count=1, stride=50):
    f = fixtures()
    env = f.FakeEvaluationEnvironment()
    a = DefaultAgent(
        TaskSkillPlanner(),
        EnvironmentVerifier(),
        InMemoryMemory(),
        f.FakePolicyBackend(),
    )
    evaluator = RoboCasa365Evaluator(
        environment=env,
        task_set="atomic_seen",
        split="target",
        output_dir=root / "eval",
        max_tasks=2,
        episodes_per_task=count,
        episode_indices=list(range(count)),
        seed=7,
        seed_episode_stride=stride,
        resume=resume,
    )
    result = evaluator.run(
        a, SkillExecutionPipeline(), SyncRuntime(output_dir=root / "traces")
    )
    return result, env


@pytest.mark.parametrize("count,stride", [(1, 50), (2, 50), (2, 2)])
def test_resume_skips_every_completed_task_without_new_attempts(
    tmp_path, count, stride
):
    first, env1 = evaluate(tmp_path, resume=False, count=count, stride=stride)
    initial = (tmp_path / "eval/attempts.jsonl").read_bytes()
    second, env2 = evaluate(tmp_path, resume=True, count=count, stride=stride)
    assert first["summary"]["episodes"] == second["summary"]["episodes"] == 2 * count
    assert not env2.tasks
    assert (tmp_path / "eval/attempts.jsonl").read_bytes() == initial


def test_fresh_run_refuses_existing_evidence(tmp_path):
    evaluate(tmp_path, resume=False)
    initial = (tmp_path / "eval/episodes.jsonl").read_bytes()
    with pytest.raises(ConfigError, match="fresh output directory"):
        evaluate(tmp_path, resume=False)
    assert (tmp_path / "eval/episodes.jsonl").read_bytes() == initial


def test_corrupt_resume_fails_explicitly_without_overwrite(tmp_path):
    evaluate(tmp_path, resume=False)
    p = tmp_path / "eval/episodes.jsonl"
    p.write_text(p.read_text() + '{"interrupted":')
    initial = p.read_bytes()
    with pytest.raises(ConfigError, match="Invalid JSON"):
        evaluate(tmp_path, resume=True)
    assert p.read_bytes() == initial
