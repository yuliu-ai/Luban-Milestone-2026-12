import json
import os
import sys

import torch


def probe(tag):
    try:
        assert torch.cuda.is_available()
        dev = torch.cuda.current_device()
        name = torch.cuda.get_device_name(dev)
        props = torch.cuda.get_device_properties(dev)
        a = torch.randn(2048, 2048, device="cuda")
        b = torch.randn(2048, 2048, device="cuda")
        c = (a @ b).sum()
        torch.cuda.synchronize()
        val = float(c.item())
        free, total = torch.cuda.mem_get_info()
        return {
            "tag": tag,
            "visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "ok": True,
            "current_device": dev,
            "device_name": name,
            "uuid": str(getattr(props, "uuid", "n/a")),
            "pci_bus_id": getattr(props, "pci_bus_id", "n/a"),
            "total_mem_gb": round(props.total_memory / 1024**3, 2),
            "free_gb": round(free / 1024**3, 2),
            "sum_result_finite": bool(val == val and abs(val) != float("inf")),
            "sum_abs": round(abs(val), 3),
        }
    except Exception as e:
        return {
            "tag": tag,
            "visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1] if len(sys.argv) > 1 else "default"), indent=2))
