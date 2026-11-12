from __future__ import annotations

import json
from copy import deepcopy

import numpy as np
import pytest

from luban.agent_core import (
    ATOMIC_SKILL_CONTRACT_SCHEMA_VERSION,
    ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES,
    AtomicRecoveryEffectTracker,
    AtomicRecoverySkillRegistry,
    RoboCasaAtomicSkillCatalog,
    SpatialSceneMemory,
    TaskDecomposer,
)
from luban.environments.benchmarks.robocasa import RoboCasaEnvironment
from luban.exceptions import ProtocolSchemaError
from luban.perception import (
    VisualPerceptionServer,
    compile_concept_queries,
    extract_molmo2_multi_image_points,
    validate_perception_response,
)
from luban.pipelines import AtomicSpatialSkillExecutionPipeline

CAMERA = "video.robot0_agentview_left"


def visual_response(*, provider: str = "sam3", frame_index: int = 0) -> dict:
    provenance = {
        "source_provider": provider,
        "source_modality": "rgb",
        "frame_index": frame_index,
        "camera_id": CAMERA,
        "derived_from": ["rgb pixels"],
    }
    return {
        "schema_version": "visual-perception-v1",
        "provider": provider,
        "request_id": "request-1",
        "frame_index": frame_index,
        "timestamp": 10.0,
        "entities": [
            {
                "track_id": "mug-1",
                "label": "mug",
                "confidence": 0.9,
                "camera_id": CAMERA,
                "bbox_xyxy": [10, 20, 60, 80],
                "point_xy": [35, 50],
                "mask_ref": "masks/request-1/mug-1.rle.json",
                "attributes": {"color": "red"},
                "provenance": provenance,
            }
        ],
        "relations": [],
        "latency_ms": 25.0,
        "warnings": [],
    }


def privileged_state(frame_index: int = 0) -> dict:
    return {
        "schema_version": "robocasa-mujoco-object-state-v1",
        "source_class": "simulator_privileged",
        "frame_index": frame_index,
        "environment_steps": 16,
        "objects": [
            {
                "logical_name": "mug",
                "model_name": "mug_1",
                "body_id": 7,
                "position_xyz": [0.1, 0.2, 0.3],
                "orientation_wxyz": [1.0, 0.0, 0.0, 0.0],
                "linear_velocity_xyz": [0.0, 0.0, 0.0],
                "angular_velocity_xyz": [0.0, 0.0, 0.0],
            }
        ],
    }


def test_visual_contract_rejects_privileged_field() -> None:
    payload = visual_response()
    payload["entities"][0]["attributes"]["mujoco_body_xpos"] = [0, 0, 0]
    with pytest.raises(ProtocolSchemaError, match="privileged simulator field"):
        validate_perception_response(payload)


def test_spatial_memory_keeps_visual_and_privileged_sources_separate() -> None:
    memory = SpatialSceneMemory(allow_privileged_object_state=True)
    state = memory.initialize(session_id="episode-1")
    visual_update = memory.ingest_visual(state, visual_response())
    privileged_update = memory.ingest_privileged(state, privileged_state())
    snapshot = memory.prompt_snapshot(state)

    assert visual_update["source_class"] == "visual_rgb"
    assert privileged_update["source_class"] == "simulator_privileged"
    assert state["source_counts"] == {
        "visual_rgb": 1,
        "simulator_privileged": 1,
    }
    assert snapshot["source_policy"] == "visual_plus_explicit_simulator_privileged"
    assert snapshot["real_robot_transfer_warning"] is True
    entity = snapshot["entities"][0]
    assert entity["visual"]["provider"] == "sam3"
    assert (
        entity["simulator_privileged"]["provenance"]["deployable_to_real_robot"]
        is False
    )


def test_privileged_state_is_rejected_when_disabled() -> None:
    memory = SpatialSceneMemory(allow_privileged_object_state=False)
    with pytest.raises(
        ProtocolSchemaError, match="privileged object state is disabled"
    ):
        memory.ingest_privileged(memory.initialize(), privileged_state())


def test_atomic_registry_regrasps_without_claiming_effect_from_motion() -> None:
    registry = AtomicRecoverySkillRegistry()
    selected = registry.select(
        verification={"reason": "the mug slipped and is not held", "evidence": []},
        active_execution={"subtask": "grasp the mug", "expected_outcome": "held"},
        spatial_memory={"entities": {"mug": {}}},
    )
    assert selected.name == "regrasp"
    invocation = registry.invocation(
        selected,
        episode_attempt_id="attempt-1",
        node_id="grasp",
        invocation_index=1,
        parent_execution={"execution_id": "e1", "attempt_id": "a1"},
        spatial_revision=2,
        trigger={"reason": "slipped"},
    )
    tracker = AtomicRecoveryEffectTracker()
    before = {"revision": 2, "entities": {}, "relations": [], "source_counts": {}}
    after = {"revision": 2, "entities": {}, "relations": [], "source_counts": {}}
    outcome = tracker.assess(
        invocation,
        before_memory=before,
        after_memory=after,
        memory_delta=SpatialSceneMemory.compare(before, after),
        environment_result={"executed_steps": 8, "last_action_success": True},
        task_success=False,
    )
    assert outcome["status"] == "postcondition_unverifiable"
    assert outcome["postcondition_satisfied"] is False
    assert outcome["observed_effect"] is False


def test_effect_tracker_ignores_revision_timestamp_and_frame_only_changes() -> None:
    registry = AtomicRecoverySkillRegistry()
    skill = registry.select(
        verification={"reason": "camera view is ambiguous", "evidence": []},
        active_execution={"subtask": "find the mug", "expected_outcome": "visible"},
        spatial_memory={"entities": {"mug": {}}},
    )
    invocation = registry.invocation(
        skill,
        episode_attempt_id="attempt-1",
        node_id="observe",
        invocation_index=1,
        parent_execution={"execution_id": "e1", "attempt_id": "a1"},
        spatial_revision=1,
        trigger={"reason": "ambiguous"},
    )
    visual = {
        "source_class": "visual_rgb",
        "provider": "sam3",
        "frame_index": 1,
        "timestamp": 10.0,
        "camera_id": CAMERA,
        "confidence": 0.9,
        "bbox_xyxy": [10, 20, 60, 80],
        "point_xy": [35, 50],
        "mask_ref": "mask",
        "attributes": {},
        "provenance": {},
    }
    before = {
        "revision": 1,
        "entities": {"mug": {"observations": [visual]}},
        "relations": [],
        "source_counts": {"visual_rgb": 1},
    }
    after_visual = deepcopy(visual)
    after_visual.update({"frame_index": 2, "timestamp": 11.0})
    after = {
        "revision": 2,
        "entities": {"mug": {"observations": [visual, after_visual]}},
        "relations": [],
        "source_counts": {"visual_rgb": 2},
    }
    outcome = AtomicRecoveryEffectTracker.assess(
        invocation,
        before_memory=before,
        after_memory=after,
        memory_delta=SpatialSceneMemory.compare(before, after),
        environment_result={"executed_steps": 8, "last_action_success": True},
        task_success=False,
    )
    assert outcome["postcondition_satisfied"] is False
    assert outcome["observed_effect"] is False


def test_effect_tracker_accepts_meaningful_privileged_pose_change() -> None:
    registry = AtomicRecoverySkillRegistry()
    skill = registry.select(
        verification={"reason": "the mug slipped and is not held", "evidence": []},
        active_execution={"subtask": "grasp the mug", "expected_outcome": "held"},
        spatial_memory={"entities": {"mug": {}}},
    )
    invocation = registry.invocation(
        skill,
        episode_attempt_id="attempt-1",
        node_id="grasp",
        invocation_index=1,
        parent_execution={"execution_id": "e1", "attempt_id": "a1"},
        spatial_revision=1,
        trigger={"reason": "slipped"},
    )
    before = SpatialSceneMemory(allow_privileged_object_state=True).initialize()
    after = SpatialSceneMemory(allow_privileged_object_state=True).initialize()
    memory = SpatialSceneMemory(allow_privileged_object_state=True)
    memory.ingest_privileged(before, privileged_state(frame_index=1))
    moved = privileged_state(frame_index=2)
    moved["objects"][0]["position_xyz"] = [0.12, 0.2, 0.3]
    memory.ingest_privileged(after, moved)
    outcome = AtomicRecoveryEffectTracker.assess(
        invocation,
        before_memory=before,
        after_memory=after,
        memory_delta=SpatialSceneMemory.compare(before, after),
        environment_result={"executed_steps": 8, "last_action_success": True},
        task_success=False,
    )
    assert outcome["postcondition_satisfied"] is True
    assert outcome["postcondition_basis"] == "simulator_privileged_state"


def test_atomic_pipeline_restores_parent_subtask_before_verification() -> None:
    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        spatial_memory_enabled=False,
        atomic_recovery_enabled=True,
        node_selective_recovery_enabled=True,
    )
    parent = {
        "execution_id": "e1",
        "attempt_id": "a1",
        "node_id": "grasp",
        "subtask": "grasp the mug",
        "expected_outcome": "mug is held",
        "grounded_arguments": {},
        "planner_output": {
            "subtask": "grasp the mug",
            "expected_outcome": "mug is held",
            "grounded_arguments": {},
        },
    }
    skill = AtomicRecoverySkillRegistry().select(
        verification={"reason": "mug slipped"},
        active_execution=parent,
        spatial_memory=None,
    )
    invocation = AtomicRecoverySkillRegistry().invocation(
        skill,
        episode_attempt_id="attempt-1",
        node_id="grasp",
        invocation_index=1,
        parent_execution=parent,
        spatial_revision=0,
        trigger={"reason": "slipped"},
    )
    state = {
        "active_execution": deepcopy(parent),
        "pending_atomic_recovery": invocation,
        "spatial_scene_memory": {"revision": 0},
    }
    record = pipeline._activate_pending_atomic(state)
    assert record is not None
    assert state["active_execution"]["subtask"] != "grasp the mug"
    restored = pipeline._prepare_execution_for_verification(
        state,
        active_execution=state["active_execution"],
        environment_result={},
    )
    assert restored["subtask"] == "grasp the mug"
    assert state["atomic_recovery_active"]["restored_for_verification"] is True


def test_pipeline_schedules_one_atomic_recovery_exactly_at_failure_three() -> None:
    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        spatial_memory_enabled=True,
        allow_privileged_object_state=True,
        atomic_recovery_enabled=True,
        node_selective_recovery_enabled=True,
        node_failure_threshold=3,
        max_atomic_invocations_per_node=1,
    )
    active = {
        "execution_id": "e1",
        "attempt_id": "a1",
        "node_id": "grasp",
        "subtask": "grasp the mug",
        "expected_outcome": "mug is held",
    }
    state = {
        "session_id": "episode-1",
        "observation": {},
        "active_execution": active,
    }
    pipeline._ensure_atomic_state(state)
    # 未达阈值时不再抛协议错误，而是安静跳过：计数越过阈值后曾经永久抛错，
    # 直接终止 episode（真机 run13）。
    assert (
        pipeline._defer_selective_node_recovery(
            state,
            active_execution=active,
            verification={"reason": "first or second failure"},
            node_checkpoint={
                "trigger_recovery": False,
                "consecutive_failures": 2,
            },
        )
        is None
    )
    invocation = pipeline._defer_selective_node_recovery(
        state,
        active_execution=active,
        verification={"reason": "the mug slipped", "evidence": ["not held"]},
        node_checkpoint={
            "trigger_recovery": True,
            "consecutive_failures": 3,
        },
    )
    assert invocation is not None
    assert invocation["trigger"]["node_consecutive_failures"] == 3
    assert invocation["precondition_checks"]["passed"] is True
    assert state["atomic_skill_counts_by_node"] == {"grasp": 1}
    state["pending_atomic_recovery"] = None
    assert (
        pipeline._defer_selective_node_recovery(
            state,
            active_execution=active,
            verification={"reason": "still failed"},
            node_checkpoint={
                "trigger_recovery": True,
                "consecutive_failures": 3,
            },
        )
        is None
    )


class FakeData:
    body_xpos = [[0.1, 0.2, 0.3]]
    body_xquat = [[1.0, 0.0, 0.0, 0.0]]
    cvel = [[0.01, 0.02, 0.03, 0.04, 0.05, 0.06]]


class FakeSim:
    data = FakeData()


class FakeObject:
    name = "mug_model"


class FakeRawEnvironment:
    sim = FakeSim()
    obj_body_id = {"mug": 0}
    objects = {"mug": FakeObject()}


class FakeWrapper:
    unwrapped = FakeRawEnvironment()


def test_robocasa_privileged_snapshot_is_explicit_and_complete() -> None:
    environment = RoboCasaEnvironment(
        expose_privileged_object_state=True,
        environment_factory=lambda **_: None,
    )
    environment.env = FakeWrapper()
    environment.environment_steps = 16
    result = environment.privileged_object_state(frame_index=1)
    assert result["source_class"] == "simulator_privileged"
    assert result["objects"][0]["position_xyz"] == [0.1, 0.2, 0.3]
    assert result["objects"][0]["linear_velocity_xyz"] == [0.04, 0.05, 0.06]


class FakeDecomposerBackend:
    def __init__(self, atomic_skill_id: str = "TurnOnStove") -> None:
        self.atomic_skill_id = atomic_skill_id
        self.last_request = None

    def complete(self, request):
        self.last_request = deepcopy(request)
        content = {
            "nodes": [
                {
                    "id": "turn_on_burner",
                    "atomic_skill_id": self.atomic_skill_id,
                    "instruction": "Turn on the front-left stove burner.",
                    "dependencies": [],
                    "expected_outcome": "The front-left burner is on.",
                }
            ]
        }
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    def healthcheck(self):
        return {"healthy": True}

    def close(self):
        return None


def test_robocasa_catalog_covers_official_tasks_and_binds_xiaomi_node() -> None:
    assert len(ROBOCASA365_OFFICIAL_ATOMIC_TASK_NAMES) == 65
    catalog = RoboCasaAtomicSkillCatalog()
    backend = FakeDecomposerBackend()
    decomposer = TaskDecomposer(
        backend=backend,
        max_images=0,
        max_nodes=8,
        task_atomic_skill_catalog=catalog,
    )
    graph = decomposer.decompose(
        {
            "task": {"name": "KettleBoiling"},
            "initial_observation": {
                "annotation.human.task_description": (
                    "Place the kettle on a stove burner and turn the burner on."
                ),
                "available_skills": ["xiaomi_remote"],
            },
        }
    )

    assert graph["atomic_skill_bindings_validated"] is True
    assert graph["nodes"][0]["atomic_skill_id"] == "TurnOnStove"
    assert graph["nodes"][0]["atomic_skill_origin"] == (
        "robocasa365_official_atomic_task"
    )
    request_text = backend.last_request["messages"][1]["content"][0]["text"]
    assert "task_atomic_skill_catalog" in request_text
    schema = backend.last_request["response_format"]["json_schema"]["schema"]
    atomic_enum = schema["properties"]["nodes"]["items"]["properties"][
        "atomic_skill_id"
    ]["enum"]
    assert atomic_enum == ["PickPlaceCounterToStove", "TurnOnStove"]


def test_atomic_skill_contracts_are_complete_and_runtime_bound() -> None:
    catalog = RoboCasaAtomicSkillCatalog()
    contracts = catalog.contract_manifest()

    runtime_manifest = catalog.manifest_for_task({"name": "KettleBoiling"})
    assert [item["name"] for item in runtime_manifest] == [
        "PickPlaceCounterToStove",
        "TurnOnStove",
    ]
    assert all(
        item["contract"]["skill_id"] == item["name"] for item in runtime_manifest
    )
    assert len(contracts) == 41
    assert len({contract["skill_id"] for contract in contracts}) == len(contracts)
    for contract in contracts:
        assert contract["schema_version"] == ATOMIC_SKILL_CONTRACT_SCHEMA_VERSION
        assert contract["level"] == "atomic"
        assert (
            contract["input_schema"]["properties"]["task_atomic_skill_id"]["const"]
            == contract["skill_id"]
        )
        assert contract["preconditions"]
        assert contract["postconditions"]
        assert contract["termination"]["success"] == ("expected_postcondition_verified")
        assert contract["executor"]["backend"].endswith("XiaomiRemotePolicyBackend")
        assert contract["evidence_status"] == (
            "integrated_contract_without_paired_causal_effect_claim"
        )


def test_atomic_skill_hierarchy_has_no_dangling_references() -> None:
    graph = RoboCasaAtomicSkillCatalog().hierarchy_graph()
    node_ids = {node["id"] for node in graph["nodes"]}

    assert graph["summary"] == {
        "atomic_contract_count": 41,
        "primary_family_count": 9,
        "advanced_task_count": 16,
        "node_count": 66,
        "edge_count": 128,
    }
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in graph["edges"]
    )
    assert graph["claims_boundary"] == (
        "does_not_claim_final_64_skill_completion_or_causal_effectiveness"
    )


def test_task_decomposer_rejects_atomic_skill_outside_task_catalog() -> None:
    decomposer = TaskDecomposer(
        backend=FakeDecomposerBackend(atomic_skill_id="TurnOnMicrowave"),
        max_images=0,
        max_nodes=8,
        task_atomic_skill_catalog=RoboCasaAtomicSkillCatalog(),
    )
    with pytest.raises(
        ProtocolSchemaError, match="not in the task atomic skill catalog"
    ):
        decomposer.decompose(
            {
                "task": {"name": "KettleBoiling"},
                "initial_observation": {
                    "annotation.human.task_description": "Boil the kettle.",
                    "available_skills": ["xiaomi_remote"],
                },
            }
        )


def test_atomic_binding_becomes_exact_xiaomi_policy_instruction() -> None:
    graph = TaskDecomposer(
        backend=FakeDecomposerBackend(),
        max_images=0,
        max_nodes=8,
        task_atomic_skill_catalog=RoboCasaAtomicSkillCatalog(),
    ).decompose(
        {
            "task": {"name": "KettleBoiling"},
            "initial_observation": {
                "annotation.human.task_description": "Boil the kettle.",
                "available_skills": ["xiaomi_remote"],
            },
        }
    )
    node = graph["nodes"][0]
    assert node["atomic_skill_instruction"].startswith("Turn on exactly")
    execution = {
        "subtask": node["instruction"],
        "grounded_arguments": {},
        "task_atomic_skill_id": node["atomic_skill_id"],
        "task_atomic_skill_instruction": node["atomic_skill_instruction"],
        "task_atomic_skill_expected_postcondition": node[
            "atomic_skill_expected_postcondition"
        ],
    }
    policy_text = AtomicSpatialSkillExecutionPipeline._xiaomi_policy_subtask(execution)
    assert "Atomic skill [TurnOnStove]" in policy_text
    assert "Bound node instruction: Turn on the front-left stove burner." in policy_text
    assert "Skill postcondition:" in policy_text


def test_atomic_recovery_does_not_mix_parent_task_skill_prompt() -> None:
    policy_text = AtomicSpatialSkillExecutionPipeline._xiaomi_policy_subtask(
        {
            "subtask": "Reobserve the target from the wrist camera.",
            "grounded_arguments": {"atomic_recovery_skill": "reobserve_target"},
            "task_atomic_skill_id": "TurnOnStove",
            "task_atomic_skill_instruction": "Turn on the burner.",
        }
    )
    assert policy_text == "Reobserve the target from the wrist camera."


def test_perception_server_contract_and_molmo2_point_parser() -> None:
    assert compile_concept_queries(
        ["Put the mug below the coffee machine"], max_concepts=4
    ) == ["coffee machine", "mug"]
    points = extract_molmo2_multi_image_points(
        '<points coords="1 1 0500 0250;2 1 0750 0500"/>',
        [(640, 480), (320, 240)],
    )
    assert points == [(1, "1", 320.0, 120.0), (2, "1", 240.0, 120.0)]

    server = VisualPerceptionServer(
        provider="molmo2",
        analyzer=lambda request: {
            "entities": visual_response(
                provider="molmo2", frame_index=request["frame_index"]
            )["entities"],
            "relations": [],
            "warnings": [],
        },
    )
    assert server.handle({"endpoint": "ping"})["ok"] is True
    result = server.handle(
        {
            "endpoint": "analyze",
            "data": {
                "schema_version": "visual-perception-v1",
                "provider": "molmo2",
                "request_id": "request-1",
                "session_id": "episode-1",
                "frame_index": 3,
                "timestamp": 10.0,
                "queries": ["mug"],
                "frames": {CAMERA: np.zeros((8, 8, 3), dtype=np.uint8)},
                "task": {"name": "PrepareCoffee"},
                "node_id": "place_mug",
            },
        }
    )
    assert result["ok"] is True
    assert result["result"]["entities"][0]["provenance"]["source_provider"] == "molmo2"


class FakePerceptionClient:
    def __init__(self, provider: str) -> None:
        self.provider = provider


def test_perception_cadence_runs_molmo_before_sam_and_reuses_labels() -> None:
    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[
            FakePerceptionClient("sam3"),
            FakePerceptionClient("molmo2"),
        ],
        perception_required=True,
        node_selective_recovery_enabled=True,
        perception_interval_chunks=32,
        molmo2_interval_chunks=96,
    )
    assert [client.provider for client in pipeline.perception_clients] == [
        "molmo2",
        "sam3",
    ]
    state = {"last_spatial_refresh_by_provider": {"molmo2": 0, "sam3": 0}}
    assert (
        pipeline._provider_due(pipeline.perception_clients[0], chunks=32, state=state)
        is False
    )
    assert (
        pipeline._provider_due(pipeline.perception_clients[1], chunks=32, state=state)
        is True
    )
    assert pipeline._recent_visual_labels(
        {
            "entities": {
                "visual:molmo2:mug": {
                    "label": "mug",
                    "observations": [{"provider": "molmo2"}],
                }
            }
        },
        "molmo2",
    ) == ["mug"]


def test_uncertain_exhaustion_counts_as_failure_at_threshold() -> None:
    """uncertain 预算反复耗尽必须能让阈值恢复介入（真机 run5 的空转缺口）。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        spatial_memory_enabled=False,
        atomic_recovery_enabled=True,
        node_selective_recovery_enabled=True,
        node_failure_threshold=2,
        max_atomic_invocations_per_node=1,
    )
    runtime = pipeline.node_state_machine.initialize(
        {"nodes": [{"id": "grasp", "task": "grasp the mug", "dependencies": []}]}
    )
    first = pipeline.node_state_machine.record_uncertain_exhaustion(
        runtime, "grasp", action_chunk=8
    )
    assert first["consecutive_failures"] == 1
    assert first["trigger_recovery"] is False
    second = pipeline.node_state_machine.record_uncertain_exhaustion(
        runtime, "grasp", action_chunk=16
    )
    assert second["consecutive_failures"] == 2
    assert second["trigger_recovery"] is True
    node = runtime["nodes"]["grasp"]
    assert node["total_uncertain_exhaustions"] == 2
    assert node["last_uncertain_exhaustion_chunk"] == 16


def test_uncertain_failure_counting_is_opt_in() -> None:
    """默认关闭：v22/B7 的既有行为不变。"""

    default_pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )
    assert default_pipeline.uncertain_exhaustion_counts_as_failure is False
    enabled = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
        uncertain_exhaustion_counts_as_failure=True,
    )
    assert enabled.uncertain_exhaustion_counts_as_failure is True


def test_uncertain_exhaustion_survives_intervening_checkpoints() -> None:
    """中间的 in_progress checkpoint 不得让阈值永远达不到（真机 run6 的空转）。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        spatial_memory_enabled=False,
        atomic_recovery_enabled=True,
        node_selective_recovery_enabled=True,
        node_failure_threshold=3,
        max_atomic_invocations_per_node=1,
    )
    machine = pipeline.node_state_machine
    runtime = machine.initialize(
        {"nodes": [{"id": "grasp", "task": "grasp the mug", "dependencies": []}]}
    )
    machine.record_uncertain_exhaustion(runtime, "grasp", action_chunk=8)
    machine.record_checkpoint(
        runtime,
        "grasp",
        checkpoint_status="in_progress",
        action_chunk=16,
        verification={"reason": "still running"},
    )
    machine.record_uncertain_exhaustion(runtime, "grasp", action_chunk=24)
    third = machine.record_uncertain_exhaustion(runtime, "grasp", action_chunk=32)
    assert third["consecutive_failures"] == 3
    assert third["trigger_recovery"] is True


def test_deterministic_progress_evidence_tracks_scene_entity_delta() -> None:
    """无变化判 False、有变化判 True 并推进基线（不再依赖 VLM 猜）。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )

    def memory(point_x: int, revision: int) -> dict:
        return {
            "revision": revision,
            "entities": {
                "visual:sam3:sponge-1": {
                    "label": "sponge",
                    "camera_id": CAMERA,
                    "bbox_xyxy": [point_x, 20, point_x + 40, 60],
                }
            },
            "relations": [],
        }

    state: dict = {"spatial_scene_memory": memory(10, 0)}
    baseline = pipeline._deterministic_progress_evidence(state, {})
    assert baseline is not None
    assert baseline["progress"] is None
    state["spatial_scene_memory"] = memory(10, 1)
    same = pipeline._deterministic_progress_evidence(state, {})
    assert same is not None
    assert same["progress"] is False
    assert same["summary"]["changed_entities"] == 0
    state["spatial_scene_memory"] = memory(40, 2)
    moved = pipeline._deterministic_progress_evidence(state, {})
    assert moved is not None
    assert moved["progress"] is True
    assert moved["summary"]["changed_entities"] == 1
    # 基线推进后，同一场景再判无变化
    state["spatial_scene_memory"] = memory(40, 3)
    again = pipeline._deterministic_progress_evidence(state, {})
    assert again is not None
    assert again["progress"] is False


def test_deterministic_no_progress_limit_is_opt_in() -> None:
    """默认 None：v22/B7 的判定链不变。"""

    default_pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )
    assert default_pipeline.deterministic_no_progress_limit is None
    enabled = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
        deterministic_no_progress_limit=2,
    )
    assert enabled.deterministic_no_progress_limit == 2
    # 最小执行门槛：limit * planner_check_interval_chunks（run7 在 step 3 判死是误判）
    assert enabled.deterministic_no_progress_min_chunks() == 16
    later = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
        deterministic_no_progress_limit=3,
    )
    assert later.deterministic_no_progress_min_chunks() == 24


def test_body_state_evidence_distinguishes_frozen_from_exploring() -> None:
    """本体状态证据：死闭僵直判 frozen，开合探索判 not frozen（run5 vs run4）。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )

    def chunk(gripper: float, arm: float, steps: int = 16) -> np.ndarray:
        row = [arm, 0.0, 0.0, 0.0, 0.0, 0.0, gripper, 0.0, 0.0, 0.0, 0.0, 0.0]
        return np.array([row for _ in range(steps)], dtype=float)

    # run5 型：连续 chunk 都是"夹爪全力闭合 + 手臂几乎不动"
    frozen_state: dict = {}
    for _ in range(3):
        frozen = pipeline._body_state_evidence(frozen_state, chunk(1.0, 0.01))
    assert frozen is not None
    assert frozen["body_frozen"] is True
    assert frozen["latest"]["saturated_dims"] == [6]

    # run4 型：夹爪开合切换（窗口内饱和方向变化）⇒ 不能判冻僵
    exploring_state: dict = {}
    for gripper in (1.0, -1.0, 1.0):
        exploring = pipeline._body_state_evidence(exploring_state, chunk(gripper, 0.02))
    assert exploring is not None
    assert exploring["body_frozen"] is False


def test_deterministic_recovery_available_throttles_after_one_recovery() -> None:
    """短路节流：该节点已用过一次 bounded atomic recovery 后不再短路。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )
    active = {"node_id": "grasp"}
    assert pipeline._deterministic_recovery_available({}, active) is True
    state = {"atomic_skill_counts_by_node": {"grasp": 1}}
    assert pipeline._deterministic_recovery_available(state, active) is False
    other_node = {"node_id": "scrub"}
    assert pipeline._deterministic_recovery_available(state, other_node) is True


def test_proprioceptive_evidence_reads_ee_and_gripper_and_tracks_motion() -> None:
    """末端/夹爪本体读数进 verifier 输入，并给出与上一 chunk 的变化量。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )
    state: dict = {}
    observation = {
        "state.end_effector_position_relative": [0.1, 0.2, 0.3],
        "state.end_effector_rotation_relative": [1.0, 0.0, 0.0, 0.0],
        "state.gripper_qpos": [0.04, 0.04],
        "video.robot0_agentview_left": "ignored",
    }
    first = pipeline._proprioceptive_evidence(state, observation)
    assert first is not None
    assert first["frame_id"] == "robot0_base"
    assert "state.gripper_qpos" in first["keys"]
    assert first["readings"]["state.gripper_qpos"] == [0.04, 0.04]
    assert first["delta_since_last_chunk"] == {}
    assert first["moved_since_last_chunk"] is False

    # 夹爪闭合 + 末端移动 ⇒ 变化量可见
    moved = dict(observation)
    moved["state.gripper_qpos"] = [0.0, 0.0]
    moved["state.end_effector_position_relative"] = [0.15, 0.2, 0.3]
    second = pipeline._proprioceptive_evidence(state, moved)
    assert second is not None
    assert second["moved_since_last_chunk"] is True
    assert second["delta_since_last_chunk"]["state.gripper_qpos"] > 0.03

    # 观测里没有本体键时返回 None（不改老行为）
    assert pipeline._proprioceptive_evidence({}, {"video.x": "only image"}) is None


def test_episode_only_ends_when_the_environment_budget_is_used_up() -> None:
    """用户契约：环境提前 done（预算未用尽）不得终止 episode，应回 planner 重规划。"""

    pipeline = AtomicSpatialSkillExecutionPipeline(
        perception_clients=[],
        perception_required=False,
        atomic_recovery_enabled=False,
    )
    assert (
        pipeline._environment_budget_exhausted({"remaining_environment_steps": 120}, {})
        is False
    )
    assert (
        pipeline._environment_budget_exhausted({"remaining_environment_steps": 0}, {})
        is True
    )
    # 没有预算信息时保持旧行为（结束），避免无上限依据时无限循环
    assert pipeline._environment_budget_exhausted({}, {}) is True
