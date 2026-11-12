"""恢复—记忆研究运行器的身份、次序与 horizon 注入测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from luban.evals.recovery_memory_study import (
    ARM_SPECS,
    ARMS,
    HORIZON_MULTIPLIER,
    STUDY_TASKS,
    StudyIdentity,
    StudyState,
    _apply_trial_overrides,
    build_study_plan,
    classify_outcome,
    effective_horizon,
    safety_violations,
    validate_task_names,
)
from luban.exceptions import ConfigError


def test_study_covers_eight_composite_seen_tasks():
    assert len(STUDY_TASKS) == 8
    names = [name for name, _ in STUDY_TASKS]
    assert len(set(names)) == 8


@pytest.mark.parametrize(
    "registry_horizon,expected",
    [
        (1200, 3000),
        (1500, 3750),
        (1650, 4125),
        (1800, 4500),
        (2100, 5250),
        (2400, 6000),
    ],
)
def test_horizon_multiplier_is_exactly_two_point_five(registry_horizon, expected):
    assert effective_horizon(registry_horizon) == expected
    assert effective_horizon(registry_horizon) == int(
        registry_horizon * HORIZON_MULTIPLIER
    )


def test_horizon_multiplier_rejects_bad_input():
    with pytest.raises(ValueError):
        effective_horizon(0)
    with pytest.raises(ValueError):
        effective_horizon(1200, 0)
    with pytest.raises(TypeError):
        effective_horizon(1200, True)


def test_plan_is_eighty_identities_and_four_hundred_trials():
    plan = build_study_plan(initial_conditions=10)

    assert len(plan.identities) == 80
    assert len(plan.trials) == 80 * len(ARMS)
    assert len(plan.trials) == 400
    assert {trial.arm for trial in plan.trials} == set(ARMS)
    assert len(ARMS) == 5


def test_each_identity_runs_every_arm_exactly_once():
    plan = build_study_plan(initial_conditions=10)

    grouped: dict[str, list[str]] = {}
    for trial in plan.trials:
        grouped.setdefault(trial.identity.canonical_identity, []).append(trial.arm)

    assert len(grouped) == 80
    for arms in grouped.values():
        assert sorted(arms) == sorted(ARMS)


def test_arm_order_is_balanced_not_constant():
    plan = build_study_plan(initial_conditions=10)

    orders = {
        tuple(
            trial.arm
            for trial in plan.trials
            if trial.identity.canonical_identity == identity.canonical_identity
        )
        for identity in plan.identities
    }
    # 若所有实例都是同一个顺序，说明次序没有随机化
    assert len(orders) > 1
    assert all(sorted(order) == sorted(ARMS) for order in orders)


def test_arm_order_is_reproducible_from_the_seed():
    first = build_study_plan(initial_conditions=10, order_seed=7)
    second = build_study_plan(initial_conditions=10, order_seed=7)

    assert [trial.arm for trial in first.trials] == [
        trial.arm for trial in second.trials
    ]


def test_plan_horizons_are_the_multiplied_values():
    plan = build_study_plan(initial_conditions=10)

    assert plan.horizons()["ScrubCuttingBoard"] == 3000
    assert plan.horizons()["StirVegetables"] == 6000


def test_identity_excludes_arm_but_trial_id_includes_it():
    identity = StudyIdentity(task_name="ScrubCuttingBoard", episode_index=11)

    assert identity.canonical_identity == "target/ScrubCuttingBoard/0011"
    assert "A" not in identity.canonical_identity.split("/")[-1]
    assert identity.source_group_id == "target-ScrubCuttingBoard-0011"


def test_identity_rejects_bad_values():
    with pytest.raises(ValueError):
        StudyIdentity(task_name="", episode_index=0)
    with pytest.raises(ValueError):
        StudyIdentity(task_name="X", episode_index=-1)


def test_validate_task_names_reports_missing():
    available = [name for name, _ in STUDY_TASKS]
    assert validate_task_names(available) == []
    assert validate_task_names(available[:-1]) == [available[-1]]


def test_classify_outcome_separates_infrastructure_from_failure():
    assert classify_outcome({"success": True}) == "success"
    assert classify_outcome({"success": False, "termination_reason": "horizon"}) == (
        "failure"
    )
    assert (
        classify_outcome({"success": False, "termination_reason": "exception"})
        == "infrastructure_error"
    )
    assert classify_outcome({"success": False, "exceptions": 1}) == (
        "infrastructure_error"
    )
    assert (
        classify_outcome({"success": False, "error_type": "BackendError"})
        == "infrastructure_error"
    )


def test_safety_violations_are_surfaced():
    assert safety_violations({"termination_reason": "policy_action_out_of_range"}) == [
        "policy_action_out_of_range"
    ]
    assert safety_violations({"safety_violation": "collision"}) == ["collision"]
    assert safety_violations({}) == []


def test_state_resume_skips_only_complete_and_verified(tmp_path: Path):
    state = StudyState(tmp_path / "trials.jsonl").load()
    state.append(
        {
            "event": "trial_finished",
            "trial_id": "target/T/0000/A",
            "arm": "A",
            "status": "complete",
            "checksums_ok": True,
            "task_success": True,
        }
    )
    state.append(
        {
            "event": "trial_finished",
            "trial_id": "target/T/0000/B",
            "arm": "B",
            "status": "complete",
            "checksums_ok": False,
        }
    )
    state.append(
        {
            "event": "trial_finished",
            "trial_id": "target/T/0000/C",
            "arm": "C",
            "status": "partial",
            "checksums_ok": True,
        }
    )

    completed = state.completed_trials()

    assert list(completed) == ["target/T/0000/A"]


def test_state_reload_from_disk_preserves_records(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    first = StudyState(path).load()
    first.append({"event": "trial_attempt", "trial_id": "t1"})
    first.append({"event": "trial_attempt", "trial_id": "t1"})

    reloaded = StudyState(path).load()

    assert reloaded.attempt_counts() == {"t1": 2}
    assert reloaded.summary()["trial_attempts"] == 2


def _run_config() -> dict:
    return {
        "agent_config": "../agents/x.yaml",
        "pipeline": {
            "init_args": {
                "atomic_recovery_enabled": True,
                "config": {
                    "node_selective_recovery_enabled": True,
                    "recovery_enabled": False,
                },
            }
        },
        "runtime": {
            "init_args": {
                "config": {
                    "max_environment_steps": 3600,
                    "max_action_chunks": 256,
                    "max_pipeline_iterations": 512,
                    "output_dir": "runs/original",
                }
            }
        },
        "benchmark": {
            "init_args": {
                "task_names": ["original"],
                "max_tasks": 0,
                "episodes_per_task": 0,
                "output_dir": "runs/original",
            }
        },
    }


def test_horizon_multiplier_lands_in_both_environment_and_runtime(tmp_path: Path):
    """环境 horizon 与 runtime 上限必须是同一个值，且都等于协议倍率。"""
    run_config = _run_config()
    # run config 自带一个多余的 3.0 倍率，研究必须覆写它，否则环境 horizon 会
    # 静默变成 3.0x 而 runtime 仍按 2.5x 截断。
    run_config["benchmark"]["init_args"]["horizon_multiplier"] = 3.0
    agent_config = {"agent": {"init_args": {}}}
    identity = StudyIdentity(task_name="StirVegetables", episode_index=0)
    horizon = effective_horizon(2400)

    _apply_trial_overrides(
        run_config, agent_config, tmp_path, identity, ARM_SPECS["B"], horizon
    )

    runtime_config = run_config["runtime"]["init_args"]["config"]
    benchmark_args = run_config["benchmark"]["init_args"]
    assert horizon == 6000
    assert benchmark_args["horizon_multiplier"] == HORIZON_MULTIPLIER
    assert effective_horizon(2400, benchmark_args["horizon_multiplier"]) == 6000
    assert runtime_config["max_environment_steps"] == 6000
    assert runtime_config["max_environment_steps"] > 3600
    assert benchmark_args["episode_indices"] == [0]


def test_arm_specs_only_differ_in_declared_treatment_fields(tmp_path: Path):
    """A/B/C 的配置差异必须只有声明的 treatment 字段。"""
    identity = StudyIdentity(task_name="KettleBoiling", episode_index=3)
    horizon = effective_horizon(1500)
    projections = {}
    run_configs = {}
    for arm in ARMS:
        run_config = _run_config()
        agent_config = {"agent": {"init_args": {}}}
        _apply_trial_overrides(
            run_config,
            agent_config,
            tmp_path / arm,
            identity,
            ARM_SPECS[arm],
            horizon,
        )
        run_configs[arm] = run_config
        projections[arm] = run_config["pipeline"]["init_args"]["config"]

    assert projections["A"]["node_selective_recovery_enabled"] is False
    assert projections["B"]["node_selective_recovery_enabled"] is True
    assert projections["C"]["node_selective_recovery_enabled"] is True
    for arm in ARMS:
        assert projections[arm]["recovery_enabled"] is False
        assert (
            run_configs[arm]["pipeline"]["init_args"]["atomic_recovery_enabled"]
            is False
        )


def test_arm_specs_expose_only_declared_treatment_diff():
    assert ARM_SPECS["A"].treatment_fields() == {
        "node_selective_recovery_enabled": False,
        "recovery_advisor_mode": None,
    }
    assert ARM_SPECS["C"].treatment_fields() == {
        "node_selective_recovery_enabled": True,
        "recovery_advisor_mode": "advisor",
    }
    # B 与 C 的唯一差别是顾问
    diff = {
        key
        for key in ARM_SPECS["B"].treatment_fields()
        if ARM_SPECS["B"].treatment_fields()[key]
        != ARM_SPECS["C"].treatment_fields()[key]
    }
    assert diff == {"recovery_advisor_mode"}


@pytest.mark.parametrize(
    "arm,mode",
    [("C", "advisor"), ("D", "direct"), ("E", "no_retrieval")],
)
def test_advisor_arms_differ_from_b_only_in_the_advisor_mode(arm, mode):
    """控制臂 D/E 必须与 B 只差一个字段，否则归因不成立。"""
    spec = ARM_SPECS[arm]
    baseline = ARM_SPECS["B"].treatment_fields()

    assert spec.advisor_mode == mode
    assert spec.advisor_enabled is True
    assert spec.node_selective_recovery is True
    assert (
        spec.treatment_fields()["node_selective_recovery_enabled"]
        == (baseline["node_selective_recovery_enabled"])
    )
    assert (
        spec.treatment_fields()["recovery_advisor_mode"]
        != (baseline["recovery_advisor_mode"])
    )


def test_control_arms_a_and_b_have_no_advisor():
    assert ARM_SPECS["A"].advisor_enabled is False
    assert ARM_SPECS["B"].advisor_enabled is False
    assert ARM_SPECS["A"].advisor_mode is None
    assert ARM_SPECS["B"].advisor_mode is None


def test_every_arm_shares_the_same_recovery_setting_except_a():
    for arm in ("B", "C", "D", "E"):
        assert ARM_SPECS[arm].node_selective_recovery is True
    assert ARM_SPECS["A"].node_selective_recovery is False


def test_private_stores_are_redirected_per_trial(tmp_path: Path):
    run_config = _run_config()
    agent_config = {
        "agent": {
            "init_args": {
                "module_evolution_manager": {"init_args": {"path": "runs/prod.jsonl"}},
                "experience_capsule_store": {"init_args": {"path": "runs/caps.jsonl"}},
            }
        }
    }
    identity = StudyIdentity(task_name="WashLettuce", episode_index=1)

    _apply_trial_overrides(
        run_config,
        agent_config,
        tmp_path,
        identity,
        ARM_SPECS["C"],
        effective_horizon(1650),
    )

    init_args = agent_config["agent"]["init_args"]
    assert init_args["module_evolution_manager"]["init_args"]["path"].startswith(
        str(tmp_path)
    )
    assert init_args["experience_capsule_store"]["init_args"]["path"].startswith(
        str(tmp_path)
    )
    assert run_config["benchmark"]["init_args"]["output_dir"] == str(tmp_path)


def test_smoke_budget_presses_runtime_but_not_environment_horizon(tmp_path: Path):
    """smoke 只压 runtime 预算；环境 horizon 仍是协议的 2.5x。"""
    run_config = _run_config()
    agent_config = {"agent": {"init_args": {}}}
    identity = StudyIdentity(task_name="ScrubCuttingBoard", episode_index=30)

    _apply_trial_overrides(
        run_config,
        agent_config,
        tmp_path,
        identity,
        ARM_SPECS["B"],
        effective_horizon(1200),
        smoke_budget={
            "max_environment_steps": 64,
            "max_action_chunks": 32,
            "max_pipeline_iterations": 64,
        },
    )

    benchmark_args = run_config["benchmark"]["init_args"]
    runtime_config = run_config["runtime"]["init_args"]["config"]
    assert benchmark_args["horizon_multiplier"] == HORIZON_MULTIPLIER
    assert runtime_config["max_environment_steps"] == 64
    assert runtime_config["max_action_chunks"] == 32
    assert runtime_config["max_pipeline_iterations"] == 64


def test_smoke_budget_must_be_below_the_protocol_horizon(tmp_path: Path):
    run_config = _run_config()
    agent_config = {"agent": {"init_args": {}}}
    identity = StudyIdentity(task_name="ScrubCuttingBoard", episode_index=30)
    with pytest.raises(ConfigError):
        _apply_trial_overrides(
            run_config,
            agent_config,
            tmp_path,
            identity,
            ARM_SPECS["B"],
            effective_horizon(1200),
            smoke_budget={"max_environment_steps": 99_999},
        )


def test_forced_trigger_uses_existing_chunk_budget_path(tmp_path: Path):
    """forced trigger 只能改既有 chunk budget 与验证节奏旋钮，且 A 臂必须拒绝。"""
    run_config = _run_config()
    agent_config = {
        "agent": {"init_args": {}},
        "verifier": {"init_args": {"check_interval_chunks": 32}},
    }
    identity = StudyIdentity(task_name="ScrubCuttingBoard", episode_index=30)

    _apply_trial_overrides(
        run_config,
        agent_config,
        tmp_path,
        identity,
        ARM_SPECS["C"],
        effective_horizon(1200),
        forced_trigger=True,
    )
    projection = run_config["pipeline"]["init_args"]["config"]
    assert projection["planner_check_interval_chunks"] == 1
    assert projection["max_chunks_per_skill"] == 1
    assert projection["replan_on_chunk_budget"] is True
    assert projection["node_selective_recovery_enabled"] is True
    # 验证节奏在 verifier 自己的字段上；不改它的话选择性恢复不会触发
    assert agent_config["verifier"]["init_args"]["check_interval_chunks"] == 1

    with pytest.raises(ConfigError):
        _apply_trial_overrides(
            _run_config(),
            {"agent": {"init_args": {}}},
            tmp_path,
            identity,
            ARM_SPECS["A"],
            effective_horizon(1200),
            forced_trigger=True,
        )


def test_natural_runs_keep_the_verifier_interval_untouched(tmp_path: Path):
    run_config = _run_config()
    agent_config = {"verifier": {"init_args": {"check_interval_chunks": 32}}}

    _apply_trial_overrides(
        run_config,
        agent_config,
        tmp_path,
        StudyIdentity(task_name="ScrubCuttingBoard", episode_index=30),
        ARM_SPECS["B"],
        effective_horizon(1200),
    )

    assert agent_config["verifier"]["init_args"]["check_interval_chunks"] == 32
    projection = run_config["pipeline"]["init_args"]["config"]
    assert projection.get("max_chunks_per_skill") != 1


def test_classify_outcome_marks_smoke_budget_exhausted():
    episode = {
        "success": False,
        "termination_reason": "max_environment_steps",
        "environment_steps": 64,
    }
    assert classify_outcome(episode) == "failure"
    assert (
        classify_outcome(episode, smoke_budget_environment_steps=64)
        == "smoke_budget_exhausted"
    )
    # 未跑满预算不算 smoke 结束
    assert (
        classify_outcome(
            {**episode, "environment_steps": 32},
            smoke_budget_environment_steps=64,
        )
        == "failure"
    )


def test_development_and_diagnostic_identities_never_overlap():
    development = build_study_plan(initial_conditions=3, episode_offset=0)
    diagnostic = build_study_plan(initial_conditions=10, episode_offset=10)

    dev_ids = {identity.canonical_identity for identity in development.identities}
    diag_ids = {identity.canonical_identity for identity in diagnostic.identities}
    dev_groups = {identity.source_group_id for identity in development.identities}
    diag_groups = {identity.source_group_id for identity in diagnostic.identities}

    assert not (dev_ids & diag_ids)
    assert not (dev_groups & diag_groups)
    assert len(dev_ids) == 24
    assert len(diag_ids) == 80


def test_plan_task_filter_keeps_smoke_small():
    plan = build_study_plan(
        initial_conditions=4,
        episode_offset=30,
        task_names=["ScrubCuttingBoard"],
    )

    assert len(plan.identities) == 4
    assert {identity.task_name for identity in plan.identities} == {"ScrubCuttingBoard"}
    assert {identity.episode_index for identity in plan.identities} == {30, 31, 32, 33}
    with pytest.raises(ValueError):
        build_study_plan(initial_conditions=1, task_names=["NotATask"])


def test_plan_rejects_overlapping_offsets_with_diagnostic():
    development = build_study_plan(initial_conditions=3, episode_offset=0)
    overlapping = build_study_plan(initial_conditions=10, episode_offset=2)
    dev_ids = {identity.canonical_identity for identity in development.identities}
    overlap_ids = {identity.canonical_identity for identity in overlapping.identities}
    assert dev_ids & overlap_ids
