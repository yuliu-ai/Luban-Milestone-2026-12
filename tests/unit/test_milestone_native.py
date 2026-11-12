"""Target-call accounting and the corrected fixed-table opening contract."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from luban.environments.atomic.native.bullet_manipulation_backend import (
    BulletManipulationEnvironment,
)


def test_prerequisite_success_does_not_replace_missing_target(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts/milestone"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "native_check", scripts / "native_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prerequisite = {"skill": "G03", "result": {"status": "succeeded"}}
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"records": [prerequisite]}))
    with pytest.raises(ValueError, match="Expected one I06 result"):
        module.evaluated_result(tmp_path, "I06")
    target = {"skill": "I06", "result": {"status": "failed"}}
    summary.write_text(json.dumps({"records": [prerequisite, target]}))
    result, preparation = module.evaluated_result(tmp_path, "I06")
    assert result["status"] == "failed"
    assert preparation == [prerequisite]


def test_open_parallel_gripper_refuses_closing_target():
    plant = SimpleNamespace(current={"time_s": 0, "arms": {"right": {"width_m": 0.07}}})
    environment = BulletManipulationEnvironment(plant)
    invocation = SimpleNamespace(
        inputs={"verification_profile_ref": environment.profile_ref, "width_m": 0.05},
        skill_ref=SimpleNamespace(skill_id="G01"),
    )
    with pytest.raises(ValueError, match="larger target width"):
        environment.execute(invocation, None)
