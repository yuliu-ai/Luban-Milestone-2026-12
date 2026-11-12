"""汇总 G0 双 worker 并发端到端结果。"""

import json
import sys

PATHS = [
    ("worker0", "/tmp/g0/e2e_worker0.json"),
    ("worker1", "/tmp/g0/e2e_worker1.json"),
]

for tag, path in PATHS:
    try:
        raw = open(path, encoding="utf-8").read()
        payload = json.loads(raw[raw.find("{") :])
    except Exception as error:
        print(tag, "PARSE_FAIL", type(error).__name__, error)
        continue

    health = payload.get("backend_healthcheck", {})
    print(
        "--- {!s}: port={!s} CUDA_VISIBLE_DEVICES={!s} reset_ok={!s}".format(
            *(
                tag,
                payload.get("port"),
                payload.get("CUDA_VISIBLE_DEVICES"),
                payload.get("reset_ok"),
            )
        )
    )
    print(
        "    healthy={!s} contract_mismatches={!s} protocol={!s}".format(
            *(
                health.get("healthy"),
                health.get("contract_mismatches"),
                health.get("protocol"),
            )
        )
    )
    print(
        "    total_env_steps={!s} requests={!s} failed={!s}".format(
            *(
                payload.get("total_env_steps"),
                payload.get("action_requests"),
                payload.get("failed_action_requests"),
            )
        )
    )
    for step in payload.get("steps", []):
        report = step.get("action_report", {}) or {}
        horizon = None
        dims = 0
        finite = True
        for value in report.values():
            horizon = value.get("horizon") or horizon
            dims += value.get("dim") or 0
            finite = finite and bool(value.get("finite"))
        print(
            "    step{!s}: "
            "predict_s={!s} "
            "horizon={!s} "
            "dim={!s} finite={!s} executed={!s} feedback={!s}"
            " success={!s}".format(
                *(
                    step.get("step_index"),
                    step.get("predict_seconds"),
                    horizon,
                    dims,
                    finite,
                    step.get("executed_steps"),
                    step.get("env_feedback"),
                    step.get("task_success"),
                )
            )
        )
        for key in ("predict_error", "execute_error"):
            if step.get(key):
                print("      {!s}: {!s}".format(*(key, step[key])))
    print(
        "    closed: backend={!s} env={!s}".format(
            *(payload.get("backend_closed"), payload.get("env_closed"))
        )
    )

sys.exit(0)
