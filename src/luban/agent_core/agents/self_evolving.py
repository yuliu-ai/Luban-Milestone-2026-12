import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

from luban.agent_core.agents.base import BaseAgent
from luban.agent_core.atomic_skill_system import AtomicSkillSystem
from luban.agent_core.delegation import (
    DelegationRequest,
    DelegationResult,
    SubAgentScheduler,
)
from luban.agent_core.evolution_candidates import (
    extract_evolution_candidates,
)
from luban.agent_core.experience_capsules import (
    ExperienceCapsule,
    ExperienceCapsuleStore,
)
from luban.agent_core.high_skills import (
    HighLevelSkill,
    HighSkillReasoner,
    SkillManager,
    SkillSelection,
    SkillUpdateProposal,
    build_high_level_skill_context,
)
from luban.agent_core.high_skills.episode import (
    apply_skill_updates,
    build_episode_update_input,
    collect_reflection_images,
    high_skill_artifact_dir,
    write_json_artifact,
)
from luban.agent_core.knowledge import KnowledgeBase, KnowledgeEntry
from luban.agent_core.memories.base import Memory
from luban.agent_core.module_evolution import (
    EVOLUTION_TARGETS,
    ModuleEvolutionCandidate,
    ModuleEvolutionManager,
    ModuleEvolutionReasoner,
)
from luban.agent_core.planners.base import Planner
from luban.agent_core.streaming_memory import StreamingMemorySidecar
from luban.agent_core.task_decomposer import TaskDecomposer
from luban.agent_core.verifiers.base import Verifier
from luban.backends.skills.base import SkillBackend
from luban.backends.world_models.base import WorldModelBackend


class SelfEvolvingAgent(BaseAgent):
    """An isolated DefaultAgent copy with proposal-first XR-1 evolution.

    The original ``DefaultAgent`` remains untouched.  This copy adds the
    versioned 64-skill catalog and four independently attributed evolution
    targets while preserving the existing Agent API.
    """

    def __init__(
        self,
        planner: Planner,
        verifier: Verifier | None,
        memory: Memory,
        skill_backend: SkillBackend,
        world_model: WorldModelBackend | None = None,
        task_decomposer: TaskDecomposer | None = None,
        skill_manager: SkillManager | None = None,
        high_skill_reasoner: HighSkillReasoner | None = None,
        high_skill_write_mode: str = "propose",
        experience_capsule_store: ExperienceCapsuleStore | None = None,
        experience_context: Mapping[str, Any] | None = None,
        max_validated_experiences: int = 4,
        atomic_skill_system: AtomicSkillSystem | None = None,
        module_evolution_manager: ModuleEvolutionManager | None = None,
        module_evolution_reasoner: ModuleEvolutionReasoner | None = None,
        module_versions: Mapping[str, str] | None = None,
        subagent_scheduler: SubAgentScheduler | None = None,
        knowledge_base: KnowledgeBase | None = None,
        robot_profile: str = "xr1_mobile_manipulator",
        robot_capabilities: list[str] | tuple[str, ...] | None = None,
        knowledge_limit: int = 6,
        streaming_memory_sidecar: StreamingMemorySidecar | None = None,
    ) -> None:
        if (skill_manager is None) != (high_skill_reasoner is None):
            raise ValueError(
                "skill_manager and high_skill_reasoner must be configured together"
            )
        if high_skill_write_mode not in {"propose", "apply"}:
            raise ValueError("high_skill_write_mode must be 'propose' or 'apply'")
        if (experience_capsule_store is None) != (experience_context is None):
            raise ValueError(
                "experience_capsule_store and experience_context must be "
                "configured together"
            )
        if experience_context is not None and not isinstance(
            experience_context, Mapping
        ):
            raise TypeError("experience_context must be a mapping")
        if experience_capsule_store is not None and not isinstance(
            experience_capsule_store, ExperienceCapsuleStore
        ):
            raise TypeError(
                "experience_capsule_store must be an ExperienceCapsuleStore"
            )
        if experience_capsule_store is not None and high_skill_write_mode != "propose":
            raise ValueError(
                "Experience Capsules require high_skill_write_mode='propose'"
            )
        if (
            not isinstance(max_validated_experiences, int)
            or isinstance(max_validated_experiences, bool)
            or max_validated_experiences <= 0
        ):
            raise ValueError("max_validated_experiences must be positive")
        if atomic_skill_system is not None and not isinstance(
            atomic_skill_system, AtomicSkillSystem
        ):
            raise TypeError("atomic_skill_system must be an AtomicSkillSystem")
        if module_evolution_manager is not None and not isinstance(
            module_evolution_manager, ModuleEvolutionManager
        ):
            raise TypeError("module_evolution_manager must be a ModuleEvolutionManager")
        if module_evolution_reasoner is not None and module_evolution_manager is None:
            raise ValueError(
                "module_evolution_reasoner requires module_evolution_manager"
            )
        if module_evolution_manager is not None and atomic_skill_system is None:
            raise ValueError(
                "module evolution requires atomic_skill_system for atomic-skill policy"
            )
        if module_evolution_manager is not None and high_skill_write_mode != "propose":
            raise ValueError(
                "module evolution requires high_skill_write_mode='propose'"
            )
        base_policy_ids = {
            value
            for value in (
                atomic_skill_system.base_policy
                if atomic_skill_system is not None
                else None,
                module_evolution_manager.base_policy
                if module_evolution_manager is not None
                else None,
                experience_context.get("base_policy")
                if experience_context is not None
                else None,
            )
            if value is not None
        }
        if len(base_policy_ids) > 1:
            raise ValueError(
                "atomic skills, module evolution, and experience capsules must "
                "use one base_policy identity"
            )
        resolved_versions = {
            "planner": "planner-v1",
            "verifier": "verifier-v1",
            "graph": "graph-v1",
            "atomic_skill": "atomic-catalog-v1",
        }
        if module_versions is not None:
            if not isinstance(module_versions, Mapping):
                raise TypeError("module_versions must be a mapping")
            unknown_versions = sorted(set(module_versions) - EVOLUTION_TARGETS)
            if unknown_versions:
                raise ValueError(
                    f"module_versions has unknown targets: {unknown_versions}"
                )
            for target, version in module_versions.items():
                if not isinstance(version, str) or not version.strip():
                    raise ValueError(
                        f"module_versions.{target} must be a non-empty string"
                    )
                resolved_versions[target] = version.strip()
        super().__init__(planner, verifier, memory, skill_backend, world_model)
        self.task_decomposer = task_decomposer
        self.episode_task_graph: dict[str, Any] | None = None
        self.skill_manager = skill_manager
        self.high_skill_reasoner = high_skill_reasoner
        self.high_skill_write_mode = high_skill_write_mode
        self.active_high_level_skills: tuple[HighLevelSkill, ...] = ()
        self.high_skill_selection: SkillSelection | None = None
        self.high_skill_update_proposals: tuple[SkillUpdateProposal, ...] = ()
        self._initial_high_skill_images: list[Any] = []
        self.experience_capsule_store = experience_capsule_store
        self.experience_context = (
            dict(experience_context) if experience_context is not None else None
        )
        self.max_validated_experiences = max_validated_experiences
        self.validated_experience_capsules: tuple[ExperienceCapsule, ...] = ()
        self.shadow_experience_capsules: tuple[ExperienceCapsule, ...] = ()
        self.atomic_skill_system = atomic_skill_system
        self.module_evolution_manager = module_evolution_manager
        self.module_evolution_reasoner = module_evolution_reasoner
        self.module_versions = resolved_versions
        if subagent_scheduler is not None and not isinstance(
            subagent_scheduler, SubAgentScheduler
        ):
            raise TypeError("subagent_scheduler must be a SubAgentScheduler")
        if knowledge_base is not None and not isinstance(knowledge_base, KnowledgeBase):
            raise TypeError("knowledge_base must implement KnowledgeBase")
        if not isinstance(robot_profile, str) or not robot_profile.strip():
            raise ValueError("robot_profile must be a non-empty string")
        if robot_capabilities is None:
            resolved_capabilities: tuple[str, ...] = ()
        elif not isinstance(robot_capabilities, (list, tuple)):
            raise TypeError("robot_capabilities must be a list or tuple")
        else:
            resolved_capabilities = _normalize_texts(
                "robot_capabilities", robot_capabilities
            )
        if (
            not isinstance(knowledge_limit, int)
            or isinstance(knowledge_limit, bool)
            or knowledge_limit <= 0
        ):
            raise ValueError("knowledge_limit must be positive")
        self.subagent_scheduler = subagent_scheduler
        self.knowledge_base = knowledge_base
        self.robot_profile = robot_profile.strip()
        self.robot_capabilities = resolved_capabilities
        self.knowledge_limit = knowledge_limit
        if streaming_memory_sidecar is not None and not isinstance(
            streaming_memory_sidecar, StreamingMemorySidecar
        ):
            raise TypeError("streaming_memory_sidecar must be a StreamingMemorySidecar")
        self.streaming_memory_sidecar = streaming_memory_sidecar
        self._stream_scope: dict[str, Any] = {}
        self.retrieved_knowledge: tuple[KnowledgeEntry, ...] = ()
        self.knowledge_retrieval_audit: dict[str, Any] = {}
        self.knowledge_query: dict[str, Any] = {}
        self.last_knowledge_context: dict[str, Any] = {}
        self.last_streaming_packet: dict[str, Any] | None = None
        self.streaming_packets_in_planner_input = 0
        self.active_module_evolution: dict[
            str, tuple[ModuleEvolutionCandidate, ...]
        ] = {target: () for target in EVOLUTION_TARGETS}
        self.module_evolution_proposals: tuple[ModuleEvolutionCandidate, ...] = ()
        if self.module_evolution_manager is not None:
            self.module_evolution_manager.bind_atomic_skill_system(
                self.atomic_skill_system
            )

    def plan(self, inputs: dict[str, Any]) -> Any:
        self._refresh_retrieval(inputs)
        knowledge_context = self._knowledge_context()
        packet = self._poll_streaming(inputs, boundary="planner")
        if packet is not None:
            knowledge_context["streaming_evidence"] = packet
            self.streaming_packets_in_planner_input += 1
        inputs["knowledge_context"] = knowledge_context
        self.last_knowledge_context = deepcopy(knowledge_context)
        planner_inputs = dict(inputs)
        should_delegate = (
            self.subagent_scheduler is not None
            and self.subagent_scheduler.should_delegate(planner_inputs)
        )
        if should_delegate:
            planner_inputs["subagent_result"] = self.delegate_to_subagent(
                planner_inputs
            ).to_dict()
        if self.episode_task_graph is not None and "task_graph" not in planner_inputs:
            planner_inputs["task_graph"] = self.episode_task_graph
        high_skill_context = build_high_level_skill_context(
            self.active_high_level_skills
        )
        high_skill_context["experiences"].extend(
            capsule.planner_context() for capsule in self.validated_experience_capsules
        )
        high_skill_context["experiences"].extend(
            capsule.planner_context() for capsule in self.shadow_experience_capsules
        )
        if self.atomic_skill_system is not None:
            planner_inputs["atomic_skill_context"] = (
                self.atomic_skill_system.planner_context()
            )
            high_skill_context["procedures"].append(
                self.atomic_skill_system.high_level_context_entry()
            )
        for candidate in self.active_module_evolution["planner"]:
            high_skill_context["experiences"].append(
                _module_candidate_context_entry(candidate)
            )
        for candidate in self.active_module_evolution["graph"]:
            high_skill_context["procedures"].append(
                _module_candidate_context_entry(candidate)
            )
        if high_skill_context["procedures"] or high_skill_context["experiences"]:
            planner_inputs["high_level_skill_context"] = high_skill_context
        return self.planner.plan(planner_inputs)

    def _poll_streaming(
        self,
        inputs: Mapping[str, Any],
        *,
        boundary: str = "planner",
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Poll and schedule one bounded sidecar request for the current context.

        The boundary keeps the planner and verifier prefetch slots apart, and the
        explicit ``query`` keeps the verifier's failure-conditioned query from
        invalidating the planner's packet.
        """

        if self.streaming_memory_sidecar is None:
            return None
        camera_views = _current_camera_views(inputs.get("observation"), self.planner)
        node = (
            inputs.get("assigned_task_graph_node")
            if isinstance(inputs.get("assigned_task_graph_node"), Mapping)
            else None
        )
        if node is None:
            state = inputs.get("state")
            graph = state.get("task_graph") if isinstance(state, Mapping) else None
            active = (
                state.get("task_graph_active_node_id")
                if isinstance(state, Mapping)
                else None
            )
            if isinstance(graph, Mapping) and isinstance(graph.get("nodes"), list):
                for candidate in graph["nodes"]:
                    if isinstance(candidate, Mapping) and candidate.get("id") == active:
                        node = candidate
                        break
        env_step = inputs.get("env_step")
        active_query = dict(query) if query is not None else self.knowledge_query
        packet = self.streaming_memory_sidecar.poll_ready(
            scope=self._stream_scope,
            query=active_query,
            camera_views=camera_views,
            env_step=env_step if isinstance(env_step, int) else None,
            node=node,
            boundary=boundary,
        )
        if packet is not None:
            self.last_streaming_packet = deepcopy(packet)
        self.streaming_memory_sidecar.request_prefetch(
            active_query,
            camera_views=camera_views,
            node=node,
            env_step=env_step if isinstance(env_step, int) else None,
            boundary=boundary,
        )
        return packet

    def _decomposer_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Offer the 64-item atomic skill directory as the Decomposer's candidate set.

        The Decomposer selects each node's ``atomic_skill_ids`` from this list, so a
        node carries only the chosen ids instead of the full catalog.
        """

        resolved = dict(context)
        if self.atomic_skill_system is not None:
            resolved["atomic_skill_directory"] = (
                self.atomic_skill_system.compact_catalog()
            )
        return resolved

    def _refresh_retrieval(
        self, inputs: Mapping[str, Any], *, store: bool = True
    ) -> dict[str, Any]:
        query = _task_context_query(inputs.get("task"))
        node = inputs.get("assigned_task_graph_node")
        if isinstance(node, Mapping):
            for field_name in ("id", "atomic_skill_id", "macro_skill"):
                value = node.get(field_name)
                if isinstance(value, str) and value.strip():
                    query_key = {
                        "id": "node_id",
                        "atomic_skill_id": "atomic_skill_id",
                        "macro_skill": "macro_skill",
                    }[field_name]
                    if query_key == "atomic_skill_id":
                        query[query_key] = _atomic_skill_query_values(
                            self.task_decomposer, value.strip()
                        )
                    else:
                        query[query_key] = value.strip()
            directory_ids = node.get("atomic_skill_ids")
            if isinstance(directory_ids, list):
                chosen = [
                    item.strip()
                    for item in directory_ids
                    if isinstance(item, str) and item.strip()
                ]
                if chosen:
                    query["atomic_skill_ids"] = chosen
        for field_name in ("macro_skill", "atomic_skill_id"):
            value = inputs.get(field_name)
            if isinstance(value, str) and value.strip():
                if field_name == "atomic_skill_id":
                    query[field_name] = _atomic_skill_query_values(
                        self.task_decomposer, value.strip()
                    )
                else:
                    query[field_name] = value.strip()
        for source in (
            inputs.get("verifier_result"),
            inputs.get("recovery_context"),
        ):
            if not isinstance(source, Mapping):
                continue
            for field_name in ("failure_category", "failure_class"):
                value = source.get(field_name)
                if isinstance(value, str) and value.strip():
                    query["failure_category"] = value.strip()
                    break
        query["robot_profile"] = self.robot_profile
        # 能力词汇对齐（方案 A）：查询以"正要执行该技能"的身份携带其契约能力词，
        # 契约衍生知识才可被检索；无关本体的条目仍被子集闸门过滤。sidecar 与
        # 规划器共用同一查询对象，这里一处注入两条检索路径同时生效。
        capabilities = list(self.robot_capabilities)
        node_skill_names: list[str] = []
        if isinstance(node, Mapping):
            directory_ids = node.get("atomic_skill_ids")
            if isinstance(directory_ids, list):
                node_skill_names.extend(
                    item.strip()
                    for item in directory_ids
                    if isinstance(item, str) and item.strip()
                )
            value = node.get("atomic_skill_id")
            if isinstance(value, str) and value.strip():
                node_skill_names.append(value.strip())
        if not node_skill_names:
            value = inputs.get("atomic_skill_id")
            if isinstance(value, str) and value.strip():
                node_skill_names.append(value.strip())
        if node_skill_names and self.atomic_skill_system is not None:
            for skill_name in node_skill_names:
                for word in self.atomic_skill_system.skill_required_capabilities(
                    skill_name
                ):
                    if word not in capabilities:
                        capabilities.append(word)
        query["capabilities"] = capabilities
        query["cross_robot"] = bool(
            query.get("cross_robot") is True
            or inputs.get("cross_robot_transfer")
            or inputs.get("cross_robot")
        )
        if store:
            self.knowledge_query = query
            if self.knowledge_base is not None:
                self.retrieved_knowledge = tuple(
                    self.knowledge_base.retrieve(query, limit=self.knowledge_limit)
                )
                self.knowledge_retrieval_audit = getattr(
                    self.knowledge_base, "last_retrieval", {}
                )
            if self.experience_capsule_store is not None:
                self.validated_experience_capsules = tuple(
                    capsule
                    for capsule in self._retrieve_experiences(
                        query, limit=self.max_validated_experiences
                    )
                    if capsule.skill_type == "spatial_reasoning"
                )
        return query

    def _retrieve_experiences(
        self, query: Mapping[str, Any], *, limit: int
    ) -> list[ExperienceCapsule]:
        if self.experience_capsule_store is None:
            return []
        contextual = getattr(
            self.experience_capsule_store, "retrieve_for_context", None
        )
        if callable(contextual):
            result = contextual(query, limit=limit)
            if not isinstance(result, list) or any(
                not isinstance(item, ExperienceCapsule) for item in result
            ):
                raise TypeError(
                    "ExperienceCapsuleStore contextual retrieval must return "
                    "a list of ExperienceCapsule"
                )
            return result
        task_name = query.get("task_name")
        return self.experience_capsule_store.retrieve(
            applicability=task_name if isinstance(task_name, str) else None,
            limit=limit,
        )

    def _knowledge_context(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "retrieval_mode": "validated_only",
            "query": deepcopy(self.knowledge_query),
            "external_knowledge": [
                entry.planner_context() for entry in self.retrieved_knowledge
            ],
            "validated_experiences": [
                capsule.transfer_context()
                for capsule in self.validated_experience_capsules
            ],
            "shadow_experiences": [
                {
                    **capsule.transfer_context(),
                    "state": "shadow",
                    "injection": "explicit_paired_evaluation",
                }
                for capsule in self.shadow_experience_capsules
            ],
            "retrieval_audit": deepcopy(self.knowledge_retrieval_audit),
            "transfer_policy": (
                "Use retrieved knowledge as advisory evidence. Re-ground object, "
                "pose, timing, and robot-specific arguments from current views. "
                "Check listed preconditions before acting and listed verification "
                "evidence after acting. Never replay a raw trajectory or bypass "
                "the current task graph and Verifier."
            ),
        }

    def set_shadow_experience_capsules(
        self, capsules: Sequence[ExperienceCapsule]
    ) -> None:
        """Inject candidate capsules only for an explicit paired evaluation arm."""

        if not isinstance(capsules, (list, tuple)):
            raise TypeError("shadow experience capsules must be a sequence")
        if any(not isinstance(capsule, ExperienceCapsule) for capsule in capsules):
            raise TypeError("shadow experience capsules must contain ExperienceCapsule")
        self.shadow_experience_capsules = tuple(capsules)

    def delegate_to_subagent(self, inputs: Mapping[str, Any]) -> DelegationResult:
        if self.subagent_scheduler is None:
            return DelegationResult(
                "rejected", {}, reason="subagent_scheduler_disabled"
            )
        node = inputs.get("assigned_task_graph_node")
        if not isinstance(node, Mapping):
            return DelegationResult(
                "rejected", {}, reason="assigned_task_graph_node_required"
            )
        name = inputs.get("subagent_name", "0")
        if not isinstance(name, str):
            return DelegationResult("rejected", {}, reason="subagent_name_must_be_text")
        knowledge = inputs.get("validated_knowledge")
        if knowledge is None:
            knowledge = [
                capsule.planner_context()
                for capsule in self.validated_experience_capsules
            ]
        if not isinstance(knowledge, (list, tuple)):
            return DelegationResult(
                "rejected", {}, reason="validated_knowledge_must_be_sequence"
            )
        budget = inputs.get("delegation_budget", 1)
        if not isinstance(budget, int) or isinstance(budget, bool):
            return DelegationResult(
                "rejected", {}, reason="delegation_budget_must_be_integer"
            )
        request = DelegationRequest(
            node=node,
            observation=inputs.get("observation"),
            knowledge=tuple(item for item in knowledge if isinstance(item, Mapping)),
            reason=str(inputs.get("delegation_reason", "")),
            budget=budget,
        )
        return self.subagent_scheduler.delegate(name, request)

    def predict_action(self, inputs: dict[str, Any]) -> Any:
        return self.skill_backend.predict(inputs)

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if self.verifier is None:
            raise RuntimeError("Verifier is disabled for this Agent")
        verifier_inputs = dict(inputs)
        if "knowledge_context" not in verifier_inputs:
            verifier_inputs["knowledge_context"] = deepcopy(self.last_knowledge_context)
        if self.active_module_evolution["verifier"]:
            verifier_inputs["verifier_evolution_context"] = [
                candidate.to_dict()
                for candidate in self.active_module_evolution["verifier"]
            ]
        packet = self._poll_streaming(verifier_inputs, boundary="verifier")
        if packet is not None:
            knowledge_context = deepcopy(
                verifier_inputs.get("knowledge_context", self.last_knowledge_context)
            )
            if not isinstance(knowledge_context, dict):
                knowledge_context = self._knowledge_context()
            knowledge_context["streaming_evidence"] = packet
            verifier_inputs["knowledge_context"] = knowledge_context
            self.last_knowledge_context = deepcopy(knowledge_context)
        result = self.verifier.verify(verifier_inputs)
        if self.streaming_memory_sidecar is not None:
            self.streaming_memory_sidecar.observe_event(
                {
                    "verification": deepcopy(result),
                    "observation_keys": sorted(
                        str(k) for k in (inputs.get("observation") or {}).keys()
                    )
                    if isinstance(inputs.get("observation"), Mapping)
                    else [],
                }
            )
            refreshed = dict(inputs)
            refreshed["verifier_result"] = result
            verifier_query = self._refresh_retrieval(refreshed, store=False)
            self._poll_streaming(refreshed, boundary="verifier", query=verifier_query)
        return result

    def adjudicate(self, inputs: dict[str, Any]) -> dict[str, Any]:
        adjudicate = getattr(self.planner, "adjudicate", None)
        if not callable(adjudicate):
            raise RuntimeError("Planner does not support joint adjudication")
        result = adjudicate(inputs)
        if not isinstance(result, dict):
            raise TypeError("Planner adjudication must return a dict")
        return result

    def reset(self, session_id: str) -> None:
        super().reset(session_id)
        self._reset_prompt_audits()
        self.active_high_level_skills = ()
        self.high_skill_selection = None
        self.high_skill_update_proposals = ()
        self._initial_high_skill_images = []
        self.validated_experience_capsules = ()
        self.shadow_experience_capsules = ()
        self.retrieved_knowledge = ()
        self.knowledge_retrieval_audit = {}
        self.knowledge_query = {}
        self.last_knowledge_context = {}
        self.last_streaming_packet = None
        self.episode_task_graph = None
        self.active_module_evolution = {target: () for target in EVOLUTION_TARGETS}
        self.module_evolution_proposals = ()
        if self.subagent_scheduler is not None:
            self.subagent_scheduler.reset()
        if self.streaming_memory_sidecar is not None:
            self.streaming_memory_sidecar.invalidate("agent_reset")

    def reset_for_partial_recovery(
        self,
        session_id: str,
        *,
        frozen_task_graph: Mapping[str, Any],
    ) -> None:
        """Reset semantic memory while retaining the current plan hypothesis."""
        super().reset(session_id)
        self.episode_task_graph = deepcopy(dict(frozen_task_graph))

    def reconcile_partial_recovery(self, context: Mapping[str, Any]) -> dict[str, Any]:
        if self.task_decomposer is None:
            raise RuntimeError("partial recovery requires a TaskDecomposer")
        if self.episode_task_graph is None:
            raise RuntimeError(
                "partial recovery requires the current episode task graph"
            )
        revise = getattr(self.task_decomposer, "revise_plan_graph", None)
        if not callable(revise):
            raise RuntimeError("TaskDecomposer does not support plan revision")
        result = revise(
            {
                **dict(context),
                "current_task_graph": self.episode_task_graph,
            }
        )
        if not isinstance(result, dict):
            raise TypeError("TaskDecomposer plan revision must return a dict")
        revised_graph = result.get("task_graph")
        if not isinstance(revised_graph, dict):
            raise TypeError("TaskDecomposer plan revision must include task_graph")
        self.episode_task_graph = deepcopy(revised_graph)
        return result

    def replan_frozen_task_graph_branch(
        self, context: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.task_decomposer is None:
            raise RuntimeError("selective recovery requires a TaskDecomposer")
        if self.episode_task_graph is None:
            raise RuntimeError("selective recovery requires an episode task graph")
        replan = getattr(self.task_decomposer, "replan_frozen_branch", None)
        if not callable(replan):
            raise RuntimeError("TaskDecomposer does not support selective replan")
        current_graph = context.get("current_task_graph")
        if not isinstance(current_graph, Mapping):
            current_graph = self.episode_task_graph
        result = replan(
            {
                **dict(context),
                "current_task_graph": current_graph,
            }
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("task_graph"), dict
        ):
            raise TypeError("selective TaskDecomposer replan must return task_graph")
        self.episode_task_graph = deepcopy(result["task_graph"])
        return result

    def _reset_prompt_audits(self) -> None:
        for component in (self.task_decomposer, self.planner, self.verifier):
            reset_audit = getattr(
                getattr(component, "prompt_skill", None), "reset_audit", None
            )
            if callable(reset_audit):
                reset_audit()
            if component is not None:
                cast(Any, component).last_prompt_manifest = None
                if hasattr(component, "last_model_usage"):
                    component.last_model_usage = None
                if hasattr(component, "last_atomic_skill_manifest"):
                    component.last_atomic_skill_manifest = []
                if hasattr(component, "last_atomic_skill_directory"):
                    component.last_atomic_skill_directory = []

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        artifact: dict[str, Any] = {}
        self._stream_scope = _stream_scope(context)
        if self.streaming_memory_sidecar is not None:
            self.streaming_memory_sidecar.start_episode(self._stream_scope)
        task_name = _task_name(context.get("task")) or "*"
        partial_restart = context.get("partial_reset") is True
        self._refresh_retrieval(
            {
                "task": context.get("task"),
                "cross_robot": (
                    context.get("cross_robot_transfer") is True
                    or (
                        isinstance(context.get("task"), Mapping)
                        and context["task"].get("cross_robot_transfer") is True
                    )
                ),
            }
        )
        if self.streaming_memory_sidecar is not None:
            self.streaming_memory_sidecar.request_prefetch(self.knowledge_query)
        if self.knowledge_base is not None:
            artifact["knowledge_base"] = {
                "retrieval_mode": "validated_only_tag_overlap",
                "query": deepcopy(self.knowledge_query),
                "entry_ids": [entry.knowledge_id for entry in self.retrieved_knowledge],
                "audit": deepcopy(self.knowledge_retrieval_audit),
            }
        if self.module_evolution_manager is not None and not partial_restart:
            advisory_states = self.module_evolution_manager.advisory_states
            # SubtaskVerifier 的生产准入只接受 promoted（见
            # verifiers/subtask.py 的 verifier_evolution_context 契约）；
            # planner/graph 候选仍按 advisory_states 进入顾问上下文。
            verifier_states = ("promoted",)
            retrieval_states = {
                target: (verifier_states if target == "verifier" else advisory_states)
                for target in EVOLUTION_TARGETS
            }
            self.active_module_evolution = {
                target: self.module_evolution_manager.retrieve(
                    target=target,
                    applicability=task_name,
                    parent_version=self.module_versions[target],
                    states=retrieval_states[target],
                )
                for target in EVOLUTION_TARGETS
            }
            artifact["module_evolution"] = {
                # Advisory context only. Applying a graph patch still requires
                # a promoted candidate.
                "retrieval_mode": "advisory_" + "_".join(sorted(advisory_states)),
                "retrieval_states": list(advisory_states),
                "retrieval_states_by_target": {
                    target: list(states) for target, states in retrieval_states.items()
                },
                "task_name": task_name,
                "module_versions": dict(self.module_versions),
                "proposal_ids": {
                    target: [item.proposal_id for item in candidates]
                    for target, candidates in self.active_module_evolution.items()
                },
            }
        if self.experience_capsule_store is not None and not partial_restart:
            self.validated_experience_capsules = tuple(
                capsule
                for capsule in self._retrieve_experiences(
                    self.knowledge_query,
                    limit=self.max_validated_experiences,
                )
                if capsule.skill_type == "spatial_reasoning"
            )
            artifact["experience_capsules"] = {
                "retrieval_mode": "validated_only_contextual",
                "task_name": task_name,
                "capsule_ids": [
                    capsule.capsule_id for capsule in self.validated_experience_capsules
                ],
                "query": deepcopy(self.knowledge_query),
            }
        if self.task_decomposer is not None:
            self.episode_task_graph = self.task_decomposer.decompose(
                self._decomposer_context(context)
            )
            if self.atomic_skill_system is not None:
                self.episode_task_graph = self.atomic_skill_system.annotate_task_graph(
                    self.episode_task_graph
                )
            if self.module_evolution_manager is not None:
                self.episode_task_graph = (
                    self.module_evolution_manager.apply_graph_candidates(
                        self.episode_task_graph,
                        task_name,
                        parent_version=self.module_versions["graph"],
                    )
                )
            nodes = (
                self.episode_task_graph.get("nodes")
                if isinstance(self.episode_task_graph, Mapping)
                else None
            )
            if not isinstance(nodes, list) or not nodes:
                raise ValueError(
                    "task_decomposer must produce a task graph with non-empty nodes"
                )
            artifact["task_graph"] = self.episode_task_graph
            atomic_manifest = getattr(
                self.task_decomposer, "last_atomic_skill_manifest", None
            )
            if isinstance(atomic_manifest, list):
                artifact["task_atomic_skill_manifest"] = deepcopy(atomic_manifest)
            model_usage = getattr(self.task_decomposer, "last_model_usage", None)
            if isinstance(model_usage, Mapping):
                artifact["task_decomposer_usage"] = dict(model_usage)
        if self.atomic_skill_system is not None:
            atomic_candidates = self.active_module_evolution["atomic_skill"]
            artifact["atomic_skill_evolution"] = {
                "application_status": "not_applied",
                "reason": (
                    "Atomic-skill changes require a separately implemented and "
                    "validated XR-1 adapter/verifier binding; catalog planning "
                    "context alone cannot modify execution."
                ),
                "catalog_mode": self.atomic_skill_system.mode,
                "retrieved_candidate_ids": [
                    item.proposal_id for item in atomic_candidates
                ],
            }
        if partial_restart:
            artifact["partial_restart"] = {
                "reused_cognitive_skills": [
                    skill.name for skill in self.active_high_level_skills
                ],
                "reused_experience_capsules": [
                    capsule.capsule_id for capsule in self.validated_experience_capsules
                ],
                "reused_module_candidates": {
                    target: [item.proposal_id for item in candidates]
                    for target, candidates in self.active_module_evolution.items()
                },
            }
            return artifact
        if self.skill_manager is None or self.high_skill_reasoner is None:
            return artifact or None
        full_catalog = self.skill_manager.list_skills()
        catalog = self.skill_manager.list_skills(skill_type="spatial_reasoning")
        errors: list[str] = []
        try:
            selection = self.high_skill_reasoner.select_skills(context, catalog)
        except Exception as error:
            errors.append(_error_text("selection", error))
            selection = SkillSelection(
                (), "Selection failed; continuing without skills."
            )
        self.high_skill_selection = selection
        selected_high_level_skills = tuple(
            self.skill_manager.load_skill(name) for name in selection.selected_skills
        )
        rejected = [
            skill.name
            for skill in selected_high_level_skills
            if skill.skill_type != "spatial_reasoning"
        ]
        self.active_high_level_skills = tuple(
            skill
            for skill in selected_high_level_skills
            if skill.skill_type == "spatial_reasoning"
        )
        self._initial_high_skill_images = self.high_skill_reasoner.observation_images(
            context.get("initial_observation")
        )
        artifact.update(
            {
                **selection.to_dict(),
                "active_cognitive_skills": [
                    skill.name for skill in self.active_high_level_skills
                ],
                "rejected_executable_high_skills": rejected,
                "catalog_size": len(catalog),
                "excluded_non_cognitive_catalog_size": len(full_catalog) - len(catalog),
                "errors": errors,
            }
        )
        write_json_artifact(
            high_skill_artifact_dir(context) / "selection.json", artifact
        )
        return artifact

    def end_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        if self.streaming_memory_sidecar is not None:
            stream_metrics = self.streaming_memory_sidecar.end_episode(context)
            if stream_metrics is not None:
                stream_metrics["packets_in_planner_input"] = int(
                    self.streaming_packets_in_planner_input
                )
        else:
            stream_metrics = None
        if self.skill_manager is None or self.high_skill_reasoner is None:
            errors: list[str] = []
            capsule_records: list[dict[str, Any]] = []
            if self.experience_capsule_store is not None:
                explicit = self._explicit_experience_proposals(context, errors)
                try:
                    capsule_records = self._record_experience_candidates(
                        context, explicit
                    )
                except Exception as error:
                    errors.append(_error_text("experience_capsules", error))
            module_records = self._record_module_evolution(context, errors)
            attribution_context = dict(context)
            if stream_metrics is not None:
                attribution_context["streaming_memory"] = stream_metrics
            memory_attribution = self._memory_attribution(attribution_context)
            _write_memory_attribution(context, memory_attribution)
            result = (
                {
                    "module_evolution_candidate_count": len(module_records),
                    "module_evolution_candidates": module_records,
                    "experience_capsule_candidate_count": len(capsule_records),
                    "experience_capsule_ids": [
                        item["capsule_id"] for item in capsule_records
                    ],
                    "memory_attribution": memory_attribution,
                    "errors": errors,
                }
                if (
                    self.module_evolution_manager is not None
                    or self.knowledge_base is not None
                    or self.experience_capsule_store is not None
                )
                else None
            )
            if result is not None and stream_metrics is not None:
                result["streaming_memory"] = stream_metrics
            return result
        artifact_dir = high_skill_artifact_dir(context)
        update_input = build_episode_update_input(
            context, self.active_high_level_skills
        )
        write_json_artifact(artifact_dir / "update_input.json", update_input)
        state = context.get("state")
        final_observation = (
            state.get("observation") if isinstance(state, Mapping) else None
        )
        images = collect_reflection_images(
            self._initial_high_skill_images,
            self.high_skill_reasoner.observation_images(final_observation),
            context.get("memory_context"),
            self.high_skill_reasoner.max_update_images,
        )
        errors = []
        try:
            reflection_input = {
                **update_input,
                "active_high_level_skill_context": (
                    build_high_level_skill_context(self.active_high_level_skills)
                ),
            }
            raw_proposals = self.high_skill_reasoner.propose_updates(
                reflection_input,
                self.skill_manager.list_skills(skill_type="spatial_reasoning"),
                images=images,
            )
        except Exception as error:
            errors.append(_error_text("reflection", error))
            raw_proposals = []
        rejected_proposals = [
            proposal
            for proposal in raw_proposals
            if proposal.operation != "noop"
            and proposal.skill_type != "spatial_reasoning"
        ]
        proposals = [
            proposal
            for proposal in raw_proposals
            if proposal.operation == "noop"
            or proposal.skill_type == "spatial_reasoning"
        ]
        proposals.extend(self._explicit_experience_proposals(context, errors))
        self.high_skill_update_proposals = tuple(proposals)
        write_json_artifact(
            artifact_dir / "update_proposals.json",
            {
                "proposals": [proposal.to_dict() for proposal in proposals],
                "rejected_non_cognitive_proposals": [
                    proposal.to_dict() for proposal in rejected_proposals
                ],
                "errors": errors,
            },
        )
        applied = apply_skill_updates(
            self.skill_manager, self.high_skill_write_mode, proposals
        )
        write_json_artifact(
            artifact_dir / "applied_updates.json",
            {"write_mode": self.high_skill_write_mode, "updates": applied},
        )
        capsule_records = []
        if self.experience_capsule_store is not None:
            try:
                capsule_records = self._record_experience_candidates(
                    context,
                    proposals,
                )
            except Exception as error:
                errors.append(_error_text("experience_capsules", error))
            write_json_artifact(
                artifact_dir / "experience_capsule_candidates.json",
                {
                    "retrieval_mode": "validated_only",
                    "candidate_count": len(capsule_records),
                    "candidates": capsule_records,
                    "errors": errors,
                },
            )
        module_records = self._record_module_evolution(context, errors)
        attribution_context = dict(context)
        if stream_metrics is not None:
            attribution_context["streaming_memory"] = stream_metrics
        memory_attribution = self._memory_attribution(attribution_context)
        _write_memory_attribution(context, memory_attribution)
        return {
            "proposal_count": len(proposals),
            "rejected_non_cognitive_proposal_count": len(rejected_proposals),
            "applied_count": sum(item["status"] == "applied" for item in applied),
            "experience_capsule_candidate_count": len(capsule_records),
            "experience_capsule_ids": [item["capsule_id"] for item in capsule_records],
            "module_evolution_candidate_count": len(module_records),
            "module_evolution_candidates": module_records,
            "memory_attribution": memory_attribution,
            "proposals": [
                {
                    "operation": proposal.operation,
                    "skill_name": proposal.skill_name,
                    "type": proposal.skill_type,
                    "kind": proposal.kind,
                    "cause": proposal.cause,
                    "evidence_steps": list(proposal.evidence_steps),
                }
                for proposal in proposals
            ],
            "errors": errors,
            **(
                {"streaming_memory": stream_metrics}
                if stream_metrics is not None
                else {}
            ),
        }

    def _memory_attribution(self, context: Mapping[str, Any]) -> dict[str, Any]:
        result = context.get("result")
        state = context.get("state")
        authoritative_success: bool | None = None
        if isinstance(result, Mapping):
            environment_info = result.get("env_info")
            if isinstance(environment_info, Mapping) and "success" in environment_info:
                authoritative_success = environment_info.get("success") is True
            elif "success" in result:
                authoritative_success = result.get("success") is True
        reconciliation_failures = 0
        for source in (state, result):
            if not isinstance(source, Mapping):
                continue
            value = source.get("terminal_graph_reconciliation_failures", 0)
            if isinstance(value, int) and not isinstance(value, bool):
                reconciliation_failures = max(reconciliation_failures, value)
        history = state.get("history", []) if isinstance(state, Mapping) else []
        completed_ledger = _ledger(state, "completed_executions")
        completed_execution_count = len(completed_ledger)
        if not completed_execution_count and isinstance(history, list):
            seen_completed: set[str] = set()
            for event in history:
                if not isinstance(event, Mapping):
                    continue
                transition = event.get("transition")
                transition_status = (
                    transition.get("next_status")
                    if isinstance(transition, Mapping)
                    else None
                )
                status = (
                    event.get("next_status") or transition_status or event.get("status")
                )
                if status not in {"completed", "subtask_completed", "task_success"}:
                    continue
                execution_id = str(event.get("execution_id") or len(seen_completed))
                if execution_id not in seen_completed:
                    seen_completed.add(execution_id)
            completed_execution_count = len(seen_completed)

        verifier_evidence_count = 0
        seen_verifier_events: set[tuple[str, str]] = set()

        def count_verifier_evidence(event: Mapping[str, Any], ordinal: int) -> None:
            nonlocal verifier_evidence_count
            event_type = str(event.get("event") or event.get("event_type") or "")
            verification = event.get("verification")
            if not isinstance(verification, Mapping):
                verification = event.get("output")
            evidence = event.get("verifier_evidence")
            if not evidence and isinstance(verification, Mapping):
                evidence = verification.get("evidence")
            if not evidence:
                return
            execution_id = str(event.get("execution_id") or "")
            step = event.get("step")
            if step is not None:
                # A history entry and its trace-level verifier_call describe
                # the same checkpoint; step is the shared identity even when
                # the compact trace event omits execution_id.
                key = (f"step:{step}", "")
            elif execution_id:
                key = (f"execution:{execution_id}", "")
            else:
                key = (f"anonymous:{ordinal}", event_type)
            if key not in seen_verifier_events:
                seen_verifier_events.add(key)
                verifier_evidence_count += 1

        ordinal = 0
        if isinstance(history, list):
            for event in history:
                if isinstance(event, Mapping):
                    count_verifier_evidence(event, ordinal)
                    ordinal += 1
        trace_events = []
        if isinstance(state, Mapping) and isinstance(state.get("trace_events"), list):
            trace_events.extend(state["trace_events"])
        if isinstance(context.get("trace_events"), list):
            trace_events.extend(context["trace_events"])
        for event in trace_events:
            if isinstance(event, Mapping):
                count_verifier_evidence(event, ordinal)
                ordinal += 1
        retrieved_knowledge_ids = [
            entry.knowledge_id for entry in self.retrieved_knowledge
        ]
        retrieved_experience_ids = [
            capsule.capsule_id for capsule in self.validated_experience_capsules
        ]
        shadow_experience_ids = [
            capsule.capsule_id for capsule in self.shadow_experience_capsules
        ]
        retrieved_any = bool(
            retrieved_knowledge_ids or retrieved_experience_ids or shadow_experience_ids
        )
        if reconciliation_failures:
            attribution_status = "integrity_failure"
        elif authoritative_success is True and retrieved_any:
            attribution_status = "correlated_only"
        elif authoritative_success is True:
            attribution_status = "no_retrieved_memory"
        elif authoritative_success is False:
            attribution_status = "success_not_observed"
        else:
            attribution_status = "outcome_unknown"
        observed_factors = [
            "authoritative environment success"
            if authoritative_success is True
            else "authoritative success was not observed",
            f"verified execution evidence count: {verifier_evidence_count}",
            f"completed execution count: {completed_execution_count}",
        ]
        if reconciliation_failures:
            observed_factors.append(
                "terminal graph reconciliation failures: "
                + str(reconciliation_failures)
            )
        if retrieved_knowledge_ids:
            observed_factors.append(
                "external knowledge retrieved: " + ", ".join(retrieved_knowledge_ids)
            )
        if retrieved_experience_ids:
            observed_factors.append(
                "validated experience retrieved: " + ", ".join(retrieved_experience_ids)
            )
        if shadow_experience_ids:
            observed_factors.append(
                "shadow experience injected: " + ", ".join(shadow_experience_ids)
            )
        return {
            "causal_attribution": attribution_status,
            "causal_claim": (
                "A memory contribution requires a paired held-out control/treatment "
                "comparison with fixed code, config, policy and horizon."
            ),
            "authoritative_success": authoritative_success,
            "retrieved_external_knowledge_ids": retrieved_knowledge_ids,
            "retrieved_experience_capsule_ids": retrieved_experience_ids,
            "shadow_experience_capsule_ids": shadow_experience_ids,
            "streaming_memory": (
                deepcopy(context.get("streaming_memory"))
                if isinstance(context.get("streaming_memory"), Mapping)
                else None
            ),
            "retrieval_counts": {
                "external_knowledge": len(retrieved_knowledge_ids),
                "validated_experiences": len(retrieved_experience_ids),
                "shadow_experiences": len(shadow_experience_ids),
            },
            "streaming_packet_published": self.last_streaming_packet is not None,
            "terminal_graph_reconciliation_failures": reconciliation_failures,
            "promotion_eligible": reconciliation_failures == 0,
            "verifier_evidence_count": verifier_evidence_count,
            "completed_execution_count": completed_execution_count,
            "observed_factors": observed_factors,
            "termination_reason": (
                result.get("termination_reason")
                if isinstance(result, Mapping)
                else None
            ),
        }

    def _explicit_experience_proposals(
        self,
        context: Mapping[str, Any],
        errors: list[str],
    ) -> list[SkillUpdateProposal]:
        """Accept normalized proposals from any reflection backend.

        This optional input keeps capsule creation independent of one specific
        LLM response schema. The normal high-level reasoner remains supported.
        """

        raw_proposals = context.get("experience_capsule_proposals", [])
        if raw_proposals in (None, []):
            return []
        if not isinstance(raw_proposals, list):
            errors.append(
                "experience_capsules: experience_capsule_proposals must be a list"
            )
            return []
        proposals: list[SkillUpdateProposal] = []
        for index, raw in enumerate(raw_proposals):
            try:
                if isinstance(raw, SkillUpdateProposal):
                    proposal = raw
                elif isinstance(raw, Mapping):
                    operation = raw.get("operation", "create")
                    skill_name = raw.get("skill_name")
                    skill_type = raw.get(
                        "type", raw.get("skill_type", "spatial_reasoning")
                    )
                    kind = raw.get("kind", "experience")
                    description = raw.get("description")
                    instructions = raw.get("instructions")
                    cause = raw.get("cause", "skill_gap")
                    reason = raw.get("reason")
                    evidence_steps = _normalize_evidence_steps(
                        raw.get("evidence_steps", [])
                    )
                    if operation not in {"noop", "create", "update"}:
                        raise ValueError("operation must be noop, create, or update")
                    if operation != "noop":
                        for name, value in (
                            ("skill_name", skill_name),
                            ("type", skill_type),
                            ("kind", kind),
                            ("reason", reason),
                        ):
                            if not isinstance(value, str) or not value.strip():
                                raise ValueError(f"{name} must be non-empty")
                        if skill_type != "spatial_reasoning":
                            raise ValueError(
                                "experience capsule type must be spatial_reasoning"
                            )
                        if kind != "experience":
                            raise ValueError(
                                "experience capsule kind must be experience"
                            )
                        if cause not in {
                            "skill_gap",
                            "execution_lapse",
                            "policy_failure",
                            "environment_failure",
                            "insufficient_evidence",
                        }:
                            raise ValueError("cause is not a supported evolution cause")
                        if operation == "create" and (
                            not isinstance(description, str)
                            or not description.strip()
                            or not isinstance(instructions, str)
                            or not instructions.strip()
                        ):
                            raise ValueError(
                                "create experience proposals require description "
                                "and instructions"
                            )
                    proposal = SkillUpdateProposal(
                        operation=cast(Literal["noop", "create", "update"], operation),
                        skill_name=(
                            skill_name.strip() if isinstance(skill_name, str) else None
                        ),
                        skill_type=(
                            skill_type.strip() if isinstance(skill_type, str) else None
                        ),
                        kind=kind.strip() if isinstance(kind, str) else None,
                        description=(
                            description.strip()
                            if isinstance(description, str)
                            else None
                        ),
                        instructions=(
                            instructions.strip()
                            if isinstance(instructions, str)
                            else None
                        ),
                        cause=str(cause),
                        reason=str(reason or "").strip(),
                        evidence_steps=tuple(evidence_steps),
                    )
                else:
                    raise TypeError("proposal must be a mapping")
                if proposal.operation != "noop":
                    if (
                        proposal.skill_type != "spatial_reasoning"
                        or proposal.kind != "experience"
                    ):
                        raise ValueError(
                            "only spatial_reasoning/experience proposals become "
                            "capsules"
                        )
                    if not proposal.evidence_steps:
                        raise ValueError(
                            "experience capsule proposal needs evidence_steps"
                        )
                proposals.append(proposal)
            except Exception as error:
                errors.append(
                    _error_text(f"experience_capsule_proposal[{index}]", error)
                )
        return proposals

    def record_experience_candidate(
        self,
        context: Mapping[str, Any],
        proposal: Mapping[str, Any] | SkillUpdateProposal,
    ) -> dict[str, Any]:
        """Persist one provider-independent candidate capsule envelope."""

        errors: list[str] = []
        if isinstance(proposal, SkillUpdateProposal):
            normalized = proposal
        else:
            normalized_list = self._explicit_experience_proposals(
                {"experience_capsule_proposals": [proposal]}, errors
            )
            if not normalized_list:
                raise ValueError(
                    "invalid experience capsule proposal: " + "; ".join(errors)
                )
            normalized = normalized_list[0]
        records = self._record_experience_candidates(context, [normalized])
        if not records:
            raise ValueError("proposal did not produce an experience capsule")
        return records[0]

    def _record_experience_candidates(
        self,
        context: Mapping[str, Any],
        proposals: list[SkillUpdateProposal],
    ) -> list[dict[str, Any]]:
        if self.experience_capsule_store is None or self.experience_context is None:
            return []
        task = context.get("task")
        source_identity = _source_identity(task)
        horizon = task.get("horizon") if isinstance(task, Mapping) else None
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
            raise ValueError("Experience Capsule source task requires positive horizon")
        code_version = self.experience_context.get("code_version")
        config_version = self.experience_context.get("config_version")
        base_policy = self.experience_context.get("base_policy")
        responsible_module = self.experience_context.get(
            "responsible_module", "planner"
        )
        expires_at = self.experience_context.get("expires_at")
        task_name = source_identity["task_name"]
        task_set = task.get("task_set") if isinstance(task, Mapping) else None
        task_family = task.get("task_family") if isinstance(task, Mapping) else None
        source_robot_profile = self.experience_context.get(
            "source_robot_profile", self.robot_profile
        )
        compatible_robot_profiles = self.experience_context.get(
            "compatible_robot_profiles", (self.robot_profile,)
        )
        required_capabilities = self.experience_context.get(
            "required_capabilities", self.robot_capabilities
        )
        transfer_notes = self.experience_context.get("transfer_notes", "")
        if not isinstance(source_robot_profile, str):
            raise ValueError("experience_context.source_robot_profile must be a string")
        compatible_robot_profiles = _normalize_texts(
            "experience_context.compatible_robot_profiles",
            compatible_robot_profiles,
        )
        required_capabilities = _normalize_texts(
            "experience_context.required_capabilities",
            required_capabilities,
            allow_empty=True,
        )
        if not isinstance(transfer_notes, str):
            raise ValueError("experience_context.transfer_notes must be a string")
        graph = context.get("task_graph")
        if not isinstance(graph, Mapping):
            graph = self.episode_task_graph
        graph_nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        graph_atomic_skill_ids = (
            [
                str(node["atomic_skill_id"])
                for node in graph_nodes
                if isinstance(node, Mapping)
                and isinstance(node.get("atomic_skill_id"), str)
                and node["atomic_skill_id"].strip()
            ]
            if isinstance(graph_nodes, list)
            else []
        )
        trace_path = context.get("trace_path")
        records: list[dict[str, Any]] = []
        for proposal in proposals:
            if (
                proposal.operation == "noop"
                or proposal.kind != "experience"
                or proposal.skill_type != "spatial_reasoning"
                or not proposal.evidence_steps
            ):
                continue
            description = proposal.description
            instructions = proposal.instructions
            if proposal.operation == "update":
                if self.skill_manager is None:
                    raise RuntimeError(
                        "Experience Capsule update proposals require SkillManager"
                    )
                current = self.skill_manager.load_skill(proposal.skill_name or "")
                description = description or current.description
                instructions = instructions or current.instructions
            evidence_refs = [
                str(trace_path) if trace_path else "episode_trace_unavailable",
                *(f"step:{step}" for step in proposal.evidence_steps),
            ]
            applicability = [str(task_name), f"task:{task_name}"]
            if isinstance(task_set, str) and task_set:
                applicability.extend((task_set, f"task_set:{task_set}"))
            if isinstance(task_family, str) and task_family:
                applicability.extend((task_family, f"family:{task_family}"))
            for atomic_skill_id in graph_atomic_skill_ids:
                applicability.extend((atomic_skill_id, f"atomic:{atomic_skill_id}"))
            if proposal.skill_name:
                applicability.extend(
                    (proposal.skill_name, f"macro:{proposal.skill_name}")
                )
            capsule = ExperienceCapsule.candidate(
                source_identity=source_identity,
                code_version=cast(str, code_version),
                config_version=cast(str, config_version),
                base_policy=cast(str, base_policy),
                horizon=horizon,
                skill_name=proposal.skill_name or "",
                skill_type=proposal.skill_type or "",
                description=description or "",
                instructions=instructions or "",
                failure_signature=f"{proposal.cause}: {proposal.reason}",
                responsible_module=responsible_module,
                applicability=tuple(dict.fromkeys(applicability)),
                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                expires_at=expires_at,
                source_robot_profile=source_robot_profile,
                compatible_robot_profiles=compatible_robot_profiles,
                required_capabilities=required_capabilities,
                transfer_notes=transfer_notes,
            )
            self.experience_capsule_store.add_candidate(capsule)
            records.append(
                {
                    "capsule_id": capsule.capsule_id,
                    "state": capsule.state,
                    "skill_name": capsule.skill_name,
                    "source_identity": dict(capsule.source_identity),
                    "source_robot_profile": capsule.source_robot_profile,
                    "compatible_robot_profiles": list(
                        capsule.compatible_robot_profiles
                    ),
                    "evidence_refs": list(capsule.evidence_refs),
                }
            )
        return records

    def _record_module_evolution(
        self,
        context: Mapping[str, Any],
        errors: list[str],
    ) -> list[dict[str, Any]]:
        if self.module_evolution_manager is None:
            return []
        raw_proposals: list[Mapping[str, Any]] = []
        explicit = context.get("module_evolution_proposals", [])
        if isinstance(explicit, list):
            raw_proposals.extend(item for item in explicit if isinstance(item, Mapping))
        elif explicit is not None:
            errors.append(
                "module_evolution: explicit proposals must be a list of mappings"
            )
        # Deterministic rules run first so a provider failure in the advisory
        # reasoner cannot leave the episode with zero attributable candidates.
        episode_state = context.get("state")
        try:
            raw_proposals.extend(
                extract_evolution_candidates(
                    completed_executions=_ledger(episode_state, "completed_executions"),
                    failed_executions=_ledger(episode_state, "failed_executions"),
                    result=(
                        context.get("result")
                        if isinstance(context.get("result"), Mapping)
                        else None
                    ),
                )
            )
        except Exception as error:
            errors.append(_error_text("evolution_candidate_extraction", error))
        if self.module_evolution_reasoner is not None:
            try:
                raw_proposals.extend(
                    self.module_evolution_reasoner.propose(
                        self._module_reasoner_input(context)
                    )
                )
            except Exception as error:
                errors.append(_error_text("module_evolution_reasoner", error))

        session_id = str(context.get("session_id") or "unknown_episode")
        task = context.get("task")
        task_name = _task_name(task) or "*"
        task_set = task.get("task_set") if isinstance(task, Mapping) else None
        default_applicability = [task_name]
        if isinstance(task_set, str) and task_set.strip():
            default_applicability.append(task_set.strip())
        trace_path = context.get("trace_path")
        records: list[ModuleEvolutionCandidate] = []
        rejected_records: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_proposals):
            try:
                target = raw.get("target")
                operation = raw.get("operation")
                patch = raw.get("patch")
                reason = raw.get("reason")
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("target must be a non-empty string")
                target = target.strip()
                if isinstance(operation, str):
                    operation = operation.strip()
                parent_version = self.module_versions.get(target)
                if not isinstance(parent_version, str) or not parent_version:
                    raise ValueError(f"unknown module evolution target: {target}")
                evidence_refs = [
                    str(trace_path) if trace_path else "episode_trace_unavailable"
                ]
                steps = _normalize_evidence_steps(raw.get("evidence_steps", []))
                evidence_refs.extend(f"step:{step}" for step in steps)
                supplied_evidence = raw.get("evidence_refs", [])
                if not isinstance(supplied_evidence, list) or any(
                    not isinstance(item, str) or not item.strip()
                    for item in supplied_evidence
                ):
                    raise ValueError("evidence_refs must be a list of strings")
                evidence_refs.extend(supplied_evidence)
                applicability = raw.get("applicability", default_applicability)
                # provider 常把单值写成字符串；归一化为列表而不是丢弃候选
                # （2026-09-11 实测：每次 episode 丢 2 个候选）。
                if isinstance(applicability, str) and applicability.strip():
                    applicability = [applicability.strip()]
                if not isinstance(applicability, list):
                    raise ValueError("applicability must be a list")
                if not all(
                    isinstance(item, str) and item.strip() for item in applicability
                ):
                    raise ValueError("applicability entries must be non-empty strings")
                candidate = self.module_evolution_manager.propose(
                    target=target,
                    operation=cast(Literal["noop", "create", "update"], operation),
                    patch=cast(Mapping[str, Any], patch),
                    reason=cast(str, reason),
                    evidence_refs=evidence_refs,
                    applicability=applicability,
                    source_episode_id=session_id,
                    parent_version=parent_version,
                )
                records.append(candidate)
            except Exception as error:
                rejected_records.append(
                    {
                        "proposal_index": index,
                        "target": raw.get("target"),
                        "operation": raw.get("operation"),
                        "reason": raw.get("reason"),
                        "evidence_steps": raw.get("evidence_steps"),
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )
                errors.append(_error_text(f"module_evolution_proposal[{index}]", error))
        self.module_evolution_proposals = tuple(records)
        artifact_dir = high_skill_artifact_dir(context)
        write_json_artifact(
            artifact_dir / "module_evolution_candidates.json",
            {
                "write_mode": "proposal_only",
                "base_policy": (
                    self.module_evolution_manager.base_policy
                    if self.module_evolution_manager is not None
                    else None
                ),
                "candidate_count": len(records),
                "candidates": [candidate.to_dict() for candidate in records],
                "rejected_candidate_count": len(rejected_records),
                "rejected_candidates": rejected_records,
                "errors": errors,
            },
        )
        if records:
            # Query-style audit export for every candidate this episode touched.
            # Field meanings, so the summary is not misread: before_after_metrics
            # are paired success counts (not before/after images);
            # promotion_decision mirrors the latest validation_decision and does
            # not mean the candidate was promoted; memory_attribution is supplied
            # by the caller when available and implies no causal contribution.
            write_json_artifact(
                artifact_dir / "module_evolution_trace.json",
                {
                    "note": (
                        "Audit summary over recorded evidence. Promotion still "
                        "requires an explicit approved_by promote; validated does "
                        "not mean statistically reliable improvement."
                    ),
                    "candidate_ids": [candidate.proposal_id for candidate in records],
                    "traces": [
                        self.module_evolution_manager.evolution_trace(
                            candidate.proposal_id
                        )
                        for candidate in records
                    ],
                },
            )
        return [candidate.to_dict() for candidate in records]

    def _module_reasoner_input(self, context: Mapping[str, Any]) -> dict[str, Any]:
        result = context.get("result")
        compact_result = (
            {
                key: value
                for key, value in result.items()
                if key
                in {
                    "success",
                    "task_progress",
                    "termination_reason",
                    "pipeline_iterations",
                    "environment_steps",
                    "executed_action_chunks",
                    "planner_calls",
                    "verifier_calls",
                    "invalid_actions",
                    "replans",
                    "recovery_attempts",
                    "exceptions",
                    "error_type",
                    "error",
                }
            }
            if isinstance(result, Mapping)
            else {}
        )
        # Reuse evidence summaries instead of repeating model responses and frames.
        episode_input = build_episode_update_input(
            context, self.active_high_level_skills
        )
        return {
            "task": context.get("task"),
            "result": compact_result,
            "task_graph": self.episode_task_graph,
            "recent_execution_events": episode_input["step_summaries"][-12:],
            "memory_context": episode_input["memory"],
            "trace_path": str(context.get("trace_path") or ""),
            "module_versions": self.module_versions,
            "atomic_skill_system": (
                self.atomic_skill_system.healthcheck()
                if self.atomic_skill_system is not None
                else None
            ),
            "base_policy": (
                self.module_evolution_manager.base_policy
                if self.module_evolution_manager is not None
                else None
            ),
        }

    def healthcheck(self) -> dict[str, Any]:
        health = super().healthcheck()
        if self.task_decomposer is not None:
            decomposer_health = self.task_decomposer.healthcheck()
            health["task_decomposer"] = decomposer_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                decomposer_health.get("healthy")
            )
        if self.experience_capsule_store is not None:
            capsule_health = self.experience_capsule_store.healthcheck()
            health["experience_capsules"] = capsule_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                capsule_health.get("healthy")
            )
        if self.knowledge_base is not None:
            knowledge_health = self.knowledge_base.healthcheck()
            health["knowledge_base"] = knowledge_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                knowledge_health.get("healthy")
            )
        if self.streaming_memory_sidecar is not None:
            sidecar_health = self.streaming_memory_sidecar.healthcheck()
            health["streaming_memory_sidecar"] = sidecar_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                sidecar_health.get("healthy")
            )
        if self.atomic_skill_system is not None:
            atomic_health = self.atomic_skill_system.healthcheck()
            health["atomic_skill_system"] = atomic_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                atomic_health.get("healthy")
            )
        if self.module_evolution_manager is not None:
            evolution_health = self.module_evolution_manager.healthcheck()
            if self.module_evolution_reasoner is not None:
                try:
                    evolution_health["reasoner"] = (
                        self.module_evolution_reasoner.healthcheck()
                    )
                except Exception as error:
                    evolution_health["reasoner"] = {
                        "healthy": False,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
            health["module_evolution"] = evolution_health
            health["healthy"] = bool(health.get("healthy")) and bool(
                evolution_health.get("healthy")
            )
        if self.skill_manager is None or self.high_skill_reasoner is None:
            return health
        planner_supported = bool(
            getattr(self.planner, "supports_high_level_skills", False)
        )
        try:
            manager_health: dict[str, Any] = {
                "healthy": True,
                "catalog_size": len(self.skill_manager.list_skills()),
                "root": str(self.skill_manager.root),
            }
        except Exception as error:
            manager_health = {
                "healthy": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        try:
            reasoner_health = self.high_skill_reasoner.healthcheck()
        except Exception as error:
            reasoner_health = {
                "healthy": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        high_skill_healthy = (
            planner_supported
            and bool(manager_health.get("healthy"))
            and bool(reasoner_health.get("healthy"))
        )
        health["high_level_skills"] = {
            "healthy": high_skill_healthy,
            "planner_supported": planner_supported,
            "manager": manager_health,
            "reasoner": reasoner_health,
            "write_mode": self.high_skill_write_mode,
        }
        health["healthy"] = bool(health.get("healthy")) and high_skill_healthy
        return health

    def close(self) -> None:
        if self._closed:
            return
        errors: list[str] = []
        if self.task_decomposer is not None:
            try:
                self.task_decomposer.close()
            except Exception as error:
                errors.append(f"task_decomposer: {type(error).__name__}: {error}")
        if self.high_skill_reasoner is not None:
            try:
                self.high_skill_reasoner.close()
            except Exception as error:
                errors.append(f"high_skill_reasoner: {type(error).__name__}: {error}")
        if self.module_evolution_reasoner is not None:
            try:
                self.module_evolution_reasoner.close()
            except Exception as error:
                errors.append(
                    f"module_evolution_reasoner: {type(error).__name__}: {error}"
                )
        if self.knowledge_base is not None:
            try:
                self.knowledge_base.close()
            except Exception as error:
                errors.append(f"knowledge_base: {type(error).__name__}: {error}")
        if self.streaming_memory_sidecar is not None:
            try:
                self.streaming_memory_sidecar.close()
            except Exception as error:
                errors.append(
                    f"streaming_memory_sidecar: {type(error).__name__}: {error}"
                )
        try:
            super().close()
        except Exception as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError("Failed to close Agent components: " + "; ".join(errors))


def _current_camera_views(
    observation: Any, planner: Planner | None
) -> dict[str, Any] | None:
    """Select configured current camera views without copying image payloads."""

    if not isinstance(observation, Mapping):
        return None
    configured = getattr(planner, "camera_keys", None)
    if isinstance(configured, list) and configured:
        selected = {key: observation[key] for key in configured if key in observation}
    else:
        selected = {
            str(key): value
            for key, value in observation.items()
            if str(key).startswith("video.") or "camera" in str(key).lower()
        }
    return selected or None


def _stream_scope(context: Mapping[str, Any]) -> dict[str, Any]:
    """Build a stable episode scope without copying observations or secrets."""
    fields = (
        "study",
        "protocol",
        "trial_id",
        "arm",
        "session_id",
        "episode_id",
        "config_version",
        "policy_config_identity",
        "task_name",
    )
    scope = {
        name: context[name]
        for name in fields
        if name in context and isinstance(context[name], (str, int, float, bool))
    }
    task = context.get("task")
    if isinstance(task, Mapping) and isinstance(task.get("name"), str):
        scope.setdefault("task_name", task["name"])
    return scope


def _task_name(task: Any) -> str | None:
    if isinstance(task, str) and task.strip():
        return task.rsplit("/", 1)[-1]
    if isinstance(task, Mapping):
        for key in ("name", "task_name", "env_name", "id"):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                return value.rsplit("/", 1)[-1]
    return None


def _source_identity(task: Any) -> dict[str, str | int]:
    if not isinstance(task, Mapping):
        raise ValueError("Experience Capsule source task must be a mapping")
    task_name = _task_name(task)
    episode_index = task.get("episode_index")
    seed = task.get("seed")
    if task_name is None:
        raise ValueError("Experience Capsule source task requires task_name")
    for name, value in (("episode_index", episode_index), ("seed", seed)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Experience Capsule source task requires integer {name}")
    return {
        "task_name": task_name,
        "episode_index": cast(int, episode_index),
        "seed": cast(int, seed),
    }


def _normalize_texts(
    name: str,
    values: Any,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple")
    normalized = tuple(
        item.strip() for item in values if isinstance(item, str) and item.strip()
    )
    if len(normalized) != len(values):
        raise ValueError(f"{name} must contain non-empty strings")
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must not contain duplicates")
    return normalized


def _task_context_query(task: Any) -> dict[str, Any]:
    query: dict[str, Any] = {}
    task_name = _task_name(task)
    if task_name is not None:
        query["task_name"] = task_name
    if isinstance(task, Mapping):
        for field_name in ("task_set", "task_family"):
            value = task.get(field_name)
            if isinstance(value, str) and value.strip():
                query[field_name] = value.strip()
        if task.get("cross_robot_transfer") is True:
            query["cross_robot"] = True
    return query


def _atomic_skill_query_values(
    task_decomposer: TaskDecomposer | None, task_skill_name: str
) -> str | tuple[str, ...]:
    """Keep the task-level key and add canonical contract IDs for retrieval."""

    values = [task_skill_name]
    catalog = getattr(task_decomposer, "task_atomic_skill_catalog", None)
    resolver = getattr(catalog, "directory_skill_ids_for_name", None)
    if callable(resolver):
        aliases = resolver(task_skill_name)
        if isinstance(aliases, (list, tuple)):
            values.extend(
                alias for alias in aliases if isinstance(alias, str) and alias.strip()
            )
    unique = tuple(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else unique


def _write_memory_attribution(
    context: Mapping[str, Any], attribution: Mapping[str, Any]
) -> None:
    artifact_dir = context.get("artifact_dir")
    if not isinstance(artifact_dir, (str, Path)):
        return
    write_json_artifact(Path(artifact_dir) / "memory_attribution.json", attribution)


def _ledger(state: Any, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(state, Mapping):
        return []
    records = state.get(key)
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, Mapping)]


def _error_text(stage: str, error: Exception) -> str:
    return f"{stage}: {type(error).__name__}: {error}"


def _normalize_evidence_steps(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("evidence_steps must be a JSON list")
    normalized: list[int] = []
    seen: set[int] = set()
    for index, item in enumerate(value):
        if type(item) is int:
            step = item
        elif isinstance(item, str):
            if item.isascii() and item.isdecimal():
                step = int(item)
            elif item.startswith("step:"):
                suffix = item.removeprefix("step:")
                if not suffix.isascii() or not suffix.isdecimal():
                    raise ValueError(
                        f"evidence_steps[{index}] has an invalid step:<integer> value"
                    )
                step = int(suffix)
            else:
                raise ValueError(
                    f"evidence_steps[{index}] must be an integer, decimal string, "
                    "or step:<integer>"
                )
        else:
            raise ValueError(
                f"evidence_steps[{index}] must be an integer, decimal string, "
                f"or step:<integer>; got {type(item).__name__}"
            )
        if step < 0:
            raise ValueError(f"evidence_steps[{index}] must be non-negative")
        if step not in seen:
            seen.add(step)
            normalized.append(step)
    return normalized


def _module_candidate_context_entry(
    candidate: ModuleEvolutionCandidate,
) -> dict[str, str]:
    return {
        "name": f"{candidate.target}_{candidate.proposal_id}",
        "type": "spatial_reasoning",
        "description": candidate.reason,
        "instructions": (
            f"{candidate.state} module-evolution guidance backed by held-out paired "
            "validation. Apply only inside its recorded scope and do not change the "
            "official goal, safety rules, or action schema. "
            + json.dumps(candidate.patch, ensure_ascii=False, sort_keys=True)
        ),
    }
