"""Collect actual VLABench episode outcomes from explicit batch directories."""

import argparse
import json
from pathlib import Path

from vlabench_dual_check import pair_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.manifest.read_text())["tasks"]
    desired = {case["task_id"]: case for case in cases}
    collected = {"luban": {}, "openvla": {}}
    for directory in args.run_dir:
        source_cases = json.loads((directory / "manifest.json").read_text())["tasks"]
        for case in source_cases:
            key = case["task_id"]
            if key not in desired:
                continue
            for field in ("task", "episode", "seed", "horizon"):
                if case[field] != desired[key][field]:
                    raise ValueError(f"{key}: inconsistent {field} in {directory}")
            for method in collected:
                path = (
                    directory
                    / key
                    / method
                    / ("pair_result.json" if method == "luban" else "result.json")
                )
                if not path.exists():
                    continue
                if key in collected[method]:
                    raise ValueError(
                        f"Multiple {method} executions for {key}; specify one source"
                    )
                raw = json.loads(path.read_text())
                if method == "luban":
                    status = pair_status(raw["branches"])
                    success = status == "success"
                    error = "branch_execution_error" if status == "error" else None
                else:
                    success, error = raw["success"], raw["error"]
                collected[method][key] = {
                    **case,
                    "success": success,
                    "error": error,
                    "wall_seconds": raw.get("wall_seconds"),
                    "source_result": str(path.resolve()),
                    "source_error": raw.get("error"),
                }
    args.output.mkdir(parents=True, exist_ok=True)
    for method, records in collected.items():
        (args.output / f"{method}.jsonl").write_text(
            "".join(
                json.dumps(records[case["task_id"]], ensure_ascii=False) + "\n"
                for case in cases
                if case["task_id"] in records
            )
        )
        print(
            f"{method}: {len(records)}/{len(cases)} 回合有记录，"
            f"成功 {sum(r['success'] and not r['error'] for r in records.values())}，"
            f"运行异常 {sum(bool(r['error']) for r in records.values())}",
            flush=True,
        )


if __name__ == "__main__":
    main()
