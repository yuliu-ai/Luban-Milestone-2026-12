"""恢复—记忆研究的五臂运行器。

五臂定义（差异被严格限制在声明的 treatment 字段内）：

- A：固定 Planner + XR-1，关闭本研究所有特殊 recovery 入口，无顾问。
- B：与 A 相同，但启用当前节点选择性 Semantic Recovery，无顾问。
- C：与 B 相同，仅在相同恢复入口增加冻结的 MemoryAdvisor v0。
- D：与 B 相同，把检索到的记忆确定性拼成建议，不调用顾问 LLM。
- E：与 B 相同，顾问照常调用 LLM，但不做任何记忆检索。

D/E 是把 C 的整体增量拆成可归因部分的控制臂：没有它们，只能声称「C 相对 B
有效」，无法区分是记忆检索起了作用、还是单纯多算了一次模型。

本模块只负责「把五臂按正确的身份、次序和隔离跑起来」，不负责分析。
每个 trial 独立重建 Agent / Pipeline / Runtime / Environment / session / 私有
store；五臂共用同一 base_config_hash，只有声明的 treatment 字段进入
effective_config_hash。

horizon 按 registry 原值的 ``horizon_multiplier`` 倍**同时**写入环境 horizon 与
runtime ``max_environment_steps``。evaluator 的 ``horizon_multiplier`` 决定环境
horizon（``ceil(registry * multiplier)``）；runtime 的上限必须取同一个值，否则
runtime 会先截断，终止原因变成 ``max_environment_steps`` 而不是环境自身的
``horizon``，实验作废。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from luban.config import (
    build_agent,
    build_run_components,
    instantiate,
    load_yaml,
    resolve_config_path,
)
from luban.evals.study_evidence import (
    build_evidence_bundle,
    reconstruct_attempt,
    verify_evidence_bundle,
)
from luban.exceptions import ConfigError

HORIZON_MULTIPLIER = 2.5

# 8 个 composite_seen 任务。horizon 是 registry 原值，不是 2.5 倍后的值。
# 任务名与 horizon 必须与现场 registry 一致；构造计划前用 validate_task_names 核对。
STUDY_TASKS: tuple[tuple[str, int], ...] = (
    ("ScrubCuttingBoard", 1200),
    ("KettleBoiling", 1500),
    ("WashLettuce", 1650),
    ("PrepareCoffee", 1800),
    ("LoadDishwasher", 1800),
    ("StackBowlsCabinet", 2100),
    ("SteamInMicrowave", 2100),
    ("StirVegetables", 2400),
)

ARMS = ("A", "B", "C", "D", "E")

_INFRASTRUCTURE_TERMINATIONS = {"exception", "timeout", "api_deferred"}


def _sha256_json(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def effective_horizon(
    registry_horizon: int, multiplier: float = HORIZON_MULTIPLIER
) -> int:
    """环境 horizon 与 runtime 上限共用的同一个值。

    取整方式与 evaluator 一致（``math.ceil(registry * multiplier)``），保证
    ``task["horizon_override"]`` 与 runtime ``max_environment_steps`` 逐位相等；
    否则 runtime 会先截断并把终止原因写成 ``max_environment_steps``。
    """
    if (
        not isinstance(registry_horizon, int)
        or isinstance(registry_horizon, bool)
        or registry_horizon <= 0
    ):
        raise ValueError("registry_horizon must be a positive integer")
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
        raise TypeError("multiplier must be a number")
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("multiplier must be positive")
    return math.ceil(registry_horizon * multiplier)


@dataclass(frozen=True)
class StudyIdentity:
    """一个根任务实例。身份不含 arm。"""

    task_name: str
    episode_index: int
    split: str = "target"
    domain_split: str = "composite_seen"

    def __post_init__(self) -> None:
        for name in ("task_name", "split", "domain_split"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            not isinstance(self.episode_index, int)
            or isinstance(self.episode_index, bool)
            or self.episode_index < 0
        ):
            raise ValueError("episode_index must be a non-negative integer")

    @property
    def canonical_identity(self) -> str:
        return f"{self.split}/{self.task_name}/{self.episode_index:04d}"

    @property
    def source_group_id(self) -> str:
        """根实例标识，用于 leave-one-source-group-out 与切分。"""
        return f"target-{self.task_name}-{self.episode_index:04d}"


@dataclass(frozen=True)
class ArmSpec:
    arm: str
    node_selective_recovery: bool
    advisor_mode: str | None
    description: str

    @property
    def advisor_enabled(self) -> bool:
        return self.advisor_mode is not None

    def treatment_fields(self) -> dict[str, Any]:
        return {
            "node_selective_recovery_enabled": self.node_selective_recovery,
            "recovery_advisor_mode": self.advisor_mode,
        }


# 5 臂。A/B/C 是主比较所需的三个条件，D/E 是把 C 的整体增量拆开归因的两个控制：
#   D 去掉 LLM 推理，只保留检索拼接  -> 子 Agent 架构本身是否必要
#   E 去掉检索，只保留一次额外 LLM 调用 -> 是记忆有用，还是多算一次有用
# 没有 D/E 就只能声称「C 相对 B 有效」，无法归因到记忆或架构。
ARM_SPECS: Mapping[str, ArmSpec] = {
    "A": ArmSpec(
        arm="A",
        node_selective_recovery=False,
        advisor_mode=None,
        description=(
            "Planner + XR-1 without node-selective recovery and without advisor"
        ),
    ),
    "B": ArmSpec(
        arm="B",
        node_selective_recovery=True,
        advisor_mode=None,
        description="Node-selective semantic recovery, no advisor",
    ),
    "C": ArmSpec(
        arm="C",
        node_selective_recovery=True,
        advisor_mode="advisor",
        description="Node-selective semantic recovery plus frozen MemoryAdvisor v0",
    ),
    "D": ArmSpec(
        arm="D",
        node_selective_recovery=True,
        advisor_mode="direct",
        description=(
            "Recovery plus deterministic concatenation of retrieved memories, "
            "no advisor LLM call"
        ),
    ),
    "E": ArmSpec(
        arm="E",
        node_selective_recovery=True,
        advisor_mode="no_retrieval",
        description=(
            "Recovery plus one extra advisor LLM call with no memory retrieval"
        ),
    ),
}


@dataclass(frozen=True)
class Trial:
    identity: StudyIdentity
    arm: str
    order_index: int

    @property
    def trial_id(self) -> str:
        return f"{self.identity.canonical_identity}/{self.arm}"


@dataclass
class StudyPlan:
    """80 个根实例 × 3 臂，含预生成的近均衡臂次序。"""

    identities: tuple[StudyIdentity, ...]
    trials: tuple[Trial, ...]
    horizon_multiplier: float
    seed: int

    def horizons(self) -> dict[str, int]:
        return {
            name: effective_horizon(horizon, self.horizon_multiplier)
            for name, horizon in STUDY_TASKS
        }


def build_study_plan(
    *,
    initial_conditions: int = 10,
    horizon_multiplier: float = HORIZON_MULTIPLIER,
    order_seed: int = 20260911,
    episode_offset: int = 0,
    task_names: Sequence[str] | None = None,
) -> StudyPlan:
    """构造某个 research split 的根实例，并为每个实例生成近均衡的臂次序。

    ``episode_offset`` 把不同 research split 映射到互不重叠的 episode index 区间：
    development 用 0 起，diagnostic_eval_only 用 10 起。两类 split 的根实例与来源
    轨迹必须完全不重叠，否则诊断集里混着开发集的答案，held-out 声明不成立。

    臂次序用固定种子预生成，避免「所有 A 上午跑、C 夜间跑」的系统性时间偏差。
    每个实例内所有臂各出现一次，顺序是 ARMS 的随机排列。
    """
    if (
        not isinstance(initial_conditions, int)
        or isinstance(initial_conditions, bool)
        or initial_conditions <= 0
    ):
        raise ValueError("initial_conditions must be a positive integer")
    if (
        not isinstance(episode_offset, int)
        or isinstance(episode_offset, bool)
        or episode_offset < 0
    ):
        raise ValueError("episode_offset must be a non-negative integer")

    if task_names is None:
        selected_tasks = STUDY_TASKS
    else:
        known = dict(STUDY_TASKS)
        missing = [name for name in task_names if name not in known]
        if missing:
            raise ValueError(f"unknown study tasks: {missing}")
        selected_tasks = tuple((name, known[name]) for name in task_names)

    identities: list[StudyIdentity] = []
    for task_name, _ in selected_tasks:
        for step in range(initial_conditions):
            identities.append(
                StudyIdentity(
                    task_name=task_name,
                    episode_index=episode_offset + step,
                    split="target",
                    domain_split="composite_seen",
                )
            )

    rng = random.Random(order_seed)
    trials: list[Trial] = []
    for identity in identities:
        order = list(ARMS)
        rng.shuffle(order)
        for order_index, arm in enumerate(order):
            trials.append(Trial(identity=identity, arm=arm, order_index=order_index))

    return StudyPlan(
        identities=tuple(identities),
        trials=tuple(trials),
        horizon_multiplier=horizon_multiplier,
        seed=order_seed,
    )


def validate_task_names(available: Sequence[str]) -> list[str]:
    """核对 8 个任务名是否都在现场 registry 中，返回缺失项。"""
    known = set(available)
    return [name for name, _ in STUDY_TASKS if name not in known]


@dataclass
class StudyState:
    """append-only 的 trial 状态，支撑 resume 与可靠性报表。"""

    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)

    def load(self) -> StudyState:
        if self.path.exists():
            self.records = [
                json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return self

    def append(self, record: Mapping[str, Any]) -> None:
        payload = dict(record)
        payload.setdefault("recorded_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.records.append(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def completed_trials(self) -> dict[str, dict[str, Any]]:
        """只把 complete 且校验通过的 trial 视为可跳过。"""
        completed: dict[str, dict[str, Any]] = {}
        for record in self.records:
            if record.get("event") != "trial_finished":
                continue
            if record.get("status") != "complete":
                continue
            if record.get("checksums_ok") is not True:
                continue
            completed[str(record.get("trial_id"))] = record
        return completed

    def attempt_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            if record.get("event") != "trial_attempt":
                continue
            key = str(record.get("trial_id"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        finished = [
            record for record in self.records if record.get("event") == "trial_finished"
        ]
        by_arm: dict[str, dict[str, int]] = {}
        for record in finished:
            arm = str(record.get("arm"))
            bucket = by_arm.setdefault(
                arm, {"finished": 0, "success": 0, "infrastructure_error": 0}
            )
            bucket["finished"] += 1
            if record.get("task_success"):
                bucket["success"] += 1
            if record.get("outcome") == "infrastructure_error":
                bucket["infrastructure_error"] += 1
        return {
            "trial_attempts": len(
                [r for r in self.records if r.get("event") == "trial_attempt"]
            ),
            "trial_finished": len(finished),
            "completed_and_verified": len(self.completed_trials()),
            "by_arm": by_arm,
        }


def classify_outcome(
    episode: Mapping[str, Any],
    *,
    smoke_budget_environment_steps: int | None = None,
) -> str:
    if bool(episode.get("success", False)):
        return "success"
    termination = str(episode.get("termination_reason") or "")
    if termination == "interrupted":
        return "deferred"
    if termination in _INFRASTRUCTURE_TERMINATIONS or int(episode.get("exceptions", 0)):
        return "infrastructure_error"
    error_type = str(episode.get("error_type") or "")
    error_text = str(episode.get("error") or "")
    if error_type in {"BackendError", "ServiceError"} or "openai-compatible" in (
        error_text.lower()
    ):
        return "infrastructure_error"
    if (
        smoke_budget_environment_steps is not None
        and termination == "max_environment_steps"
        and int(episode.get("environment_steps") or 0) >= smoke_budget_environment_steps
    ):
        # smoke 的预算是人为截断，不是真实失败，也不是真实 horizon 结论。
        return "smoke_budget_exhausted"
    return "failure"


def safety_violations(episode: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    if episode.get("termination_reason") == "policy_action_out_of_range":
        violations.append("policy_action_out_of_range")
    if episode.get("safety_violation"):
        violations.append(str(episode["safety_violation"]))
    return violations


def _strip_arm_paths(config: Any) -> Any:
    text = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    for marker in ("output_dir", "traces", "trials", "experiments"):
        text = text.replace(marker, "<arm>")
    return json.loads(text)


def _last_episode(trial_dir: Path) -> dict[str, Any]:
    episodes_path = trial_dir / "episodes.jsonl"
    if not episodes_path.exists():
        raise RuntimeError(f"trial produced no episodes.jsonl: {trial_dir}")
    lines = [
        line
        for line in episodes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        raise RuntimeError(f"trial produced no episode records: {trial_dir}")
    record = json.loads(lines[-1])
    if not isinstance(record, dict):
        raise RuntimeError(f"trial episode record must be an object: {trial_dir}")
    return record


def build_study_runner(
    *,
    run_config_path: str | Path,
    study_dir: str | Path,
    advisor_factory: Callable[[Path, str], Any] | None = None,
    memory_bank_path: str | Path | None = None,
    horizon_multiplier: float = HORIZON_MULTIPLIER,
    max_attempts_per_trial: int = 2,
    smoke_budget: Mapping[str, int] | None = None,
    forced_trigger: bool = False,
    agent_config_path: str | Path | None = None,
) -> Callable[[StudyIdentity, str], dict[str, Any]]:
    """构造 ``run_trial(identity, arm)`` 回调。

    ``advisor_factory(trial_dir)`` 只在 C 臂被调用，返回冻结的顾问实例；它为
    None 时 C 臂退化为 B，调用方必须先自行确认这是有意的。

    ``smoke_budget`` 非空时把 runtime 上限压到 smoke 值（例如每臂 64 环境步），
    并把结果标记为 smoke；``forced_trigger`` 把节点 chunk 预算压到 1 并每个 chunk
    验证一次，让选择性恢复与顾问链在 smoke 中真实触发——两者都只允许用于
    smoke，正式运行不得携带，且 ``forced_trigger`` 的结论不算真实 recovery 证据。
    """
    run_path = Path(run_config_path)
    study = Path(study_dir)
    study.mkdir(parents=True, exist_ok=True)

    base_run_config = load_yaml(run_path)
    resolved_agent_path = (
        Path(agent_config_path)
        if agent_config_path is not None
        else resolve_config_path(base_run_config["agent_config"], run_path)
    )
    agent_config_path = resolved_agent_path
    base_agent_config = load_yaml(agent_config_path)

    horizons = {
        name: effective_horizon(horizon, horizon_multiplier)
        for name, horizon in STUDY_TASKS
    }
    shared_projection = {
        "run": _strip_arm_paths(copy.deepcopy(base_run_config)),
        "agent": _strip_arm_paths(copy.deepcopy(base_agent_config)),
        "horizons": horizons,
        "horizon_multiplier": horizon_multiplier,
        "arms": {arm: spec.treatment_fields() for arm, spec in ARM_SPECS.items()},
        "smoke_budget": dict(smoke_budget) if smoke_budget is not None else None,
        "forced_trigger": bool(forced_trigger),
    }
    base_config_hash = _sha256_json(shared_projection)
    protocol_hash = _sha256_json(
        {**shared_projection, "study_tasks": list(STUDY_TASKS)}
    )
    _write_json(study / "protocol.json", shared_projection)
    _write_json(
        study / "provenance.json",
        {
            "run_config_path": str(run_path),
            "agent_config_path": str(agent_config_path),
            "base_config_hash": base_config_hash,
            "protocol_hash": protocol_hash,
            "horizon_multiplier": horizon_multiplier,
            "horizons": horizons,
            "memory_bank_path": (
                str(memory_bank_path) if memory_bank_path is not None else None
            ),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )

    def run_trial(identity: StudyIdentity, arm: str) -> dict[str, Any]:
        if arm not in ARM_SPECS:
            raise ValueError(f"unknown arm: {arm!r}")
        spec = ARM_SPECS[arm]
        identity_dir = study / "trials" / identity.canonical_identity.replace("/", "-")
        trial_root = identity_dir / arm
        trial_root.mkdir(parents=True, exist_ok=True)
        attempt_dir = trial_root / "attempt-0"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        run_config = copy.deepcopy(base_run_config)
        agent_config = copy.deepcopy(base_agent_config)
        horizon = horizons[identity.task_name]
        # forced trigger 只对拥有选择性恢复路径的臂生效；A 臂保持自然行为。
        arm_forced_trigger = bool(forced_trigger and spec.node_selective_recovery)
        _apply_trial_overrides(
            run_config,
            agent_config,
            attempt_dir,
            identity,
            spec,
            horizon,
            horizon_multiplier=horizon_multiplier,
            smoke_budget=smoke_budget,
            forced_trigger=arm_forced_trigger,
        )
        effective_config_hash = _sha256_json({"run": run_config, "agent": agent_config})

        advisor = None
        if spec.advisor_enabled and advisor_factory is not None:
            assert spec.advisor_mode is not None
            advisor = advisor_factory(attempt_dir, spec.advisor_mode)

        agent = build_agent(agent_config)
        pipeline, runtime, environment = build_run_components(run_config)
        if advisor is not None:
            cast(Any, pipeline).recovery_advisor = advisor
            cast(Any, pipeline).recovery_advisor_context = {
                "task_name": identity.task_name,
                "environment_step_budget": horizon,
                "source_group_ids": [identity.source_group_id],
            }
        benchmark = instantiate(run_config["benchmark"], environment=environment)

        started = time.time()
        error: dict[str, Any] | None = None
        try:
            benchmark.run(agent, pipeline, runtime)
        except Exception as failure:
            error = {
                "error_type": type(failure).__name__,
                "error": str(failure),
            }
        finally:
            for component in (agent, environment, advisor):
                close = getattr(component, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        episode = _last_episode(attempt_dir)
        smoke_budget_steps = (
            int(smoke_budget["max_environment_steps"])
            if smoke_budget is not None and "max_environment_steps" in smoke_budget
            else None
        )
        outcome = classify_outcome(
            episode, smoke_budget_environment_steps=smoke_budget_steps
        )
        advice_records: list[dict[str, Any]] = []
        if advisor is not None:
            records = getattr(advisor, "advice_records", None)
            if callable(records):
                advice_records = [record.to_dict() for record in records()]
        conditions = {
            "identity": identity.canonical_identity,
            "source_group_id": identity.source_group_id,
            "arm": arm,
            "arm_description": spec.description,
            "base_config_hash": base_config_hash,
            "effective_config_hash": effective_config_hash,
            "allowed_diff": spec.treatment_fields(),
            "registry_horizon": dict(STUDY_TASKS)[identity.task_name],
            "horizon_multiplier": horizon_multiplier,
            "effective_horizon": horizon,
            # 两处必须一致：环境 horizon（evaluator 的 horizon_multiplier）与
            # runtime 上限（max_environment_steps）。
            "environment_horizon_multiplier": run_config["benchmark"]["init_args"][
                "horizon_multiplier"
            ],
            "environment_horizon": effective_horizon(
                dict(STUDY_TASKS)[identity.task_name],
                float(run_config["benchmark"]["init_args"]["horizon_multiplier"]),
            ),
            "runtime_max_environment_steps": run_config["runtime"]["init_args"][
                "config"
            ]["max_environment_steps"],
            "smoke": (
                {
                    "enabled": True,
                    "forced_trigger": arm_forced_trigger,
                    "budget": dict(smoke_budget),
                }
                if smoke_budget is not None
                else {"enabled": False, "forced_trigger": arm_forced_trigger}
            ),
            "advisor_enabled": advisor is not None,
            "advisor_mode": spec.advisor_mode,
            "episode_index": identity.episode_index,
            "split": identity.split,
        }
        _write_json(attempt_dir / "arm_conditions.json", conditions)
        _write_json(
            attempt_dir / "advice_events.json",
            {"advice_count": len(advice_records), "advice": advice_records},
        )

        # G1 证据包：封存文件摘要并立即重算校验；checksums_ok 由真实校验决定，
        # resume 只跳过 complete 且校验通过的 trial。
        evidence: dict[str, Any] = {
            "checksums_ok": False,
            "reconstruction_ok": False,
            "issues": [],
            "warnings": [],
            "manifest_path": None,
            "checksums_path": None,
        }
        try:
            manifest = build_evidence_bundle(
                attempt_dir,
                study_meta={
                    "identity": identity.canonical_identity,
                    "source_group_id": identity.source_group_id,
                    "arm": arm,
                    "base_config_hash": base_config_hash,
                    "effective_config_hash": effective_config_hash,
                    "study_tasks": list(STUDY_TASKS),
                    "horizon_multiplier": horizon_multiplier,
                    "smoke_budget": dict(smoke_budget) if smoke_budget else None,
                    "forced_trigger": arm_forced_trigger,
                },
            )
            verification = verify_evidence_bundle(attempt_dir)
            reconstruction = reconstruct_attempt(attempt_dir)
            evidence = {
                "checksums_ok": bool(verification["ok"]),
                "reconstruction_ok": bool(reconstruction["ok"]),
                "issues": [*verification["issues"], *reconstruction["issues"]],
                "warnings": list(reconstruction["warnings"]),
                "manifest_path": str(attempt_dir / "evidence_manifest.json"),
                "checksums_path": str(attempt_dir / "checksums.sha256"),
                "file_count": manifest["file_count"],
                "reconstruction": reconstruction,
            }
        except Exception as failure:  # 证据失败不允许静默通过
            evidence["issues"] = [
                *evidence["issues"],
                f"evidence bundle: {type(failure).__name__}: {failure}",
            ]

        evidence_refs = [
            str(attempt_dir / "episodes.jsonl"),
            str(attempt_dir / "arm_conditions.json"),
            str(attempt_dir / "advice_events.json"),
        ]
        if evidence["manifest_path"]:
            evidence_refs.extend(
                [evidence["manifest_path"], evidence["checksums_path"]]
            )
        traces = attempt_dir / "traces"
        if traces.exists():
            evidence_refs.append(str(traces))

        return {
            "trial_id": f"{identity.canonical_identity}/{arm}",
            "identity": identity.canonical_identity,
            "source_group_id": identity.source_group_id,
            "arm": arm,
            "outcome": outcome,
            "attempt_dir": str(attempt_dir),
            "wall_seconds": round(time.time() - started, 3),
            "evidence_refs": evidence_refs,
            "evidence": evidence,
            "safety_violations": safety_violations(episode),
            "advice_count": len(advice_records),
            "arm_conditions": {
                "base_config_hash": base_config_hash,
                "effective_config_hash": effective_config_hash,
                "allowed_diff": spec.treatment_fields(),
                "effective_horizon": horizon,
                "environment_horizon_multiplier": conditions[
                    "environment_horizon_multiplier"
                ],
                "runtime_max_environment_steps": conditions[
                    "runtime_max_environment_steps"
                ],
                "smoke": conditions["smoke"],
            },
            "episode": {
                "success": bool(episode.get("success", False)),
                "termination_reason": episode.get("termination_reason"),
                "environment_steps": episode.get("environment_steps"),
                "action_chunks": episode.get("action_chunks"),
                "planner_calls": episode.get("planner_calls"),
                "verifier_calls": episode.get("verifier_calls"),
                "recovery_attempts": episode.get("recovery_attempts"),
                "exceptions": episode.get("exceptions"),
                "latency_seconds": episode.get("latency_seconds"),
            },
            "error": error,
        }

    run_trial.protocol_hash = protocol_hash  # type: ignore[attr-defined]
    run_trial.base_config_hash = base_config_hash  # type: ignore[attr-defined]
    return run_trial


def _apply_trial_overrides(
    run_config: dict[str, Any],
    agent_config: dict[str, Any],
    attempt_dir: Path,
    identity: StudyIdentity,
    spec: ArmSpec,
    horizon: int,
    *,
    horizon_multiplier: float = HORIZON_MULTIPLIER,
    smoke_budget: Mapping[str, int] | None = None,
    forced_trigger: bool = False,
) -> None:
    benchmark_args = run_config["benchmark"]["init_args"]
    benchmark_args["task_names"] = [identity.task_name]
    benchmark_args["max_tasks"] = 1
    benchmark_args["episodes_per_task"] = 1
    benchmark_args["episode_indices"] = [identity.episode_index]
    benchmark_args["split"] = identity.split
    benchmark_args["resume"] = False
    benchmark_args["output_dir"] = str(attempt_dir)
    # 环境 horizon 只能由 evaluator 的 horizon_multiplier 决定：
    # task["horizon_override"] = ceil(registry_horizon * horizon_multiplier)。
    # 必须改写为研究协议的值，否则 run config 自带的多余倍率会静默生效。
    benchmark_args["horizon_multiplier"] = horizon_multiplier

    pipeline_init_args = run_config["pipeline"]["init_args"]
    pipeline_config = pipeline_init_args["config"]
    pipeline_config["node_selective_recovery_enabled"] = spec.node_selective_recovery
    pipeline_config["recovery_enabled"] = False
    # atomic_recovery_enabled 属于 init_args 层级，不是 SkillExecutionConfig 的字段
    pipeline_init_args["atomic_recovery_enabled"] = False
    if forced_trigger:
        # 仅用于 smoke 的强制触发：把节点 chunk 预算压到 1 并每个 chunk 验证一次，
        # 走的是既有 chunk_budget -> trigger_recovery -> 选择性恢复路径，
        # 不新增绕过路径。结论只算链路连通性，不算真实 recovery 成功。
        # （A 臂没有恢复路径，调用方按臂裁剪后再传入，见 run_trial。）
        if not spec.node_selective_recovery:
            raise ConfigError("forced_trigger smoke requires node-selective recovery")
        pipeline_config["planner_check_interval_chunks"] = 1
        pipeline_config["max_chunks_per_skill"] = 1
        pipeline_config["replan_on_chunk_budget"] = True
        # 验证节奏由 verifier 自己的 check_interval_chunks 决定，和 pipeline 的
        # planner_check_interval_chunks 是两个字段。不改这里的话 chunk 预算失败
        # 永远走不到 node checkpoint，选择性恢复与顾问链都不会触发
        # （2026-09-11 实测：3 个 chunk 后 execution_aborted，advice_count=0）。
        verifier_args = agent_config.get("verifier", {}).get("init_args")
        if isinstance(verifier_args, dict):
            verifier_args["check_interval_chunks"] = 1

    runtime_config = run_config["runtime"]["init_args"].setdefault("config", {})
    runtime_config["output_dir"] = str(attempt_dir / "traces")
    # horizon 的 2.5 倍必须同时落到 runtime，否则 runtime 会先截断。
    runtime_config["max_environment_steps"] = horizon
    runtime_config["max_action_chunks"] = max(
        int(runtime_config.get("max_action_chunks", 256)), horizon
    )
    runtime_config["max_pipeline_iterations"] = max(
        int(runtime_config.get("max_pipeline_iterations", 512)), horizon
    )
    if smoke_budget is not None:
        # smoke 预算只允许压低 runtime 上限；环境 horizon 保持协议值不动。
        for key, value in smoke_budget.items():
            if key in {
                "max_environment_steps",
                "max_action_chunks",
                "max_pipeline_iterations",
            }:
                runtime_config[key] = int(value)
        smoke_steps = int(smoke_budget.get("max_environment_steps", horizon))
        if smoke_steps >= horizon:
            raise ConfigError(
                "smoke budget must be below the protocol horizon; use the study "
                "stage for full-horizon runs"
            )

    agent_args = agent_config.get("agent", {}).get("init_args", {})
    manager = agent_args.get("module_evolution_manager", {}).get("init_args")
    if isinstance(manager, dict) and "path" in manager:
        manager["path"] = str(attempt_dir / "module_evolution.jsonl")
    capsules = agent_args.get("experience_capsule_store", {}).get("init_args")
    if isinstance(capsules, dict) and "path" in capsules:
        capsules["path"] = str(attempt_dir / "experience_capsules.jsonl")


__all__ = [
    "ARM_SPECS",
    "ARMS",
    "HORIZON_MULTIPLIER",
    "STUDY_TASKS",
    "ArmSpec",
    "StudyIdentity",
    "StudyPlan",
    "StudyState",
    "Trial",
    "build_study_plan",
    "build_study_runner",
    "classify_outcome",
    "effective_horizon",
    "safety_violations",
    "validate_task_names",
]
