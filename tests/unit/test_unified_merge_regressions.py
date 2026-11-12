import sys
from pathlib import Path
from types import ModuleType

import pytest

from luban import cli
from luban.config import load_yaml


def test_yaml_expands_nested_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_TEST_MODEL_ROOT", "/tmp/example-model")
    config = tmp_path / "config.yaml"
    config.write_text(
        'model: "${OMNI_TEST_MODEL_ROOT}/policy"\n'
        'nested:\n  paths: ["$OMNI_TEST_MODEL_ROOT", 3, true]\n',
        encoding="utf-8",
    )
    assert load_yaml(config) == {
        "model": "/tmp/example-model/policy",
        "nested": {"paths": ["/tmp/example-model", 3, True]},
    }


def test_dashboard_cli_dispatches_without_loading_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str, int]] = []
    app = ModuleType("luban.dashboard.app")

    def launch(runs_dir: Path, *, host: str, port: int) -> None:
        calls.append((runs_dir, host, port))

    monkeypatch.setattr(app, "launch", launch, raising=False)
    monkeypatch.setitem(sys.modules, "luban.dashboard.app", app)
    monkeypatch.setattr(
        sys,
        "argv",
        ["luban", "dashboard", "--runs-dir", str(tmp_path), "--port", "7861"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    assert calls == [(tmp_path, "127.0.0.1", 7861)]
