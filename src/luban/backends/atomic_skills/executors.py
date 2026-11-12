"""Stub executors that turn validated atomic-skill inputs into explicit commands.

A stub proves the binding chain (contract -> prepare -> environment -> verifier)
without touching a camera, a simulator or a robot.  Every command it produces is
tagged ``success_claimed: False`` and ``backend_kind: stub`` so downstream
evidence can never mistake a stub run for a real backend run.
"""

from __future__ import annotations

from collections.abc import Mapping

from omni_atomic_skills.contracts import JsonValue, SkillContract
from omni_atomic_skills.runtime import ExecutionContext, PreparedCommand


class StubSkillBackend:
    """Minimal ``SkillBackend`` implementation for the first-batch skills.

    ``command_op`` must equal the adapter key declared by the contract, so the
    produced command stays traceable to one exact contract version.
    """

    backend_kind = "stub"

    def __init__(self, command_op: str) -> None:
        if not isinstance(command_op, str) or not command_op.strip():
            raise ValueError("command_op must be a non-empty string")
        self.command_op = command_op.strip()

    def prepare(
        self,
        contract: SkillContract,
        inputs: Mapping[str, JsonValue],
        context: ExecutionContext,
    ) -> PreparedCommand:
        return PreparedCommand(
            command={
                "op": self.command_op,
                "skill_id": contract.skill_id,
                "skill_version": str(contract.version),
                "inputs": dict(inputs),
            },
            metadata={
                "source_api": "luban.backends.atomic_skills.StubSkillBackend",
                "success_claimed": False,
                "backend_kind": self.backend_kind,
                "episode_id": context.episode_id,
            },
        )


__all__ = ["StubSkillBackend"]
