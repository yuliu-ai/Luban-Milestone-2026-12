"""Closure tests for the proposal-only self-evolution loop.

They cover the path that actually blocked the real Gemini 3.7 episode: an advisory
reasoner that returned empty provider content, which left the episode with zero
candidates and therefore nothing to validate, promote or retrieve.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from luban.agent_core.agents.self_evolving import SelfEvolvingAgent
from luban.agent_core.atomic_skill_system import AtomicSkillSystem
from luban.agent_core.evolution_candidates import extract_evolution_candidates
from luban.agent_core.memories import InMemoryMemory
from luban.agent_core.module_evolution import (
    ModuleEvolutionManager,
    ModuleEvolutionReasoner,
)
from luban.agent_core.planners.base import Planner
from luban.agent_core.verifiers.base import Verifier
from luban.backends.skills.base import SkillBackend
from luban.evals.evolution_validation import (
    ValidationIdentity,
    build_protocol_id,
    run_paired_validation,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOGS = [ROOT / "catalogs/atomic_skills.json", ROOT / "catalogs/composites.json"]

MODULE_VERSIONS = {
    "planner": "planner@v1",
    "verifier": "verifier@v1",
    "graph": "graph@v1",
    "atomic_skill": "atomic@v1",
}


class StubBackend:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": self.message}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}

    def close(self) -> None:
        return None


def execution(**overrides: Any) -> dict[str, Any]:
    record = {
        "execution_id": "exec-1",
        "attempt_id": "attempt-1",
        "attempt_count": 1,
        "skill": "Pick_Place",
        "skill_id": 7,
        "node_id": "pick_sponge",
        "subtask": "pick up the sponge",
        "grounded_arguments": {"object": "sponge"},
        "expected_outcome": "the sponge is held",
        "status": "completed",
        "chunk_count": 4,
        "failure_count": 0,
        "reason": "sponge is held",
        "confidence": 0.9,
        "evidence": [],
    }
    record.update(overrides)
    return record


def manager(tmp_path: Any, **overrides: Any) -> ModuleEvolutionManager:
    options: dict[str, Any] = {
        "base_policy": "xr1",
        "min_paired_samples": 2,
        "max_regressions": 0,
    }
    options.update(overrides)
    return ModuleEvolutionManager(tmp_path / "evolution.jsonl", **options)


def propose_planner_candidate(store: ModuleEvolutionManager) -> str:
    return store.propose(
        target="planner",
        operation="add_guideline",
        patch={"guideline": "re-ground the sponge before picking"},
        reason="repeated failure on pick_sponge",
        evidence_refs=("rule:repeated_node_failure", "execution:exec-1"),
        applicability=("ScrubCuttingBoard",),
        source_episode_id="episode-11",
        parent_version=MODULE_VERSIONS["planner"],
    ).proposal_id


# --- provider output parsing (the observed real failure) --------------------


def test_empty_provider_content_reports_the_real_cause() -> None:
    reasoner = ModuleEvolutionReasoner(StubBackend({"content": ""}))

    with pytest.raises(ValueError, match="empty content"):
        reasoner.propose({"task": "ScrubCuttingBoard"})


def test_thinking_gateway_answer_in_reasoning_content_is_recovered() -> None:
    payload = json.dumps({"candidates": [{"target": "planner"}]})
    reasoner = ModuleEvolutionReasoner(
        StubBackend({"content": "", "reasoning_content": f"thinking... {payload}"})
    )

    assert reasoner.propose({}) == [{"target": "planner"}]


def test_fenced_and_multipart_content_are_parsed() -> None:
    payload = json.dumps({"candidates": [{"target": "verifier"}]})
    fenced = ModuleEvolutionReasoner(
        StubBackend({"content": f"```json\n{payload}\n```"})
    )
    multipart = ModuleEvolutionReasoner(StubBackend({"content": [{"text": payload}]}))

    assert fenced.propose({}) == [{"target": "verifier"}]
    assert multipart.propose({}) == [{"target": "verifier"}]


def test_malformed_provider_envelope_is_reported_not_crashed() -> None:
    reasoner = ModuleEvolutionReasoner(StubBackend({"content": "not json"}))

    with pytest.raises(ValueError, match="invalid JSON"):
        reasoner.propose({})


def test_missing_message_envelope_and_null_content_are_reported() -> None:
    no_message = ModuleEvolutionReasoner(StubBackend({}))
    null_content = ModuleEvolutionReasoner(StubBackend({"content": None}))

    for reasoner in (no_message, null_content):
        with pytest.raises(ValueError, match="content"):
            reasoner.propose({})


def test_truncated_provider_json_is_reported_as_invalid_json() -> None:
    truncated = '{"candidates": [{"target": "plan'
    reasoner = ModuleEvolutionReasoner(StubBackend({"content": truncated}))

    with pytest.raises(ValueError, match="invalid JSON"):
        reasoner.propose({})


def test_multipart_content_with_empty_segments_still_parses_the_payload() -> None:
    payload = json.dumps({"candidates": [{"target": "graph"}]})
    reasoner = ModuleEvolutionReasoner(
        StubBackend({"content": [{"text": ""}, {"text": payload}, {}]})
    )

    assert reasoner.propose({}) == [{"target": "graph"}]


def test_candidates_that_are_not_a_list_are_rejected() -> None:
    reasoner = ModuleEvolutionReasoner(
        StubBackend({"content": json.dumps({"candidates": {"target": "planner"}})})
    )

    with pytest.raises(ValueError, match="candidates list"):
        reasoner.propose({})


# --- deterministic candidate extraction -------------------------------------


def test_clean_success_yields_no_candidates() -> None:
    candidates = extract_evolution_candidates(
        completed_executions=[execution()],
        failed_executions=[],
        result={"success": True, "termination_reason": "task_success"},
    )

    assert candidates == []


def test_repeated_node_failure_becomes_a_planner_candidate() -> None:
    failures = [
        execution(execution_id="exec-1", status="failed", reason="sponge not grasped"),
        execution(execution_id="exec-2", status="failed", reason="sponge slipped"),
    ]

    candidates = extract_evolution_candidates(failed_executions=failures)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["target"] == "planner"
    assert candidate["operation"] == "add_guideline"
    assert "pick_sponge" in candidate["patch"]["guideline"]
    assert "sponge not grasped" in candidate["patch"]["guideline"]
    assert candidate["evidence_refs"][0] == "rule:repeated_node_failure"
    assert "execution:exec-2" in candidate["evidence_refs"]


def test_single_failure_is_treated_as_normal_recovery() -> None:
    assert (
        extract_evolution_candidates(failed_executions=[execution(status="failed")])
        == []
    )


def test_chunk_overrun_becomes_a_planner_candidate() -> None:
    candidates = extract_evolution_candidates(
        completed_executions=[execution(chunk_count=83)]
    )

    assert [item["target"] for item in candidates] == ["planner"]
    assert "83 action chunks" in candidates[0]["patch"]["guideline"]


def test_low_confidence_completion_becomes_a_verifier_candidate() -> None:
    candidates = extract_evolution_candidates(
        completed_executions=[execution(confidence=0.2)]
    )

    assert [item["target"] for item in candidates] == ["verifier"]
    assert candidates[0]["operation"] == "add_predicate_rule"
    assert candidates[0]["patch"]["predicate"] == "node_completed::pick_sponge"


def test_infrastructure_failure_is_not_a_learning_signal() -> None:
    assert (
        extract_evolution_candidates(
            failed_executions=[
                execution(execution_id="a", status="failed"),
                execution(execution_id="b", status="failed"),
            ],
            result={"termination_reason": "infrastructure_error"},
        )
        == []
    )


def test_extracted_candidates_are_accepted_by_the_store(tmp_path: Any) -> None:
    store = manager(tmp_path)
    extracted = extract_evolution_candidates(
        failed_executions=[
            execution(execution_id="a", status="failed", reason="not grasped"),
            execution(execution_id="b", status="failed", reason="slipped"),
        ],
        completed_executions=[execution(confidence=0.1)],
    )

    for item in extracted:
        candidate = store.propose(
            target=item["target"],
            operation=item["operation"],
            patch=item["patch"],
            reason=item["reason"],
            evidence_refs=item["evidence_refs"],
            applicability=("ScrubCuttingBoard",),
            source_episode_id="episode-11",
            parent_version=MODULE_VERSIONS[item["target"]],
        )
        assert candidate.state == "candidate"
    assert len(extracted) == 2


# --- paired validation, promotion and quarantine ----------------------------


def identities(count: int) -> list[ValidationIdentity]:
    return [
        ValidationIdentity(
            task_name="ScrubCuttingBoard", episode_index=index, split="target"
        )
        for index in range(count)
    ]


def arm_outcomes(mapping: dict[tuple[int, str], str]):
    def run_episode(identity: ValidationIdentity, arm: str) -> dict[str, Any]:
        return {"outcome": mapping[(identity.episode_index, arm)]}

    return run_episode


def protocol() -> str:
    return build_protocol_id(
        "heldout", policy_version="xr1@abc", config_version="cfg@1", horizon=1200
    )


def test_improving_candidate_is_validated_and_becomes_retrievable(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)

    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )

    assert report["decision"]["decision"] == "validated"
    assert store.get(proposal_id).state == "validated"
    context = store.context("ScrubCuttingBoard", MODULE_VERSIONS)
    assert [item["proposal_id"] for item in context["planner"]] == [proposal_id]


def test_regressing_candidate_is_quarantined_and_hidden_from_the_planner(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)

    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "success",
                (0, "candidate"): "failure",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )

    assert report["decision"]["decision"] == "quarantined"
    assert "regression limit" in " ".join(report["decision"]["gate_failures"])
    assert store.get(proposal_id).state == "quarantined"
    context = store.context("ScrubCuttingBoard", MODULE_VERSIONS)
    assert context["planner"] == []


def test_insufficient_pairs_stay_shadow_as_pending_evidence(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path, min_paired_samples=5)
    proposal_id = propose_planner_candidate(store)

    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )

    # Insufficient evidence is not evidence of harm: the candidate stays in
    # shadow so new held-out identities can be added later.
    assert report["decision"]["decision"] == "pending_evidence"
    assert "insufficient paired samples" in " ".join(
        report["decision"]["gate_failures"]
    )
    assert store.get(proposal_id).state == "shadow"
    # Quarantine remains reserved for sufficient-evidence gate failures.
    assert "quarantined" not in report["decision"]["decision"]


def test_changing_horizon_mid_validation_fails_the_single_protocol_gate(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)
    always_better = arm_outcomes(
        {
            (0, "baseline"): "failure",
            (0, "candidate"): "success",
            (1, "baseline"): "failure",
            (1, "candidate"): "success",
        }
    )

    run_paired_validation(
        store,
        proposal_id,
        identities=identities(1),
        run_episode=always_better,
        protocol_id=protocol(),
        settle=False,
    )
    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(2)[1:],
        run_episode=always_better,
        protocol_id=build_protocol_id(
            "heldout", policy_version="xr1@abc", config_version="cfg@1", horizon=2400
        ),
    )

    assert report["decision"]["decision"] == "quarantined"
    assert "exactly one protocol" in " ".join(report["decision"]["gate_failures"])


def test_duplicate_identity_is_refused(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)
    duplicate = identities(1) * 2

    with pytest.raises(ValueError, match="must be unique"):
        run_paired_validation(
            store,
            proposal_id,
            identities=duplicate,
            run_episode=arm_outcomes(
                {(0, "baseline"): "failure", (0, "candidate"): "success"}
            ),
            protocol_id=protocol(),
        )


def test_deferred_pairs_are_excluded_instead_of_counted(tmp_path: Any) -> None:
    store = manager(tmp_path, min_paired_samples=1)
    proposal_id = propose_planner_candidate(store)

    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "infrastructure_error",
                (1, "candidate"): "deferred",
            }
        ),
        protocol_id=protocol(),
    )

    assert report["paired_summary"]["eligible_pairs"] == 1
    assert report["paired_summary"]["excluded_rows"] == 1
    assert report["decision"]["decision"] == "validated"


def test_promotion_stays_off_by_default_and_needs_an_explicit_switch(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)
    run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )

    assert store.proposal_only is True
    with pytest.raises(RuntimeError, match="proposal_only"):
        store.promote(proposal_id, approved_by="operator")

    opened = ModuleEvolutionManager(
        tmp_path / "evolution.jsonl", min_paired_samples=2, proposal_only=False
    )
    assert opened.promote(proposal_id, approved_by="operator").state == "promoted"


def test_validated_candidates_are_advisory_only_and_never_edit_the_graph(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = store.propose(
        target="graph",
        operation="add_dependency",
        patch={"source": "rinse", "target": "scrub"},
        reason="observed ordering failure",
        evidence_refs=("execution:exec-1",),
        applicability=("ScrubCuttingBoard",),
        source_episode_id="episode-11",
        parent_version=MODULE_VERSIONS["graph"],
    ).proposal_id
    run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )
    graph = {
        "nodes": [
            {"id": "scrub", "dependencies": []},
            {"id": "rinse", "dependencies": []},
        ]
    }

    assert store.get(proposal_id).state == "validated"
    applied = store.apply_graph_candidates(
        graph, "ScrubCuttingBoard", parent_version=MODULE_VERSIONS["graph"]
    )
    assert "evolution_provenance" not in applied
    assert applied["nodes"][0]["dependencies"] == []


def test_evolution_trace_records_identity_metrics_and_decision(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)
    run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )

    trace = store.evolution_trace(
        proposal_id, memory_attribution={"status": "correlated_only"}
    )

    assert trace["candidate_id"] == proposal_id
    assert trace["source_episode"] == "episode-11"
    assert trace["validation_identities"] == [
        "target/ScrubCuttingBoard/0000",
        "target/ScrubCuttingBoard/0001",
    ]
    assert trace["protocol_ids"] == [protocol()]
    assert trace["before_after_metrics"]["baseline_successes"] == 0
    assert trace["before_after_metrics"]["candidate_successes"] == 2
    assert trace["before_after_metrics"]["regressions"] == 0
    assert trace["promotion_decision"]["decision"] == "validated"
    assert trace["retrievable_by_planner"] is True
    assert trace["memory_attribution"] == {"status": "correlated_only"}


def test_candidate_store_is_append_only(tmp_path: Any) -> None:
    path = tmp_path / "evolution.jsonl"
    store = ModuleEvolutionManager(path, min_paired_samples=1)
    proposal_id = propose_planner_candidate(store)
    store.start_shadow(proposal_id)
    store.quarantine(proposal_id, reason="manual")

    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
    assert events == ["candidate", "candidate", "candidate"]
    assert store.get(proposal_id).state == "quarantined"
    assert store.get(proposal_id).revision == 2


# --- regression against the real ScrubCuttingBoard/0011 episode -------------

# Copied from runs/self_evolving_xr1_atomic_single_episode_gemini37_compatible2.
# That episode succeeded but recorded zero candidates because both LLM proposal
# hooks failed; the deterministic rules must still find the 83-chunk overrun.
REAL_COMPLETED_EXECUTION = {
    "execution_id": (
        "robocasa-target-ScrubCuttingBoard-0011-attempt-"
        "d212692f1f574c11ad91380474e14017:1"
    ),
    "attempt_count": 1,
    "skill": "Pick_Place",
    "skill_id": 7,
    "node_id": "scrub_cutting_board",
    "subtask": (
        "Grasp the sponge from the counter, scrub across multiple points on the "
        "cutting board, and release the sponge."
    ),
    "grounded_arguments": {"object": "sponge", "target": "cutting board"},
    "expected_outcome": (
        "The sponge contacted multiple separated points across the cutting board "
        "and is released."
    ),
    "status": "completed",
    "chunk_count": 83,
    "failure_count": 0,
    "reason": "task_success",
    "confidence": 1.0,
    "evidence": ["environment success=true"],
}
REAL_RESULT = {
    "success": True,
    "termination_reason": "task_success",
    "executed_action_chunks": 83,
    "planner_calls": 1,
    "verifier_calls": 2,
    "replans": 0,
    "environment_steps": 1316,
}


def test_real_successful_episode_still_yields_one_attributable_candidate() -> None:
    candidates = extract_evolution_candidates(
        completed_executions=[REAL_COMPLETED_EXECUTION],
        failed_executions=[],
        result=REAL_RESULT,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["target"] == "planner"
    assert candidate["evidence_refs"][0] == "rule:chunk_overrun"
    assert "scrub_cutting_board" in candidate["reason"]
    assert "83" in candidate["reason"]


def test_real_episode_candidate_is_accepted_but_not_validated(tmp_path: Any) -> None:
    store = manager(tmp_path)
    item = extract_evolution_candidates(
        completed_executions=[REAL_COMPLETED_EXECUTION], result=REAL_RESULT
    )[0]

    candidate = store.propose(
        target=item["target"],
        operation=item["operation"],
        patch=item["patch"],
        reason=item["reason"],
        evidence_refs=item["evidence_refs"],
        applicability=("ScrubCuttingBoard",),
        source_episode_id="robocasa-target-ScrubCuttingBoard-0011",
        parent_version=MODULE_VERSIONS["planner"],
    )

    # Execution success alone must not make it retrievable.
    assert candidate.state == "candidate"
    assert store.context("ScrubCuttingBoard", MODULE_VERSIONS)["planner"] == []


# --- closure: does episode N actually change episode N+1? -------------------


class StubPlanner(Planner):
    supports_high_level_skills = True

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(inputs)
        return {"skill": "Pick_Place", "subtask": "pick", "expected_outcome": "held"}


class StubVerifier(Verifier):
    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"execution_status": "in_progress"}


class StubSkillBackend(SkillBackend):
    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"action": 0}


def build_agent(store: ModuleEvolutionManager) -> tuple[SelfEvolvingAgent, StubPlanner]:
    planner = StubPlanner()
    agent = SelfEvolvingAgent(
        planner=planner,
        verifier=StubVerifier(),
        memory=InMemoryMemory(),
        skill_backend=StubSkillBackend(),
        atomic_skill_system=AtomicSkillSystem(
            CATALOGS, base_policy="xr1", embodiment_profile="mobile_manipulator"
        ),
        module_evolution_manager=store,
        module_versions=MODULE_VERSIONS,
    )
    return agent, planner


def validated_candidate_from_a_failed_episode(store: ModuleEvolutionManager) -> str:
    extracted = extract_evolution_candidates(
        failed_executions=[
            execution(execution_id="a", status="failed", reason="sponge not grasped"),
            execution(execution_id="b", status="failed", reason="sponge slipped"),
        ]
    )
    item = extracted[0]
    proposal_id = store.propose(
        target=item["target"],
        operation=item["operation"],
        patch=item["patch"],
        reason=item["reason"],
        evidence_refs=item["evidence_refs"],
        applicability=("DeliverStraw",),
        source_episode_id="episode-n",
        parent_version=MODULE_VERSIONS[item["target"]],
    ).proposal_id
    run_paired_validation(
        store,
        proposal_id,
        identities=[
            ValidationIdentity(
                task_name="DeliverStraw", episode_index=i, split="target"
            )
            for i in range(2)
        ],
        run_episode=lambda identity, arm: {
            "outcome": "success" if arm == "candidate" else "failure"
        },
        protocol_id=protocol(),
    )
    return proposal_id


def test_validated_candidate_reaches_the_next_episode_planner(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = validated_candidate_from_a_failed_episode(store)
    assert store.get(proposal_id).state == "validated"

    agent, planner = build_agent(store)
    begin = agent.begin_episode(
        {"task": {"name": "DeliverStraw"}, "initial_observation": {}}
    )
    agent.plan({"task": {"name": "DeliverStraw"}, "available_skills": ["Pick_Place"]})

    assert begin["module_evolution"]["proposal_ids"]["planner"] == [proposal_id]
    experiences = planner.inputs[-1]["high_level_skill_context"]["experiences"]
    entry = next(item for item in experiences if proposal_id in item["name"])
    assert "validated" in entry["instructions"]
    assert "pick_sponge" in entry["instructions"]


def test_quarantined_candidate_never_reaches_the_next_episode_planner(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = validated_candidate_from_a_failed_episode(store)
    store.quarantine(proposal_id, reason="regression observed later")

    agent, planner = build_agent(store)
    begin = agent.begin_episode(
        {"task": {"name": "DeliverStraw"}, "initial_observation": {}}
    )
    agent.plan({"task": {"name": "DeliverStraw"}, "available_skills": ["Pick_Place"]})

    assert begin["module_evolution"]["proposal_ids"]["planner"] == []
    experiences = (
        planner.inputs[-1].get("high_level_skill_context", {}).get("experiences", [])
    )
    assert all(proposal_id not in item["name"] for item in experiences)


def test_unvalidated_candidate_never_reaches_the_next_episode_planner(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)
    assert store.get(proposal_id).state == "candidate"

    agent, planner = build_agent(store)
    begin = agent.begin_episode(
        {"task": {"name": "ScrubCuttingBoard"}, "initial_observation": {}}
    )
    agent.plan({"task": {"name": "ScrubCuttingBoard"}})

    assert begin["module_evolution"]["proposal_ids"]["planner"] == []
    experiences = (
        planner.inputs[-1].get("high_level_skill_context", {}).get("experiences", [])
    )
    assert all(proposal_id not in item["name"] for item in experiences)


# --- production state admission per target (real Agent + real Verifier) -----
#
# SubtaskVerifier 的生产准入只接受 promoted 候选（见 verifiers/subtask.py 的
# verifier_evolution_context 契约）。Agent 的生产检索因此按 target 区分：
# planner/graph 沿用 advisory_states，verifier 固定 ("promoted",)。隔离实验
# 臂的候选注入与该生产检索无关，由 run_paired_validation 显式记录。


class FakeVerifierLLMBackend:
    """OpenAI-compatible-shaped backend for the real SubtaskVerifier."""

    def __init__(self, content: str) -> None:
        self.content = content

    def complete(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}

    def healthcheck(self) -> dict[str, Any]:
        return {"healthy": True}

    def close(self) -> None:
        return None


def verifier_inputs() -> dict[str, Any]:
    from PIL import Image

    return {
        "task": "scrub the cutting board",
        "observation": {"head_rgb": Image.new("RGB", (2, 2), "red")},
        "active_execution": {
            "skill": "Pick_Place",
            "subtask": "pick up the sponge",
            "grounded_arguments": {"object": "sponge"},
            "expected_outcome": "the sponge is held",
            "chunk_count": 1,
        },
        "environment_result": {
            "observation": {"head_rgb": Image.new("RGB", (2, 2), "blue")},
            "task_success": False,
            "task_progress": 0.0,
            "last_action_success": True,
            "done": False,
        },
    }


def validated_verifier_candidate(store: ModuleEvolutionManager) -> str:
    proposal_id = store.propose(
        target="verifier",
        operation="add_predicate_rule",
        patch={
            "predicate": "the sponge stays visible in both camera views",
            "rule": "report uncertain unless the sponge is visible before and after",
        },
        reason="completed without observable visual evidence",
        evidence_refs=("rule:low_confidence_completion", "execution:exec-1"),
        applicability=("ScrubCuttingBoard",),
        source_episode_id="episode-11",
        parent_version=MODULE_VERSIONS["verifier"],
    ).proposal_id
    run_paired_validation(
        store,
        proposal_id,
        identities=identities(2),
        run_episode=arm_outcomes(
            {
                (0, "baseline"): "failure",
                (0, "candidate"): "success",
                (1, "baseline"): "failure",
                (1, "candidate"): "success",
            }
        ),
        protocol_id=protocol(),
    )
    return proposal_id


def test_validated_verifier_candidate_stays_out_of_production_verifier(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path)
    proposal_id = validated_verifier_candidate(store)
    assert store.get(proposal_id).state == "validated"

    from luban.agent_core import SubtaskVerifier

    backend = FakeVerifierLLMBackend(
        json.dumps(
            {
                "execution_status": "in_progress",
                "reason": "the sponge is not yet lifted",
                "confidence": 0.5,
                "evidence": ["the sponge is still on the cutting board"],
            }
        )
    )
    agent = SelfEvolvingAgent(
        planner=StubPlanner(),
        verifier=SubtaskVerifier(backend),
        memory=InMemoryMemory(),
        skill_backend=StubSkillBackend(),
        atomic_skill_system=AtomicSkillSystem(
            CATALOGS, base_policy="xr1", embodiment_profile="mobile_manipulator"
        ),
        module_evolution_manager=store,
        module_versions=MODULE_VERSIONS,
    )
    begin = agent.begin_episode(
        {"task": {"name": "ScrubCuttingBoard"}, "initial_observation": {}}
    )

    # 生产检索：verifier 臂只收 promoted，validated 候选留在 store 中。
    assert agent.active_module_evolution["verifier"] == ()
    states_by_target = begin["module_evolution"]["retrieval_states_by_target"]
    assert states_by_target["verifier"] == ["promoted"]
    assert set(states_by_target["planner"]) == {"validated", "promoted"}

    # 真实 SubtaskVerifier 不再因为 validated 候选抛 VerifierOutputError。
    output = agent.verify(verifier_inputs())
    assert output["execution_status"] == "in_progress"


def test_promoted_verifier_candidate_reaches_the_real_subtask_verifier(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path, proposal_only=False)
    proposal_id = validated_verifier_candidate(store)
    store.promote(proposal_id, approved_by="state-admission-regression-test")

    from luban.agent_core import SubtaskVerifier

    backend = FakeVerifierLLMBackend(
        json.dumps(
            {
                "execution_status": "in_progress",
                "reason": "the sponge is not yet lifted",
                "confidence": 0.5,
                "evidence": ["the sponge is still on the cutting board"],
            }
        )
    )
    agent = SelfEvolvingAgent(
        planner=StubPlanner(),
        verifier=SubtaskVerifier(backend),
        memory=InMemoryMemory(),
        skill_backend=StubSkillBackend(),
        atomic_skill_system=AtomicSkillSystem(
            CATALOGS, base_policy="xr1", embodiment_profile="mobile_manipulator"
        ),
        module_evolution_manager=store,
        module_versions=MODULE_VERSIONS,
    )
    agent.begin_episode(
        {"task": {"name": "ScrubCuttingBoard"}, "initial_observation": {}}
    )

    assert [
        candidate.proposal_id for candidate in agent.active_module_evolution["verifier"]
    ] == [proposal_id]
    output = agent.verify(verifier_inputs())
    assert output["execution_status"] == "in_progress"


# --- isolated experiment injection stays separate from production retrieval --


def test_source_identity_overlap_is_rejected(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)

    with pytest.raises(ValueError, match="overlap"):
        run_paired_validation(
            store,
            proposal_id,
            identities=identities(2),
            run_episode=arm_outcomes({}),
            protocol_id=protocol(),
            source_identities=["target/ScrubCuttingBoard/0000"],
        )

    # 重叠在启动 shadow 之前被拒绝，store 保持 untouched。
    assert store.get(proposal_id).state == "candidate"


def test_arm_conditions_shared_fields_must_match(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)

    def run_episode(identity: Any, arm: str) -> dict[str, Any]:
        return {
            "outcome": "success",
            "evidence_refs": [f"runs/{arm}"],
            "arm_conditions": {
                "base_config_hash": "cfg-a" if arm == "baseline" else "cfg-b",
                "seed": "seed-7",
                "effective_config_hash": f"eff-{arm}",
            },
        }

    with pytest.raises(ValueError, match="diverged"):
        run_paired_validation(
            store,
            proposal_id,
            identities=identities(1),
            run_episode=run_episode,
            protocol_id=protocol(),
        )


def test_arm_conditions_require_evidence_refs(tmp_path: Any) -> None:
    store = manager(tmp_path)
    proposal_id = propose_planner_candidate(store)

    def run_episode(identity: Any, arm: str) -> dict[str, Any]:
        return {
            "outcome": "success",
            "arm_conditions": {"base_config_hash": "cfg-a", "seed": "seed-7"},
        }

    with pytest.raises(ValueError, match="evidence_refs"):
        run_paired_validation(
            store,
            proposal_id,
            identities=identities(1),
            run_episode=run_episode,
            protocol_id=protocol(),
        )


def test_arm_conditions_and_injection_are_recorded_in_pairs(
    tmp_path: Any,
) -> None:
    store = manager(tmp_path, min_paired_samples=1)
    proposal_id = propose_planner_candidate(store)

    def run_episode(identity: Any, arm: str) -> dict[str, Any]:
        return {
            "outcome": "success" if arm == "candidate" else "failure",
            "evidence_refs": [f"runs/paired/{identity.canonical_identity}/{arm}"],
            "arm_conditions": {
                "base_config_hash": "cfg-base-20260910",
                "seed": "seed-1318",
                "effective_config_hash": (
                    "eff-base" if arm == "baseline" else "eff-candidate"
                ),
                "injected_candidate_ids": [] if arm == "baseline" else [proposal_id],
            },
        }

    report = run_paired_validation(
        store,
        proposal_id,
        identities=identities(1),
        run_episode=run_episode,
        protocol_id=protocol(),
    )

    pair = report["pairs"][0]
    assert pair["arm_conditions"]["baseline"]["base_config_hash"] == "cfg-base-20260910"
    assert pair["arm_conditions"]["candidate"]["injected_candidate_ids"] == [
        proposal_id
    ]
    assert pair["arm_conditions"]["baseline"]["injected_candidate_ids"] == []
    assert report["decision"]["decision"] == "validated"
