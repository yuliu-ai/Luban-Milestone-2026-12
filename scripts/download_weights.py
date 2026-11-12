#!/usr/bin/env python3
"""Download model weights from Hugging Face with mirror fallback.

国内网络直连 huggingface.co 超时时,设置 HF 镜像即可,例如:

    export HF_ENDPOINT=https://hf-mirror.com
    python scripts/download_weights.py \
        --repo <org>/<model> --dest ~/models/<name>

示例(本项目常用的三类资产):

下载完成后,把目录的绝对路径写进对应的环境变量
(如 LUBAN_MODEL_PATH),或写入 agent 配置的 init_args。
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="HF repo id, e.g. org/model")
    parser.add_argument("--dest", required=True, help="local destination directory")
    parser.add_argument(
        "--revision",
        default=None,
        help="optional branch/tag/commit to pin (recommended for reproducibility)",
    )
    args = parser.parse_args()

    if os.environ.get("HF_ENDPOINT"):
        print(f"using HF mirror: {os.environ['HF_ENDPOINT']}")
    print("hint: unset HF_ENDPOINT to download from huggingface.co directly")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "huggingface_hub is required: pip install -U huggingface_hub",
            file=sys.stderr,
        )
        return 2

    path = snapshot_download(
        repo_id=args.repo,
        local_dir=args.dest,
        revision=args.revision,
    )
    print(f"downloaded to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
