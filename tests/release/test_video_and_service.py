"""Real FFmpeg output and isolated, model-free perception server robustness."""

import json
import shutil
import socket
import subprocess
import sys

import numpy as np
import pytest

from luban.backends.skills.rldx import _RLDXMsgSerializer
from luban.observability.video import EpisodeVideoWriter


@pytest.mark.parametrize("size", [(32, 32), (255, 255), (128, 96)])
def test_real_video_encodes_decodable_frames(tmp_path, size):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe unavailable")
    writer = EpisodeVideoWriter(tmp_path / "episode.mp4", ["front"], 4, ffmpeg)
    for i in range(4):
        writer.add_frame(
            {"front": np.full((*size, 3), i * 50, dtype=np.uint8)}, ["audit frame"]
        )
    result = writer.close()
    assert result["error"] is None, result["error"]
    result = json.loads(
        subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,nb_frames,width,height",
                "-of",
                "json",
                str(tmp_path / "episode.mp4"),
            ]
        )
    )
    stream = result["streams"][0]
    assert stream["codec_name"] == "h264" and int(stream["nb_frames"]) == 4
    subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(tmp_path / "episode.mp4"), "-f", "null", "-"],
        check=True,
        timeout=10,
        capture_output=True,
    )


@pytest.fixture
def perception_server():
    zmq = pytest.importorskip("zmq")
    pytest.importorskip("msgpack")

    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    port = reservation.getsockname()[1]
    reservation.close()
    code = (
        "from luban.perception.server import VisualPerceptionServer; "
        "VisualPerceptionServer(provider='sam3',analyzer=lambda payload: {})"
        ".serve_forever(bind_host='127.0.0.1',port=" + str(port) + ")"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = zmq.Context()

    def connect():
        s = context.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, 5000)
        s.setsockopt(zmq.SNDTIMEO, 5000)
        s.connect(f"tcp://127.0.0.1:{port}")
        return s

    client = connect()
    try:
        client.send(_RLDXMsgSerializer.to_bytes({"endpoint": "ping"}))
        assert _RLDXMsgSerializer.from_bytes(client.recv())["ok"]
        yield process, client
    finally:
        client.close(0)
        context.term()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


def test_perception_bad_logical_request_returns_error_and_survives(perception_server):
    process, client = perception_server
    client.send(_RLDXMsgSerializer.to_bytes({"endpoint": "invalid"}))
    assert _RLDXMsgSerializer.from_bytes(client.recv())["ok"] is False
    assert process.poll() is None


def test_perception_malformed_wire_does_not_kill_service(perception_server):
    process, client = perception_server
    client.send(b"\xc1")  # Invalid MessagePack byte, sent only to our new child.
    assert _RLDXMsgSerializer.from_bytes(client.recv())["ok"] is False
    client.send(_RLDXMsgSerializer.to_bytes({"endpoint": "ping"}))
    assert _RLDXMsgSerializer.from_bytes(client.recv())["ok"] is True
    assert process.poll() is None
