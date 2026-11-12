"""Serve Molmo2 as the catalogue's second perception provider.

SAM3 and Molmo2 are the two whitelisted providers precisely so one can check the
other: Molmo2 is an open-vocabulary pointing VLM, so it returns points rather than
boxes, which is enough for a *second opinion* on whether a queried concept is
present at roughly the place SAM3 reported.

Reuses the project's own prompt compiler and response decoder
(``luban.perception.server``) so the wire contract stays identical to SAM3.

Example: python scripts/serve_perception_molmo2.py --help
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_SRC = str(Path(__file__).resolve().parents[1] / "src")
POINT_PROMPT = (
    "Point out every {concept} in this image. "
    'Report each one as a point in the format <points coords="1 X Y"/> '
    "where X and Y are integers between 0 and 1000."
)


def _to_pil(frame: Any):
    from PIL import Image

    array = np.asarray(frame)
    if array.ndim == 4:
        array = array[-1]
    if array.ndim == 2:
        array = np.stack([array] * 3, axis=-1)
    if array.shape[-1] == 4:
        array = array[..., :3]
    if array.dtype != np.uint8:
        scale = 255.0 if float(array.max(initial=0)) <= 1.0 else 1.0
        array = np.clip(array * scale, 0, 255).astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(array))


def build_analyzer(model: Any, processor: Any, *, max_new_tokens: int) -> Any:
    from luban.perception.server import (
        compile_concept_queries,
        extract_molmo2_multi_image_points,
    )

    # P06 复用同一个服务做属性问答：模式由请求的 ``task.mode`` 选择，pointing
    # 分支（P01 的第二来源复核）因此完全不受影响。
    vqa_mode = "vqa"
    vqa_prompt = (
        "Look at the object in this image and answer the question.\n"
        "Question: {question}\n"
        "Reply with {instruction}"
    )

    def _build_inputs(image: Any, prompt: str) -> Mapping[str, Any]:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "image": image},
                ],
            }
        ]
        return processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )

    def _generate(image: Any, prompt: str) -> str:
        import torch

        inputs = _build_inputs(image, prompt)
        inputs = {
            key: (value.to(model.device) if hasattr(value, "to") else value)
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                stop_strings="<|endoftext|>",
                tokenizer=processor.tokenizer,
            )
        prompt_len = inputs["input_ids"].size(1)
        return processor.tokenizer.decode(
            generated[0, prompt_len:], skip_special_tokens=True
        )

    def _build_vqa_prompt(key: str, candidate_values: Any) -> str:
        allowed: list[str] | None = None
        if isinstance(candidate_values, Mapping):
            value = candidate_values.get(key)
            if isinstance(value, (list, tuple)) and value:
                allowed = [str(item) for item in value]
        instruction = (
            "exactly one of these options: " + ", ".join(allowed)
            if allowed
            else "at most three words, or 'unknown' if the image does not show it"
        )
        question = f"What is the {str(key).replace('_', ' ')} of the object?"
        return vqa_prompt.format(question=question, instruction=instruction)

    def analyze(payload: Mapping[str, Any]) -> dict[str, Any]:
        queries = [
            item
            for item in payload["queries"]
            if isinstance(item, str) and item.strip()
        ]
        frames = payload["frames"]
        frame_index = int(payload["frame_index"])
        entities: list[dict[str, Any]] = []
        warnings: list[str] = []
        task = payload.get("task")
        mode = task.get("mode") if isinstance(task, Mapping) else None

        if mode == vqa_mode:
            candidate_values = task.get("candidate_values")
            # 问答模式没有可用的模型置信度，如实声明而不是编造一个数。
            warnings.append(
                "vqa mode: the model reports no calibrated confidence; entity "
                "confidence is 0.0 and means 'unavailable'"
            )
            for camera_id in sorted(frames):
                image = _to_pil(frames[camera_id])
                for key in queries:
                    prompt = _build_vqa_prompt(key, candidate_values)
                    try:
                        answer = _generate(image, prompt).strip()
                    except Exception as error:  # noqa: BLE001 - model boundary
                        warnings.append(
                            f"{key}: generation failed ("
                            f"{type(error).__name__}: {error})"
                        )
                        continue
                    entities.append(
                        {
                            "track_id": f"{camera_id}/{frame_index}/{key}/answer",
                            "label": key,
                            "confidence": 0.0,
                            "camera_id": camera_id,
                            "bbox_xyxy": None,
                            "point_xy": None,
                            "mask_ref": None,
                            "attributes": {
                                "model_family": "molmo2",
                                "prompt_mode": "vqa",
                                "answer": answer,
                                "question": prompt,
                                "confidence_source": "unavailable",
                            },
                            "provenance": {
                                "source_provider": "molmo2",
                                "source_modality": "rgb",
                                "frame_index": frame_index,
                                "camera_id": camera_id,
                                "derived_from": [],
                            },
                        }
                    )
            return {"entities": entities, "relations": [], "warnings": warnings}

        concepts = compile_concept_queries(queries) or ["object"]
        for camera_id in sorted(frames):
            image = _to_pil(frames[camera_id])
            size = (image.width, image.height)
            for concept in concepts:
                prompt = POINT_PROMPT.format(concept=concept)
                try:
                    text = _generate(image, prompt)
                except Exception as error:  # noqa: BLE001 - model boundary
                    warnings.append(
                        f"{concept}: generation failed ("
                        f"{type(error).__name__}: {error})"
                    )
                    continue

                points = extract_molmo2_multi_image_points(text, [size])
                if not points:
                    warnings.append(f"{concept}: no points decoded from response")
                    continue
                for frame_id, point_id, x, y in points:
                    entities.append(
                        {
                            "track_id": f"{camera_id}/{frame_index}/"
                            f"{concept}/{point_id}",
                            "label": concept,
                            "confidence": 1.0,
                            "camera_id": camera_id,
                            "bbox_xyxy": None,
                            "point_xy": [float(x), float(y)],
                            "mask_ref": None,
                            "attributes": {
                                "model_family": "molmo2",
                                "prompt_mode": "pointing",
                                "raw_frame_id": frame_id,
                            },
                            "provenance": {
                                "source_provider": "molmo2",
                                "source_modality": "rgb",
                                "frame_index": frame_index,
                                "camera_id": camera_id,
                                "derived_from": [],
                            },
                        }
                    )
        return {"entities": entities, "relations": [], "warnings": warnings}

    return analyze


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Molmo2 perception server for Luban"
    )
    parser.add_argument(
        "--model-path",
        default=os.getenv("MOLMO2_MODEL_PATH"),
        required=not bool(os.getenv("MOLMO2_MODEL_PATH")),
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"]
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18300)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    args = parser.parse_args()

    sys.path.insert(0, os.environ.get("LUBAN_SRC", DEFAULT_SRC))

    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    from luban.perception.server import VisualPerceptionServer

    dtype = getattr(torch, args.dtype)
    started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(args.model_path, trust_remote_code=True)
    model = AutoModelForImageTextToText.from_pretrained(
        args.model_path,
        trust_remote_code=True,
        torch_dtype=dtype,
        device_map=args.device,
    )
    model.eval()
    print(
        f"molmo2 loaded in {time.perf_counter() - started:.1f}s "
        f"(processor={type(processor).__name__}, dtype={args.dtype})",
        flush=True,
    )

    server = VisualPerceptionServer(
        provider="molmo2",
        analyzer=build_analyzer(model, processor, max_new_tokens=args.max_new_tokens),
        model_metadata={
            "model": "molmo2-4b",
            "checkpoint": os.path.basename(args.model_path.rstrip("/")),
            "device": args.device,
            "dtype": args.dtype,
        },
    )
    print(
        f"molmo2 perception server ready on {args.host}:"
        f"{args.port} (device={args.device})",
        flush=True,
    )
    server.serve_forever(bind_host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
