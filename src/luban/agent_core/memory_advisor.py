"""Arm C 的跨 episode 恢复顾问：不训练，只提案。

边界（由契约强制，不靠自觉）：

- 输入只来自 ``DelegationRequest``：当前节点、当前观测、有界事件窗口、前一完成
  节点的相关事实、已尝试的恢复方式、剩余预算、检索到的参考经验。全部证据的时间
  不得晚于决策时刻。
- 输出只允许 proposal / evidence：选用既有宏技能、说明适用条件、提出局部恢复建议
  与可观察期望结果。
- 无可用经验或建议不成立时返回 ``abstain``，不编造建议。
- 不能生成低层 action、不能改官方目标、不能改整张图、不能改安全约束、不能晋级候选
  ——这些字段在 ``DelegationResult`` 里是 forbidden，构造即报错。

跨 episode 库在正式运行期间只读；本模块不写回。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from luban.agent_core.delegation import (
    DelegationRequest,
    DelegationResult,
    SubAgent,
)
from luban.agent_core.memories.recovery import (
    RecoveryMemoryBank,
    RetrievalHit,
)
from luban.backends.llm.base import LLMBackend
from luban.exceptions import PlannerOutputError
from luban.serialization import to_jsonable

DEFAULT_CAMERA_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)

# 三种顾问模式，用于把 C 的整体增量拆成可归因的部分：
#   advisor      —— 检索 + LLM 推理（C 臂）
#   direct       —— 检索 + 确定性拼接，不调 LLM（D 臂：子 Agent 架构是否必要）
#   no_retrieval —— 不检索但照常调 LLM（E 臂：是记忆有用还是多算一次有用）
ADVISOR_MODES = ("advisor", "direct", "no_retrieval")

NO_RETRIEVAL_NOTICE = (
    "No cross-episode recovery memories are available for this decision. "
    "Reason only from the current node, the current camera views and the bounded "
    "event window. Abstain if the observation is too weak to ground a skill."
)

DEFAULT_MEMORY_ADVISOR_PROMPT = """You advise a mobile-manipulation robot Planner
during a recovery decision. You are a proposal-only advisor: you never execute an
action, never change the official goal, never rewrite the task graph, never relax a
safety constraint, and never promote a candidate.

You receive: the current task-graph node, the current camera views, a bounded window
of recent events from this episode, facts from the previously completed node, the
recovery attempts already made, the remaining step budget, and a short list of
cross-episode recovery memories retrieved by tag overlap.

Each retrieved memory carries its own applicability conditions (valid_when /
invalid_when) and an evidence_state. Treat them as reference, not as truth. If a
memory's conditions do not hold in the current views, say so instead of forcing it.

Return JSON with exactly these keys:
{
  "abstain": false,
  "skill": "one configured macro skill, or empty string when abstaining",
  "grounded_arguments": {"argument": "observation-grounded value"},
  "expected_outcome": "short observable result that would confirm the local recovery",
  "recovery_suggestion": "one local correction, or empty string",
  "reasoning": "short reason naming the observation that supports the proposal",
  "evidence": [{"text": "short observation-grounded evidence"}]
}

Set "abstain" to true, leave "skill" empty, and explain in "reasoning" when: no
retrieved memory applies, the current views contradict the memories, the remaining
budget cannot cover the proposal, or the observation is too weak to ground a skill.
Abstaining is a valid and expected answer."""


@dataclass(frozen=True)
class AdviceRecord:
    """一次顾问调用的完整审计记录，供 G6 的采纳链与分母统计使用。"""

    advice_id: str
    decision_time: str
    status: str
    task_name: str
    node_id: str
    skill: str
    failure_type: str
    retrieved_memory_ids: tuple[str, ...]
    retrieval_matched: tuple[str, ...]
    retrieval_scores: tuple[int, ...]
    remaining_budget: int
    event_window_size: int
    image_count: int
    latency_seconds: float
    abstain_reason: str
    proposal: Mapping[str, Any]
    model_call: Mapping[str, Any]
    media_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class MemoryAdvisorSubAgent(SubAgent):
    """按既有 SubAgent 契约实现的恢复顾问。"""

    def __init__(
        self,
        *,
        backend: LLMBackend,
        memory_bank: RecoveryMemoryBank,
        system_prompt: str = DEFAULT_MEMORY_ADVISOR_PROMPT,
        skill_definitions: Mapping[str, str] | None = None,
        camera_keys: Sequence[str] = DEFAULT_CAMERA_KEYS,
        max_images: int = 6,
        top_k: int = 3,
        event_window: int = 16,
        max_calls: int = 4,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        response_format_mode: str = "json_object",
        robot_profile: str = "xr1_mobile_manipulator",
        policy_version: str = "",
        advice_log_path: str | Path | None = None,
        media_log_dir: str | Path | None = None,
        mode: str = "advisor",
    ) -> None:
        if mode not in ADVISOR_MODES:
            raise ValueError(f"mode must be one of {sorted(ADVISOR_MODES)}")
        if not isinstance(backend, LLMBackend):
            raise TypeError("memory advisor requires an LLMBackend")
        if not isinstance(memory_bank, RecoveryMemoryBank):
            raise TypeError("memory advisor requires a RecoveryMemoryBank")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be a non-empty string")
        for name, value in (
            ("max_images", max_images),
            ("top_k", top_k),
            ("event_window", event_window),
            ("max_calls", max_calls),
            ("max_tokens", max_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if response_format_mode not in {"json_object", "text"}:
            raise ValueError("response_format_mode must be 'json_object' or 'text'")
        resolved_keys = tuple(camera_keys)
        if not resolved_keys or any(
            not isinstance(key, str) or not key for key in resolved_keys
        ):
            raise ValueError("camera_keys must contain non-empty strings")
        self.backend = backend
        self.memory_bank = memory_bank
        self.system_prompt = system_prompt
        self.skill_definitions = dict(skill_definitions or {})
        self.camera_keys = resolved_keys
        self.max_images = max_images
        self.top_k = top_k
        self.event_window = event_window
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.temperature = float(temperature)
        self.response_format_mode = response_format_mode
        self.robot_profile = robot_profile
        self.policy_version = policy_version
        self.mode = mode
        self.advice_log_path = (
            Path(advice_log_path) if advice_log_path is not None else None
        )
        self.media_log_dir = Path(media_log_dir) if media_log_dir is not None else None
        self.calls = 0
        self.llm_calls = 0
        self.records: list[AdviceRecord] = []
        self._last_retrieval: tuple[RetrievalHit, ...] = ()

    # -- 供 Pipeline / Runner 读取的只读视图 -------------------------------

    def advice_records(self) -> tuple[AdviceRecord, ...]:
        return tuple(self.records)

    def last_retrieval(self) -> tuple[RetrievalHit, ...]:
        return self._last_retrieval

    def healthcheck(self) -> dict[str, Any]:
        return {
            "healthy": True,
            "mode": self.mode,
            "calls": self.calls,
            "llm_calls": self.llm_calls,
            "max_calls": self.max_calls,
            "top_k": self.top_k,
            "event_window": self.event_window,
            "max_images": self.max_images,
            "robot_profile": self.robot_profile,
            "policy_version": self.policy_version,
            "media_log_dir": (
                str(self.media_log_dir) if self.media_log_dir is not None else None
            ),
            "memory_bank": self.memory_bank.healthcheck(),
            "memory_bank_fingerprint": self.memory_bank.fingerprint(),
        }

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()

    # -- 主入口 -------------------------------------------------------------

    def propose(self, request: DelegationRequest) -> DelegationResult:
        if not isinstance(request, DelegationRequest):
            raise TypeError("request must be a DelegationRequest")

        advice_id = uuid.uuid4().hex
        started = time.perf_counter()
        decision_time = request.decision_time or _utc_now()

        if self.calls >= self.max_calls:
            return self._abstain(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                reason="call_budget_exhausted",
                started=started,
                hits=(),
                image_count=0,
                model_call={},
            )

        if self.mode == "no_retrieval":
            # E 臂：不检索，但照常调 LLM，用于把「记忆有用」与「多算一次有用」分开
            hits: tuple[RetrievalHit, ...] = ()
        else:
            hits = tuple(
                self.memory_bank.retrieve(
                    task_name=request.task_name,
                    skill=request.skill,
                    failure_type=request.failure_type,
                    robot_profile=self.robot_profile,
                    policy_version=self.policy_version,
                    limit=self.top_k,
                    exclude_source_groups=request.source_group_ids,
                )
            )
        self._last_retrieval = hits
        if self.mode != "no_retrieval" and not hits:
            return self._abstain(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                reason="empty_retrieval",
                started=started,
                hits=hits,
                image_count=0,
                model_call={},
            )

        images = self._current_images(request.observation)
        if not images:
            return self._abstain(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                reason="no_current_camera_views",
                started=started,
                hits=hits,
                image_count=0,
                model_call={},
            )

        self.calls += 1
        # G1：把决策时刻真正送给顾问的图像落盘（无损 npy / 原字节），
        # 索引写入 media_index.jsonl；内容去重，不改写任何运行时状态。
        media_refs = self._persist_images(advice_id, images)

        if self.mode == "direct":
            # D 臂：检索结果直接拼接成建议，不经过 LLM
            return self._direct_proposal(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                hits=hits,
                image_count=len(images),
                started=started,
                media_refs=media_refs,
            )

        self.llm_calls += 1
        try:
            response = self.backend.complete(
                {
                    "messages": self._messages(request, hits, images),
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "response_format": self._response_format(),
                }
            )
        except Exception as error:
            return self._record(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                status="failed",
                reason=f"{type(error).__name__}: {error}",
                abstain_reason="",
                started=started,
                hits=hits,
                image_count=len(images),
                model_call={},
                proposal={},
                evidence=(),
                adoptable=False,
                media_refs=media_refs,
            )

        model_call = self._model_call_digest(response)
        try:
            parsed = self._parse_json(self._response_text(response))
        except PlannerOutputError as error:
            return self._record(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                status="failed",
                reason=str(error),
                abstain_reason="",
                started=started,
                hits=hits,
                image_count=len(images),
                model_call=model_call,
                proposal={},
                evidence=(),
                adoptable=False,
                media_refs=media_refs,
            )

        if bool(parsed.get("abstain", False)):
            return self._abstain(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                reason=str(parsed.get("reasoning") or "model_abstained"),
                started=started,
                hits=hits,
                image_count=len(images),
                model_call=model_call,
                media_refs=media_refs,
            )

        try:
            proposal = self._proposal(parsed)
            evidence = self._evidence(parsed.get("evidence", []))
        except PlannerOutputError as error:
            return self._record(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                status="failed",
                reason=str(error),
                abstain_reason="",
                started=started,
                hits=hits,
                image_count=len(images),
                model_call=model_call,
                proposal={},
                evidence=(),
                adoptable=False,
                media_refs=media_refs,
            )

        return self._record(
            advice_id=advice_id,
            request=request,
            decision_time=decision_time,
            status="proposal",
            reason=str(parsed.get("reasoning", "")),
            abstain_reason="",
            started=started,
            hits=hits,
            image_count=len(images),
            model_call=model_call,
            proposal=proposal,
            evidence=evidence,
            adoptable=True,
            media_refs=media_refs,
        )

    # -- 内部 ---------------------------------------------------------------

    def _abstain(
        self,
        *,
        advice_id: str,
        request: DelegationRequest,
        decision_time: str,
        reason: str,
        started: float,
        hits: Sequence[RetrievalHit],
        image_count: int,
        model_call: Mapping[str, Any],
        media_refs: tuple[str, ...] = (),
    ) -> DelegationResult:
        return self._record(
            advice_id=advice_id,
            request=request,
            decision_time=decision_time,
            status="abstain",
            reason=reason,
            abstain_reason=reason,
            started=started,
            hits=hits,
            image_count=image_count,
            model_call=model_call,
            proposal={},
            evidence=(),
            adoptable=False,
            media_refs=media_refs,
        )

    def _direct_proposal(
        self,
        *,
        advice_id: str,
        request: DelegationRequest,
        decision_time: str,
        hits: Sequence[RetrievalHit],
        image_count: int,
        started: float,
        media_refs: tuple[str, ...] = (),
    ) -> DelegationResult:
        """D 臂：把 top-1 记忆直接拼成建议，不调用 LLM。

        这样 C 与 D 的差别就只剩「有没有 LLM 推理」这一项，用于回答
        「子 Agent 架构本身是否必要」。
        """
        memory = hits[0].memory
        skill = request.skill or (memory.skills[0] if memory.skills else "")
        if not skill:
            return self._abstain(
                advice_id=advice_id,
                request=request,
                decision_time=decision_time,
                reason="direct_mode_has_no_skill_to_apply",
                started=started,
                hits=hits,
                image_count=image_count,
                model_call={},
                media_refs=media_refs,
            )
        expected = "; ".join(memory.expected_postconditions) or memory.recovery_strategy
        proposal = {
            "skill": skill,
            "recovery_suggestion": memory.recovery_strategy,
            "expected_outcome": expected,
            "reasoning": (
                f"direct concatenation of memory {memory.memory_id} "
                f"(failure_type={memory.failure_type})"
            ),
        }
        evidence = tuple(
            {"text": f"memory {memory.memory_id}: {condition}"}
            for condition in memory.valid_when[:3]
        )
        return self._record(
            advice_id=advice_id,
            request=request,
            decision_time=decision_time,
            status="proposal",
            reason=proposal["reasoning"],
            abstain_reason="",
            started=started,
            hits=hits,
            image_count=image_count,
            model_call={"mode": "direct", "llm_called": False},
            proposal=proposal,
            evidence=evidence,
            adoptable=True,
            media_refs=media_refs,
        )

    def _record(
        self,
        *,
        advice_id: str,
        request: DelegationRequest,
        decision_time: str,
        status: str,
        reason: str,
        abstain_reason: str,
        started: float,
        hits: Sequence[RetrievalHit],
        image_count: int,
        model_call: Mapping[str, Any],
        proposal: Mapping[str, Any],
        evidence: tuple[Mapping[str, Any], ...],
        adoptable: bool,
        media_refs: tuple[str, ...] = (),
    ) -> DelegationResult:
        record = AdviceRecord(
            advice_id=advice_id,
            decision_time=decision_time,
            status=status,
            task_name=request.task_name,
            node_id=str(request.node.get("id", "")),
            skill=request.skill,
            failure_type=request.failure_type,
            retrieved_memory_ids=tuple(hit.memory.memory_id for hit in hits),
            retrieval_matched=tuple(
                f"{hit.memory.memory_id}:{','.join(hit.matched)}" for hit in hits
            ),
            retrieval_scores=tuple(hit.score for hit in hits),
            remaining_budget=request.budget,
            event_window_size=len(request.event_window),
            image_count=image_count,
            latency_seconds=round(time.perf_counter() - started, 4),
            abstain_reason=abstain_reason,
            proposal=dict(proposal),
            model_call=dict(model_call),
            media_refs=tuple(media_refs),
        )
        self.records.append(record)
        self._append_log(record)
        return DelegationResult(
            status=status,
            proposal=dict(proposal),
            evidence=evidence,
            reason=reason,
            adoptable=adoptable,
        )

    def _append_log(self, record: AdviceRecord) -> None:
        if self.advice_log_path is None:
            return
        self.advice_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.advice_log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(to_jsonable(record.to_dict()), ensure_ascii=False) + "\n"
            )

    # -- G1 决策时刻图像落盘 -------------------------------------------------

    def _persist_images(
        self, advice_id: str, images: Sequence[tuple[str, Any]]
    ) -> tuple[str, ...]:
        if self.media_log_dir is None or not images:
            return ()
        directory = self.media_log_dir
        directory.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for camera_key, image in images:
            entry = self._persist_image(directory, advice_id, camera_key, image)
            if entry is not None:
                entries.append(entry)
        if entries:
            index_path = directory / "media_index.jsonl"
            with index_path.open("a", encoding="utf-8") as stream:
                for entry in entries:
                    stream.write(
                        json.dumps(to_jsonable(entry), ensure_ascii=False) + "\n"
                    )
        return tuple(str(entry["media_id"]) for entry in entries)

    def _persist_image(
        self, directory: Path, advice_id: str, camera_key: str, image: Any
    ) -> dict[str, Any] | None:
        """按内容去重落盘单帧；返回 media_index 条目。"""
        import hashlib

        blob: bytes
        suffix: str
        array: Any = None
        shape: Any = None
        dtype: Any = None
        try:
            import numpy as np
        except Exception:  # pragma: no cover - numpy 是硬依赖
            np = None  # type: ignore[assignment]
        if np is not None and isinstance(image, np.ndarray):
            array = np.ascontiguousarray(image)
            blob = array.tobytes()
            suffix = ".npy"
            shape = list(array.shape)
            dtype = str(array.dtype)
        elif isinstance(image, (bytes, bytearray)):
            blob = bytes(image)
            suffix = ".bin"
        elif isinstance(image, str):
            text = image
            marker = "base64,"
            if text.startswith("data:") and marker in text:
                import base64

                try:
                    blob = base64.b64decode(text.split(marker, 1)[1])
                except Exception:
                    blob = text.encode("utf-8")
                suffix = ".bin"
            else:
                blob = text.encode("utf-8")
                suffix = ".txt"
        else:
            return None
        digest = hashlib.sha256(blob).hexdigest()
        media_id = f"media_{digest[:16]}"
        path = directory / f"{media_id}{suffix}"
        if not path.exists():
            if array is not None:
                np.save(path, array, allow_pickle=False)
            else:
                path.write_bytes(blob)
        return {
            "media_id": media_id,
            "advice_id": advice_id,
            "camera_key": camera_key,
            "path": path.name,
            "sha256": f"sha256:{digest}",
            "bytes": len(blob),
            "shape": shape,
            "dtype": dtype,
            "recorded_at": _utc_now(),
        }

    def _messages(
        self,
        request: DelegationRequest,
        hits: Sequence[RetrievalHit],
        images: Sequence[tuple[str, Any]],
    ) -> list[dict[str, Any]]:
        node_text = json.dumps(
            to_jsonable(dict(request.node), max_sequence_items=16),
            ensure_ascii=False,
        )
        window = list(request.event_window)[-self.event_window :]
        sections = [
            f"Current node:\n{node_text}",
            f"Trigger reason: {request.reason or 'recovery_decision'}",
            f"Remaining environment-step budget: {request.budget}",
            "Recent events in this episode, oldest first "
            f"(last {len(window)} of {len(request.event_window)}):\n"
            + (
                json.dumps(to_jsonable(window), ensure_ascii=False)
                if window
                else "(no recorded events)"
            ),
            "Facts from the previously completed node:\n"
            + (
                json.dumps(
                    to_jsonable(list(request.previous_node_facts)),
                    ensure_ascii=False,
                )
                if request.previous_node_facts
                else "(none recorded)"
            ),
            "Recovery attempts already made in this episode: "
            + (
                ", ".join(request.attempted_recoveries)
                if request.attempted_recoveries
                else "(none)"
            ),
            (
                NO_RETRIEVAL_NOTICE
                if self.mode == "no_retrieval"
                else "Retrieved cross-episode recovery memories:\n"
                + json.dumps(
                    [hit.memory.planner_context() for hit in hits],
                    ensure_ascii=False,
                    indent=2,
                )
            ),
        ]
        content: list[dict[str, Any]] = [
            {"type": "text", "text": "\n\n".join(sections)}
        ]
        content.append(
            {"type": "text", "text": "Current camera views, latest frame per camera:"}
        )
        for camera_key, image in images:
            content.append({"type": "text", "text": f"Camera {camera_key}"})
            content.append({"type": "image_url", "image_url": {"url": image}})
        return [
            {"role": "system", "content": self._system_content()},
            {"role": "user", "content": content},
        ]

    def _system_content(self) -> str:
        if not self.skill_definitions:
            return self.system_prompt
        catalog = "\n".join(
            f"- {skill}: {description}"
            for skill, description in self.skill_definitions.items()
        )
        return f"{self.system_prompt}\n\nConfigured candidate skills:\n{catalog}"

    def _response_format(self) -> dict[str, Any]:
        if self.response_format_mode == "json_object":
            return {"type": "json_object"}
        return {"type": "text"}

    def _current_images(self, observation: Any) -> list[tuple[str, Any]]:
        if not isinstance(observation, Mapping):
            return []
        images: list[tuple[str, Any]] = []
        for camera_key in self.camera_keys:
            value = observation.get(camera_key)
            if value is None:
                continue
            frames = (
                list(value)
                if isinstance(value, Sequence)
                and not isinstance(value, (str, bytes, bytearray, Mapping))
                else [value]
            )
            if frames:
                images.append((camera_key, frames[-1]))
            if len(images) >= self.max_images:
                break
        return images

    @staticmethod
    def _proposal(parsed: Mapping[str, Any]) -> dict[str, Any]:
        skill = parsed.get("skill")
        if not isinstance(skill, str) or not skill.strip():
            raise PlannerOutputError(
                "memory advisor proposal requires a non-empty skill"
            )
        proposal: dict[str, Any] = {"skill": skill.strip()}
        arguments = parsed.get("grounded_arguments")
        if isinstance(arguments, Mapping):
            proposal["grounded_arguments"] = {
                str(key): value for key, value in arguments.items()
            }
        expected = parsed.get("expected_outcome")
        if isinstance(expected, str) and expected.strip():
            proposal["expected_outcome"] = expected.strip()
        suggestion = parsed.get("recovery_suggestion")
        if isinstance(suggestion, str) and suggestion.strip():
            proposal["recovery_suggestion"] = suggestion.strip()
        reasoning = parsed.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            proposal["reasoning"] = reasoning.strip()
        return proposal

    @staticmethod
    def _evidence(value: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return ()
        items: list[Mapping[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                items.append({str(key): val for key, val in item.items()})
        return tuple(items)

    @staticmethod
    def _response_text(response: Mapping[str, Any]) -> str:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise PlannerOutputError(
                "memory advisor response is missing choices[0].message"
            ) from error
        if not isinstance(message, Mapping):
            raise PlannerOutputError("memory advisor message must be a mapping")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            joined = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping)
            )
            if joined.strip():
                return joined
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and "{" in reasoning:
            return reasoning
        raise PlannerOutputError("memory advisor response content is empty")

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.startswith("json"):
                candidate = candidate[4:]
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise PlannerOutputError("memory advisor response contains no JSON object")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            raise PlannerOutputError(
                f"memory advisor response is not valid JSON: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise PlannerOutputError("memory advisor JSON must be an object")
        return parsed

    @staticmethod
    def _model_call_digest(response: Mapping[str, Any]) -> dict[str, Any]:
        digest: dict[str, Any] = {}
        if isinstance(response, Mapping):
            for key in ("id", "model", "created", "usage", "service_tier"):
                if key in response:
                    digest[key] = response[key]
            choices = response.get("choices")
            if isinstance(choices, Sequence) and choices:
                first = choices[0]
                if isinstance(first, Mapping):
                    digest["finish_reason"] = first.get("finish_reason")
        return digest


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "ADVISOR_MODES",
    "DEFAULT_CAMERA_KEYS",
    "DEFAULT_MEMORY_ADVISOR_PROMPT",
    "NO_RETRIEVAL_NOTICE",
    "AdviceRecord",
    "MemoryAdvisorSubAgent",
]
