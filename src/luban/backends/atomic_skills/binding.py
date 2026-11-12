"""Wire the first-batch atomic skills onto a ``SkillRuntime``.

Registration keys come from the catalogue itself, so a binding that drifts from
the declared adapter key fails loudly instead of silently attaching the wrong
backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from luban.backends.atomic_skills.environments import StubWorldEnvironment
from luban.backends.atomic_skills.executors import StubSkillBackend
from luban.backends.atomic_skills.guards import AtomicSemanticGuard
from luban.backends.atomic_skills.verifiers import (
    DetectObjectsVerifier,
    FollowTerrainSegmentVerifier,
    WipeToolStrokeVerifier,
)
from omni_atomic_skills import SkillRegistry
from omni_atomic_skills.runtime import EventSink, SkillRuntime, SkillVerifier

# 环境适配器不在契约里声明，由调用方按 name 选择；stub 链路固定用这个名字。
STUB_ENVIRONMENT_KEY = "stub_world"

# 首批绑定：契约 executor.adapter_key -> 契约 verifier.adapter_key
FIRST_BATCH_BINDINGS = (
    ("detect_objects", "verify_detect_objects"),
    ("wipe_tool_stroke", "verify_wipe_tool_stroke"),
    ("follow_terrain_segment", "verify_follow_terrain_segment"),
)

_VERIFIER_FACTORIES: dict[str, type[SkillVerifier]] = {
    "verify_detect_objects": DetectObjectsVerifier,
    "verify_wipe_tool_stroke": WipeToolStrokeVerifier,
    "verify_follow_terrain_segment": FollowTerrainSegmentVerifier,
}


def build_stub_atomic_skill_runtime(
    catalog_paths: Sequence[str | Path],
    *,
    event_sink: EventSink | None = None,
) -> SkillRuntime:
    """Register the first-batch stub bindings and return the runtime.

    Stub verifiers stay ``independent=False``: they are separate code from the
    executors, but they do not watch a real world.
    """

    registry = SkillRegistry.load_json_files(catalog_paths)
    runtime = SkillRuntime(registry, event_sink=event_sink)
    for executor_key, verifier_key in FIRST_BATCH_BINDINGS:
        runtime.register_backend(executor_key, StubSkillBackend(executor_key))
        runtime.register_verifier(verifier_key, _VERIFIER_FACTORIES[verifier_key]())
    runtime.register_environment(STUB_ENVIRONMENT_KEY, StubWorldEnvironment())
    runtime.register_guard("default", AtomicSemanticGuard())
    return runtime


__all__ = [
    "FIRST_BATCH_BINDINGS",
    "STUB_ENVIRONMENT_KEY",
    "build_stub_atomic_skill_runtime",
]
