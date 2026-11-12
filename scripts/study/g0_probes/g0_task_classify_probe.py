"""导出 target50 的 domain_split 分类与 horizon，作为 G2 任务选择的事实依据。"""

import importlib
import json

registry = importlib.import_module("robocasa.utils.dataset_registry")
task_sets = registry.TASK_SET_REGISTRY
utils = importlib.import_module("robocasa.utils.dataset_registry_utils")

target50 = list(dict.fromkeys(task_sets["target50"]))
composite_seen = list(dict.fromkeys(task_sets["composite_seen"]))
composite_unseen = list(dict.fromkeys(task_sets["composite_unseen"]))
atomic_seen = list(dict.fromkeys(task_sets["atomic_seen"]))

composite = importlib.import_module(
    "robocasa.utils.dataset_registry"
).COMPOSITE_TASK_DATASETS
atomic = importlib.import_module("robocasa.utils.dataset_registry").ATOMIC_TASK_DATASETS


def classify(task):
    if task in composite_seen:
        return "composite_seen"
    if task in composite_unseen:
        return "composite_unseen"
    if task in atomic_seen:
        return "atomic_seen"
    return "unclassified"


rows = []
for task in target50:
    entry = composite.get(task) or atomic.get(task) or {}
    target_entry = entry.get("target") if isinstance(entry, dict) else None
    rows.append(
        {
            "task": task,
            "domain_split": classify(task),
            "horizon_registry": entry.get("horizon")
            if isinstance(entry, dict)
            else None,
            "horizon_utils": int(utils.get_task_horizon(task)),
            "target_human_path": (target_entry or {}).get("human_path"),
            "has_pretrain": bool((entry or {}).get("pretrain")),
        }
    )

print("target50 size:", len(target50))
print("composite_seen size:", len(composite_seen))
print("composite_unseen size:", len(composite_unseen))
print("atomic_seen size:", len(atomic_seen))

print("\n=== target50 x domain_split ===")
for row in rows:
    print(
        f"{row['domain_split']:<18} {row['task']:<30} horizon="
        f"{row['horizon_utils']:<6} "
        f"pretrain={row['has_pretrain']}"
    )

summary = {}
for row in rows:
    summary.setdefault(row["domain_split"], []).append(row["task"])
print("\n=== 分组计数 ===")
print(json.dumps({k: len(v) for k, v in summary.items()}, ensure_ascii=False))

with open("/tmp/g0/target50_classified.json", "w", encoding="utf-8") as stream:
    json.dump(
        {"rows": rows, "groups": summary, "horizon_source": "dataset_registry_utils"},
        stream,
        indent=2,
        ensure_ascii=False,
    )
print("written /tmp/g0/target50_classified.json")
