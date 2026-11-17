"""Optional browser view of live simulator camera observations."""

import io
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class LiveViewPublisher:
    def __init__(self, url: str, name: str) -> None:
        self.url = url.rstrip("/") + "/frame"
        self.name = name
        self.frames: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.last_frame = 0.0
        self.thread = threading.Thread(target=self._send, daemon=True)
        self.thread.start()

    @classmethod
    def from_environment(cls, task: Any, session: str) -> "LiveViewPublisher | None":
        url = os.environ.get("LUBAN_LIVE_VIEW_URL")
        if not url:
            return None
        name = task.get("name", "task") if isinstance(task, Mapping) else str(task)
        episode = task.get("episode_index", "") if isinstance(task, Mapping) else ""
        return cls(url, f"{name}:{episode} / {session} / {os.getpid()}")

    def publish(self, observation: Any, step: int) -> None:
        if not isinstance(observation, Mapping) or (
            time.monotonic() - self.last_frame < 0.5
        ):
            return
        for key in (
            "image.base",
            "video.robot0_agentview_left",
            "image.front",
            "head_rgb",
            "rgb",
        ):
            frame = observation.get(key)
            if isinstance(frame, list) and frame:
                frame = frame[min(2, len(frame) - 1)]
            if hasattr(frame, "shape") and len(frame.shape) == 4:
                frame = frame[min(2, len(frame) - 1)]
            if (
                hasattr(frame, "shape")
                and len(frame.shape) == 3
                and frame.shape[-1] in (3, 4)
            ):
                try:
                    self.frames.put_nowait((frame.copy(), step))
                    self.last_frame = time.monotonic()
                except queue.Full:
                    pass
                return

    def _send(self) -> None:
        from PIL import Image

        while not self.closed.is_set() or not self.frames.empty():
            try:
                item = self.frames.get(timeout=0.3)
            except queue.Empty:
                continue
            frame, step = item
            buffer = io.BytesIO()
            Image.fromarray(frame).convert("RGB").save(
                buffer, format="JPEG", quality=78
            )
            request = urllib.request.Request(
                self.url,
                data=buffer.getvalue(),
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Luban-Stream": urllib.parse.quote(self.name, safe=""),
                    "X-Luban-Step": str(step),
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=0.5):
                    pass
            except (OSError, urllib.error.URLError):
                pass

    def close(self) -> None:
        self.closed.set()


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Luban 实时仿真</title>
<style>
body{font:16px sans-serif;max-width:960px;margin:32px auto;padding:0 16px;color:#17212b}
select{font-size:15px;max-width:100%;padding:6px}
img{display:block;max-width:100%;width:720px;margin-top:18px;background:#eee}
#status{margin-top:12px}
</style>
<h2>Luban 实时仿真画面</h2><p>选择正在运行的任务或分支。</p>
<select id="streams"><option value="">等待仿真画面</option></select>
<div id="status"></div><img id="frame" alt="等待实时画面">
<script>
const s=document.querySelector('#streams'),img=document.querySelector('#frame');
const status=document.querySelector('#status');
async function update(){
  try{
    const rows=await(await fetch('/api/streams',{cache:'no-store'})).json();
    const previous=s.value;
    s.replaceChildren();
    if(!rows.length){s.add(new Option('等待仿真画面',''));return;}
    for(const row of rows)s.add(new Option(row.name,row.name));
    s.value=rows.some(r=>r.name===previous)?previous:rows[0].name;
    const row=rows.find(r=>r.name===s.value);
    status.textContent='步数 '+row.step+'  |  上次更新 '+row.age_s.toFixed(1)+' 秒前';
    img.src='/frame?name='+encodeURIComponent(s.value)+'&t='+Date.now();
  }catch(e){status.textContent='画面服务连接中断';}
}
s.onchange=update;update();setInterval(update,700);
</script></html>""".encode()


class LiveViewServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, LiveViewHandler)
        self.frames: dict[str, tuple[bytes, str, float]] = {}
        self.lock = threading.Lock()


class LiveViewHandler(BaseHTTPRequestHandler):
    server: LiveViewServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/frame":
            self._reply(404, "text/plain", b"not found")
            return
        name = urllib.parse.unquote(self.headers["X-Luban-Stream"])
        step = self.headers["X-Luban-Step"]
        image = self.rfile.read(int(self.headers["Content-Length"]))
        with self.server.lock:
            self.server.frames[name] = (image, step, time.time())
        self._reply(200, "text/plain", b"ok")

    def do_GET(self) -> None:
        target = urllib.parse.urlsplit(self.path)
        if target.path == "/":
            self._reply(200, "text/html; charset=utf-8", PAGE)
        elif target.path == "/api/streams":
            now = time.time()
            with self.server.lock:
                rows = [
                    {"name": name, "step": step, "age_s": now - timestamp}
                    for name, (_, step, timestamp) in self.server.frames.items()
                ]
            rows.sort(key=lambda item: item["age_s"])
            self._reply(200, "application/json", json.dumps(rows).encode())
        elif target.path == "/frame":
            name = urllib.parse.parse_qs(target.query).get("name", [""])[0]
            with self.server.lock:
                frame = self.server.frames.get(name)
            if frame:
                self._reply(200, "image/jpeg", frame[0])
            else:
                self._reply(404, "text/plain", b"not found")
        else:
            self._reply(404, "text/plain", b"not found")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    server = LiveViewServer(("127.0.0.1", args.port))
    print(f"Live simulation viewer: http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
