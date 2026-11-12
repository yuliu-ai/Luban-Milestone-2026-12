#!/usr/bin/env python3
"""恢复—记忆研究的一条命令入口。

默认不执行任何付费 rollout。只有显式 ``--execute`` 才会真正跑 episode。

    # 无付费：生成计划、核对前提
    PYTHONPATH=src python scripts/run_recovery_memory_study.py \
        --protocol configs/experiments/recovery_memory_v1.yaml --stage plan

    # 单卡 worker（另一个 worker 用 --worker-index 1 另起进程）
    PYTHONPATH=src CUDA_VISIBLE_DEVICES=0 python scripts/run_recovery_memory_study.py \
        --protocol configs/experiments/recovery_memory_v1.yaml \
        --stage diagnose --execute --worker-index 0 --worker-count 2 --xr1-port 18100

``next`` 依据真实完成状态推进，依赖不足即停止并说明，不跳过失败 gate。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luban.agent_core.memories.recovery import (  # noqa: E402
    DEFAULT_ALLOWED_STATES,
    RecoveryMemoryBank,
)
from luban.agent_core.memory_advisor import (  # noqa: E402
    DEFAULT_CAMERA_KEYS,
    MemoryAdvisorSubAgent,
)
from luban.config import instantiate, load_yaml  # noqa: E402
from luban.evals.recovery_memory_study import (  # noqa: E402
    ARMS,
    STUDY_TASKS,
    StudyState,
    build_study_plan,
    build_study_runner,
    validate_task_names,
)
from luban.evals.study_evidence import (  # noqa: E402
    reconstruct_attempt,
    verify_evidence_bundle,
)

DEFAULT_RUN_CONFIG = "configs/runs/robocasa365_xr1_self_evolving_single_episode.yaml"
DEFAULT_AGENT_CONFIG = "configs/agents/self_evolving_xr1_atomic.yaml"
PAID_STAGES = {"develop", "diagnose", "smoke"}


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _load_protocol(path: Path) -> dict[str, Any]:
    protocol = load_yaml(path)
    required = ("study_id", "horizon_multiplier", "tasks", "arms", "advisor")
    missing = [key for key in required if key not in protocol]
    if missing:
        raise SystemExit(f"protocol is missing required keys: {missing}")
    declared = list(protocol["tasks"])
    known = dict(STUDY_TASKS)
    for entry in declared:
        name = entry["name"]
        if name not in known:
            raise SystemExit(f"protocol task {name!r} is not in the study task list")
        if int(entry["registry_horizon"]) != known[name]:
            raise SystemExit(
                f"protocol horizon for {name!r} ({entry['registry_horizon']}) "
                f"does not match the study definition ({known[name]})"
            )
    return protocol


def _study_root(protocol: dict[str, Any]) -> Path:
    return Path("runs") / "recovery_memory_study" / str(protocol["study_id"])


def _memory_bank_path(
    protocol: dict[str, Any], args: argparse.Namespace | None = None
) -> Path:
    override = getattr(args, "memory_bank", None) if args is not None else None
    if override:
        return Path(override)
    return Path(protocol["memory_bank"]["path"])


def _registry_available() -> tuple[list[str], str | None]:
    try:
        registry = importlib.import_module("robocasa.utils.dataset_registry")
    except Exception as error:  # pragma: no cover - depends on the simulator env
        return [], f"{type(error).__name__}: {error}"
    task_sets = registry.TASK_SET_REGISTRY
    seen = list(dict.fromkeys(task_sets.get("composite_seen", [])))
    return seen, None


def _xr1_health(port: int) -> dict[str, Any]:
    import zmq

    from luban.backends.skills.rldx import _RLDXMsgSerializer

    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.RCVTIMEO, 15000)
    socket.setsockopt(zmq.SNDTIMEO, 15000)
    try:
        socket.connect(f"tcp://127.0.0.1:{port}")
        socket.send(_RLDXMsgSerializer.to_bytes({"endpoint": "ping"}))
        response = _RLDXMsgSerializer.from_bytes(socket.recv())
        server = response.get("server", {}) if isinstance(response, dict) else {}
        return {
            "reachable": True,
            "port": port,
            "model_path": server.get("model_path"),
            "instruction_mode": server.get("instruction_mode"),
            "instruction_contract_version": server.get("instruction_contract_version"),
            "policy_seed_mode": server.get("policy_seed_mode"),
        }
    except Exception as error:
        return {
            "reachable": False,
            "port": port,
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        socket.close(linger=0)
        context.term()


def _agent_backend_config(agent_config_path: Path) -> dict[str, Any]:
    agent_config = load_yaml(agent_config_path)
    reasoner = (
        agent_config.get("agent", {})
        .get("init_args", {})
        .get("module_evolution_reasoner", {})
    )
    backend = reasoner.get("init_args", {}).get("backend")
    if not isinstance(backend, dict):
        raise SystemExit(
            f"cannot find an LLM backend under module_evolution_reasoner in "
            f"{agent_config_path}"
        )
    return backend


def stage_plan(protocol: dict[str, Any], args: argparse.Namespace) -> int:
    plan = build_study_plan(
        initial_conditions=int(protocol["diagnostic"]["initial_conditions"]),
        horizon_multiplier=float(protocol["horizon_multiplier"]),
        episode_offset=int(protocol["diagnostic"].get("episode_offset", 0)),
    )
    dev_conditions = int(protocol["development"]["initial_conditions"])
    dev_identities = len(protocol["tasks"]) * dev_conditions
    arm_count = len(protocol["arms"])
    dev_offset = int(protocol["development"].get("episode_offset", 0))
    diag_offset = int(protocol["diagnostic"].get("episode_offset", 0))
    overlap = sorted(
        {dev_offset + i for i in range(dev_conditions)}
        & {
            diag_offset + i
            for i in range(int(protocol["diagnostic"]["initial_conditions"]))
        }
    )
    if overlap:
        raise SystemExit(
            f"development and diagnostic episode ranges overlap at {overlap}; "
            "the diagnostic set must not reuse development root identities"
        )
    smoke_section = protocol.get("smoke", {})
    smoke_conditions = int(smoke_section.get("initial_conditions", 0))
    smoke_tasks = smoke_section.get("tasks") or [name for name, _ in STUDY_TASKS]

    horizons = plan.horizons()
    registry_total = sum(h for _, h in STUDY_TASKS)
    multiplied_total = sum(horizons.values())

    payload = {
        "study_id": protocol["study_id"],
        "horizon_multiplier": protocol["horizon_multiplier"],
        "arms": list(protocol["arms"]),
        "arm_count": arm_count,
        "tasks": [
            {
                "name": name,
                "registry_horizon": registry,
                "effective_horizon": horizons[name],
            }
            for name, registry in STUDY_TASKS
        ],
        "horizon_totals": {
            "registry_per_sweep": registry_total,
            "effective_per_sweep": multiplied_total,
        },
        "development": {
            "identities": dev_identities,
            "episode_offset": dev_offset,
            "trials": dev_identities * arm_count,
        },
        "diagnostic": {
            "identities": len(plan.identities),
            "episode_offset": diag_offset,
            "trials": len(plan.trials),
        },
        "smoke": {
            "tasks": list(smoke_tasks),
            "identities": len(smoke_tasks) * smoke_conditions,
            "episode_offset": int(smoke_section.get("episode_offset", 0)),
            "trials": len(smoke_tasks) * smoke_conditions * arm_count,
            "max_environment_steps": int(
                smoke_section.get("max_environment_steps", 64)
            ),
        },
        "environment_step_upper_bound": {
            "diagnostic": len(plan.trials) * max(horizons.values()),
            "note": "上界按最长任务计；实际按各任务自己的 horizon 累计",
        },
    }
    root = _study_root(protocol)
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    _print(payload)
    return 0


def stage_preflight(protocol: dict[str, Any], args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {}
    ok = True

    seen, error = _registry_available()
    if error is not None:
        checks["registry"] = {"ok": False, "error": error}
        ok = False
    else:
        missing = validate_task_names(seen)
        checks["registry"] = {
            "ok": not missing,
            "composite_seen_size": len(seen),
            "missing_study_tasks": missing,
        }
        ok = ok and not missing

    health = []
    for port in protocol["workers"]["xr1_ports"]:
        entry = _xr1_health(int(port))
        health.append(entry)
        ok = ok and bool(entry.get("reachable"))
    checks["xr1_services"] = health

    bank_path = _memory_bank_path(protocol, args)
    bank = RecoveryMemoryBank(
        bank_path,
        bank_version=str(protocol["memory_bank"]["version"]),
        allowed_states=tuple(protocol["memory_bank"]["allowed_evidence_states"])
        or DEFAULT_ALLOWED_STATES,
    )
    fingerprint = bank.fingerprint()
    frozen = protocol["memory_bank"].get("frozen_fingerprint")
    checks["memory_bank"] = {
        "path": str(bank_path),
        "exists": bank_path.exists(),
        "entry_count": len(bank.entries()),
        "fingerprint": fingerprint,
        "frozen_fingerprint": frozen,
        "matches_frozen": (frozen is None) or (frozen == fingerprint),
    }

    agent_config_path = Path(args.agent_config)
    backend = _agent_backend_config(agent_config_path)
    checks["llm_backend"] = {
        "class_path": backend.get("class_path"),
        "base_url": backend.get("init_args", {}).get("base_url"),
        "model": backend.get("init_args", {}).get("model"),
        "api_key_present": bool(
            os.environ.get("OPENAI_API_KEY")
            or backend.get("init_args", {}).get("api_key")
        ),
    }

    usage = os.statvfs(".")
    free_gb = usage.f_bavail * usage.f_frsize / 1024**3
    checks["disk"] = {"free_gb": round(free_gb, 1), "ok": free_gb > 20}
    ok = ok and free_gb > 20

    checks["ok"] = ok
    _print(checks)
    return 0 if ok else 2


def _advisor_factory(
    protocol: dict[str, Any], agent_config_path: Path, args: argparse.Namespace
) -> Any:
    advisor_config = dict(protocol["advisor"])
    bank_path = _memory_bank_path(protocol, args)
    backend_config = _agent_backend_config(agent_config_path)
    skill_definitions = {}
    agent_config = load_yaml(agent_config_path)
    planner = agent_config.get("planner", {}).get("init_args", {})
    if isinstance(planner.get("skill_definitions"), dict):
        skill_definitions = dict(planner["skill_definitions"])

    def factory(attempt_dir: Path, mode: str) -> MemoryAdvisorSubAgent:
        backend = instantiate(backend_config)
        bank = RecoveryMemoryBank(
            bank_path,
            bank_version=str(protocol["memory_bank"]["version"]),
            allowed_states=tuple(protocol["memory_bank"]["allowed_evidence_states"])
            or DEFAULT_ALLOWED_STATES,
        )
        return MemoryAdvisorSubAgent(
            backend=backend,
            memory_bank=bank,
            mode=mode,
            skill_definitions=skill_definitions,
            camera_keys=DEFAULT_CAMERA_KEYS,
            top_k=int(advisor_config["top_k"]),
            event_window=int(advisor_config["event_window"]),
            max_images=int(advisor_config["max_images"]),
            max_calls=int(advisor_config["max_calls_per_episode"]),
            max_tokens=int(advisor_config["max_tokens"]),
            temperature=float(advisor_config["temperature"]),
            response_format_mode=str(advisor_config["response_format_mode"]),
            robot_profile=str(advisor_config["robot_profile"]),
            advice_log_path=attempt_dir / "advice_events.jsonl",
            media_log_dir=attempt_dir / "advisor_media",
        )

    return factory


def _require_approval(
    protocol: dict[str, Any], args: argparse.Namespace, root: Path
) -> None:
    """正式付费阶段必须先有 approved 批准文件，且 hash 与现场一致。"""
    if args.stage not in {"develop", "diagnose"} or not args.execute:
        return
    approval_path = (
        Path(args.approval)
        if args.approval
        else Path("approvals") / f"{protocol['study_id']}.json"
    )
    if not approval_path.exists():
        raise SystemExit(
            f"paid stage requires an approval file: {approval_path} "
            "(generate it with scripts/study/write_approval.py)"
        )
    payload = json.loads(approval_path.read_text(encoding="utf-8"))
    if payload.get("status") != "approved":
        raise SystemExit(
            f"approval {approval_path} is {payload.get('status')!r}; the execution "
            "agent cannot approve paid scale on its own — fill the budget fields "
            "and set status=approved"
        )
    provenance_path = root / "provenance.json"
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        bound = (payload.get("bindings") or {}).get("protocol_hash")
        if bound and bound != provenance.get("protocol_hash"):
            raise SystemExit(
                "approval protocol_hash does not match the current protocol/config; "
                "a material change voids the approval"
            )
    bank = (payload.get("bindings") or {}).get("memory_bank") or {}
    frozen = protocol["memory_bank"].get("frozen_fingerprint")
    if not frozen or bank.get("frozen_fingerprint") != frozen:
        raise SystemExit(
            "formal runs require a frozen memory_bank fingerprint bound in the "
            "approval file and in the protocol"
        )


def _selected_arms(
    protocol: dict[str, Any], section: dict[str, Any], args: argparse.Namespace
) -> tuple[str, ...]:
    """演示/子集运行时允许只跑声明的臂；默认跑协议声明的全部臂。"""
    declared = tuple(section.get("arms") or protocol["arms"])
    override = getattr(args, "arms", None)
    if override:
        requested = tuple(
            part.strip() for part in str(override).split(",") if part.strip()
        )
        unknown = [arm for arm in requested if arm not in declared]
        if unknown:
            raise SystemExit(
                f"--arms contains arms not declared for this stage: {unknown}"
            )
        return requested
    return declared


def _run_stage(
    protocol: dict[str, Any], args: argparse.Namespace, *, conditions_key: str
) -> int:
    section = protocol[conditions_key]
    conditions = int(section["initial_conditions"])
    episode_offset = int(section.get("episode_offset", 0))
    task_filter = section.get("tasks")
    plan = build_study_plan(
        initial_conditions=conditions,
        horizon_multiplier=float(protocol["horizon_multiplier"]),
        episode_offset=episode_offset,
        task_names=(
            [str(name) for name in task_filter]
            if isinstance(task_filter, list)
            else None
        ),
    )
    root = _study_root(protocol) / conditions_key
    root.mkdir(parents=True, exist_ok=True)
    state = StudyState(root / "jobs.jsonl").load()
    completed = state.completed_trials()

    smoke_budget: dict[str, int] | None = None
    forced_trigger = False
    if conditions_key == "smoke":
        smoke_section = protocol.get("smoke", {})
        budget_steps = int(
            args.max_environment_steps
            if args.max_environment_steps is not None
            else smoke_section.get("max_environment_steps", 64)
        )
        smoke_budget = {
            "max_environment_steps": budget_steps,
            "max_action_chunks": int(smoke_section.get("max_action_chunks", 32)),
            "max_pipeline_iterations": int(
                smoke_section.get("max_pipeline_iterations", 64)
            ),
        }
        forced_trigger = bool(
            args.forced_trigger or smoke_section.get("forced_trigger", False)
        )

    runner = build_study_runner(
        run_config_path=Path(args.run_config),
        study_dir=root,
        advisor_factory=_advisor_factory(protocol, Path(args.agent_config), args),
        memory_bank_path=_memory_bank_path(protocol),
        horizon_multiplier=float(protocol["horizon_multiplier"]),
        smoke_budget=smoke_budget,
        forced_trigger=forced_trigger,
        agent_config_path=Path(args.agent_config),
    )

    _require_approval(protocol, args, root)

    identity_index = {
        identity.canonical_identity: index
        for index, identity in enumerate(plan.identities)
    }
    arms = _selected_arms(protocol, section, args)
    selected = [
        trial
        for trial in plan.trials
        if trial.arm in arms
        and identity_index[trial.identity.canonical_identity] % args.worker_count
        == args.worker_index
    ]

    if not args.execute:
        _print(
            {
                "stage": conditions_key,
                "dry_run": True,
                "message": "未加 --execute，不执行任何 rollout",
                "worker_index": args.worker_index,
                "worker_count": args.worker_count,
                "planned_trials": len(selected),
                "already_completed": len(
                    [t for t in selected if t.trial_id in completed]
                ),
                "first_trials": [t.trial_id for t in selected[:5]],
            }
        )
        return 0

    if args.xr1_port is not None:
        os.environ["XIAOMI_REMOTE_PORTS"] = str(args.xr1_port)

    already_completed = len(
        [trial for trial in selected if trial.trial_id in completed]
    )
    pending = [trial for trial in selected if trial.trial_id not in completed]
    if args.max_trials is not None:
        pending = pending[: args.max_trials]
    print(
        f"[study] worker {args.worker_index}/{args.worker_count} "
        f"planned={len(selected)} pending={len(pending)} "
        f"already_completed={already_completed}"
        + (f" max_trials_cap={args.max_trials}" if args.max_trials else ""),
        flush=True,
    )

    for trial in pending:
        trial_id = trial.trial_id
        state.append(
            {
                "event": "trial_attempt",
                "trial_id": trial_id,
                "identity": trial.identity.canonical_identity,
                "source_group_id": trial.identity.source_group_id,
                "arm": trial.arm,
                "worker_index": args.worker_index,
                "order_index": trial.order_index,
            }
        )
        result = runner(trial.identity, trial.arm)
        evidence = result.get("evidence", {}) or {}
        record = {
            "event": "trial_finished",
            "trial_stage": conditions_key,
            "excluded_from_statistics": conditions_key == "demo",
            "status": "complete" if evidence.get("checksums_ok") else "partial",
            "checksums_ok": bool(evidence.get("checksums_ok")),
            "reconstruction_ok": bool(evidence.get("reconstruction_ok")),
            "task_success": bool(result["episode"]["success"]),
            **{key: result[key] for key in ("trial_id", "identity", "arm", "outcome")},
            "source_group_id": result["source_group_id"],
            "advice_count": result["advice_count"],
            "wall_seconds": result["wall_seconds"],
            "safety_violations": result["safety_violations"],
            "episode": result["episode"],
            "error": result["error"],
            "evidence": {
                "checksums_ok": bool(evidence.get("checksums_ok")),
                "reconstruction_ok": bool(evidence.get("reconstruction_ok")),
                "issues": list(evidence.get("issues") or []),
                "warnings": list(evidence.get("warnings") or []),
                "manifest_path": evidence.get("manifest_path"),
                "counts": (
                    (evidence.get("reconstruction") or {}).get("counts")
                    if isinstance(evidence.get("reconstruction"), dict)
                    else None
                ),
            },
            "arm_conditions": result.get("arm_conditions"),
        }
        state.append(record)
        print(
            f"[study] {trial_id} arm={trial.arm} outcome={result['outcome']} "
            f"success={result['episode']['success']} "
            f"steps={result['episode']['environment_steps']} "
            f"wall={result['wall_seconds']}s",
            flush=True,
        )
        if result["safety_violations"]:
            state.append(
                {
                    "event": "safety_pause",
                    "trial_id": trial_id,
                    "violations": result["safety_violations"],
                }
            )
            print(
                f"[study] SAFETY VIOLATION at {trial_id}: "
                f"{result['safety_violations']} — 暂停",
                flush=True,
            )
            return 3

    _print(state.summary())
    return 0


def stage_verify_evidence(protocol: dict[str, Any], args: argparse.Namespace) -> int:
    """G1 离线检查命令：重算摘要并从真实产物重建模型请求边界与已执行动作。"""
    if not args.trial_id:
        raise SystemExit("--trial-id is required for verify-evidence")
    identity_key, _, arm = str(args.trial_id).rpartition("/")
    if not identity_key or not arm:
        raise SystemExit("--trial-id must look like target/Task/0000/C")
    directory_key = identity_key.replace("/", "-")
    report: dict[str, Any] = {"trial_id": args.trial_id, "attempts": {}}
    ok = True
    found = False
    for key in ("smoke", "development", "diagnostic"):
        trial_dir = _study_root(protocol) / key / "trials" / directory_key / arm
        if not trial_dir.exists():
            continue
        found = True
        for attempt in sorted(path for path in trial_dir.iterdir() if path.is_dir()):
            verification = verify_evidence_bundle(attempt)
            reconstruction = reconstruct_attempt(attempt)
            ok = ok and verification["ok"] and reconstruction["ok"]
            report["attempts"][f"{key}/{attempt.name}"] = {
                "verification": verification,
                "reconstruction": reconstruction,
            }
    if not found:
        raise SystemExit(f"trial not found under the study root: {args.trial_id}")
    report["ok"] = ok
    _print(report)
    return 0 if ok else 4


def stage_status(protocol: dict[str, Any], args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"study_id": protocol["study_id"]}
    for key in ("development", "diagnostic"):
        path = _study_root(protocol) / key / "jobs.jsonl"
        state = StudyState(path).load()
        payload[key] = {
            "jobs_path": str(path),
            "exists": path.exists(),
            **state.summary(),
        }
    _print(payload)
    return 0


def stage_analyze(protocol: dict[str, Any], args: argparse.Namespace) -> int:
    """配对比较：主比较 C-B，次要 B-A；控制臂 D-B 与 E-B。"""
    path = _study_root(protocol) / "diagnostic" / "jobs.jsonl"
    state = StudyState(path).load()
    finished = [
        record for record in state.records if record.get("event") == "trial_finished"
    ]
    by_identity: dict[str, dict[str, dict[str, Any]]] = {}
    for record in finished:
        by_identity.setdefault(str(record["identity"]), {})[str(record["arm"])] = record

    comparisons = [("C", "B"), ("B", "A"), ("D", "B"), ("E", "B")]
    report: dict[str, Any] = {
        "identities_finished": len(by_identity),
        "trials_finished": len(finished),
        "by_arm": {},
        "comparisons": {},
    }
    for arm in ARMS:
        rows = [r for r in finished if r.get("arm") == arm]
        report["by_arm"][arm] = {
            "trials": len(rows),
            "success": sum(1 for r in rows if r.get("task_success")),
            "infrastructure_error": sum(
                1 for r in rows if r.get("outcome") == "infrastructure_error"
            ),
            "advice_events": sum(int(r.get("advice_count") or 0) for r in rows),
        }

    for treatment, control in comparisons:
        paired = [
            (arms[treatment], arms[control])
            for arms in by_identity.values()
            if treatment in arms and control in arms
        ]
        b = sum(
            1 for t, c in paired if not t.get("task_success") and c.get("task_success")
        )
        c = sum(
            1 for t, c in paired if t.get("task_success") and not c.get("task_success")
        )
        n = len(paired)
        report["comparisons"][f"{treatment}-{control}"] = {
            "paired_identities": n,
            "baseline_fail_candidate_success": b,
            "baseline_success_candidate_fail": c,
            "net_success_rate_change": (round((b - c) / n, 4) if n else None),
            "discordant_pairs": b + c,
        }
    _print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recovery-memory study driver")
    parser.add_argument("--protocol", required=True)
    parser.add_argument(
        "--stage",
        required=True,
        choices=(
            "plan",
            "preflight",
            "smoke",
            "demo",
            "develop",
            "diagnose",
            "status",
            "analyze",
            "verify-evidence",
            "next",
        ),
    )
    parser.add_argument("--execute", action="store_true", help="真正执行 rollout")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--xr1-port", type=int, default=None)
    parser.add_argument(
        "--max-trials",
        type=int,
        default=None,
        help="只跑前 N 个 trial，用于 smoke；正式运行不要使用",
    )
    parser.add_argument(
        "--max-environment-steps",
        type=int,
        default=None,
        help="smoke 的环境步上限（默认取协议 smoke 段；正式运行不要使用）",
    )
    parser.add_argument(
        "--forced-trigger",
        action="store_true",
        help=(
            "仅 smoke：把节点 chunk 预算压到 1 以真实触发顾问链，结果不算 recovery 证据"
        ),
    )
    parser.add_argument("--trial-id", default=None, help="verify-evidence 的目标 trial")
    parser.add_argument(
        "--memory-bank",
        default=None,
        help="覆盖协议里的记忆库路径；smoke 用 seed 库，正式运行必须用协议路径",
    )
    parser.add_argument(
        "--approval",
        default=None,
        help="批准文件路径；develop/diagnose 的 --execute 必须提供 approved 文件",
    )
    parser.add_argument(
        "--arms",
        default=None,
        help="只跑指定臂（逗号分隔，例如 B 或 B,C）；默认跑协议声明的全部臂",
    )
    parser.add_argument("--run-config", default=DEFAULT_RUN_CONFIG)
    parser.add_argument("--agent-config", default=DEFAULT_AGENT_CONFIG)
    args = parser.parse_args(argv)

    if args.worker_count < 1:
        raise SystemExit("--worker-count must be >= 1")
    if not 0 <= args.worker_index < args.worker_count:
        raise SystemExit("--worker-index must be within [0, worker-count)")

    protocol = _load_protocol(Path(args.protocol))

    if args.stage == "plan":
        return stage_plan(protocol, args)
    if args.stage == "preflight":
        return stage_preflight(protocol, args)
    if args.stage == "status":
        return stage_status(protocol, args)
    if args.stage == "analyze":
        return stage_analyze(protocol, args)
    if args.stage == "verify-evidence":
        return stage_verify_evidence(protocol, args)
    if args.stage == "smoke":
        if "smoke" not in protocol:
            raise SystemExit("protocol lacks a smoke section")
        return _run_stage(protocol, args, conditions_key="smoke")
    if args.stage == "demo":
        if "demo" not in protocol:
            raise SystemExit("protocol lacks a demo section")
        return _run_stage(protocol, args, conditions_key="demo")
    if args.stage == "develop":
        return _run_stage(protocol, args, conditions_key="development")
    if args.stage == "diagnose":
        return _run_stage(protocol, args, conditions_key="diagnostic")
    if args.stage == "next":
        # 依据真实完成状态推进：smoke -> 开发集 -> 诊断集；smoke 只做链路验收。
        smoke_state = StudyState(_study_root(protocol) / "smoke" / "jobs.jsonl").load()
        if not smoke_state.completed_trials():
            print(
                "[next] smoke 尚未完成；先跑 smoke（链路验收），再进入开发集",
                flush=True,
            )
            if "smoke" in protocol:
                return _run_stage(protocol, args, conditions_key="smoke")
        dev_state = StudyState(
            _study_root(protocol) / "development" / "jobs.jsonl"
        ).load()
        if not dev_state.completed_trials():
            print("[next] 开发集尚未完成，推进到 develop", flush=True)
            return _run_stage(protocol, args, conditions_key="development")
        print("[next] 开发集已完成，推进到 diagnose", flush=True)
        return _run_stage(protocol, args, conditions_key="diagnostic")
    raise SystemExit(f"unhandled stage {args.stage!r}")


if __name__ == "__main__":
    sys.exit(main())
