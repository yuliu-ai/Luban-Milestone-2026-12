"""导出 RoboCasa target50 的任务分类事实。

只读 registry，不改任何东西。用于确定 G2 的 8 个任务选择依据：
composite seen / composite unseen 各 4 个。
"""

import importlib
import json

registry = importlib.import_module("robocasa.utils.dataset_registry")
task_sets = registry.TASK_SET_REGISTRY

print("available task_sets:", sorted(task_sets))
target50 = list(dict.fromkeys(task_sets["target50"]))
print("target50 size:", len(target50))

# 找出保存每个任务元数据的字典
candidate_attrs = [
    name
    for name in dir(registry)
    if isinstance(getattr(registry, name), dict) and name != "TASK_SET_REGISTRY"
]
print("dict attrs:", candidate_attrs)

task_meta = None
for name in candidate_attrs:
    table = getattr(registry, name)
    if target50[0] in table:
        task_meta = (name, table)
        break

if task_meta is None:
    print("未找到任务元数据表")
    raise SystemExit(1)

table_name, table = task_meta
print("task metadata table:", table_name)
print("sample entry:", json.dumps(table[target50[0]], indent=2, default=str)[:1200])

rows = []
for task in target50:
    entry = table.get(task, {})
    human_path = entry.get("human_path")
    rows.append(
        {
            "task": task,
            "human_path": human_path,
            "keys": sorted(entry.keys()),
        }
    )

print("\n=== per-task human_path ===")
for row in rows:
    print(f"{row['task']:<32} {row['human_path']}")

with open("/tmp/g0/target50_meta.json", "w", encoding="utf-8") as stream:
    json.dump(rows, stream, indent=2, ensure_ascii=False, default=str)
print("\nwritten /tmp/g0/target50_meta.json")
