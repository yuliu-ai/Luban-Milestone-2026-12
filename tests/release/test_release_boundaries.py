"""Regression tests for release contracts and diagnostic boundaries."""

import errno
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from luban.agent_core import DefaultAgent, InMemoryMemory, Planner, Verifier
from luban.backends.llm import OpenAICompatibleLLMBackend
from luban.backends.skills import LanguageSkillBackend
from luban.config import build_agent, load_yaml
from luban.dashboard import store
from luban.environments import Environment
from luban.exceptions import BackendError, ConfigError
from luban.pipelines import DirectPipeline
from luban.runtimes import SyncRuntime


class Plan(Planner):
    def plan(self, inputs):
        return {"skill": "move"}


class Verify(Verifier):
    def verify(self, inputs):
        r = inputs["environment_result"]
        return {
            "task_success": r["task_success"],
            "task_progress": 0,
            "last_action_success": True,
            "environment_done": r["done"],
        }


class Env(Environment):
    def __init__(self, *, success=True, fault=None):
        self.success, self.fault, self.closed, self.calls = success, fault, False, 0

    def reset(self, task):
        if self.fault == "reset":
            raise RuntimeError("synthetic reset failure")
        return {"available_skills": ["move"]}

    def execute(self, action, execute_steps=None):
        self.calls += 1
        if self.fault == "execute":
            raise RuntimeError("synthetic execution failure")
        if self.fault == "secret":
            raise RuntimeError("api_key=AUDIT_SYNTHETIC_SECRET_12345")
        if self.fault == "interrupt":
            raise KeyboardInterrupt("synthetic interruption")
        return {
            "observation": {},
            "task_success": self.success,
            "task_progress": float(self.success),
            "last_action_success": True,
            "done": self.success,
            "executed_steps": 1,
        }

    def close(self):
        self.closed = True
        if self.fault == "close":
            raise RuntimeError("synthetic close failure")


def agent():
    return DefaultAgent(Plan(), Verify(), InMemoryMemory(), LanguageSkillBackend())


@pytest.mark.parametrize(
    "field",
    [
        "max_steps",
        "max_environment_steps",
        "max_action_chunks",
        "max_invalid_actions",
        "max_retries",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_runtime_budget_rejects_invalid_values(field, value):
    with pytest.raises((ValueError, TypeError)):
        SyncRuntime(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_runtime_timeout_rejects_invalid_values(value):
    with pytest.raises((ValueError, TypeError)):
        SyncRuntime(timeout_seconds=value)


@pytest.mark.parametrize("value", [True, 1.5, float("nan")])
def test_direct_pipeline_chunk_size_must_be_integer(value):
    with pytest.raises((ValueError, TypeError)):
        DirectPipeline(action_execution_mode="receding_horizon", execute_steps=value)


@pytest.mark.parametrize("fault", ["reset", "execute", "close"])
def test_runtime_failure_closes_resources_and_writes_result(tmp_path, fault):
    a, e = agent(), Env(fault=fault)
    r = SyncRuntime(output_dir=tmp_path).run(
        a, DirectPipeline(), e, "task", session_id=fault
    )
    assert e.closed and a._closed
    assert (
        json.loads((tmp_path / fault / "result.json").read_text())["session_id"]
        == fault
    )
    if fault != "close":
        assert r["termination_reason"] == "exception" and not r["success"]
    else:
        assert r["success"] and r["close_errors"]


@pytest.mark.parametrize("session", ["../escaped", "../../escaped"])
def test_runtime_rejects_session_path_traversal(tmp_path, session):
    output = tmp_path / "sandbox" / "runs"
    with pytest.raises((ValueError, ConfigError)):
        SyncRuntime(output_dir=output).run(
            agent(), DirectPipeline(), Env(), "task", session_id=session
        )


def test_runtime_rejects_absolute_session_path(tmp_path):
    with pytest.raises((ValueError, ConfigError)):
        SyncRuntime(output_dir=tmp_path / "runs").run(
            agent(),
            DirectPipeline(),
            Env(),
            "task",
            session_id=str(tmp_path / "outside"),
        )


def test_runtime_preserves_existing_session_evidence(tmp_path):
    runtime = SyncRuntime(output_dir=tmp_path)
    runtime.run(agent(), DirectPipeline(), Env(), "first task", session_id="same")
    original = (tmp_path / "same" / "trace.jsonl").read_bytes()
    try:
        runtime.run(agent(), DirectPipeline(), Env(), "second task", session_id="same")
    except (ValueError, FileExistsError):
        pass
    assert (tmp_path / "same" / "trace.jsonl").read_bytes() == original


def test_runtime_invalid_task_still_closes_resources(tmp_path):
    a, e = agent(), Env()
    with pytest.raises(ValueError):
        SyncRuntime(output_dir=tmp_path).run(a, DirectPipeline(), e, {"horizon": -1})
    assert e.closed and a._closed


def test_runtime_redacts_exception_credentials(tmp_path):
    r = SyncRuntime(output_dir=tmp_path).run(
        agent(), DirectPipeline(), Env(fault="secret"), "task"
    )
    assert "AUDIT_SYNTHETIC_SECRET_12345" not in Path(r["trace_path"]).read_text()


def test_runtime_interrupt_keeps_terminal_record(tmp_path):
    a, e = agent(), Env(fault="interrupt")
    try:
        SyncRuntime(output_dir=tmp_path).run(
            a, DirectPipeline(), e, "task", session_id="interrupted"
        )
    except KeyboardInterrupt:
        pass
    assert a._closed and e.closed
    assert (tmp_path / "interrupted" / "result.json").exists()


def test_runtime_disk_full_on_trace_still_closes_resources(tmp_path, monkeypatch):
    a, e = agent(), Env()
    original = Path.write_text

    def full(path, *args, **kwargs):
        if path.name == "trace.jsonl":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", full)
    with pytest.raises(OSError):
        SyncRuntime(output_dir=tmp_path).run(a, DirectPipeline(), e, "task")
    assert a._closed and e.closed


def test_cli_infrastructure_failure_has_nonzero_exit(monkeypatch):
    import luban.cli as cli

    monkeypatch.setattr(
        cli,
        "run_config",
        lambda p: {
            "success": False,
            "termination_reason": "exception",
            "exceptions": 1,
        },
    )
    monkeypatch.setattr(sys, "argv", ["luban", "run", "--config", "synthetic.yaml"])
    code = 0
    try:
        cli.main()
    except SystemExit as error:
        code = error.code
    assert code != 0


def test_config_partial_construction_closes_created_backend(monkeypatch):
    import luban.config as config

    closed = []

    class Backend(LanguageSkillBackend):
        def close(self):
            closed.append(True)

    backend = Backend()
    original = config.instantiate
    monkeypatch.setattr(
        config,
        "instantiate",
        lambda spec, **kw: backend if spec == "backend" else original(spec, **kw),
    )
    with pytest.raises(ConfigError):
        build_agent(
            {
                "agent": {"class_path": "luban.agent_core.DefaultAgent"},
                "planner": {"class_path": "does.not.Exist"},
                "memory": {},
                "skill_backend": "backend",
            }
        )
    assert closed


@pytest.mark.parametrize(
    "payload", [None, [], {"data": None}, {"data": [1]}, {"data": "bad"}]
)
def test_llm_malformed_health_response_is_unhealthy(payload):
    backend = OpenAICompatibleLLMBackend(
        "http://audit.invalid",
        "test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    try:
        assert backend.healthcheck()["healthy"] is False
    finally:
        backend.close()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_llm_errors_are_bounded_and_redacted(status, capsys):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": "api_key=AUDIT_SYNTHETIC_SECRET_12345"}}
        )

    backend = OpenAICompatibleLLMBackend(
        "http://audit.invalid",
        "test",
        max_retries=1,
        transport=httpx.MockTransport(respond),
    )
    backend._retry_delay = lambda attempt: 0
    try:
        with pytest.raises(BackendError) as error:
            backend.complete({"messages": [{"role": "user", "content": "hi"}]})
        assert len(calls) == 2
        assert (
            "AUDIT_SYNTHETIC_SECRET_12345"
            not in str(error.value) + capsys.readouterr().out
        )
    finally:
        backend.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]},
    ],
)
def test_llm_malformed_completion_is_classified(payload):
    backend = OpenAICompatibleLLMBackend(
        "http://audit.invalid",
        "test",
        max_retries=0,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    try:
        with pytest.raises(BackendError, match="envelope invalid"):
            backend.complete({"messages": [{"role": "user", "content": "hi"}]})
    finally:
        backend.close()


def test_yaml_rejects_object_execution(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("!!python/object/apply:builtins.print ['AUDIT']")
    with pytest.raises(ConfigError):
        load_yaml(p)


@pytest.mark.parametrize(
    "name", ["../outside", "/tmp/outside", "a/b", "a\\b", "..", ""]
)
def test_dashboard_rejects_traversal(tmp_path, name):
    with pytest.raises(ConfigError):
        store.load_result(tmp_path, name, "session")


def test_dashboard_does_not_follow_run_symlink_outside_root(tmp_path):
    root, outside = tmp_path / "runs", tmp_path / "outside"
    root.mkdir()
    s = outside / "traces" / "s"
    s.mkdir(parents=True)
    (s / "result.json").write_text('{"private": "synthetic"}')
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError):
        store.load_result(root, "linked", "s")


def test_dashboard_tolerates_incomplete_final_jsonl_line(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "episodes.jsonl").write_text('{"session_id":"done"}\n{"session_id":')
    assert store.list_episodes(tmp_path, "run") == [{"session_id": "done"}]


def test_many_episodes_have_unique_complete_artifacts(tmp_path):
    runtime = SyncRuntime(output_dir=tmp_path)
    for _ in range(100):
        result = runtime.run(agent(), DirectPipeline(), Env(), "拿起杯子")
        assert result["success"]
    sessions = list(tmp_path.iterdir())
    assert len(sessions) == 100
    for s in sessions:
        assert json.loads((s / "result.json").read_text())["success"]
        rows = [
            json.loads(line) for line in (s / "trace.jsonl").read_text().splitlines()
        ]
        assert (
            rows[0]["event"] == "episode_start" and rows[-1]["event"] == "episode_end"
        )


def test_gzip_trace_roundtrip(tmp_path):
    import gzip

    r = SyncRuntime(output_dir=tmp_path, trace_compression="gzip").run(
        agent(), DirectPipeline(), Env(), "task"
    )
    with gzip.open(r["trace_path"], "rt") as f:
        rows = [json.loads(line) for line in f]
    assert rows[-1]["success"] and not Path(r["trace_path"]).with_suffix("").exists()


def test_authoritative_environment_overrules_false_positive_verifier(tmp_path):
    class Liar(Verify):
        def verify(self, inputs):
            return {"task_success": True, "last_action_success": True}

    a = agent()
    a.verifier = Liar()
    r = SyncRuntime(output_dir=tmp_path, max_steps=2).run(
        a, DirectPipeline(), Env(success=False), "task"
    )
    assert not r["success"] and r["steps"] == 2


def test_optional_validator_does_not_hide_broken_module_import():
    spec = importlib.util.spec_from_file_location(
        "audit_validator", Path.cwd() / "scripts/validate_configs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    error = ModuleNotFoundError(
        "No module named 'totally_misspelled_module'", name="totally_misspelled_module"
    )
    assert module._classify(error) == "FAIL"
