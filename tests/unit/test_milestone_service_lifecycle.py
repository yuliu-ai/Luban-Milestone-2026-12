import importlib.util
import os
import sys
from pathlib import Path

import pytest


def test_owned_service_stops_when_test_body_raises(tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts/milestone/run_suite.py"
    spec = importlib.util.spec_from_file_location("milestone_run_suite", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pid_file = tmp_path / "pid"
    code = (
        "import os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "print('{\"ready\": true}',flush=True); time.sleep(60)"
    )
    with pytest.raises(ValueError, match="test stopped"):
        with module.services(
            [("owned", [sys.executable, "-u", "-c", code, str(pid_file)], {})],
            tmp_path / "logs",
            dict(os.environ),
        ):
            pid = int(pid_file.read_text())
            os.kill(pid, 0)
            raise ValueError("test stopped")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_failed_model_start_does_not_wait_for_readiness_timeout(tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts/milestone/run_suite.py"
    spec = importlib.util.spec_from_file_location("milestone_run_suite", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="exited 7"):
        with module.services(
            [("broken", [sys.executable, "-c", "raise SystemExit(7)"], {})],
            tmp_path / "logs",
            dict(os.environ),
        ):
            pytest.fail("failed service cannot be ready")
