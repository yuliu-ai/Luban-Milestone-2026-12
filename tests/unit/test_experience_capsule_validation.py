from __future__ import annotations

from pathlib import Path

import pytest

from luban.agent_core.agents.self_evolving import SelfEvolvingAgent
from luban.agent_core.experience_capsules import (
    ExperienceCapsule,
    ExperienceCapsuleStore,
)
from luban.agent_core.memories import InMemoryMemory
from luban.agent_core.planners.base import Planner
from luban.agent_core.verifiers.base import Verifier
from luban.backends.skills.base import SkillBackend
from luban.evals.evolution_validation import (
    ValidationIdentity,
    run_experience_capsule_validation,
)

CONTEXT = {
    "code_version": "code-v1",
    "config_version": "config-v1",
    "base_policy": "policy-v1",
    "horizon": 1200,
}


def _store(tmp_path: Path) -> tuple[ExperienceCapsuleStore, str]:
    store = ExperienceCapsuleStore(tmp_path / "capsules.jsonl")
    capsule = ExperienceCapsule.candidate(
        source_identity={
            "task_name": "ScrubCuttingBoard",
            "episode_index": 0,
            "seed": 100,
        },
        **CONTEXT,
        skill_name="verify_grasp",
        skill_type="spatial_reasoning",
        description="Verify the grasp before sweeping.",
        instructions="Lift briefly and require visible separation.",
        failure_signature="skill_gap:grasp",
        responsible_module="planner",
        applicability=("ScrubCuttingBoard", "task:ScrubCuttingBoard"),
        evidence_refs=("trace:source",),
        source_robot_profile="xr1_mobile_manipulator",
        compatible_robot_profiles=("xr1_mobile_manipulator",),
        required_capabilities=("mobile_manipulation",),
        transfer_notes="Re-ground the target robot.",
    )
    store.add_candidate(capsule)
    return store, capsule.capsule_id


def _identities() -> list[ValidationIdentity]:
    return [
        ValidationIdentity(
            "ScrubCuttingBoard", episode_index=1, split="target", seed=101
        ),
        ValidationIdentity(
            "ScrubCuttingBoard", episode_index=2, split="target", seed=102
        ),
    ]


def test_experience_pairs_validate_and_become_retrievable(tmp_path: Path) -> None:
    store, capsule_id = _store(tmp_path)

    def run_episode(identity: ValidationIdentity, arm: str):
        return {
            "outcome": "failure" if arm == "baseline" else "success",
            "evidence_refs": [f"{arm}:{identity.episode_index}"],
            "verifier_confirmed": True,
        }

    report = run_experience_capsule_validation(
        store,
        capsule_id,
        identities=_identities(),
        run_episode=run_episode,
        **CONTEXT,
        source_identities=["target/ScrubCuttingBoard/0000"],
    )

    assert report["pairs_recorded"] == 2
    assert report["counts"]["positive"] == 2
    assert report["state"] == "validated"
    retrieved = store.retrieve_for_context(
        {
            "task_name": "ScrubCuttingBoard",
            "robot_profile": "xr1_mobile_manipulator",
            "capabilities": ["mobile_manipulation"],
        }
    )
    assert [item.capsule_id for item in retrieved] == [capsule_id]


def test_experience_regression_quarantines_capsule(tmp_path: Path) -> None:
    store, capsule_id = _store(tmp_path)

    def run_episode(identity: ValidationIdentity, arm: str):
        return {
            "outcome": "success" if arm == "baseline" else "failure",
            "evidence_refs": [f"{arm}:{identity.episode_index}"],
            "verifier_confirmed": True,
        }

    report = run_experience_capsule_validation(
        store,
        capsule_id,
        identities=[_identities()[0]],
        run_episode=run_episode,
        **CONTEXT,
    )

    assert report["state"] == "quarantined"
    assert report["counts"]["regression"] == 1
    assert store.retrieve_for_context({"task_name": "ScrubCuttingBoard"}) == []


def test_experience_invalid_pairs_are_excluded_without_promotion(
    tmp_path: Path,
) -> None:
    store, capsule_id = _store(tmp_path)

    def run_episode(identity: ValidationIdentity, arm: str):
        return {
            "outcome": "deferred",
            "evidence_refs": [f"{arm}:{identity.episode_index}"],
        }

    report = run_experience_capsule_validation(
        store,
        capsule_id,
        identities=[_identities()[0]],
        run_episode=run_episode,
        **CONTEXT,
    )

    assert report["pairs_recorded"] == 0
    assert report["excluded_count"] == 1
    assert report["state"] == "candidate"
    assert report["positive_validations"] == 0


def test_experience_pair_requires_explicit_verifier_confirmation(
    tmp_path: Path,
) -> None:
    store, capsule_id = _store(tmp_path)
    report = run_experience_capsule_validation(
        store,
        capsule_id,
        identities=[_identities()[0]],
        run_episode=lambda identity, arm: {
            "outcome": "success",
            "evidence_refs": [f"trace:{arm}"],
        },
        **CONTEXT,
    )
    assert report["pairs_recorded"] == 0
    assert "verifier_not_confirmed" in report["excluded"][0]["reason"]


def test_experience_validation_requires_seed_and_matching_context(
    tmp_path: Path,
) -> None:
    store, capsule_id = _store(tmp_path)

    with pytest.raises(ValueError, match="context"):
        run_experience_capsule_validation(
            store,
            capsule_id,
            identities=[_identities()[0]],
            run_episode=lambda identity, arm: {
                "outcome": "success",
                "evidence_refs": ["trace"],
            },
            code_version="other-code",
            config_version=CONTEXT["config_version"],
            base_policy=CONTEXT["base_policy"],
            horizon=CONTEXT["horizon"],
        )

    no_seed = ValidationIdentity("ScrubCuttingBoard", 3, "target")
    report = run_experience_capsule_validation(
        store,
        capsule_id,
        identities=[no_seed],
        run_episode=lambda identity, arm: {
            "outcome": "success",
            "evidence_refs": ["trace"],
        },
        **CONTEXT,
    )
    assert report["pairs_recorded"] == 0
    assert "explicit seed" in report["excluded"][0]["reason"]


class _Planner(Planner):
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def plan(self, inputs):
        self.inputs.append(inputs)
        return {
            "skill": "Pick_Place",
            "subtask": "pick the object",
            "grounded_arguments": {"object": "object"},
            "expected_outcome": "object held",
        }


class _Verifier(Verifier):
    def verify(self, inputs):
        return {"execution_status": "in_progress"}


class _Backend(SkillBackend):
    def predict(self, inputs):
        return {"action": 0}


def test_validated_capsule_reaches_next_agent_planner(tmp_path: Path) -> None:
    store, capsule_id = _store(tmp_path)
    identity = _identities()[0]

    run_experience_capsule_validation(
        store,
        capsule_id,
        identities=[identity, _identities()[1]],
        run_episode=lambda current, arm: {
            "outcome": "failure" if arm == "baseline" else "success",
            "evidence_refs": [f"trace:{arm}:{current.episode_index}"],
            "verifier_confirmed": True,
        },
        **CONTEXT,
    )
    assert store.get(capsule_id).state == "validated"

    planner = _Planner()
    agent = SelfEvolvingAgent(
        planner=planner,
        verifier=_Verifier(),
        memory=InMemoryMemory(),
        skill_backend=_Backend(),
        experience_capsule_store=store,
        experience_context={
            "code_version": CONTEXT["code_version"],
            "config_version": CONTEXT["config_version"],
            "base_policy": CONTEXT["base_policy"],
            "required_capabilities": [],
            "source_robot_profile": "xr1_mobile_manipulator",
            "compatible_robot_profiles": ["xr1_mobile_manipulator"],
            "transfer_notes": "Re-ground before execution.",
        },
        robot_profile="xr1_mobile_manipulator",
        robot_capabilities=["mobile_manipulation"],
    )
    task = {
        "name": "ScrubCuttingBoard",
        "task_set": "target50",
        "episode_index": 5,
        "seed": 105,
        "horizon": CONTEXT["horizon"],
    }
    begin = agent.begin_episode({"task": task, "initial_observation": {}})
    assert begin is not None
    assert begin["experience_capsules"]["capsule_ids"] == [capsule_id]
    agent.set_shadow_experience_capsules([store.get(capsule_id)])
    agent.plan({"task": task, "observation": {}, "available_skills": ["Pick_Place"]})
    assert (
        planner.inputs[-1]["knowledge_context"]["validated_experiences"][0][
            "capsule_id"
        ]
        == capsule_id
    )
    assert (
        planner.inputs[-1]["knowledge_context"]["shadow_experiences"][0]["state"]
        == "shadow"
    )
    agent.close()
