"""Serve frozen SAM3/Molmo2 SDKs without importing the unified Agent."""

from __future__ import annotations

import argparse
import base64
import fcntl
import io
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4


def parse_points(text: str, width: int, height: int) -> list[list[float]]:
    """Decode explicit Molmo2 0–1000 coordinates; never guess missing points."""
    points = []
    for coords in re.findall(r'coords=["\']([^"\']+)["\']', text):
        numbers = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", coords)]
        if len(numbers) % 3:
            continue
        for index in range(0, len(numbers), 3):
            _, x, y = numbers[index : index + 3]
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                points.append([x * (width - 1) / 1000, y * (height - 1) / 1000])
    return points


def reserve_model_call(path: Path, provider: str) -> int:
    """Count every attempted forward pass against the shared frozen budget."""
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ledger = json.load(stream)
        if ledger["used"] >= ledger["limit"]:
            raise ValueError("Local model inference budget exhausted")
        ledger["used"] += 1
        ledger[provider] = ledger.get(provider, 0) + 1
        stream.seek(0)
        stream.truncate()
        json.dump(ledger, stream, indent=2)
        stream.write("\n")
        stream.flush()
        return ledger["used"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=["sam3", "molmo2", "smolvlm2", "metric_depth", "qwen3vl"],
        required=True,
    )
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if not 1 <= args.max_new_tokens <= 256:
        raise ValueError("Token limit must be within the frozen profile")
    import numpy as np
    import torch
    from PIL import Image

    if args.provider == "sam3":
        from sam3 import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor

        model = build_sam3_image_model(
            checkpoint_path=str(args.model_path),
            device="cuda",
            eval_mode=True,
            load_from_HF=False,
        )
        processor = Sam3Processor(model, device="cuda", confidence_threshold=0.3)
    elif args.provider == "metric_depth":
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation

        processor = AutoImageProcessor.from_pretrained(
            args.model_path, local_files_only=True
        )
        model = (
            AutoModelForDepthEstimation.from_pretrained(
                args.model_path, local_files_only=True
            )
            .to("cuda")
            .eval()
        )
    else:
        from transformers import AutoModelForImageTextToText, AutoProcessor

        processor = AutoProcessor.from_pretrained(
            args.model_path, trust_remote_code=True, local_files_only=True
        )
        if args.provider == "molmo2":
            processor.image_processor.max_crops = 2
        model = (
            AutoModelForImageTextToText.from_pretrained(
                args.model_path,
                trust_remote_code=True,
                local_files_only=True,
                dtype=torch.bfloat16,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )

    metadata = {
        "pid": os.getpid(),
        "process_group": os.getpgrp(),
        "provider": args.provider,
        "python": sys.version,
        "torch": torch.__version__,
        "gpu": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "model": {"checkpoint": args.model_path.name},
        "model_path": str(args.model_path),
        "sam3_threshold": 0.3,
        "molmo2_max_crops": 2 if args.provider == "molmo2" else None,
        "max_new_tokens": args.max_new_tokens,
        "metric_depth_type": getattr(
            getattr(model, "config", None), "depth_estimation_type", None
        ),
        "metric_max_depth_m": getattr(
            getattr(model, "config", None), "max_depth", None
        ),
    }

    (args.output / "model_identity.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )

    def generate(image, prompt):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )
        inputs = {
            key: value.to(model.device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            tokens = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                tokenizer=processor.tokenizer,
                stop_strings="<|endoftext|>",
            )
        return processor.tokenizer.decode(
            tokens[0, inputs["input_ids"].shape[1] :], skip_special_tokens=True
        )

    def analyze(payload):
        start = time.perf_counter()
        entities, answers, calls = [], [], []
        request_id = uuid4().hex
        for camera, encoded in payload["frames"].items():
            raw = base64.b64decode(encoded, validate=True)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            if image.width > 1024 or image.height > 1024:
                raise ValueError("Frame exceeds frozen 1024px limit")
            state = None
            for query in (
                payload["queries"][:1]
                if payload.get("task", {}).get("mode") == "grounding"
                else payload["queries"]
            ):
                call_id = reserve_model_call(args.budget, args.provider)
                calls.append(call_id)
                if args.provider == "metric_depth":
                    pixels = processor(images=image, return_tensors="pt").to(
                        model.device
                    )
                    with torch.inference_mode():
                        predicted = model(**pixels).predicted_depth
                        resized = torch.nn.functional.interpolate(
                            predicted[:, None],
                            size=(image.height, image.width),
                            mode="bicubic",
                            align_corners=False,
                        )[0, 0]
                    depth = resized.float().cpu().numpy()
                    depth_range = payload.get("task", {}).get(
                        "depth_range_m", [0.1, 3.0]
                    )
                    valid = (
                        np.isfinite(depth)
                        & (depth >= depth_range[0])
                        & (depth <= depth_range[1])
                    )
                    depth = np.where(valid, depth, np.nan).astype(np.float32)
                    depth_path = args.output / f"{request_id}-{call_id}-depth.npy"
                    confidence_path = (
                        args.output / f"{request_id}-{call_id}-validity.npy"
                    )
                    np.save(depth_path, depth)
                    np.save(confidence_path, valid.astype(np.float32))
                    answers.append(
                        {
                            "query": query,
                            "depth_ref": str(depth_path),
                            "confidence_ref": str(confidence_path),
                            "valid_ratio": float(valid.mean()),
                            "confidence_kind": (
                                "valid-range indicator; "
                                "no model uncertainty calibration"
                            ),
                        }
                    )
                elif args.provider == "sam3":
                    with (
                        torch.inference_mode(),
                        torch.autocast("cuda", dtype=torch.bfloat16),
                    ):
                        if state is None:
                            state = processor.set_image(image)
                        state = processor.set_text_prompt(query, state)
                        box = payload.get("task", {}).get("box_xyxy")
                        if box is not None:
                            x1, y1, x2, y2 = map(float, box)
                            state = processor.add_geometric_prompt(
                                [
                                    (x1 + x2) / (2 * image.width),
                                    (y1 + y2) / (2 * image.height),
                                    (x2 - x1) / image.width,
                                    (y2 - y1) / image.height,
                                ],
                                True,
                                state,
                            )
                    boxes = state["boxes"].detach().float().cpu().numpy()
                    scores = state["scores"].detach().float().cpu().numpy()
                    masks = state["masks"].detach().float().cpu().numpy()
                    for index, (box, score, mask) in enumerate(
                        zip(boxes, scores, masks, strict=True)
                    ):
                        mask_path = args.output / f"{request_id}-{call_id}-{index}.png"
                        Image.fromarray(
                            (np.squeeze(mask) > 0).astype(np.uint8) * 255
                        ).save(mask_path)
                        entities.append(
                            {
                                "track_id": f"sam3/{request_id}/{call_id}/{index}",
                                "label": query,
                                "confidence": float(score),
                                "bbox_xyxy": box.tolist(),
                                "camera_id": camera,
                                "mask_ref": str(mask_path),
                            }
                        )
                else:
                    task = payload.get("task", {})
                    if task.get("mode") == "grounding":
                        prompt = (
                            "Locate every separate visible instance of these object classes: "
                            + ", ".join(payload["queries"])
                            + ". Use the exact class names. Return only a JSON array of objects, each with label and bbox_xyxy. Bounding box coordinates must be integers normalized to 0-1000: [left,top,right,bottom]. Do not label the countertop, sink or knife block as a requested movable object. Return [] when absent."
                        )
                    elif task.get("mode") == "vqa":
                        candidates = task.get("candidate_values", {}).get(query, [])
                        if not candidates:
                            candidates = task.get("allowed_values", {}).get(query, [])
                        question = {
                            "color": "What is the dominant color of the central object?",
                            "material": "What visible surface material does the central object appear to have?",
                            "shape": "What is the geometric shape of the central object?",
                            "texture": "What visible texture does the central object have?",
                            "tone": "What is the brightness tone of the central object?",
                            "cleanliness": "Does the central object look clean or visibly dirty?",
                            "open_state": "Look at the main kitchen cabinet. Are its storage shelves exposed because its doors are open, or hidden behind shut door panels? If the cabinet interior or shelves are exposed, answer open. If door panels cover the front, answer closed.",
                        }.get(
                            query,
                            "What is the requested visual attribute " + query + "?",
                        )
                        prompt = (
                            question
                            + " Answer exactly one option: "
                            + ", ".join(map(str, candidates))
                            + ". Answer unknown if the image cannot show it."
                        )
                    else:
                        prompt = (
                            f"Point out every {query} in this image. Report as "
                            '<points coords="1 X Y 2 X Y"/> where X and Y are '
                            "integers between 0 and 1000. Return <points/> if absent."
                        )
                    raw_answer = generate(image, prompt)
                    answers.append({"query": query, "text": raw_answer})
                    if task.get("mode") == "vqa":
                        entities.append(
                            {
                                "track_id": f"{args.provider}/{request_id}/{call_id}",
                                "label": query,
                                "confidence": 1.0,
                                "camera_id": camera,
                                "attributes": {"answer": raw_answer},
                            }
                        )
                    elif task.get("mode") != "grounding":
                        for index, point in enumerate(
                            parse_points(raw_answer, image.width, image.height)
                        ):
                            entities.append(
                                {
                                    "track_id": (
                                        f"molmo2/{request_id}/{call_id}/{index}"
                                    ),
                                    "label": query,
                                    "confidence": 1.0,
                                    "point_xy": point,
                                    "camera_id": camera,
                                }
                            )
        response = {
            "provider": args.provider,
            "server": metadata,
            "entities": entities,
            "relations": [],
            "warnings": [],
            "raw_answers": answers,
            "latency_ms": (time.perf_counter() - start) * 1000,
            "model_call_ids": calls,
            "session_id": payload["session_id"],
            "frame_index": payload["frame_index"],
        }
        with (args.output / "exchanges.jsonl").open("a") as log:
            log.write(json.dumps(response, allow_nan=False) + "\n")
        return response

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200, {"healthy": True, **metadata})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/analyze" or not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("Invalid model request")
                payload = json.loads(self.rfile.read(length))
                if not 1 <= len(payload["queries"]) <= 8:
                    raise ValueError("Expected 1–8 model queries")
                self.respond(200, analyze(payload))
            except Exception as error:  # model boundary, all attempts stay in budget
                source = locals().get("payload", {})
                record = {
                    "error": f"{type(error).__name__}: {error}",
                    "provider": args.provider,
                    "at_s": time.time(),
                    "session_id": source.get("session_id")
                    if isinstance(source, dict)
                    else None,
                    "frame_index": source.get("frame_index")
                    if isinstance(source, dict)
                    else None,
                }
                with (args.output / "errors.jsonl").open("a") as log:
                    log.write(json.dumps(record) + "\n")
                print(json.dumps(record), flush=True)
                self.respond(400, record)

    print(json.dumps({"ready": True, "port": args.port, **metadata}), flush=True)
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
