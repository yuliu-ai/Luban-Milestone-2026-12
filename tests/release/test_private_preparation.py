"""Regressions for independently distributed source and diagnostics."""

import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from test_release_boundary_v2 import evaluation, propose, write_latent_checkpoint

from luban.agent_core.module_evolution import ModuleEvolutionManager
from luban.backends.world_models.latent import LightweightLatentDynamics
from luban.config import load_yaml, resolve_config_path
from luban.serialization import redact_secrets


@pytest.mark.parametrize("name", ["password", "api_key", "OPENAI_API_KEY"])
def test_escaped_json_credentials_are_fully_redacted(name):
    secret = 'prefix"SyntheticEscapedTail991\\suffix'
    filtered = redact_secrets(json.dumps({name: secret}))
    assert "SyntheticEscapedTail991" not in filtered
    assert filtered == redact_secrets(filtered)


@pytest.mark.parametrize(
    "value",
    [
        "https://user:SyntheticPassword991@example.invalid/v1",
        "Cookie: session=SyntheticPassword991",
        "https://example.invalid/blob?sig=SyntheticPassword991&st=tomorrow",
        "aws_secret_access_key=SyntheticPassword991",
        "密码=SyntheticPassword991",
    ],
)
def test_common_diagnostic_credentials(value):
    assert "SyntheticPassword991" not in redact_secrets(value)


def test_config_explicit_environment_default(tmp_path, monkeypatch):
    p = tmp_path / "test.yaml"
    p.write_text('url: "${PREPARATION_TEST_URL:-http://127.0.0.1:8000/v1}"')
    monkeypatch.delenv("PREPARATION_TEST_URL", raising=False)
    assert load_yaml(p)["url"] == "http://127.0.0.1:8000/v1"
    monkeypatch.setenv("PREPARATION_TEST_URL", "https://example.invalid/v1")
    assert load_yaml(p)["url"] == "https://example.invalid/v1"


def test_agent_config_owner_precedes_unrelated_cwd_file(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    caller = tmp_path / "caller"
    owner.mkdir()
    caller.mkdir()
    (owner / "agent.yaml").write_text("source: owner")
    (caller / "agent.yaml").write_text("source: caller")
    monkeypatch.chdir(caller)
    assert resolve_config_path("agent.yaml", owner / "run.yaml") == owner / "agent.yaml"


def test_finite_latent_endpoints_do_not_overflow_interpolation(tmp_path):
    p = tmp_path / "model.npz"
    write_latent_checkpoint(p, y_mean=np.full(32, 3e38, dtype=np.float32))
    model = LightweightLatentDynamics(p)
    result = model.predict(np.full(32, -3e38, dtype=np.float32), np.zeros(48), 16)
    assert all(np.isfinite(item).all() for item in result["latent_sequence"])
    np.testing.assert_array_equal(
        result["latent_sequence"][-1], result["predicted_features"]
    )


def test_contended_lifecycle_keeps_ledger_readable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    store = ModuleEvolutionManager(path, proposal_only=False, min_paired_samples=1)
    for iteration in range(30):
        proposal = propose(store)
        store.record_pair(evaluation(proposal, f"identity-{iteration}"))
        store.validate(proposal)
        barrier = threading.Barrier(2)

        def transition(operation, barrier=barrier, proposal=proposal):
            barrier.wait(timeout=5)
            try:
                if operation == "promote":
                    return store.promote(proposal, approved_by="synthetic-test")
                return store.quarantine(proposal, reason="synthetic-test")
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(transition, ["promote", "quarantine"]))
        assert store.get(proposal).state in {"promoted", "quarantined"}
        reopened = ModuleEvolutionManager(
            path, proposal_only=False, min_paired_samples=1
        )
        assert reopened.get(proposal) == store.get(proposal)


def test_archive_provenance_does_not_inherit_caller_repository(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "prepare_archive", root / "scripts/prepare_submission_study.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "scripts").mkdir()
    module.__file__ = str(tmp_path / "scripts/prepare_submission_study.py")
    monkeypatch.chdir(root)
    assert module.code_commit() is None


@pytest.mark.parametrize("name", ["g0_mapping_probe", "g0_concurrency_probe"])
def test_developer_probe_resolves_bundled_script_and_configured_python(
    name, monkeypatch
):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("LUBAN_SIM_PYTHON", "/example/simulator/python")
    spec = importlib.util.spec_from_file_location(
        name, root / "scripts/study/g0_probes" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.PROBE).is_file()
    assert Path(module.PROJECT) == root
    assert module.PYTHON == "/example/simulator/python"
