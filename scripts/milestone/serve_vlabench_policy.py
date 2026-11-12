"""Serve VLABench XR-1 or merged OpenVLA weights with their native action conventions.

XR-1 preprocessing follows Xiaomi-Robotics-1/eval_vlabench/main.py (Apache-2.0).
OpenVLA preprocessing follows VLABench/evaluation/model/policy/openvla.py.
"""

import argparse
import base64
import io
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["xr1", "openvla"], required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--attention", default="eager")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    args.output.mkdir(parents=True, exist_ok=False)
    import numpy as np
    import torch
    from PIL import Image
    from transformers import AutoModel, AutoModelForVision2Seq, AutoProcessor

    processor = AutoProcessor.from_pretrained(
        args.model_path, trust_remote_code=True, local_files_only=True, use_fast=False
    )
    if args.provider == "xr1":
        model = (
            AutoModel.from_pretrained(
                args.model_path,
                trust_remote_code=True,
                local_files_only=True,
                dtype=torch.bfloat16,
                attn_implementation=args.attention,
            )
            .to("cuda")
            .eval()
        )
    else:
        # This VLABench checkpoint already contains the merged model, without a PEFT adapter.
        model = (
            AutoModelForVision2Seq.from_pretrained(
                args.model_path,
                trust_remote_code=True,
                local_files_only=True,
                torch_dtype=torch.bfloat16,
                low_cpu_mem_usage=True,
                attn_implementation=args.attention,
            )
            .to("cuda")
            .eval()
        )
        if "primitive" not in model.norm_stats:
            raise ValueError("VLABench primitive normalization is absent")
    metadata = {
        "provider": args.provider,
        "model_path": str(args.model_path),
        "gpu": args.gpu,
        "pid": os.getpid(),
        "action_mode": "ee_delta",
        "action_dim": 7,
        "unnorm_key": "primitive" if args.provider == "openvla" else "vlabench_choice",
    }
    (args.output / "model.json").write_text(json.dumps(metadata, indent=2))

    def predict(payload):
        images = [
            Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")
            for value in payload["images"]
        ]
        instruction = payload["instruction"]
        started = time.monotonic()
        with torch.inference_mode():
            if args.provider == "xr1":
                images = [
                    image.resize((480, 480), Image.Resampling.BILINEAR)
                    for image in images
                ]
                content = []
                for index, (name, image) in enumerate(
                    zip(
                        ["Ego View", "Base View", "Left-Wrist View"],
                        images,
                        strict=True,
                    )
                ):
                    prefix = (
                        "The following observations are captured from multiple views.\n"
                        if index == 0
                        else "\n"
                    )
                    content.extend(
                        [
                            {"type": "text", "text": prefix + "# " + name + "\n"},
                            {"type": "image", "image": image},
                        ]
                    )
                content.append(
                    {
                        "type": "text",
                        "text": "\nGenerate robot actions for the task:\n"
                        + instruction
                        + " /no_cot",
                    }
                )
                messages = [
                    {"role": "user", "content": content},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "<cot></cot>"}],
                    },
                ]
                state = np.zeros((1, 1, 60), dtype=np.float32)
                state[0, 0, :7] = np.asarray(payload["state"], dtype=np.float32)
                inputs = processor.apply_chat_template(
                    messages,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    state=state,
                    robot_type="vlabench_choice",
                )
                inputs = {
                    key: value.to(device="cuda", dtype=torch.bfloat16)
                    if isinstance(value, torch.Tensor) and value.is_floating_point()
                    else value.to("cuda")
                    if isinstance(value, torch.Tensor)
                    else value
                    for key, value in inputs.items()
                }
                inputs.update(
                    task_id="vlabench_choice", seed=int(payload.get("seed", 42))
                )
                raw = model(**inputs).actions
                decoded = processor.decode_action(raw, robot_type="vlabench_choice")
                if isinstance(decoded, torch.Tensor):
                    decoded = decoded.detach().float().cpu().numpy()
                actions = np.asarray(decoded)[0, :, :7]
            else:
                prompt = f"In: What action should the robot take to {instruction.lower()}?\nOut: "
                prompt = prompt.replace("_seen", "").replace("_unseen", "")
                inputs = processor(prompt, images[0]).to("cuda", dtype=torch.bfloat16)
                action = model.predict_action(
                    **inputs, unnorm_key="primitive", do_sample=False
                )
                actions = np.asarray(action).reshape(1, 7)
        if actions.ndim != 2 or actions.shape[1] != 7 or not np.isfinite(actions).all():
            raise ValueError("Policy returned invalid VLABench actions")
        result = {
            "actions": actions.tolist(),
            "seconds": time.monotonic() - started,
            "provider": args.provider,
            "action_mode": "ee_delta",
        }
        with (args.output / "inference.jsonl").open("a") as log:
            log.write(
                json.dumps(
                    {
                        "session_id": payload.get("session_id"),
                        "step": payload.get("step"),
                        **result,
                    }
                )
                + "\n"
            )
        return result

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.respond(200, {"healthy": True, **metadata})

        def do_POST(self):
            try:
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                self.respond(200, predict(payload))
            except Exception as error:
                traceback.print_exc()
                self.respond(500, {"error": f"{type(error).__name__}: {error}"})

    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(json.dumps({"ready": True, "port": args.port, **metadata}), flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
