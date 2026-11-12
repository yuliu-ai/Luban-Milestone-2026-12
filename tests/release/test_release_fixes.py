"""Additional regression checks for the release fixes and their failure boundaries."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest
from test_release_boundaries import Env, agent

from luban import cli
from luban.config import instantiate
from luban.dashboard import store
from luban.exceptions import ConfigError
from luban.observability import LocalEpisodeRecorder
from luban.pipelines import DirectPipeline
from luban.runtimes import SyncRuntime
from luban.serialization import redact_secrets


def test_session_collision_is_exclusive(tmp_path):
    def run(_):
        try:
            return SyncRuntime(output_dir=tmp_path).run(
                agent(), DirectPipeline(), Env(), "task", session_id="same"
            )["success"]
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(run, range(2))) == [False, True]
    rows = (tmp_path / "same/trace.jsonl").read_text().splitlines()
    assert [json.loads(row)["event"] for row in rows] == [
        "episode_start",
        "step",
        "episode_end",
    ]


def test_session_symlink_does_not_touch_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    trace = outside / "trace.jsonl"
    trace.write_text("preserved")
    output = tmp_path / "runs"
    output.mkdir()
    (output / "same").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileExistsError):
        SyncRuntime(output_dir=output).run(
            agent(), DirectPipeline(), Env(), "task", session_id="same"
        )
    assert trace.read_text() == "preserved"


def test_caller_owned_resources_are_not_closed_on_preflight_error(tmp_path):
    a, e = agent(), Env()
    with pytest.raises(ValueError):
        SyncRuntime(output_dir=tmp_path).run(
            a, DirectPipeline(), e, {"horizon": -1}, close_resources=False
        )
    assert not a._closed and not e.closed
    a.close()
    e.close()


def test_nested_config_failure_closes_child_once(monkeypatch):
    module = ModuleType("release_test_components")
    closed = []

    class Child:
        def close(self):
            closed.append(True)

    class Parent:
        def __init__(self, child):
            raise RuntimeError("synthetic constructor failure")

    module.Child = Child
    module.Parent = Parent
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(RuntimeError, match="synthetic"):
        instantiate(
            {
                "class_path": "release_test_components.Parent",
                "init_args": {"child": {"class_path": "release_test_components.Child"}},
            }
        )
    assert closed == [True]


@pytest.mark.parametrize(
    "message",
    [
        "api_key=AUDIT_SYNTHETIC_SECRET_12345",
        "OPENAI_API_KEY=AUDIT_SYNTHETIC_SECRET_12345",
        "client_secret: AUDIT_SYNTHETIC_SECRET_12345",
    ],
)
def test_all_runtime_diagnostic_artifacts_redact_secrets(tmp_path, message):
    recorder = LocalEpisodeRecorder(record_video=False)
    environment = Env()

    def fail(action, execute_steps=None):
        raise RuntimeError(message)

    environment.execute = fail
    result = SyncRuntime(output_dir=tmp_path, observability=recorder).run(
        agent(), DirectPipeline(), environment, "task"
    )
    assert "AUDIT_SYNTHETIC_SECRET_12345" not in repr(result)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "AUDIT_SYNTHETIC_SECRET_12345" not in path.read_text()


@pytest.mark.parametrize(
    "value",
    [
        {"api_key": "synthetic-private-value"},
        {"nested": [{"Authorization": "Bearer synthetic-private-value"}]},
        "password='synthetic-private-value with spaces'",
        'provider error {"api_key": "synthetic-private-value"}',
        {"OPENAI_API_KEY": "synthetic-private-value"},
        "OPENAI_API_KEY=synthetic-private-value",
        {"client_secret": "synthetic-private-value"},
        "x-api-key: synthetic-private-value",
        "passwd=synthetic-private-value",
        {"private_key": "synthetic-private-value"},
        "-----BEGIN PRIVATE KEY-----\nsynthetic-private-value\n"
        "-----END PRIVATE KEY-----",
    ],
)
def test_redaction_handles_structured_and_quoted_credentials(value):
    redacted = redact_secrets(value)
    assert "synthetic-private-value" not in repr(redacted)
    assert redact_secrets(redacted) == redacted


def test_dashboard_rejects_symlinked_result_file(tmp_path):
    session = tmp_path / "runs/run/traces/session"
    session.mkdir(parents=True)
    external = tmp_path / "outside.json"
    external.write_text('{"synthetic": true}')
    (session / "result.json").symlink_to(external)
    with pytest.raises(ConfigError):
        store.load_result(tmp_path / "runs", "run", "session")


def test_dashboard_does_not_ignore_corrupt_complete_jsonl_row(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "episodes.jsonl").write_text('{"session_id":"ok"}\nBROKEN\n')
    with pytest.raises(ConfigError):
        store.list_episodes(tmp_path, "run")


def test_cli_normal_task_failure_is_not_infrastructure_failure(monkeypatch):
    monkeypatch.setattr(
        cli,
        "run_config",
        lambda _: {
            "success": False,
            "termination_reason": "step_limit",
            "exceptions": 0,
        },
    )
    monkeypatch.setattr(sys, "argv", ["luban", "run", "--config", "unused"])
    cli.main()


def test_cli_real_process_failure_exit_and_redaction(tmp_path):
    module = tmp_path / "local_failure.py"
    module.write_text(
        "from luban.examples.offline import CounterEnvironment\n"
        "class Broken(CounterEnvironment):\n"
        "    def execute(self, action, execute_steps=None):\n"
        "        raise RuntimeError('api_key=SYNTHETIC_CLI_SECRET')\n"
    )
    agent_config = tmp_path / "agent.json"
    agent_config.write_text(
        json.dumps(
            {
                "agent": {"class_path": "luban.agent_core.DefaultAgent"},
                "planner": {"class_path": "luban.agent_core.TaskSkillPlanner"},
                "verifier": {"class_path": "luban.agent_core.EnvironmentVerifier"},
                "memory": {"class_path": "luban.agent_core.InMemoryMemory"},
                "skill_backend": {
                    "class_path": "luban.backends.skills.LanguageSkillBackend"
                },
            }
        )
    )
    config = tmp_path / "run.json"
    config.write_text(
        json.dumps(
            {
                "agent_config": str(agent_config),
                "pipeline": {"class_path": "luban.pipelines.DirectPipeline"},
                "runtime": {
                    "class_path": "luban.runtimes.SyncRuntime",
                    "init_args": {"output_dir": str(tmp_path / "runs")},
                },
                "environment": {"class_path": "local_failure.Broken"},
                "task": {"name": "increment"},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "luban.cli", "run", "--config", str(config)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["termination_reason"] == "exception"
    assert "SYNTHETIC_CLI_SECRET" not in result.stdout + result.stderr


def test_installed_offline_example(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "luban.examples.offline",
            "--output-dir",
            str(tmp_path / "outputs"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["success"] and payload["steps"] == 2
    assert Path(payload["trace_path"]).is_file()


@pytest.mark.parametrize(
    "result",
    [
        {"summary": {"exceptions": 1}},
        {"summary": {"exceptions": 0}, "resolved": {"deferred_api_episodes": 2}},
    ],
)
def test_cli_batch_infrastructure_errors_are_nonzero(monkeypatch, result):
    monkeypatch.setattr(cli, "run_config", lambda _: result)
    monkeypatch.setattr(sys, "argv", ["luban", "run", "--config", "unused"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1


@pytest.mark.parametrize(
    "script,variable,flag",
    [
        ("serve_perception.py", "SAM3_CHECKPOINT", "--checkpoint"),
        ("serve_perception_molmo2.py", "MOLMO2_MODEL_PATH", "--model-path"),
    ],
)
def test_perception_cli_requires_user_model_path_before_loading(script, variable, flag):
    env = {key: value for key, value in os.environ.items() if key != variable}
    result = subprocess.run(
        [sys.executable, str(Path.cwd() / "scripts" / script)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 2
    assert flag in result.stderr and "required" in result.stderr
    assert "/root/autodl" not in result.stdout + result.stderr


def test_xr1_portable_config_still_checks_model_identity(monkeypatch):
    import importlib.util

    from luban.config import build_agent, load_yaml

    path = Path.cwd() / "tests/smoke/test_xr1_self_evolution_smoke.py"
    spec = importlib.util.spec_from_file_location("xr1_release_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("XR1_MODEL_PATH", "/synthetic/other-checkpoint")
    configured = build_agent(
        load_yaml("configs/agents/self_evolving_xr1_atomic_v23.yaml")
    )
    configured.skill_backend.client_factory = module.FakeXr1PolicyClient
    try:
        assert configured.skill_backend.healthcheck()["healthy"] is False
    finally:
        configured.close()
