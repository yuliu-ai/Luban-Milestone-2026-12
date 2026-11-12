from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from luban.agent_core.agents.base import BaseAgent
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
from luban.agent_core.memories.base import Memory
from luban.agent_core.planners.base import Planner
from luban.agent_core.task_decomposer import TaskDecomposer
from luban.agent_core.verifiers.base import Verifier
from luban.backends.skills.base import SkillBackend
from luban.backends.world_models.base import WorldModelBackend


class DefaultAgent(BaseAgent):
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

    def plan(self, inputs: dict[str, Any]) -> Any:
        if not self.active_high_level_skills and not self.validated_experience_capsules:
            if self.episode_task_graph is not None and "task_graph" not in inputs:
                inputs = {**inputs, "task_graph": self.episode_task_graph}
            return self.planner.plan(inputs)
        planner_inputs = dict(inputs)
        if self.episode_task_graph is not None and "task_graph" not in planner_inputs:
            planner_inputs["task_graph"] = self.episode_task_graph
        high_skill_context = build_high_level_skill_context(
            self.active_high_level_skills
        )
        high_skill_context["experiences"].extend(
            capsule.planner_context() for capsule in self.validated_experience_capsules
        )
        planner_inputs["high_level_skill_context"] = high_skill_context
        return self.planner.plan(planner_inputs)

    def predict_action(self, inputs: dict[str, Any]) -> Any:
        return self.skill_backend.predict(inputs)

    def verify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if self.verifier is None:
            raise RuntimeError("Verifier is disabled for this Agent")
        return self.verifier.verify(inputs)

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
        self.episode_task_graph = None

    def reset_for_partial_recovery(
        self,
        session_id: str,
        *,
        frozen_task_graph: Mapping[str, Any],
    ) -> None:
        """Reset semantic memory while retaining the current plan hypothesis."""
        super().reset(session_id)
        self.active_high_level_skills = ()
        self.high_skill_selection = None
        self.high_skill_update_proposals = ()
        self._initial_high_skill_images = []
        self.validated_experience_capsules = ()
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

    def begin_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        artifact: dict[str, Any] = {}
        if self.experience_capsule_store is not None:
            task_name = _task_name(context.get("task"))
            self.validated_experience_capsules = (
                tuple(
                    self.experience_capsule_store.retrieve(
                        applicability=task_name,
                        limit=self.max_validated_experiences,
                    )
                )
                if task_name is not None
                else ()
            )
            artifact["experience_capsules"] = {
                "retrieval_mode": "validated_only",
                "task_name": task_name,
                "capsule_ids": [
                    capsule.capsule_id for capsule in self.validated_experience_capsules
                ],
            }
        if self.task_decomposer is not None:
            self.episode_task_graph = self.task_decomposer.decompose(context)
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
        if self.skill_manager is None or self.high_skill_reasoner is None:
            return artifact or None
        catalog = self.skill_manager.list_skills()
        errors: list[str] = []
        try:
            selection = self.high_skill_reasoner.select_skills(context, catalog)
        except Exception as error:
            errors.append(_error_text("selection", error))
            selection = SkillSelection(
                (), "Selection failed; continuing without skills."
            )
        self.high_skill_selection = selection
        self.active_high_level_skills = tuple(
            self.skill_manager.load_skill(name) for name in selection.selected_skills
        )
        self._initial_high_skill_images = self.high_skill_reasoner.observation_images(
            context.get("initial_observation")
        )
        artifact.update(
            {
                **selection.to_dict(),
                "catalog_size": len(catalog),
                "errors": errors,
            }
        )
        write_json_artifact(
            high_skill_artifact_dir(context) / "selection.json", artifact
        )
        return artifact

    def end_episode(self, context: dict[str, Any]) -> dict[str, Any] | None:
        if self.skill_manager is None or self.high_skill_reasoner is None:
            return None
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
        errors: list[str] = []
        try:
            reflection_input = {
                **update_input,
                "active_high_level_skill_context": (
                    build_high_level_skill_context(self.active_high_level_skills)
                ),
            }
            proposals = self.high_skill_reasoner.propose_updates(
                reflection_input,
                self.skill_manager.list_skills(),
                images=images,
            )
        except Exception as error:
            errors.append(_error_text("reflection", error))
            proposals = []
        self.high_skill_update_proposals = tuple(proposals)
        write_json_artifact(
            artifact_dir / "update_proposals.json",
            {
                "proposals": [proposal.to_dict() for proposal in proposals],
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
        capsule_records: list[dict[str, Any]] = []
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
        return {
            "proposal_count": len(proposals),
            "applied_count": sum(item["status"] == "applied" for item in applied),
            "experience_capsule_candidate_count": len(capsule_records),
            "experience_capsule_ids": [item["capsule_id"] for item in capsule_records],
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
        }

    def _record_experience_candidates(
        self,
        context: Mapping[str, Any],
        proposals: list[SkillUpdateProposal],
    ) -> list[dict[str, Any]]:
        if self.experience_capsule_store is None or self.experience_context is None:
            return []
        if self.skill_manager is None:
            raise RuntimeError("Experience Capsule proposals require SkillManager")
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
        trace_path = context.get("trace_path")
        records: list[dict[str, Any]] = []
        for proposal in proposals:
            if proposal.operation == "noop" or proposal.kind != "experience":
                continue
            description = proposal.description
            instructions = proposal.instructions
            if proposal.operation == "update":
                current = self.skill_manager.load_skill(proposal.skill_name or "")
                description = description or current.description
                instructions = instructions or current.instructions
            evidence_refs = [
                str(trace_path) if trace_path else "episode_trace_unavailable",
                *(f"step:{step}" for step in proposal.evidence_steps),
            ]
            applicability = [str(task_name)]
            if isinstance(task_set, str) and task_set:
                applicability.append(task_set)
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
            )
            self.experience_capsule_store.add_candidate(capsule)
            records.append(
                {
                    "capsule_id": capsule.capsule_id,
                    "state": capsule.state,
                    "skill_name": capsule.skill_name,
                    "source_identity": dict(capsule.source_identity),
                    "evidence_refs": list(capsule.evidence_refs),
                }
            )
        return records

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
        try:
            super().close()
        except Exception as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError("Failed to close Agent components: " + "; ".join(errors))


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


def _error_text(stage: str, error: Exception) -> str:
    return f"{stage}: {type(error).__name__}: {error}"
