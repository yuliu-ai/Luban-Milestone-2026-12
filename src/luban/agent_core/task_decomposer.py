import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from luban.agent_core.adaptive_prompt_skill import (
    AdaptivePromptSkill,
    CallMode,
    GenerateRequest,
    Role,
    SkillSpec,
)
from luban.agent_core.observation_keys import camera_images, task_description
from luban.agent_core.robocasa_atomic_skill_catalog import (
    RoboCasaAtomicSkillCatalog,
)
from luban.backends.llm.base import LLMBackend
from luban.exceptions import (
    ConfigError,
    PlannerOutputError,
    ProtocolSchemaError,
)
from luban.serialization import to_jsonable

TASK_DECOMPOSER_PROMPT = (
    "You create one detailed, bounded execution DAG for an\nem"
    "bodied manipulation episode. The official task instructi"
    "on defines every required task\ngoal. The initial camera "
    "views are evidence only for facts that are directly and\n"
    "unambiguously visible in those views.\n\nGoal coverage:\n- "
    "Preserve every explicit official task goal, including na"
    "med objects, required final\n  states, destinations, quan"
    "tities, and relations. Do not silently drop, weaken, or\n"
    "  replace an official goal with an inferred prerequisite"
    ".\n- A source or container phrase in the instruction desc"
    "ribes task semantics; it is not\n  proof that opening, cl"
    "osing, searching, navigating, or repositioning is requir"
    "ed.\n- Add a prerequisite only when it is an explicit off"
    "icial goal, or when the initial view\n  directly proves i"
    "ts state must change before an official goal can be achi"
    "eved.\n- If the initial view clearly proves an added prer"
    "equisite is already satisfied, omit it.\n\nInitial-view ev"
    "idence rules:\n- Do not infer hidden contents, object loc"
    "ations, container state, grasp state,\n  accessibility, f"
    "ailure, or completion from task wording, common sense, m"
    "issing pixels,\n  occlusion, or an unclear image.\n- Unkno"
    "wn is not evidence that a prerequisite is false. When ev"
    "idence is absent or\n  unclear, do not invent a mandatory"
    " prerequisite; defer the decision to the online\n  planne"
    "r after it re-observes the environment.\n- Available skil"
    "l names are capabilities, not observations of the world."
    "\n\nDAG rules:\n- Each node is one atomic, externally obser"
    "vable state transition with one observable\n  expected ou"
    "tcome.\n- When task_atomic_skill_catalog is non-empty, ev"
    "ery node must instantiate exactly one\n  listed capabilit"
    "y. Copy its exact name into atomic_skill_id, then bind t"
    "he named\n  objects, fixtures, direction, or target regio"
    "n in the instruction. Do not copy a\n  generic template u"
    "nchanged and do not invent a capability outside the cata"
    "log.\n- When atomic_skill_directory is non-empty, also se"
    "lect from it the atomic skills that\n  this node actually"
    " executes and copy their exact ids into atomic_skill_ids"
    " (zero or\n  more, unique). Pick only ids whose scope mat"
    "ches the node transition; prefer ids\n  whose execution_s"
    "tatus is not contract_only. Never attach the whole direc"
    "tory.\n- Separate distinct manipulation transitions inste"
    "ad of compressing a multi-stage\n  composite task into on"
    "e broad node. Use up to the configured eight-node limit,"
    " but do\n  not invent work merely to increase node count."
    "\n- Include only necessary dependency edges. Do not add l"
    "oops, alternatives,\n  verification-only nodes, duplicate"
    " goals, or compound actions.\n- The runtime executes one "
    "dependency-ready node at a time. The Planner may choose "
    "the\n  concrete skill for that node, while only verified "
    "completion unlocks downstream nodes.\n\nReturn JSON with e"
    'xactly this shape:\n{\n  "nodes": [\n    {\n      "id": "sho'
    'rt_unique_id",\n      "atomic_skill_id": "exact catalog s'
    'kill name",\n      "atomic_skill_ids": ["exact id from at'
    'omic_skill_directory"],\n      "instruction": "one atomic'
    ' action",\n      "dependencies": ["earlier_node_id"],\n   '
    '   "expected_outcome": "one observable state"\n    }\n  ]\n'
    "}\n"
)

RECOVERY_RECONCILIATION_PROMPT = (
    "You reconcile progress against one frozen\nembodied-task "
    "DAG. The DAG node IDs, instructions, dependencies, outco"
    "mes, and official\ntask goal are immutable. You must not "
    "add, delete, reorder, rename, rewrite, merge, or\nsplit n"
    "odes.\n\nYou receive several time-labelled camera groups. "
    "Each label states how many environment\nsteps and action "
    "chunks ago the group was observed. Treat older views as "
    "history, not as\nthe current state. Use a node status onl"
    "y when supported by the time-ordered visual\ntrajectory a"
    "nd execution evidence. Not visible is unknown, not faile"
    'd or absent.\n\nReturn JSON with exactly this shape:\n{\n  "'
    'node_updates": [\n    {\n      "id": "an existing frozen n'
    'ode id",\n      "status": "completed, in_progress, pendin'
    'g, or unknown",\n      "visible": true,\n      "evidence":'
    ' ["short time-grounded observation"]\n    }\n  ],\n  "failu'
    're_summary": "what blocked progress, without inventing h'
    'idden state",\n  "correctable_target": "smallest currentl'
    'y correctable target or empty string",\n  "critical_unkno'
    'wns": ["facts that require observation before acting"]\n}'
    "\n"
)

RECOVERY_PLAN_REVISION_PROMPT = (
    "You revise a mutable embodied-task plan after\npartial re"
    "covery. The official instruction is the only immutable g"
    "oal contract. The\nexisting DAG is a hypothesis and may c"
    "ontain a wrong prerequisite or miss a necessary\nstep. Us"
    "e the time-labelled images and bounded evidence bundle t"
    "o retain, add, delete,\nrewrite, or reorder nodes. Do not"
    " change, weaken, or add to the official goal.\n\nKeep 1..8"
    " atomic nodes. A completed status is allowed only for an"
    " unchanged node whose\nprior graph status was already com"
    "pleted; otherwise use in_progress, pending, or\nunknown. "
    "Verifier statements are advisory and disputed evidence s"
    "tays unknown.\nEvery new or rewritten node must use one t"
    "ask_atomic_skill_catalog entry and copy its\nexact name i"
    "nto atomic_skill_id. Preserve atomic_skill_id on an unch"
    "anged node.\nEvery new or rewritten node must also select"
    " its atomic_skill_ids from\natomic_skill_directory, and p"
    "reserve atomic_skill_ids on an unchanged node.\n\nReturn J"
    'SON with exactly this shape:\n{\n  "task_graph": {"nodes":'
    ' [{"id": "id", "atomic_skill_id": "catalog name",\n    "a'
    'tomic_skill_ids": ["directory id"],\n    "instruction": "'
    'atomic action",\n    "dependencies": [], "expected_outcom'
    'e": "observable state"}]},\n  "node_updates": [{"id": "id'
    '", "status": "completed, in_progress, pending, or unknow'
    'n",\n    "visible": true, "evidence": ["time-grounded evi'
    'dence"]}],\n  "revision_reason": "why the plan changed or'
    ' was retained",\n  "failure_summary": "current blocker wi'
    'thout hidden-state invention",\n  "correctable_target": "'
    'smallest current target or empty string",\n  "critical_un'
    'knowns": ["fact requiring observation"]\n}\n'
)

SELECTIVE_SUFFIX_REPLAN_PROMPT = (
    "You replace one frozen branch of an embodied\nTask Graph "
    "after the active node received three consecutive final f"
    "ailure judgments.\nThe physical environment, robot, objec"
    "ts, current observation, official goal, completed\nnodes,"
    " and non-frozen branches are preserved. Do not repeat or"
    " rewrite preserved nodes.\nUse the current observation, t"
    "he two-Verifier/Monitor evidence, and only the immediate"
    "ly\nprevious completed node's LongMemory and ActionMemory"
    ". Failed-node evidence is an\nuntrusted recovery bundle, "
    "not LongMemory.\nEvery replacement node must instantiate "
    "one supplied task_atomic_skill_catalog entry\nand copy it"
    "s exact name into atomic_skill_id. Every replacement nod"
    "e must also select\nits atomic_skill_ids from atomic_skil"
    "l_directory.\n\nReturn one or more atomic replacement node"
    "s. Their dependencies may reference preserved\nnode IDs o"
    "r other replacement IDs. Do not reference frozen IDs. Ke"
    "ep the merged graph\nwithin eight nodes. Return JSON with"
    ' exactly this shape:\n{\n  "replacement_nodes": [{"id": "n'
    'ew_id", "atomic_skill_id": "catalog name",\n    "atomic_s'
    'kill_ids": ["directory id"],\n    "instruction": "atomic '
    'action",\n    "dependencies": [], "expected_outcome": "ob'
    'servable state"}],\n  "revision_reason": "short evidence-'
    'grounded reason"\n}\n'
)


def _resolved_temporal_frames(
    context: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], str]:
    """决定本次修订喂给模型的帧序列，并标明来源。

    runtime 的 horizon-recovery 路径会提供真实的多帧环形缓存；pipeline 的
    uncertain-exhaustion 路径不提供。缺少多帧时退化为一帧"当前观测"，
    让修订继续执行，而不是整集以 exception 终止——2026-09-11 实测：三个
    demo episode 都在到达 uncertain 修订阈值后死于
    "recovery revision requires temporal frames"。
    """
    frames = context.get("temporal_frames")
    if isinstance(frames, list) and frames:
        return [frame for frame in frames if isinstance(frame, Mapping)], (
            "runtime_temporal_frames"
        )
    observation = context.get("current_observation")
    if isinstance(observation, Mapping) and observation:
        return (
            [
                {
                    "observation": observation,
                    "phase": "current",
                    "environment_steps": context.get("environment_steps"),
                    "action_chunks": context.get("action_chunks"),
                    "age_environment_steps": 0,
                    "age_action_chunks": 0,
                }
            ],
            "current_observation_fallback",
        )
    return [], "missing"


_NODE_FIELD_ALIASES = {
    "instruction": ("instruction", "description", "task", "goal", "subtask", "name"),
    "dependencies": ("dependencies", "depends_on", "deps", "prerequisites"),
    "expected_outcome": (
        "expected_outcome",
        "success_criteria",
        "expected_result",
        "postcondition",
        "outcome",
    ),
}


def _task_text(context: Mapping[str, Any]) -> str:
    return (
        task_description(context.get("initial_observation"), context.get("task"))
        or "Complete the requested task."
    )


def _sanitize_node_id(raw: Any, index: int, used: set[str]) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw).strip()) if raw is not None else ""
    text = text.strip("_")
    if not text:
        text = f"n{index + 1}"
    if not text[0].isalpha():
        text = f"n_{text}"
    text = text[:64]
    candidate, suffix = text, 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = text[: 64 - len(tail)] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def normalize_graph_payload(payload: Any, max_nodes: int) -> dict[str, Any]:
    """Coerce common model deviations into the strict task-graph schema.

    Handles wrapped graphs, aliased field names, non-identifier ids, dangling
    dependencies and graphs longer than ``max_nodes`` (consecutive nodes are
    merged so the step order is kept). Semantics are not invented: a node
    without an instruction is still rejected by ``validate_graph``.
    """
    if isinstance(payload, Mapping) and "nodes" not in payload:
        for key in ("task_graph", "graph", "plan"):
            inner = payload.get(key)
            if isinstance(inner, Mapping) and "nodes" in inner:
                payload = inner
                break
            if isinstance(inner, list):
                payload = {"nodes": inner}
                break
    if isinstance(payload, list):
        payload = {"nodes": payload}
    nodes = payload.get("nodes") if isinstance(payload, Mapping) else None
    if not isinstance(nodes, list):
        raise ProtocolSchemaError("protocol_schema_error: Task graph has no node list")
    used: set[str] = set()
    id_map: dict[str, str] = {}
    staged: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if isinstance(node, str):
            node = {"instruction": node}
        if not isinstance(node, Mapping):
            raise ProtocolSchemaError(
                f"protocol_schema_error: Task graph node {index} must be an object"
            )
        fields: dict[str, Any] = {}
        for target, aliases in _NODE_FIELD_ALIASES.items():
            for alias in aliases:
                if alias in node and node[alias] not in (None, ""):
                    fields[target] = node[alias]
                    break
        raw_id = node.get("id", node.get("node_id"))
        new_id = _sanitize_node_id(raw_id, index, used)
        if raw_id is not None:
            id_map.setdefault(str(raw_id).strip(), new_id)
        id_map.setdefault(new_id, new_id)
        instruction = fields.get("instruction")
        instruction = instruction.strip() if isinstance(instruction, str) else ""
        outcome = fields.get("expected_outcome")
        outcome = outcome.strip() if isinstance(outcome, str) else ""
        dependencies = fields.get("dependencies", [])
        if isinstance(dependencies, (str, int)):
            dependencies = [dependencies]
        if not isinstance(dependencies, list):
            dependencies = []
        normalized: dict[str, Any] = {
            "id": new_id,
            "instruction": instruction,
            "dependencies": [str(item).strip() for item in dependencies],
            "expected_outcome": outcome
            or (f"{instruction} is done" if instruction else ""),
        }
        if "atomic_skill_id" in node:
            normalized["atomic_skill_id"] = node["atomic_skill_id"]
        skill_ids = node.get("atomic_skill_ids")
        if isinstance(skill_ids, list):
            normalized["atomic_skill_ids"] = list(dict.fromkeys(skill_ids))
        elif skill_ids is not None:
            normalized["atomic_skill_ids"] = skill_ids
        staged.append(normalized)
    known = {node["id"] for node in staged}
    for node in staged:
        mapped = []
        for dependency in node["dependencies"]:
            target = id_map.get(dependency, dependency)
            if target in known and target != node["id"] and target not in mapped:
                mapped.append(target)
        node["dependencies"] = mapped
    if len(staged) > max_nodes:
        staged = _merge_consecutive_nodes(staged, max_nodes)
    return {"nodes": staged}


def _merge_consecutive_nodes(
    nodes: list[dict[str, Any]], max_nodes: int
) -> list[dict[str, Any]]:
    size, extra = divmod(len(nodes), max_nodes)
    groups: list[list[dict[str, Any]]] = []
    start = 0
    for index in range(max_nodes):
        end = start + size + (1 if index < extra else 0)
        groups.append(nodes[start:end])
        start = end
    owner = {node["id"]: group[0]["id"] for group in groups for node in group}
    merged: list[dict[str, Any]] = []
    for group in groups:
        group_id = group[0]["id"]
        dependencies: list[str] = []
        for node in group:
            for dependency in node["dependencies"]:
                target = owner.get(dependency)
                if target and target != group_id and target not in dependencies:
                    dependencies.append(target)
        merged.append(
            {
                "id": group_id,
                "instruction": "; then ".join(node["instruction"] for node in group),
                "dependencies": dependencies,
                "expected_outcome": "; ".join(
                    node["expected_outcome"] for node in group
                ),
            }
        )
    return merged


class TaskDecomposer:
    """Create and strictly validate one immutable episode task DAG."""

    def __init__(
        self,
        backend: LLMBackend,
        camera_keys: list[str] | None = None,
        max_images: int = 3,
        max_nodes: int = 32,
        system_prompt: str = TASK_DECOMPOSER_PROMPT,
        max_tokens: int = 3072,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        response_format_mode: str = "json_schema",
        prompt_skill: AdaptivePromptSkill | None = None,
        task_atomic_skill_catalog: RoboCasaAtomicSkillCatalog | None = None,
        recovery_max_frames: int = 4,
        recovery_images_per_frame: int = 3,
    ) -> None:
        resolved_keys = ["images", "head_rgb"] if camera_keys is None else camera_keys
        if not isinstance(resolved_keys, list) or any(
            not isinstance(key, str) or not key for key in resolved_keys
        ):
            raise ConfigError("TaskDecomposer camera_keys must be non-empty strings")
        if len(set(resolved_keys)) != len(resolved_keys):
            raise ConfigError("TaskDecomposer camera_keys must be unique")
        if (
            not isinstance(max_images, int)
            or isinstance(max_images, bool)
            or max_images < 0
        ):
            raise ConfigError(
                "TaskDecomposer max_images must be a non-negative integer"
            )
        if (
            not isinstance(max_nodes, int)
            or isinstance(max_nodes, bool)
            or max_nodes < 1
        ):
            raise ConfigError("TaskDecomposer max_nodes must be a positive integer")
        self.backend = backend
        self.camera_keys = list(resolved_keys)
        self.max_images = max_images
        self.max_nodes = max_nodes
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body or {}
        if response_format_mode not in {"json_schema", "json_object"}:
            raise ConfigError("response_format_mode must be json_schema or json_object")
        self.response_format_mode = response_format_mode
        if (
            not isinstance(recovery_max_frames, int)
            or isinstance(recovery_max_frames, bool)
            or recovery_max_frames <= 0
        ):
            raise ConfigError("recovery_max_frames must be a positive integer")
        if (
            not isinstance(recovery_images_per_frame, int)
            or isinstance(recovery_images_per_frame, bool)
            or recovery_images_per_frame <= 0
        ):
            raise ConfigError("recovery_images_per_frame must be a positive integer")
        self.recovery_max_frames = recovery_max_frames
        self.recovery_images_per_frame = recovery_images_per_frame
        self.prompt_skill = prompt_skill
        self.task_atomic_skill_catalog = task_atomic_skill_catalog
        self.last_atomic_skill_manifest: list[dict[str, Any]] = []
        self.last_atomic_skill_directory: list[dict[str, Any]] = []
        self.last_prompt_manifest: dict[str, Any] | None = None
        self.last_model_usage: dict[str, Any] | None = None
        self.last_graph_repair: dict[str, Any] | None = None

    def decompose(self, context: Mapping[str, Any]) -> dict[str, Any]:
        observation = context.get("initial_observation")
        atomic_manifest = self._atomic_skill_manifest(context.get("task"))
        self.last_atomic_skill_manifest = deepcopy(atomic_manifest)
        atomic_directory = self._atomic_skill_directory(context)
        prompt = json.dumps(
            to_jsonable(
                {
                    "task": context.get("task"),
                    "official_task_instruction": task_description(observation),
                    "available_skills": self._available_skills(observation),
                    "task_atomic_skill_catalog": atomic_manifest,
                    "atomic_skill_directory": atomic_directory,
                    **(
                        {"simulator_state": observation["simulator_object_state"]}
                        if isinstance(observation, Mapping)
                        and "simulator_object_state" in observation
                        else {}
                    ),
                },
                max_sequence_items=(
                    1_000_000
                    if isinstance(observation, Mapping)
                    and "simulator_object_state" in observation
                    else 32
                ),
            ),
            ensure_ascii=False,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in self._images(observation):
            content.append({"type": "image_url", "image_url": {"url": image}})
        simulator_guidance = (
            "\nThe simulator_state field contains the full provided initial scene "
            "configuration and simulator kinematics. Use it as evidence alongside "
            "the views when identifying objects, fixtures, and spatial relationships."
            if isinstance(observation, Mapping)
            and "simulator_object_state" in observation
            else ""
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt + simulator_guidance},
            {"role": "user", "content": content},
        ]
        if self.prompt_skill is not None:
            skills = tuple(
                SkillSpec(skill_id=skill)
                for skill in self._available_skills(observation)
            )
            generated = self.prompt_skill.generate(
                GenerateRequest(
                    role=Role.TASK_DECOMPOSER,
                    task={
                        "task": context.get("task"),
                        "official_task_instruction": (
                            observation.get("annotation.human.task_description")
                            if isinstance(observation, Mapping)
                            else None
                        ),
                    },
                    observations=(
                        observation if isinstance(observation, Mapping) else {}
                    ),
                    task_state={
                        "available_skills": [skill.skill_id for skill in skills],
                        "task_atomic_skill_catalog": atomic_manifest,
                        "atomic_skill_directory": atomic_directory,
                    },
                    execution_history=(),
                    failure_information=None,
                    available_skills=skills,
                    prompt_version=self.prompt_skill.default_version,
                    call_mode=CallMode.INITIAL_TASK_DECOMPOSITION,
                    prebuilt_messages=tuple(messages),
                )
            )
            messages = [dict(message) for message in generated.messages]
            self.last_prompt_manifest = {
                "content_hash": generated.content_hash,
                "call_mode": generated.call_mode.value,
                "task_shape": generated.task_shape.value,
                "selected_blocks": [
                    {
                        "block_id": block.block_id,
                        "version": block.version,
                        "digest": block.digest,
                    }
                    for block in generated.selected_blocks
                ],
                "version_manifest": dict(generated.version_manifest),
                "prompt_audit": dict(self.prompt_skill.last_audit or {}),
            }
        response = self.backend.complete(
            {
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": (
                    {"type": "json_object"}
                    if self.response_format_mode == "json_object"
                    else {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "episode_task_graph",
                            "schema": self._schema(atomic_manifest, atomic_directory),
                        },
                    }
                ),
                "extra_body": self.extra_body,
            }
        )
        self.last_model_usage = self._response_usage(response)
        graph = self._graph_from_response(response, messages, context)
        graph = self._validate_atomic_skill_bindings(graph, atomic_manifest)
        return self._validate_atomic_skill_ids(graph, atomic_directory)

    def _graph_from_response(
        self,
        response: Any,
        messages: list[dict[str, Any]],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate the model graph; normalize, re-ask once, then fall back.

        A malformed graph (too many nodes, numeric ids, aliased fields) used to
        abort the episode before the first action. The repair stage is kept in
        ``last_graph_repair`` so evaluations can report it.
        """
        self.last_graph_repair = None
        try:
            return self.validate_graph(
                self._parse_response(response), max_nodes=self.max_nodes
            )
        except PlannerOutputError as first_error:
            error: PlannerOutputError = first_error
        try:
            graph = self.validate_graph(
                normalize_graph_payload(self._parse_response(response), self.max_nodes),
                max_nodes=self.max_nodes,
            )
            self.last_graph_repair = {"stage": "normalized", "error": str(error)}
            return graph
        except PlannerOutputError as normalization_error:
            error = normalization_error
        retry_messages = [
            *messages,
            {"role": "assistant", "content": self._response_content(response)},
            {
                "role": "user",
                "content": (
                    f"The task graph was rejected: {error}. Return only a JSON "
                    f'object {{"nodes": [...]}} with 1 to {self.max_nodes} nodes. '
                    "Each node has exactly id, instruction, dependencies and "
                    "expected_outcome; ids match ^[A-Za-z][A-Za-z0-9_-]{0,63}$ "
                    "and dependencies only name other node ids."
                ),
            },
        ]
        try:
            retried = self.backend.complete(
                {
                    "messages": retry_messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "response_format": {"type": "json_object"},
                    "extra_body": self.extra_body,
                }
            )
            graph = self.validate_graph(
                normalize_graph_payload(self._parse_response(retried), self.max_nodes),
                max_nodes=self.max_nodes,
            )
            self.last_graph_repair = {"stage": "retried", "error": str(error)}
            return graph
        except PlannerOutputError as retry_error:
            error = retry_error
        task_text = _task_text(context)
        self.last_graph_repair = {
            "stage": "single_node_fallback",
            "error": str(error),
        }
        return self.validate_graph(
            {
                "nodes": [
                    {
                        "id": "task",
                        "instruction": task_text,
                        "dependencies": [],
                        "expected_outcome": f"The task is complete: {task_text}",
                    }
                ]
            },
            max_nodes=self.max_nodes,
        )

    @staticmethod
    def _response_content(response: Any) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else json.dumps(content)

    def reconcile_progress(self, context: Mapping[str, Any]) -> dict[str, Any]:
        graph = context.get("frozen_task_graph")
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if not isinstance(nodes, list) or not nodes:
            raise PlannerOutputError(
                "recovery reconciliation requires a frozen task graph"
            )
        node_ids = {
            str(node.get("id"))
            for node in nodes
            if isinstance(node, Mapping) and isinstance(node.get("id"), str)
        }
        if len(node_ids) != len(nodes):
            raise PlannerOutputError("frozen task graph contains invalid node IDs")
        temporal_frames, frames_source = _resolved_temporal_frames(context)
        if not temporal_frames:
            raise PlannerOutputError("recovery reconciliation requires temporal frames")
        selected_frames = temporal_frames[-self.recovery_max_frames :]
        frame_metadata: list[dict[str, Any]] = []
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(
                    to_jsonable(
                        {
                            "task": context.get("task"),
                            "frozen_task_graph": graph,
                            "recovery_handoff": context.get("recovery_handoff"),
                            "invariant": "node identities and meanings must not change",
                        }
                    ),
                    ensure_ascii=False,
                ),
            }
        ]
        current_observation: Mapping[str, Any] = {}
        for index, frame in enumerate(selected_frames):
            if not isinstance(frame, Mapping):
                continue
            observation = frame.get("observation")
            if not isinstance(observation, Mapping):
                continue
            current_observation = observation
            metadata = {
                "frame_index": index,
                "phase": frame.get("phase", "historical"),
                "environment_steps": frame.get("environment_steps"),
                "action_chunks": frame.get("action_chunks"),
                "age_environment_steps": frame.get("age_environment_steps"),
                "age_action_chunks": frame.get("age_action_chunks"),
            }
            frame_metadata.append(metadata)
            content.append(
                {
                    "type": "text",
                    "text": "TEMPORAL_FRAME "
                    + json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                }
            )
            for image in self._images(observation)[: self.recovery_images_per_frame]:
                content.append({"type": "image_url", "image_url": {"url": image}})
        if not frame_metadata:
            raise PlannerOutputError("temporal frames contain no valid observations")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": RECOVERY_RECONCILIATION_PROMPT},
            {"role": "user", "content": content},
        ]
        if self.prompt_skill is not None:
            skills = tuple(
                SkillSpec(skill_id=skill)
                for skill in self._available_skills(current_observation)
            )
            generated = self.prompt_skill.generate(
                GenerateRequest(
                    role=Role.TASK_DECOMPOSER,
                    task={
                        "task": context.get("task"),
                        "official_task_instruction": context.get(
                            "official_task_instruction"
                        ),
                    },
                    observations={},
                    task_state={
                        "task_shape": "composite",
                        "task_graph": graph,
                        "temporal_frame_metadata": frame_metadata,
                        "recovery_handoff": context.get("recovery_handoff"),
                    },
                    execution_history=(),
                    failure_information={
                        "scope": "progress_reconciliation",
                        "failure_class": "horizon_partial_recovery",
                        "summary": context.get("recovery_handoff"),
                    },
                    available_skills=skills,
                    prompt_version=self.prompt_skill.default_version,
                    call_mode=CallMode.RECOVERY_PROGRESS_RECONCILIATION,
                    prebuilt_messages=tuple(messages),
                )
            )
            messages = [dict(message) for message in generated.messages]
            self.last_prompt_manifest = {
                "content_hash": generated.content_hash,
                "call_mode": generated.call_mode.value,
                "task_shape": generated.task_shape.value,
                "selected_blocks": [
                    {
                        "block_id": block.block_id,
                        "version": block.version,
                        "digest": block.digest,
                    }
                    for block in generated.selected_blocks
                ],
                "version_manifest": dict(generated.version_manifest),
                "prompt_audit": dict(self.prompt_skill.last_audit or {}),
            }
        response = self.backend.complete(
            {
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
                "extra_body": self.extra_body,
            }
        )
        self.last_model_usage = self._response_usage(response)
        result = self.validate_reconciliation(self._parse_response(response), node_ids)
        result["temporal_frames_source"] = frames_source
        result["temporal_frame_count"] = len(frame_metadata)
        return result

    def revise_plan_graph(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Revise the plan hypothesis while keeping the official goal immutable."""
        graph = context.get("current_task_graph")
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if not isinstance(nodes, list) or not nodes:
            raise PlannerOutputError("recovery revision requires a current task graph")
        official_instruction = context.get("official_task_instruction")
        if (
            not isinstance(official_instruction, str)
            or not official_instruction.strip()
        ):
            raise PlannerOutputError(
                "recovery revision requires official task instruction"
            )
        atomic_manifest = deepcopy(self.last_atomic_skill_manifest)
        atomic_directory = deepcopy(self.last_atomic_skill_directory)
        temporal_frames, frames_source = _resolved_temporal_frames(context)
        if not temporal_frames:
            raise PlannerOutputError("recovery revision requires temporal frames")
        selected_frames = temporal_frames[-self.recovery_max_frames :]
        frame_metadata: list[dict[str, Any]] = []
        current_observation: Mapping[str, Any] = {}
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(
                    to_jsonable(
                        {
                            "immutable_goal_contract": {
                                "official_instruction": official_instruction.strip(),
                                "immutable": True,
                            },
                            "current_mutable_task_graph": graph,
                            "recovery_evidence_bundle": context.get(
                                "recovery_evidence_bundle"
                            ),
                            "task_atomic_skill_catalog": atomic_manifest,
                        }
                    ),
                    ensure_ascii=False,
                ),
            }
        ]
        for index, frame in enumerate(selected_frames):
            if not isinstance(frame, Mapping):
                continue
            observation = frame.get("observation")
            if not isinstance(observation, Mapping):
                continue
            current_observation = observation
            metadata = {
                "frame_index": index,
                "phase": frame.get("phase", "historical"),
                "environment_steps": frame.get("environment_steps"),
                "action_chunks": frame.get("action_chunks"),
                "age_environment_steps": frame.get("age_environment_steps"),
                "age_action_chunks": frame.get("age_action_chunks"),
            }
            frame_metadata.append(metadata)
            content.append(
                {
                    "type": "text",
                    "text": "TEMPORAL_FRAME "
                    + json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                }
            )
            for image in self._images(observation)[: self.recovery_images_per_frame]:
                content.append({"type": "image_url", "image_url": {"url": image}})
        if not frame_metadata:
            raise PlannerOutputError("temporal frames contain no valid observations")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": RECOVERY_PLAN_REVISION_PROMPT},
            {"role": "user", "content": content},
        ]
        if self.prompt_skill is not None:
            generated = self.prompt_skill.generate(
                GenerateRequest(
                    role=Role.TASK_DECOMPOSER,
                    task={"official_task_instruction": official_instruction.strip()},
                    observations={},
                    task_state={
                        "task_shape": "composite",
                        "goal_contract": {
                            "official_instruction": official_instruction.strip(),
                            "immutable": True,
                        },
                        "task_graph": graph,
                        "temporal_frame_metadata": frame_metadata,
                        "recovery_evidence_bundle": context.get(
                            "recovery_evidence_bundle"
                        ),
                        "task_atomic_skill_catalog": atomic_manifest,
                        "atomic_skill_directory": atomic_directory,
                    },
                    execution_history=tuple(
                        item
                        for item in (
                            context.get("recovery_evidence_bundle", {}).get(
                                "evidence_ledger", []
                            )
                            if isinstance(
                                context.get("recovery_evidence_bundle"), Mapping
                            )
                            else []
                        )
                        if isinstance(item, Mapping)
                    ),
                    failure_information={
                        "scope": "plan_graph_revision",
                        "failure_class": "horizon_partial_recovery",
                        "summary": context.get("recovery_evidence_bundle"),
                    },
                    available_skills=tuple(
                        SkillSpec(skill_id=skill)
                        for skill in self._available_skills(current_observation)
                    ),
                    prompt_version=self.prompt_skill.default_version,
                    call_mode=CallMode.RECOVERY_PLAN_REVISION,
                    prebuilt_messages=tuple(messages),
                )
            )
            messages = [dict(message) for message in generated.messages]
            self.last_prompt_manifest = {
                "content_hash": generated.content_hash,
                "call_mode": generated.call_mode.value,
                "task_shape": generated.task_shape.value,
                "selected_blocks": [
                    {
                        "block_id": block.block_id,
                        "version": block.version,
                        "digest": block.digest,
                    }
                    for block in generated.selected_blocks
                ],
                "version_manifest": dict(generated.version_manifest),
                "prompt_audit": dict(self.prompt_skill.last_audit or {}),
            }
        response = self.backend.complete(
            {
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
                "extra_body": self.extra_body,
            }
        )
        self.last_model_usage = self._response_usage(response)
        result = self.validate_plan_revision(
            self._parse_response(response),
            cast(Mapping[str, Any], graph),
            max_nodes=min(self.max_nodes, 8),
        )
        result["task_graph"] = self._validate_atomic_skill_bindings(
            result["task_graph"], atomic_manifest
        )
        result["task_graph"] = self._validate_atomic_skill_ids(
            result["task_graph"], atomic_directory
        )
        result["temporal_frames_source"] = frames_source
        result["temporal_frame_count"] = len(frame_metadata)
        return result

    def replan_frozen_branch(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Replace only a deterministically frozen node branch."""
        graph = context.get("current_task_graph")
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
        if not isinstance(nodes, list) or not nodes:
            raise PlannerOutputError("selective replan requires a current task graph")
        frozen = context.get("frozen_node_ids")
        if (
            not isinstance(frozen, list)
            or not frozen
            or any(not isinstance(node_id, str) for node_id in frozen)
        ):
            raise PlannerOutputError("selective replan requires frozen_node_ids")
        frozen_ids = set(frozen)
        old_ids = {
            str(node.get("id"))
            for node in nodes
            if isinstance(node, Mapping) and isinstance(node.get("id"), str)
        }
        if not frozen_ids <= old_ids:
            raise PlannerOutputError("selective replan references unknown frozen nodes")
        official_instruction = context.get("official_task_instruction")
        if (
            not isinstance(official_instruction, str)
            or not official_instruction.strip()
        ):
            raise PlannerOutputError(
                "selective replan requires official task instruction"
            )
        observation = context.get("current_observation")
        if not isinstance(observation, Mapping):
            raise PlannerOutputError("selective replan requires current observation")
        atomic_manifest = deepcopy(self.last_atomic_skill_manifest)
        atomic_directory = deepcopy(self.last_atomic_skill_directory)

        preserved_nodes = [
            self._semantic_node(node)
            for node in nodes
            if isinstance(node, Mapping) and node.get("id") not in frozen_ids
        ]
        replacement_limit = min(self.max_nodes, 8) - len(preserved_nodes)
        if replacement_limit < 1:
            raise ProtocolSchemaError(
                "protocol_schema_error: no Task Graph capacity for branch replacement"
            )
        payload = {
            "immutable_goal_contract": official_instruction.strip(),
            "preserved_nodes": preserved_nodes,
            "frozen_node_ids": list(frozen),
            "frozen_nodes": [
                self._semantic_node(node)
                for node in nodes
                if isinstance(node, Mapping) and node.get("id") in frozen_ids
            ],
            "previous_node_memory": context.get("previous_node_memory"),
            "recovery_evidence_bundle": context.get("recovery_evidence_bundle"),
            "maximum_replacement_nodes": replacement_limit,
            "task_atomic_skill_catalog": atomic_manifest,
            "atomic_skill_directory": atomic_directory,
        }
        # 恢复顾问的建议必须真正进入 replan 载荷；只放进 replan_context 而不读，
        # 会让 C/D/E 与 B 行为等价（2026-09-11 实测发现的 C 臂空转）。
        # 没有建议时不加键，B 的载荷保持逐字节一致。
        advisor_advice = context.get("recovery_advisor_advice")
        if isinstance(advisor_advice, Mapping) and advisor_advice:
            payload["recovery_advisor_advice"] = deepcopy(dict(advisor_advice))
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": json.dumps(to_jsonable(payload), ensure_ascii=False),
            }
        ]
        for image in self._images(observation):
            content.append({"type": "image_url", "image_url": {"url": image}})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SELECTIVE_SUFFIX_REPLAN_PROMPT},
            {"role": "user", "content": content},
        ]
        if self.prompt_skill is not None:
            generated = self.prompt_skill.generate(
                GenerateRequest(
                    role=Role.TASK_DECOMPOSER,
                    task={"official_task_instruction": official_instruction.strip()},
                    observations={},
                    task_state={
                        "task_shape": "composite",
                        "task_graph": graph,
                        "frozen_node_ids": list(frozen),
                        "previous_node_memory": context.get("previous_node_memory"),
                        "recovery_evidence_bundle": context.get(
                            "recovery_evidence_bundle"
                        ),
                        "task_atomic_skill_catalog": atomic_manifest,
                        "atomic_skill_directory": atomic_directory,
                    },
                    execution_history=(),
                    failure_information={
                        "scope": "selective_branch_replan",
                        "failure_class": "three_consecutive_node_failures",
                        "summary": context.get("recovery_evidence_bundle"),
                    },
                    available_skills=tuple(
                        SkillSpec(skill_id=skill)
                        for skill in self._available_skills(observation)
                    ),
                    prompt_version=self.prompt_skill.default_version,
                    call_mode=CallMode.RECOVERY_PLAN_REVISION,
                    prebuilt_messages=tuple(messages),
                )
            )
            messages = [dict(message) for message in generated.messages]
            self.last_prompt_manifest = {
                "content_hash": generated.content_hash,
                "call_mode": generated.call_mode.value,
                "task_shape": generated.task_shape.value,
                "selected_blocks": [
                    {
                        "block_id": block.block_id,
                        "version": block.version,
                        "digest": block.digest,
                    }
                    for block in generated.selected_blocks
                ],
                "version_manifest": dict(generated.version_manifest),
                "prompt_audit": dict(self.prompt_skill.last_audit or {}),
            }
        response = self.backend.complete(
            {
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": (
                    {"type": "json_object"}
                    if self.response_format_mode == "json_object"
                    else {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "selective_task_graph_branch_replan",
                            "schema": self._suffix_replan_schema(
                                replacement_limit, atomic_manifest, atomic_directory
                            ),
                        },
                    }
                ),
                "extra_body": self.extra_body,
            }
        )
        self.last_model_usage = self._response_usage(response)
        parsed = self._parse_response(response)
        if set(parsed) != {"replacement_nodes", "revision_reason"}:
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan response fields mismatch"
            )
        replacement_nodes = parsed.get("replacement_nodes")
        reason = parsed.get("revision_reason")
        if not isinstance(replacement_nodes, list) or not replacement_nodes:
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan requires replacement nodes"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolSchemaError(
                "protocol_schema_error: selective replan requires revision_reason"
            )
        preserved_ids = {node["id"] for node in preserved_nodes}
        taken_ids = set(preserved_ids) | {str(item) for item in frozen_ids}
        id_rewrites: dict[str, str] = {}
        replacements: list[Any] = []
        for node in replacement_nodes:
            if not isinstance(node, Mapping):
                raise ProtocolSchemaError(
                    "protocol_schema_error: replacement node must be an object"
                )
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id.strip():
                raise ProtocolSchemaError(
                    "protocol_schema_error: replacement node requires a non-empty id"
                )
            candidate = node_id
            suffix = 1
            while candidate in taken_ids:
                suffix += 1
                candidate = f"{node_id}-r{suffix}"
            taken_ids.add(candidate)
            if candidate != node_id:
                id_rewrites[node_id] = candidate
            replacements.append({**dict(node), "id": candidate})
        if id_rewrites:
            # planner 复用了已保留/冻结节点的 id：重命名成唯一的 ``-rN`` 版本并
            # 在依赖里同步改名，而不是让整个 episode 因一个命名冲突终止
            # （真机 run21）。最终 id 反映在 replacement_node_ids 里，可与
            # planner 原始输出对照审计。
            rewritten: list[Any] = []
            for node in replacements:
                dependencies = node.get("dependencies")
                if isinstance(dependencies, list):
                    node = {
                        **node,
                        "dependencies": [
                            id_rewrites.get(str(dependency), dependency)
                            for dependency in dependencies
                        ],
                    }
                rewritten.append(node)
            replacements = rewritten
        replacement_ids = [str(node["id"]) for node in replacements]
        merged = self.validate_graph(
            {"nodes": [*preserved_nodes, *replacements]},
            max_nodes=min(self.max_nodes, 8),
        )
        merged = self._validate_atomic_skill_bindings(merged, atomic_manifest)
        merged = self._validate_atomic_skill_ids(merged, atomic_directory)
        merged["revision"] = int(cast(Mapping[str, Any], graph).get("revision", 0)) + 1
        return {
            "task_graph": merged,
            "replaced_node_ids": list(frozen),
            "replacement_node_ids": [str(node_id) for node_id in replacement_ids],
            "replacement_lineage": {
                str(node_id): list(frozen) for node_id in replacement_ids
            },
            "revision_reason": reason.strip()[:2048],
        }

    @classmethod
    def validate_plan_revision(
        cls, payload: Any, old_graph: Mapping[str, Any], *, max_nodes: int = 8
    ) -> dict[str, Any]:
        required = {
            "task_graph",
            "node_updates",
            "revision_reason",
            "failure_summary",
            "correctable_target",
            "critical_unknowns",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise PlannerOutputError("plan revision response has invalid fields")
        graph = cls.validate_graph(
            payload.get("task_graph"),
            max_nodes=max_nodes,
            allow_runtime_metadata=True,
        )
        graph["revision"] = int(old_graph.get("revision", 0)) + 1
        old_completed = {
            str(node.get("id"))
            for node in old_graph.get("nodes", [])
            if isinstance(node, Mapping) and node.get("status") == "completed"
        }
        node_ids = {str(node["id"]) for node in graph["nodes"]}
        updates = payload.get("node_updates")
        if not isinstance(updates, list):
            raise PlannerOutputError("plan revision node_updates must be a list")
        by_id: dict[str, dict[str, Any]] = {}
        for update in updates:
            if not isinstance(update, Mapping) or set(update) != {
                "id",
                "status",
                "visible",
                "evidence",
            }:
                raise PlannerOutputError("plan revision node update has invalid fields")
            node_id = update.get("id")
            status = update.get("status")
            evidence = update.get("evidence")
            if node_id not in node_ids or node_id in by_id:
                raise PlannerOutputError(
                    "plan revision references unknown or duplicate node"
                )
            if status not in {"completed", "in_progress", "pending", "unknown"}:
                raise PlannerOutputError("plan revision has invalid node status")
            if status == "completed" and node_id not in old_completed:
                raise PlannerOutputError(
                    "recovery cannot newly promote a node to completed"
                )
            if not isinstance(update.get("visible"), bool):
                raise PlannerOutputError("plan revision visible must be boolean")
            if not isinstance(evidence, list) or any(
                not isinstance(item, str) for item in evidence
            ):
                raise PlannerOutputError("plan revision evidence must be a string list")
            by_id[str(node_id)] = {
                "status": status,
                "visible": update["visible"],
                "evidence": [item[:512] for item in evidence[:6]],
            }
        for node in graph["nodes"]:
            update = by_id.get(node["id"])
            if update is not None:
                node.update(update)
        text_fields = ("revision_reason", "failure_summary", "correctable_target")
        if any(not isinstance(payload.get(key), str) for key in text_fields):
            raise PlannerOutputError("plan revision summaries must be strings")
        unknowns = payload.get("critical_unknowns")
        if not isinstance(unknowns, list) or any(
            not isinstance(item, str) for item in unknowns
        ):
            raise PlannerOutputError("plan revision critical_unknowns must be strings")
        return {
            "task_graph": graph,
            "node_updates": [
                {"id": node_id, **value} for node_id, value in by_id.items()
            ],
            "revision_reason": payload["revision_reason"][:2048],
            "failure_summary": payload["failure_summary"][:2048],
            "correctable_target": payload["correctable_target"][:1024],
            "critical_unknowns": [item[:512] for item in unknowns[:12]],
        }

    @staticmethod
    def validate_reconciliation(payload: Any, node_ids: set[str]) -> dict[str, Any]:
        required = {
            "node_updates",
            "failure_summary",
            "correctable_target",
            "critical_unknowns",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise PlannerOutputError("reconciliation response has invalid fields")
        updates = payload.get("node_updates")
        if not isinstance(updates, list):
            raise PlannerOutputError("reconciliation node_updates must be a list")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, update in enumerate(updates):
            if not isinstance(update, Mapping) or set(update) != {
                "id",
                "status",
                "visible",
                "evidence",
            }:
                raise PlannerOutputError(
                    f"reconciliation node update {index} has invalid fields"
                )
            node_id = update.get("id")
            status = update.get("status")
            evidence = update.get("evidence")
            visible = update.get("visible")
            if node_id not in node_ids or node_id in seen:
                raise PlannerOutputError(
                    f"reconciliation referenced unknown or duplicate node: {node_id!r}"
                )
            if status not in {"completed", "in_progress", "pending", "unknown"}:
                raise PlannerOutputError(f"invalid reconciliation status: {status!r}")
            if not isinstance(visible, bool):
                raise PlannerOutputError("reconciliation visible must be boolean")
            if not isinstance(evidence, list) or any(
                not isinstance(item, str) for item in evidence
            ):
                raise PlannerOutputError(
                    "reconciliation evidence must be a string list"
                )
            seen.add(node_id)
            normalized.append(
                {
                    "id": node_id,
                    "status": status,
                    "visible": visible,
                    "evidence": [item[:512] for item in evidence[:6]],
                }
            )
        unknowns = payload.get("critical_unknowns")
        if not isinstance(unknowns, list) or any(
            not isinstance(item, str) for item in unknowns
        ):
            raise PlannerOutputError("critical_unknowns must be a string list")
        failure_summary = payload.get("failure_summary")
        correctable_target = payload.get("correctable_target")
        if not isinstance(failure_summary, str) or not isinstance(
            correctable_target, str
        ):
            raise PlannerOutputError("reconciliation summaries must be strings")
        return {
            "node_updates": normalized,
            "failure_summary": failure_summary[:2048],
            "correctable_target": correctable_target[:1024],
            "critical_unknowns": [item[:512] for item in unknowns[:12]],
        }

    @staticmethod
    def _response_usage(response: Any) -> dict[str, Any] | None:
        if not isinstance(response, Mapping):
            return None
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return None
        return cast(
            dict[str, Any],
            to_jsonable(
                {
                    key: response.get(key)
                    for key in ("id", "model")
                    if response.get(key) is not None
                }
                | {"usage": dict(usage)}
            ),
        )

    @classmethod
    def validate_graph(
        cls,
        graph: Any,
        *,
        max_nodes: int = 32,
        allow_runtime_metadata: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(graph, Mapping):
            raise ProtocolSchemaError(
                "protocol_schema_error: Task graph must be an object"
            )
        graph_fields = set(graph)
        required_graph_fields = {"nodes"}
        permitted_graph_metadata = {
            "schema_version",
            "revision",
            "atomic_skill_bindings_validated",
        }
        missing_graph_fields = required_graph_fields - graph_fields
        unexpected_graph_fields = graph_fields - required_graph_fields
        if allow_runtime_metadata:
            unexpected_graph_fields -= permitted_graph_metadata
        if missing_graph_fields or unexpected_graph_fields:
            raise ProtocolSchemaError(
                "protocol_schema_error: Task graph fields mismatch "
                f"missing={sorted(missing_graph_fields)} "
                f"unexpected={sorted(unexpected_graph_fields)}"
            )
        nodes = graph.get("nodes")
        if not isinstance(nodes, list) or not nodes or len(nodes) > max_nodes:
            raise ProtocolSchemaError(
                f"protocol_schema_error: Task graph must contain 1..{max_nodes} nodes"
            )
        normalized: list[dict[str, Any]] = []
        ids: list[str] = []
        required = {"id", "instruction", "dependencies", "expected_outcome"}
        semantic_metadata = {"atomic_skill_id", "atomic_skill_ids"}
        runtime_metadata = {
            "status",
            "order",
            "visible",
            "evidence",
            "atomic_skill_origin",
            "atomic_skill_family",
            "atomic_skill_instruction",
            "atomic_skill_expected_postcondition",
        }
        for index, node in enumerate(nodes):
            if not isinstance(node, Mapping):
                raise ProtocolSchemaError(
                    f"protocol_schema_error: Task graph node {index} must be an object"
                )
            node_fields = set(node)
            missing_fields = required - node_fields
            unexpected_fields = node_fields - required - semantic_metadata
            if allow_runtime_metadata:
                unexpected_fields -= runtime_metadata
            if missing_fields or unexpected_fields:
                raise ProtocolSchemaError(
                    "protocol_schema_error: "
                    f"Task graph node {index} fields mismatch "
                    f"missing={sorted(missing_fields)} "
                    f"unexpected={sorted(unexpected_fields)}"
                )
            node_id = node.get("id")
            instruction = node.get("instruction")
            outcome = node.get("expected_outcome")
            dependencies = node.get("dependencies")
            atomic_skill_id = node.get("atomic_skill_id")
            if (
                not isinstance(node_id, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", node_id) is None
            ):
                raise PlannerOutputError(f"Task graph node {index} has invalid id")
            if node_id in ids:
                raise PlannerOutputError(f"Duplicate task graph node id: {node_id}")
            if not isinstance(instruction, str) or not instruction.strip():
                raise PlannerOutputError(
                    f"Task graph node {node_id} has empty instruction"
                )
            if not isinstance(outcome, str) or not outcome.strip():
                raise PlannerOutputError(
                    f"Task graph node {node_id} has empty expected_outcome"
                )
            if (
                not isinstance(dependencies, list)
                or any(not isinstance(item, str) for item in dependencies)
                or len(set(dependencies)) != len(dependencies)
            ):
                raise PlannerOutputError(
                    f"Task graph node {node_id} has invalid dependencies"
                )
            if atomic_skill_id is not None and (
                not isinstance(atomic_skill_id, str) or not atomic_skill_id.strip()
            ):
                raise PlannerOutputError(
                    f"Task graph node {node_id} has invalid atomic_skill_id"
                )
            atomic_skill_ids = node.get("atomic_skill_ids")
            if atomic_skill_ids is not None and (
                not isinstance(atomic_skill_ids, list)
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in atomic_skill_ids
                )
                or len(set(atomic_skill_ids)) != len(atomic_skill_ids)
            ):
                raise PlannerOutputError(
                    f"Task graph node {node_id} has invalid atomic_skill_ids"
                )
            ids.append(node_id)
            normalized_node = {
                "id": node_id,
                "instruction": instruction.strip(),
                "dependencies": list(dependencies),
                "expected_outcome": outcome.strip(),
                "status": "pending",
            }
            if isinstance(atomic_skill_id, str):
                normalized_node["atomic_skill_id"] = atomic_skill_id.strip()
            if isinstance(atomic_skill_ids, list) and atomic_skill_ids:
                normalized_node["atomic_skill_ids"] = [
                    item.strip() for item in atomic_skill_ids
                ]
            if allow_runtime_metadata and isinstance(
                node.get("atomic_skill_origin"), str
            ):
                normalized_node["atomic_skill_origin"] = node["atomic_skill_origin"]
            if allow_runtime_metadata:
                for key in (
                    "atomic_skill_family",
                    "atomic_skill_instruction",
                    "atomic_skill_expected_postcondition",
                ):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        normalized_node[key] = value.strip()
            normalized.append(normalized_node)
        id_set = set(ids)
        for node in normalized:
            unknown = set(node["dependencies"]) - id_set
            if unknown or node["id"] in node["dependencies"]:
                raise PlannerOutputError(
                    f"Task graph node {node['id']} has unknown or self dependency"
                )
        ordered = cls._stable_topological_sort(normalized)
        for order, node in enumerate(ordered):
            node["order"] = order
        return {"schema_version": 1, "revision": 0, "nodes": ordered}

    @staticmethod
    def _stable_topological_sort(
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        dependencies = {node["id"]: set(node["dependencies"]) for node in nodes}
        input_order = {node["id"]: index for index, node in enumerate(nodes)}
        by_id = {node["id"]: node for node in nodes}
        remaining = set(dependencies)
        resolved: set[str] = set()
        ordered: list[dict[str, Any]] = []
        while remaining:
            ready = sorted(
                (node_id for node_id in remaining if dependencies[node_id] <= resolved),
                key=input_order.__getitem__,
            )
            if not ready:
                raise PlannerOutputError("Task graph must be acyclic")
            ordered.extend(by_id[node_id] for node_id in ready)
            resolved.update(ready)
            remaining.difference_update(ready)
        return ordered

    def healthcheck(self) -> dict[str, Any]:
        return self.backend.healthcheck()

    def close(self) -> None:
        self.backend.close()

    def _images(self, observation: Any) -> list[Any]:
        if not isinstance(observation, Mapping) or self.max_images == 0:
            return []
        images = [image for _, image in camera_images(observation, self.camera_keys)]
        return images[: self.max_images]

    @staticmethod
    def _available_skills(observation: Any) -> list[str]:
        if not isinstance(observation, Mapping):
            return []
        skills = observation.get("available_skills", [])
        return (
            [item for item in skills if isinstance(item, str)]
            if isinstance(skills, list)
            else []
        )

    def _atomic_skill_manifest(self, task: Any) -> list[dict[str, Any]]:
        if self.task_atomic_skill_catalog is None:
            return []
        manifest = self.task_atomic_skill_catalog.manifest_for_task(task)
        if not manifest:
            raise ConfigError(
                "TaskDecomposer has no atomic skill catalog entry for this task"
            )
        return manifest

    @staticmethod
    def _validate_atomic_skill_bindings(
        graph: dict[str, Any], manifest: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not manifest:
            return graph
        allowed = {
            item.get("name"): item
            for item in manifest
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        if not allowed:
            raise ConfigError("task atomic skill catalog is empty or invalid")
        for node in graph.get("nodes", []):
            skill_id = node.get("atomic_skill_id")
            if skill_id not in allowed:
                raise ProtocolSchemaError(
                    "protocol_schema_error: Task graph node "
                    f"{node.get('id')} atomic_skill_id={skill_id!r} is not in "
                    "the task atomic skill catalog"
                )
            node["atomic_skill_origin"] = allowed[skill_id].get("origin")
            node["atomic_skill_family"] = allowed[skill_id].get("family")
            node["atomic_skill_instruction"] = allowed[skill_id].get("instruction")
            node["atomic_skill_expected_postcondition"] = allowed[skill_id].get(
                "expected_postcondition"
            )
        graph["atomic_skill_bindings_validated"] = True
        return graph

    def _atomic_skill_directory(
        self, context: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Read the 64-item atomic skill directory offered to the Decomposer.

        The Decomposer selects each node's ``atomic_skill_ids`` from this list, so a
        node carries only the chosen ids instead of the full catalog. The directory is
        cached for the recovery paths (plan revision, selective replan) that reuse the
        decomposition vocabulary without a fresh context.
        """

        raw = context.get("atomic_skill_directory")
        directory: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                entry: dict[str, Any] = {"id": item["id"]}
                for field_name in ("name", "category", "execution_status"):
                    value = item.get(field_name)
                    if isinstance(value, str) and value.strip():
                        entry[field_name] = value
                directory.append(entry)
        self.last_atomic_skill_directory = deepcopy(directory)
        return directory

    @staticmethod
    def _validate_atomic_skill_ids(
        graph: dict[str, Any], directory: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Reject node ids outside the offered directory; absence stays allowed."""

        allowed = {
            str(item["id"])
            for item in directory
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if not allowed:
            return graph
        for node in graph.get("nodes", []):
            if not isinstance(node, Mapping):
                continue
            ids = node.get("atomic_skill_ids")
            if ids is None:
                continue
            if not isinstance(ids, list) or any(
                not isinstance(item, str) or not item.strip() for item in ids
            ):
                raise ProtocolSchemaError(
                    "protocol_schema_error: Task graph node "
                    f"{node.get('id')} has invalid atomic_skill_ids"
                )
            unknown = [item for item in ids if item not in allowed]
            if unknown:
                raise ProtocolSchemaError(
                    "protocol_schema_error: Task graph node "
                    f"{node.get('id')} atomic_skill_ids={unknown!r} are not in the "
                    "atomic skill directory"
                )
        return graph

    def _schema(
        self,
        atomic_manifest: list[dict[str, Any]] | None = None,
        atomic_directory: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        atomic_names = [
            item["name"]
            for item in (atomic_manifest or [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        ]
        atomic_ids = [
            item["id"]
            for item in (atomic_directory or [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
        node = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$"},
                "atomic_skill_id": {
                    "type": "string",
                    **({"enum": atomic_names} if atomic_names else {}),
                },
                "atomic_skill_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        **({"enum": atomic_ids} if atomic_ids else {}),
                    },
                },
                "instruction": {"type": "string", "minLength": 1},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "expected_outcome": {"type": "string", "minLength": 1},
            },
            "required": [
                "id",
                *(["atomic_skill_id"] if atomic_names else []),
                "instruction",
                "dependencies",
                "expected_outcome",
            ],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": self.max_nodes,
                    "items": node,
                }
            },
            "required": ["nodes"],
            "additionalProperties": False,
        }

    @staticmethod
    def _semantic_node(node: Mapping[str, Any]) -> dict[str, Any]:
        semantic = {
            key: deepcopy(node.get(key))
            for key in ("id", "instruction", "dependencies", "expected_outcome")
        }
        if isinstance(node.get("atomic_skill_id"), str):
            semantic["atomic_skill_id"] = node["atomic_skill_id"]
        if isinstance(node.get("atomic_skill_ids"), list):
            semantic["atomic_skill_ids"] = list(node["atomic_skill_ids"])
        return semantic

    @staticmethod
    def _suffix_replan_schema(
        max_items: int,
        atomic_manifest: list[dict[str, Any]] | None = None,
        atomic_directory: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        atomic_names = [
            item["name"]
            for item in (atomic_manifest or [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        ]
        atomic_ids = [
            item["id"]
            for item in (atomic_directory or [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
        node = {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                },
                "atomic_skill_id": {
                    "type": "string",
                    **({"enum": atomic_names} if atomic_names else {}),
                },
                "atomic_skill_ids": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        **({"enum": atomic_ids} if atomic_ids else {}),
                    },
                },
                "instruction": {"type": "string", "minLength": 1},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "expected_outcome": {"type": "string", "minLength": 1},
            },
            "required": [
                "id",
                *(["atomic_skill_id"] if atomic_names else []),
                "instruction",
                "dependencies",
                "expected_outcome",
            ],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "replacement_nodes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max_items,
                    "items": node,
                },
                "revision_reason": {"type": "string", "minLength": 1},
            },
            "required": ["replacement_nodes", "revision_reason"],
            "additionalProperties": False,
        }

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise PlannerOutputError(
                "TaskDecomposer response is missing content"
            ) from error
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping)
            )
        if not isinstance(content, str):
            raise PlannerOutputError("TaskDecomposer response content must be text")
        stripped = content.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
        if fenced:
            stripped = fenced.group(1).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise PlannerOutputError("TaskDecomposer returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise PlannerOutputError("TaskDecomposer JSON must be an object")
        return parsed
