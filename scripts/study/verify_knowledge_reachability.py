#!/usr/bin/env python3
"""Reachability gate check for the hand-written platform-boundary knowledge set.

Retrieval (``agent_core/knowledge.py``) keeps an entry only when
``required_capabilities ⊆ query.capabilities`` holds and the tag-overlap score is
above zero.  On RoboCasa365 + XR-1 the v23 agent declares exactly five capability
words, so almost all of the 129 contract-derived entries in
``contract_knowledge_v1.jsonl`` never survive the gate: the words they require
(force_torque_sensor, transform_tree, localization, flight_* ...) are channels the
platform does not have.

This script pins both sides of that fact:

* baseline — how many ``contract_knowledge_v1.jsonl`` entries pass the capability
  gate for the v23 declaration, and how many are returned end-to-end for a query
  that runs one of the eight verified skills (P01/P02/P06/P12/M01-M04);
* hand-written set — every entry of ``contract_knowledge_reachable_v1.jsonl`` must
  use only declared capability words and must come back through the real retrieval
  path for the skill it describes.

    PYTHONPATH=src python scripts/study/verify_knowledge_reachability.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core.knowledge import JsonlKnowledgeBase  # noqa: E402

TASK_NAME = "ScrubCuttingBoard"
TASK_SET = "target50"
VERIFIED_SKILLS = ("P01", "P02", "P06", "P12", "M01", "M02", "M03", "M04")


def _agent_boundary(agent_config: Path) -> tuple[str, list[str]]:
    data = yaml.safe_load(agent_config.read_text(encoding="utf-8"))
    init_args = data["agent"]["init_args"]
    return init_args["robot_profile"], list(init_args["robot_capabilities"])


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)["entry"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _skill_requires(catalog: Path) -> dict[str, list[str]]:
    data = json.loads(catalog.read_text(encoding="utf-8"))
    return {
        skill["id"]: list(skill.get("requires_capabilities", []))
        for skill in data["skills"]
        if skill.get("id") in VERIFIED_SKILLS
    }


def _gate_ids(records: list[dict], capabilities: list[str]) -> list[str]:
    words = set(capabilities)
    return [
        entry["knowledge_id"]
        for entry in records
        if set(entry.get("required_capabilities", ())) <= words
    ]


def _query(
    profile: str,
    capabilities: list[str],
    skill: str | None = None,
    *,
    with_task: bool,
) -> dict:
    query: dict = {
        "robot_profile": profile,
        "capabilities": list(dict.fromkeys(capabilities)),
    }
    if skill is not None:
        query["atomic_skill_id"] = skill
        query["node_id"] = f"node:{skill}"
    if with_task:
        query["task_name"] = TASK_NAME
        query["task_set"] = TASK_SET
    return query


def _queries_for(
    entry: dict, profile: str, capabilities: list[str], requires: dict
) -> list[dict]:
    skills = [
        value.split(":", 1)[-1]
        for value in entry.get("applicability", ())
        if value.split(":", 1)[-1] in VERIFIED_SKILLS
    ]
    queries: list[dict] = []
    for skill in dict.fromkeys(skills):
        caps = capabilities + requires.get(skill, [])
        queries.append(_query(profile, caps, skill, with_task=True))
    if not queries:
        queries.append(_query(profile, capabilities, with_task=True))
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description="knowledge reachability gate check")
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=REPO_ROOT / "configs/agents/self_evolving_xr1_atomic_v23.yaml",
    )
    parser.add_argument(
        "--catalog", type=Path, default=REPO_ROOT / "catalogs/v2.2/atomic_skills.json"
    )
    parser.add_argument(
        "--knowledge",
        type=Path,
        default=REPO_ROOT / "configs/knowledge/contract_knowledge_reachable_v1.jsonl",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "configs/knowledge/contract_knowledge_v1.jsonl",
    )
    args = parser.parse_args()

    profile, capabilities = _agent_boundary(args.agent_config)
    requires = _skill_requires(args.catalog)
    print(f"agent boundary : profile={profile}")
    print(f"                 capabilities={capabilities}")

    baseline = _records(args.baseline)
    gate_ids = _gate_ids(baseline, capabilities)
    baseline_index = JsonlKnowledgeBase(args.baseline)
    reachable: set[str] = set()
    for skill in VERIFIED_SKILLS:
        caps = capabilities + requires.get(skill, [])
        for query in (
            _query(profile, caps, skill, with_task=True),
            _query(profile, caps, with_task=True),
        ):
            reachable |= {
                entry.knowledge_id
                for entry in baseline_index.retrieve(query, limit=len(baseline))
            }
    print(
        f"baseline       : {len(baseline)} entries, "
        f"{len(gate_ids)} pass the capability "
        f"gate, {len(reachable)} reachable end-to-end"
    )
    print("  gate-passing : " + ", ".join(sorted(gate_ids)))
    print("  end-to-end   : " + ", ".join(sorted(reachable)))

    entries = _records(args.knowledge)
    index = JsonlKnowledgeBase(args.knowledge)
    failures: list[str] = []
    for entry in entries:
        knowledge_id = entry["knowledge_id"]
        outside = set(entry.get("required_capabilities", ())) - set(capabilities)
        if outside:
            failures.append(
                f"{knowledge_id}: capability words outside the v23 declaration "
                f"{sorted(outside)}"
            )
            print(f"  FAIL {knowledge_id} (undeclared capability words)")
            continue
        queries = _queries_for(entry, profile, capabilities, requires)
        hit = any(
            knowledge_id
            in {item.knowledge_id for item in index.retrieve(query, limit=len(entries))}
            for query in queries
        )
        if not hit:
            failures.append(f"{knowledge_id}: not returned by the retrieval path")
        print(f"  {'ok  ' if hit else 'FAIL'} {knowledge_id}")

    print(f"hand-written   : {len(entries)} entries, {len(failures)} failures")
    if failures:
        for item in failures:
            print("  FAIL:", item)
        return 1
    print("all hand-written entries are reachable under the v23 capability set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
