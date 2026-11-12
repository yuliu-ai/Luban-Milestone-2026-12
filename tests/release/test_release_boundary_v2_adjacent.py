"""Adjacent regressions for release-boundary repairs."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import pytest
from test_release_boundary_v2 import (
    PayloadEnvironment,
    agent,
    evaluation,
    propose,
    write_latent_checkpoint,
)

from luban.agent_core.module_evolution import ModuleEvolutionManager
from luban.backends.world_models.latent import LightweightLatentDynamics
from luban.exceptions import BackendError
from luban.pipelines import DirectPipeline, Pipeline
from luban.pipelines.policy_only import PolicyOnlyPipeline
from luban.runtimes import SyncRuntime
from luban.serialization import redact_secrets, to_jsonable


@pytest.mark.parametrize("steps", [0, np.int32(1), np.int64(2)])
@pytest.mark.parametrize("kind", ["direct", "policy_only"])
def test_valid_integral_environment_counts_remain_supported(tmp_path, steps, kind):
    owner, _ = agent()
    pipeline = DirectPipeline() if kind == "direct" else PolicyOnlyPipeline()
    result = SyncRuntime(max_steps=1, output_dir=tmp_path).run(
        owner,
        pipeline,
        PayloadEnvironment(executed_steps=steps),
        {"name": "increment"},
    )
    assert result["exceptions"] == 0
    assert result["environment_steps"] == int(steps)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_diagnostic_value_stays_descriptive_and_json_safe(value):
    safe = to_jsonable({"sensor_value": value, "finite_metric": 0.25})
    encoded = json.dumps(safe, allow_nan=False)
    assert json.loads(encoded)["sensor_value"]["finite"] is False
    assert safe["finite_metric"] == 0.25


class BadMetricPipeline(Pipeline):
    def __init__(self, value: Any, raises: bool):
        self.value = value
        self.raises = raises

    def step(self, agent, environment, state):
        state["task_progress"] = self.value
        if self.raises:
            raise RuntimeError("synthetic pipeline failure")
        return {"success": True}

    def is_terminal(self, output, state):
        return True


@pytest.mark.parametrize("value", [math.nan, math.inf, "invalid"])
@pytest.mark.parametrize("raises", [False, True])
def test_custom_pipeline_metric_fault_keeps_exception_result_writable(
    tmp_path, value, raises
):
    owner, backend = agent()
    environment = PayloadEnvironment()
    result = SyncRuntime(output_dir=tmp_path).run(
        owner,
        BadMetricPipeline(value, raises),
        environment,
        {"name": "increment"},
    )
    assert result["success"] is False and result["exceptions"] == 1
    assert math.isfinite(result["task_progress"])
    saved = next(tmp_path.glob("*/result.json")).read_text()
    json.loads(saved, parse_constant=lambda value: pytest.fail(value))
    assert environment.closed == backend.closed == 1


@pytest.mark.parametrize("surface", ["paired_summary", "healthcheck", "retrieve"])
def test_historical_duplicate_evidence_is_rejected_without_rewriting(tmp_path, surface):
    path = tmp_path / "evolution.jsonl"
    store = ModuleEvolutionManager(path, min_paired_samples=2)
    proposal_id = propose(store)
    store.record_pair(evaluation(proposal_id, "only-identity"))
    rows = path.read_text().splitlines()
    duplicate = rows[-1]
    with path.open("a") as file:
        file.write(duplicate + "\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate canonical identity"):
        if surface == "paired_summary":
            store.paired_summary(proposal_id)
        elif surface == "healthcheck":
            store.healthcheck()
        else:
            store.retrieve(
                target="planner",
                applicability="audit-task",
                parent_version="planner@v1",
            )
    assert path.read_bytes() == before


def test_real_thread_contention_keeps_unique_evidence_count(tmp_path):
    store = ModuleEvolutionManager(tmp_path / "evolution.jsonl", min_paired_samples=2)
    proposal_id = propose(store)
    row = evaluation(proposal_id, "one-identity")

    def record(_):
        try:
            store.record_pair(row)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(record, range(24)))
    assert sum(accepted) == 1
    assert store.paired_summary(proposal_id)["eligible_pairs"] == 1
    assert store.settle(proposal_id)["decision"] == "pending_evidence"


@pytest.mark.parametrize(
    "name",
    [
        "x_mean",
        "y_mean",
        "y_scale",
        "transition_weights",
        "progress_weights",
        "success_weights",
    ],
)
def test_all_checkpoint_numeric_arrays_require_finite_values(tmp_path, name):
    path = tmp_path / "model.npz"
    write_latent_checkpoint(path)
    with np.load(path, allow_pickle=False) as source:
        values = {key: source[key].copy() for key in source.files}
    values[name].flat[0] = math.nan
    np.savez(path, **values)
    with pytest.raises(BackendError, match="finite"):
        LightweightLatentDynamics(path)


@pytest.mark.parametrize("logit,expected", [(-1000.0, 0.0), (0.0, 0.5), (1000.0, 1.0)])
def test_stable_sigmoid_preserves_regular_and_extreme_outputs(
    tmp_path, logit, expected
):
    weights = np.zeros(81, dtype=np.float32)
    weights[-1] = logit
    path = tmp_path / "model.npz"
    write_latent_checkpoint(path, success_weights=weights)
    result = LightweightLatentDynamics(path).predict(np.zeros(32), np.zeros(48), 1)
    assert math.isfinite(result["success_probability"])
    assert result["success_probability"] == expected


@pytest.mark.parametrize("header", ["Basic", "basic", "BASIC", "Bearer"])
def test_authorization_scheme_redaction_is_complete_and_idempotent(header):
    secret = "audit-credential-value123456"
    value = header + " " + secret
    filtered = redact_secrets(value)
    assert secret not in filtered
    assert filtered == redact_secrets(filtered)
