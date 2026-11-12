"""Atomic-skill backend bindings for Luban.

The versioned catalogue declares ``executor`` / ``verifier`` adapter keys while
their ``implementation_status`` is ``backend_pending``.  This package provides
the concrete adapters, one module per role, plus the wiring helper that registers
them on a ``SkillRuntime`` under the exact declared keys.
"""

from luban.backends.atomic_skills.binding import (
    FIRST_BATCH_BINDINGS,
    STUB_ENVIRONMENT_KEY,
    build_stub_atomic_skill_runtime,
)
from luban.backends.atomic_skills.environments import StubWorldEnvironment
from luban.backends.atomic_skills.executors import StubSkillBackend
from luban.backends.atomic_skills.guards import (
    AtomicSemanticGuard,
    semantic_violations,
)
from luban.backends.atomic_skills.verifiers import (
    DetectObjectsVerifier,
    FollowTerrainSegmentVerifier,
    WipeToolStrokeVerifier,
)

__all__ = [
    "AtomicSemanticGuard",
    "DetectObjectsVerifier",
    "FIRST_BATCH_BINDINGS",
    "FollowTerrainSegmentVerifier",
    "STUB_ENVIRONMENT_KEY",
    "StubSkillBackend",
    "StubWorldEnvironment",
    "WipeToolStrokeVerifier",
    "build_stub_atomic_skill_runtime",
    "semantic_violations",
]
