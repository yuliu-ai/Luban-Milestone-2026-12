"""Serve SAM3 as the project's ``sam3`` perception provider.

``VisualPerceptionServer`` already owns the wire protocol, the request/response
validation and the rejection of privileged simulator provenance; this script only
supplies the analyzer that turns text queries plus RGB frames into entities.

Run it with a CUDA-capable interpreter (the model is ~3.4 GB in fp32/bf16):

    python scripts/serve_perception.py \
        --checkpoint /path/to/sam3.pt \
        --bpe-path /path/to/bpe_simple_vocab_16e6.txt.gz \
        --device cuda --host 127.0.0.1 --port 18200

P02 needs the mask itself, not just the box, so ``--mask-out-dir`` optionally
writes each mask as a PNG and puts its ``file://`` reference into the entity.
Without that flag the analyzer behaves exactly as before (``mask_ref`` stays
``None``), which keeps the already-running service path unchanged.

Environment overrides: ``LUBAN_SRC`` points at the checkout's ``src``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

DEFAULT_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _to_uint8_hwc(frame: Any) -> np.ndarray:
    """Normalize whatever the wire decoder produced into an HWC uint8 array."""

    array = np.asarray(frame)
    if array.ndim == 4:  # (T, H, W, C) temporal stack
        array = array[-1]
    if array.ndim != 3:
        raise ValueError(f"frame must be HWC or THWC, got shape {array.shape}")
    if array.shape[-1] not in (1, 3, 4) and array.shape[0] in (1, 3, 4):
        array = np.transpose(array, (1, 2, 0))
    if array.shape[-1] == 4:
        array = array[..., :3]
    if array.dtype != np.uint8:
        scale = 255.0 if float(array.max(initial=0)) <= 1.0 else 1.0
        array = np.clip(array * scale, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(array)


def _write_mask(mask_out_dir: str, name: str, mask: Any) -> str:
    """Write one mask as an 8-bit PNG and return its ``file://`` reference."""

    array: Any = mask
    if hasattr(array, "detach"):
        array = array.detach().cpu().numpy()
    array = np.asarray(array)
    if array.ndim == 3:
        array = array[0] if array.shape[0] == 1 else array.max(axis=0)
    if array.dtype == bool:
        array = array.astype(np.uint8) * 255
    elif not np.issubdtype(array.dtype, np.integer):
        array = (array > 0).astype(np.uint8) * 255
    else:
        array = np.clip(array, 0, 255).astype(np.uint8)
    path = Path(mask_out_dir).expanduser() / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array, mode="L").save(path)
    return f"file://{path.resolve()}"


def build_analyzer(processor: Any, mask_out_dir: str | None = None) -> Any:
    """Return the analyzer handed to ``VisualPerceptionServer``.

    ``mask_out_dir`` 为空时与之前逐字一致（``mask_ref`` 恒为 None）；给出目录时才把
    SAM3 的掩码写成 PNG 并把 ``file://`` 引用放进实体，供 P02 复核掩码本身。
    """

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
        if not queries:
            warnings.append("no non-empty queries supplied")
        for camera_id in sorted(frames):
            image = _to_uint8_hwc(frames[camera_id])
            # set_image 对 ndarray 走 shape[-2:]（按 CHW 解释）：传 HWC 数组会把
            # 高/宽与通道数错位成坐标 scale，框位置被错缩；PIL 分支用 image.size，语
            # 义明确。
            state = processor.set_image(Image.fromarray(image))
            for query in queries:
                state = processor.set_text_prompt(query, state)
                boxes = state["boxes"].detach().cpu().numpy()
                scores = state["scores"].detach().cpu().numpy()
                # SAM3 的分割输出。处理器不返回 masks 时保持 mask_ref=None，
                # 绝不拿 bbox 冒充掩码。
                masks = state.get("masks") if isinstance(state, Mapping) else None
                if mask_out_dir is not None and masks is None:
                    warnings.append(f"{camera_id}/{query}: processor returned no masks")
                for index in range(int(boxes.shape[0])):
                    box = [float(value) for value in boxes[index].tolist()]
                    score = float(scores[index])
                    mask_ref = None
                    if (
                        mask_out_dir is not None
                        and masks is not None
                        and index < len(masks)
                    ):
                        mask_ref = _write_mask(
                            mask_out_dir,
                            f"{camera_id}-{frame_index}-"
                            f"{query.strip().replace(' ', '_')}-{index}",
                            masks[index],
                        )
                    entities.append(
                        {
                            "track_id": f"{camera_id}/{frame_index}/{query}/{index}",
                            "label": query,
                            "confidence": max(0.0, min(1.0, score)),
                            "camera_id": camera_id,
                            "bbox_xyxy": box,
                            "point_xy": None,
                            "mask_ref": mask_ref,
                            "attributes": {
                                "model_family": "sam3",
                                "prompt_mode": "text",
                            },
                            "provenance": {
                                "source_provider": "sam3",
                                "source_modality": "rgb",
                                "frame_index": frame_index,
                                "camera_id": camera_id,
                                # 契约要求这里是字符串列表：本检测不派生自其它实体。
                                "derived_from": [],
                            },
                        }
                    )
        return {"entities": entities, "relations": [], "warnings": warnings}

    return analyze


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SAM3 perception server for Luban"
    )
    parser.add_argument(
        "--checkpoint",
        default=os.getenv("SAM3_CHECKPOINT"),
        required=not bool(os.getenv("SAM3_CHECKPOINT")),
    )
    # 省略即使用 sam3 包内默认的 assets/bpe_simple_vocab_16e6.txt.gz（CLIP BPE 词表），
    # 不是 ModelScope 仓库里的 merges.txt（那是 HF 格式，会被 gzip 读取拒绝）。
    parser.add_argument("--bpe-path", default=None)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--confidence-threshold", type=float, default=0.5)
    # P02 需要掩码本体；默认关闭，正在跑的 SAM3 服务路径不受影响。
    parser.add_argument("--mask-out-dir", default=None)
    args = parser.parse_args()

    sys.path.insert(0, os.environ.get("LUBAN_SRC", DEFAULT_SRC))

    from sam3 import build_sam3_image_model
    from sam3.model.sam3_image_processor import Sam3Processor

    from luban.perception.server import VisualPerceptionServer

    model = build_sam3_image_model(
        bpe_path=args.bpe_path,
        checkpoint_path=args.checkpoint,
        device=args.device,
        eval_mode=True,
        load_from_HF=False,
    )
    processor = Sam3Processor(
        model, device=args.device, confidence_threshold=args.confidence_threshold
    )
    server = VisualPerceptionServer(
        provider="sam3",
        analyzer=build_analyzer(processor, mask_out_dir=args.mask_out_dir),
        model_metadata={
            "model": "sam3",
            "checkpoint": os.path.basename(args.checkpoint),
            "device": args.device,
            "confidence_threshold": args.confidence_threshold,
            "mask_out_dir": args.mask_out_dir,
        },
    )
    print(
        f"sam3 perception server ready on {args.host}:{args.port} "
        f"(device={args.device}, threshold={args.confidence_threshold}, "
        f"mask_out_dir={args.mask_out_dir})",
        flush=True,
    )
    server.serve_forever(bind_host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
