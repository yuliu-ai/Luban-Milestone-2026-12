import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from luban.service import make_server


@pytest.fixture()
def url() -> Iterator[str]:
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _post(url: str, path: str, body: Any) -> tuple[int, Any]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(
        url + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_episode_runs_the_agent_loop(url: str) -> None:
    status, payload = _post(url, "/v1/episodes/run", {"task": {"name": "increment"}})
    assert status == 200
    assert payload["result"]["success"] is True
    with urllib.request.urlopen(url + "/health", timeout=10) as response:
        health = json.loads(response.read())
    assert health["status"] == "ok" and health["requests_ok"] == 1


def test_task_graph_is_repaired_on_request(url: str) -> None:
    graph = {"nodes": [{"id": 1, "instruction": "grasp", "expected_outcome": "held"}]}
    status, payload = _post(url, "/v1/task-graph/validate", {"graph": graph})
    assert status == 200 and payload["result"]["repaired"] is True
    status, _ = _post(url, "/v1/task-graph/validate", {"graph": graph, "repair": False})
    assert status == 400


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{oops",
        [1],
        {"max_steps": 0},
        {"max_steps": True},
        {"unexpected": 1},
        {"task": {"name": 3}},
    ],
)
def test_bad_requests_are_rejected_without_stopping(url: str, body: Any) -> None:
    status, payload = _post(url, "/v1/episodes/run", body)
    assert status == 400 and "error" in payload
    status, payload = _post(url, "/v1/episodes/run", {})
    assert status == 200 and payload["result"]["success"] is True


def test_oversized_body_and_unknown_route(url: str) -> None:
    # The server rejects Content-Length before reading an oversized body.
    connection = http.client.HTTPConnection(url.removeprefix("http://"), timeout=10)
    connection.putrequest("POST", "/v1/episodes/run")
    connection.putheader("Content-Length", "1000102")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    response.read()
    connection.close()
    status, _ = _post(url, "/v1/unknown", {})
    assert status == 404


@pytest.mark.parametrize("max_steps,success", [(1, False), (3, True)])
def test_registered_run_uses_config_and_keeps_artifacts(
    tmp_path: Path, max_steps: int, success: bool
) -> None:
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "configs/runs/offline.yaml").read_text())
    config["agent_config"] = str(root / "configs/agents/offline.yaml")
    config["runtime"]["init_args"].update(
        max_steps=max_steps, output_dir=str(tmp_path / "results")
    )
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(config))
    server = make_server("127.0.0.1", 0, {"test": path})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        status, payload = _post(url, "/v1/runs", {"config": "test"})
        assert status == 200
        result = payload["result"]
        assert result["success"] is success
        assert result["service_function_seconds"] > 0
        assert payload["server_seconds"] >= result["service_function_seconds"]
        assert result["steps"] == min(max_steps, 2)
        assert Path(result["trace_path"]).is_file()
        for body in [{"config": str(path)}, {"config": "test", "task": {}}]:
            status, _ = _post(url, "/v1/runs", body)
            assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
