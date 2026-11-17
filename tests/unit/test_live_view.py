import io
import json
import threading
import time
import unittest
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

from luban.observability.live_view import LiveViewPublisher, LiveViewServer


class LiveViewTest(unittest.TestCase):
    def test_viewer_receives_updated_camera_frames(self):
        server = LiveViewServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        publisher = LiveViewPublisher(url, "task:1 / branch-0")
        try:
            for step, value in [(1, 25), (2, 225)]:
                publisher.last_frame = 0
                publisher.publish(
                    {"image.front": np.full((16, 16, 3), value, np.uint8)}, step
                )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(url + "/api/streams") as response:
                        streams = json.load(response)
                    if streams and streams[0]["step"] == str(step):
                        break
                    time.sleep(0.02)
                self.assertEqual(streams[0]["step"], str(step))
                name = urllib.parse.quote(streams[0]["name"])
                with urllib.request.urlopen(url + "/frame?name=" + name) as response:
                    image = Image.open(io.BytesIO(response.read()))
                    self.assertAlmostEqual(image.getpixel((4, 4))[0], value, delta=5)
            with urllib.request.urlopen(url) as response:
                self.assertIn("实时仿真画面", response.read().decode())
        finally:
            publisher.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
