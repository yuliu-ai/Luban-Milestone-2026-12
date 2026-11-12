"""Independent release acceptance. Credentials are synthetic; all servers are ours."""

import ast
import gc
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
import pytest

from luban.agent_core import (
    DefaultAgent,
    EnvironmentVerifier,
    InMemoryMemory,
    TaskSkillPlanner,
)
from luban.agent_core.module_evolution import (
    ModuleEvolutionManager,
    PairedModuleEvaluation,
)
from luban.backends.llm import OpenAICompatibleLLMBackend
from luban.backends.skills import LanguageSkillBackend
from luban.backends.skills.openpi import OpenPIWebSocketPolicyBackend
from luban.backends.world_models.latent import (
    FirstCausalFailureDetector,
    LightweightLatentDynamics,
)
from luban.backends.world_models.persistence import PersistenceWorldModelBackend
from luban.config import load_yaml, resolve_config_path
from luban.examples.offline import CounterEnvironment
from luban.exceptions import BackendError, ConfigError
from luban.perception.client import VisualPerceptionClient
from luban.pipelines import DirectPipeline
from luban.pipelines.policy_only import PolicyOnlyPipeline
from luban.runtimes import SyncRuntime
from luban.serialization import redact_secrets

ROOT = Path(__file__).resolve().parents[2]


class CountingBackend(LanguageSkillBackend):
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class PayloadEnvironment(CounterEnvironment):
    def __init__(self, **payload):
        self.payload = payload
        self.closed = 0

    def execute(self, action, execute_steps=None):
        return {
            "observation": {"count": 1, "available_skills": ["increment"]},
            "task_success": False,
            "task_progress": 0.1,
            "last_action_success": True,
            "done": False,
            "executed_steps": 1,
            **self.payload,
        }

    def close(self):
        self.closed += 1


def agent():
    backend = CountingBackend()
    return DefaultAgent(
        TaskSkillPlanner(), EnvironmentVerifier(), InMemoryMemory(), backend
    ), backend


@pytest.mark.parametrize(
    "message,secret",
    [
        ('{"OPENAI_API_KEY": "audit-provider-secret"}', "audit-provider-secret"),
        ('{"password": "audit-password-with-space tail"}', "audit-password-with-space"),
        (
            "provider rejected ghp_AuditSyntheticToken12345678",
            "ghp_AuditSyntheticToken12345678",
        ),
    ],
)
def test_provider_stdout_and_health_do_not_expose_credential(message, secret, capsys):
    transport = httpx.MockTransport(
        lambda req: httpx.Response(401, json={"error": {"message": message}})
    )
    backend = OpenAICompatibleLLMBackend(
        "http://audit.invalid", "audit", transport=transport, max_retries=0
    )
    try:
        health = backend.healthcheck()
        with pytest.raises(BackendError) as failure:
            backend.complete({"messages": [{"role": "user", "content": "audit"}]})
        assert secret not in capsys.readouterr().out, (
            "provider stdout leaked a synthetic credential"
        )
        assert secret not in str(health)
        assert secret not in str(failure.value)
    finally:
        backend.close()


def test_general_redaction_covers_basic_authorization():
    encoded = "YXVkaXQtdXNlcjphdWRpdC1wYXNzd29yZA=="
    assert encoded not in redact_secrets("Authorization: Basic " + encoded)


def test_general_redaction_preserves_metrics_and_is_idempotent():
    value = {
        "success": False,
        "steps": 3,
        "latency": 1.5,
        "nested": [{"api_key": "audit-value"}],
    }
    filtered = redact_secrets(value)
    assert filtered == redact_secrets(filtered)
    assert (
        filtered["success"] is False
        and filtered["steps"] == 3
        and filtered["latency"] == 1.5
    )
    assert filtered["nested"][0]["api_key"] == "[REDACTED]"


@pytest.mark.parametrize("value", [-1, 1.5, True, "3"])
@pytest.mark.parametrize("kind", ["direct", "policy_only"])
def test_invalid_environment_step_count_is_rejected(tmp_path, value, kind):
    owner, backend = agent()
    environment = PayloadEnvironment(executed_steps=value)
    pipeline = DirectPipeline() if kind == "direct" else PolicyOnlyPipeline()
    result = SyncRuntime(max_steps=1, output_dir=tmp_path).run(
        owner, pipeline, environment, {"name": "increment"}
    )
    assert result["exceptions"] == 1, (
        "invalid adapter step accounting was silently accepted"
    )
    assert result["success"] is False
    assert environment.closed == backend.closed == 1


@pytest.mark.parametrize("progress", [math.nan, math.inf, -math.inf])
def test_nonfinite_progress_cannot_enter_result_json(tmp_path, progress):
    owner, _ = agent()
    result = SyncRuntime(max_steps=1, output_dir=tmp_path).run(
        owner,
        DirectPipeline(),
        PayloadEnvironment(task_progress=progress),
        {"name": "increment"},
    )
    assert math.isfinite(result["task_progress"]), (
        "non-finite task progress escaped the boundary"
    )
    record = next(tmp_path.glob("*/result.json")).read_text()
    json.loads(
        record,
        parse_constant=lambda value: pytest.fail(
            "non-standard JSON constant: " + value
        ),
    )


@pytest.mark.parametrize("signal_value", [False, "false", 0, None])
@pytest.mark.parametrize("kind", ["direct", "policy_only"])
def test_untrusted_success_values_do_not_produce_success(tmp_path, signal_value, kind):
    owner, _ = agent()
    pipeline = DirectPipeline() if kind == "direct" else PolicyOnlyPipeline()
    result = SyncRuntime(max_steps=1, output_dir=tmp_path).run(
        owner,
        pipeline,
        PayloadEnvironment(task_success=signal_value),
        {"name": "increment"},
    )
    assert result["success"] is False


@pytest.mark.parametrize("kind", ["direct", "policy_only"])
def test_authoritative_env_info_wins_over_conflicting_task_success(tmp_path, kind):
    owner, _ = agent()
    pipeline = DirectPipeline() if kind == "direct" else PolicyOnlyPipeline()
    result = SyncRuntime(max_steps=1, output_dir=tmp_path).run(
        owner,
        pipeline,
        PayloadEnvironment(task_success=True, env_info={"success": False}),
        {"name": "increment"},
    )
    assert result["success"] is False


def test_result_write_failure_still_closes_resources(tmp_path, monkeypatch):
    owner, backend = agent()
    environment = PayloadEnvironment()
    write_text = Path.write_text

    def fail_result(path, *args, **kwargs):
        if path.name == "result.json":
            raise OSError(28, "audit synthetic disk full")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_result)
    with pytest.raises(OSError):
        SyncRuntime(max_steps=1, output_dir=tmp_path).run(
            owner, DirectPipeline(), environment, {"name": "increment"}
        )
    assert environment.closed == backend.closed == 1


def test_runtime_timeout_is_cooperative_and_records_terminal(tmp_path):
    class SlowEnvironment(PayloadEnvironment):
        def execute(self, *args, **kwargs):
            time.sleep(0.06)
            return super().execute(*args, **kwargs)

    owner, _ = agent()
    started = time.monotonic()
    result = SyncRuntime(timeout_seconds=0.01, output_dir=tmp_path).run(
        owner, DirectPipeline(), SlowEnvironment(), {"name": "increment"}
    )
    assert result["termination_reason"] == "timeout" and result["steps"] == 1
    assert time.monotonic() - started >= 0.06


def test_cli_real_sigint_writes_interrupted_record_and_cleans(tmp_path):
    (tmp_path / "audit_slow.py").write_text("""
from pathlib import Path
import time
from luban.examples.offline import CounterEnvironment
class SlowEnv(CounterEnvironment):
    def __init__(self, ready, closed):
        self.ready, self.closed = Path(ready), Path(closed)
    def execute(self, *args, **kwargs):
        self.ready.write_text('ready')
        time.sleep(10)
        return super().execute(*args, **kwargs)
    def close(self):
        self.closed.write_text('closed')
""")
    import yaml

    agent_config = {
        "agent": {"class_path": "luban.agent_core.DefaultAgent"},
        "planner": {"class_path": "luban.agent_core.TaskSkillPlanner"},
        "verifier": {"class_path": "luban.agent_core.EnvironmentVerifier"},
        "memory": {"class_path": "luban.agent_core.InMemoryMemory"},
        "skill_backend": {"class_path": "luban.backends.skills.LanguageSkillBackend"},
    }
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(agent_config))
    run_config = {
        "agent_config": str(tmp_path / "agent.yaml"),
        "pipeline": {"class_path": "luban.pipelines.DirectPipeline"},
        "runtime": {
            "class_path": "luban.runtimes.SyncRuntime",
            "init_args": {"output_dir": str(tmp_path / "runs")},
        },
        "environment": {
            "class_path": "audit_slow.SlowEnv",
            "init_args": {
                "ready": str(tmp_path / "ready"),
                "closed": str(tmp_path / "closed"),
            },
        },
        "task": {"name": "increment"},
    }
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(run_config))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "luban",
            "run",
            "--config",
            str(tmp_path / "run.yaml"),
        ],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 130, (output, error)
        result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text())
        assert result["termination_reason"] == "interrupted"
        assert (tmp_path / "closed").read_text() == "closed"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "script",
    [
        "serve_perception.py",
        "serve_perception_molmo2.py",
        "serve_qwen35_lora_openai.py",
        "serve_robocasa_openpi.py",
        "serve_xiaomi_policy.py",
    ],
)
def test_public_server_default_host_is_loopback(script):
    tree = ast.parse((ROOT / "scripts" / script).read_text())
    defaults = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "--host" for arg in node.args
        )
        for kw in node.keywords
        if kw.arg == "default" and isinstance(kw.value, ast.Constant)
    ]
    assert defaults == ["127.0.0.1"]


def write_latent_checkpoint(path, **overrides):
    values = {
        "x_mean": np.zeros(80, dtype=np.float32),
        "x_scale": np.ones(80, dtype=np.float32),
        "y_mean": np.zeros(32, dtype=np.float32),
        "y_scale": np.ones(32, dtype=np.float32),
        "transition_weights": np.zeros((81, 32), dtype=np.float32),
        "progress_weights": np.zeros(81, dtype=np.float32),
        "success_weights": np.zeros(81, dtype=np.float32),
        "residual_scale": np.array(0.1),
        "model_id": np.array("audit-latent"),
        **overrides,
    }
    np.savez(path, **values)


def test_latent_finite_checkpoint_produces_bounded_prediction(tmp_path):
    write_latent_checkpoint(tmp_path / "model.npz")
    model = LightweightLatentDynamics(tmp_path / "model.npz")
    result, prediction = model.prediction_payload(np.zeros(32), np.zeros(48), 16)
    assert result["model_id"] == "audit-latent"
    assert len(prediction["latent_sequence"]) == 16
    assert prediction["success_probability"] == 0.5


@pytest.mark.parametrize("value", [0.0, math.nan, math.inf])
def test_latent_corrupt_normalizer_rejected_at_load(tmp_path, value):
    write_latent_checkpoint(
        tmp_path / "model.npz", x_scale=np.full(80, value, dtype=np.float32)
    )
    with pytest.raises(BackendError):
        LightweightLatentDynamics(tmp_path / "model.npz")


def test_latent_extreme_negative_logit_is_numerically_stable(tmp_path):
    weights = np.zeros(81, dtype=np.float32)
    weights[-1] = -1000
    write_latent_checkpoint(tmp_path / "model.npz", success_weights=weights)
    prediction = LightweightLatentDynamics(tmp_path / "model.npz").predict(
        np.zeros(32), np.zeros(48), 1
    )
    assert math.isfinite(prediction["success_probability"])
    assert 0 <= prediction["success_probability"] < 1e-6


@pytest.mark.parametrize("which", ["observation", "action"])
def test_latent_nonfinite_features_are_rejected(which):
    observation, action = np.zeros(32), np.zeros(48)
    (observation if which == "observation" else action)[0] = math.nan
    with pytest.raises(BackendError):
        LightweightLatentDynamics().predict(observation, action, 1)


def test_causal_detector_rejects_nonfinite_evidence():
    with pytest.raises(BackendError):
        FirstCausalFailureDetector().detect([np.zeros(32)], [np.full(32, math.nan)])


def test_causal_detector_matches_identical_features():
    result = FirstCausalFailureDetector().detect([np.zeros(32)], [np.zeros(32)])
    assert result["detected"] is False


def test_persistence_baseline_detaches_and_bounds_observation():
    source = {
        "depth": {"child": {"leaf": {"too_deep": 1}}},
        "nan": math.nan,
        "image": np.zeros((10, 10, 3)),
        "text": "x" * 200,
    }
    result = PersistenceWorldModelBackend(max_depth=2).predict_transition(
        {"observation": source, "action": np.zeros((7, 12))}
    )
    json.dumps(result, allow_nan=False)
    assert result["prediction_horizon_steps"] == 7
    assert len(result["state_summary"]["text"]) <= 160
    assert result["state_summary"]["image"]["shape"] == [10, 10, 3]
    assert "too_deep" not in json.dumps(result)


def propose(store):
    candidate = store.propose(
        target="planner",
        operation="add_guideline",
        patch={"guideline": "audit synthetic guideline"},
        reason="audit reproduction",
        evidence_refs=("audit:failure",),
        applicability=("audit-task",),
        source_episode_id="audit-episode",
        parent_version="planner@v1",
    )
    store.start_shadow(candidate.proposal_id)
    return candidate.proposal_id


def evaluation(proposal_id, identity, outcome="success"):
    return PairedModuleEvaluation(
        proposal_id=proposal_id,
        canonical_identity=identity,
        baseline_outcome="failure",
        candidate_outcome=outcome,
        protocol_id="audit-protocol",
        split_id="held-out",
        evaluation_round=1,
        evidence_refs=("audit:evidence",),
    )


def test_concurrent_duplicate_evidence_cannot_inflate_validation(tmp_path, monkeypatch):
    store = ModuleEvolutionManager(tmp_path / "evolution.jsonl", min_paired_samples=2)
    proposal_id = propose(store)
    original = store.evaluations
    barrier = threading.Barrier(2)

    def synchronized_read(identity):
        snapshot = original(identity)
        barrier.wait(timeout=3)
        return snapshot

    monkeypatch.setattr(store, "evaluations", synchronized_read)
    row = evaluation(proposal_id, "same-identity")

    def record():
        try:
            store.record_pair(row)
            return "accepted"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: record(), range(2)))
    monkeypatch.setattr(store, "evaluations", original)
    summary = store.paired_summary(proposal_id)
    assert summary["eligible_pairs"] == 1 and outcomes.count("accepted") == 1, (
        "one identity was counted as two independent pairs"
    )


def test_sequential_duplicate_evidence_is_rejected(tmp_path):
    store = ModuleEvolutionManager(tmp_path / "evolution.jsonl", min_paired_samples=2)
    proposal_id = propose(store)
    row = evaluation(proposal_id, "same-identity")
    store.record_pair(row)
    with pytest.raises(ValueError, match="duplicate"):
        store.record_pair(row)
    assert store.settle(proposal_id)["decision"] == "pending_evidence"


def test_partial_evolution_ledger_is_explicit_error_without_overwrite(tmp_path):
    path = tmp_path / "evolution.jsonl"
    store = ModuleEvolutionManager(path)
    propose(store)
    with path.open("a") as file:
        file.write('{"schema_version":')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="line"):
        store.healthcheck()
    assert path.read_bytes() == before


def test_local_relative_config_is_resolved_against_owner(tmp_path, monkeypatch):
    owner = tmp_path / "configs" / "run.yaml"
    owner.parent.mkdir()
    (owner.parent / "agent.yaml").write_text("value: audit")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert resolve_config_path("agent.yaml", owner).read_text() == "value: audit"


def test_malformed_yaml_is_classified(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("config: [\n")
    with pytest.raises(ConfigError):
        load_yaml(path)


@pytest.fixture
def websocket_server():
    pytest.importorskip("openpi_client")
    pytest.importorskip("websockets.sync.server")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    received = []

    def handle(connection):
        connection.send(msgpack_numpy.packb({"audit": True}))
        try:
            for message in connection:
                request = msgpack_numpy.unpackb(message)
                received.append(request)
                if request.get("text_fault"):
                    connection.send("synthetic server failure")
                else:
                    connection.send(
                        msgpack_numpy.packb(
                            {"actions": np.arange(12, dtype=np.float32).reshape(2, 6)}
                        )
                    )
        except Exception:
            pass

    def health(connection, request):
        if request.path == "/healthz":
            return connection.respond(200, "ok")

    server = serve(handle, "127.0.0.1", 0, process_request=health, close_timeout=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.socket.getsockname()[1], received
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_real_openpi_websocket_numpy_metadata_and_close(websocket_server):
    port, received = websocket_server
    backend = OpenPIWebSocketPolicyBackend("127.0.0.1", port, timeout_seconds=0.5)
    try:
        assert backend.healthcheck()["healthy"] is True
        result = backend.predict(
            {
                "observation": {"state": np.ones(3, dtype=np.float32)},
                "skill": "audit-skill",
                "subtask": "audit prompt",
            }
        )
        np.testing.assert_array_equal(
            result, np.arange(12, dtype=np.float32).reshape(2, 6)
        )
        assert backend.healthcheck()["metadata"]["audit"] is True
        assert received[0]["prompt"] == "audit prompt"
    finally:
        backend.close()
    assert backend.client is None


def test_real_openpi_text_error_reconnect_then_valid_request(websocket_server):
    port, received = websocket_server
    backend = OpenPIWebSocketPolicyBackend("127.0.0.1", port, timeout_seconds=0.5)
    try:
        with pytest.raises(BackendError):
            backend.predict({"observation": {"text_fault": True}})
        assert len(received) == 2
        assert backend.client is None
        assert backend.predict({"observation": {"state": np.ones(3)}}).shape == (2, 6)
    finally:
        backend.close()


@pytest.fixture
def perception_server():
    pytest.importorskip("zmq")
    pytest.importorskip("msgpack")
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    port = reserved.getsockname()[1]
    reserved.close()
    code = (
        "from luban.perception.server import VisualPerceptionServer; "
        'VisualPerceptionServer(provider="sam3",analyzer=lambda payload: {})'
        '.serve_forever(bind_host="127.0.0.1",port=' + str(port) + ")"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    probe = VisualPerceptionClient("sam3", port=port, timeout_seconds=0.1)
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if probe.healthcheck()["healthy"]:
                break
        else:
            pytest.fail("isolated perception server did not start")
        yield port
    finally:
        probe.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


def test_real_perception_client_full_rgb_exchange(perception_server):
    backend = VisualPerceptionClient(
        "sam3", port=perception_server, timeout_seconds=0.5
    )
    observation = {
        name: np.zeros((8, 8, 3), dtype=np.uint8) for name in backend.camera_ids
    }
    try:
        result = backend.analyze(
            observation,
            queries=["audit object"],
            session_id="audit-session",
            frame_index=1,
        )
        assert result["provider"] == "sam3" and result["entities"] == []
        assert result["request_id"].startswith("audit-session:")
        assert backend.requests == 1 and backend.failures == 0
    finally:
        backend.close()
    assert backend._socket is None and backend._context is None


def test_perception_unresponsive_endpoint_times_out_and_closes():
    pytest.importorskip("zmq")
    pytest.importorskip("msgpack")
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    port = reserved.getsockname()[1]
    client = VisualPerceptionClient("sam3", port=port, timeout_seconds=0.025)
    try:
        started = time.monotonic()
        assert client.healthcheck()["healthy"] is False
        assert time.monotonic() - started < 1
        assert client._socket is None and client._context is None
    finally:
        client.close()
        reserved.close()
    gc.collect()
