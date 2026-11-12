import importlib.util
from pathlib import Path

import pytest


def load_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_xr1_dual.py"
    spec = importlib.util.spec_from_file_location("dual_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "statuses,expected,branch_count",
    [
        (["success", "wait"], "success", 2),
        (["failure", "failure"], "failure", 2),
        (["failure", "error"], "error", 2),
        (["success"], "success", 1),
        (["failure"], "failure", 1),
        (["error"], "error", 1),
    ],
)
def test_pair_outcome_and_success_stops_other_branch(
    tmp_path, statuses, expected, branch_count
):
    worker = tmp_path / "worker.py"
    worker.write_text("""
import json, pathlib, signal, sys, time
args = sys.argv
branch = int(args[args.index("--branch") + 1])
case_file = pathlib.Path(args[args.index("--case-file") + 1])
case = json.loads(case_file.read_text())
root = case_file.parent
status = case["statuses"][branch]
result_dir = root / f"branch-{branch}"
result_dir.mkdir()
def finish(reason, success=False, exceptions=0):
    (result_dir / "result.json").write_text(json.dumps({"termination_reason": reason,
                              "success": success, "exceptions": exceptions}))
if status == "wait":
    signal.signal(signal.SIGINT, lambda *_: (finish("interrupted"), sys.exit(0)))
    while True: time.sleep(0.05)
if status == "success":
    time.sleep(0.3)
    (root / f"success-{branch}").touch()
    finish("task_success", success=True)
elif status == "failure": finish("horizon")
else: finish("exception", exceptions=1)
""")
    runner = load_runner()
    runner.__file__ = str(worker)
    case = {"task": "Example", "episode": 0, "statuses": statuses}
    result = runner.run_pair(case, tmp_path / "results", 1, 0, branch_count)
    assert result["status"] == expected
    if expected == "success":
        assert result["winning_branch"] == 0
        if branch_count == 2:
            assert result["branches"][1]["termination_reason"] == "interrupted"
