import importlib.util
from pathlib import Path


def test_normal_policy_failure_is_distinct_from_missing_or_exceptional_execution():
    path = (
        Path(__file__).resolve().parents[2] / "scripts/milestone/vlabench_dual_check.py"
    )
    spec = importlib.util.spec_from_file_location("vlabench_dual_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    horizon = {
        "success": False,
        "exceptions": 0,
        "termination_reason": "environment_done",
    }
    invalid = {
        "success": False,
        "exceptions": 0,
        "termination_reason": "invalid_action_limit",
    }
    assert module.pair_status([horizon, invalid]) == "failure"
    assert module.pair_status([horizon, None]) == "error"
    assert module.pair_status([horizon, {**invalid, "exceptions": 1}]) == "error"
    success = {"success": True, "exceptions": 0, "termination_reason": "task_success"}
    interrupted = {
        "success": False,
        "exceptions": 1,
        "termination_reason": "interrupted",
    }
    assert module.pair_status([success, interrupted]) == "success"
