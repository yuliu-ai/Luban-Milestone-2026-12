from typing import Any

from luban.agent_core.memories.base import Memory


class InMemoryMemory(Memory):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.recovery_memories: list[dict[str, Any]] = []
        self.physical_failure_memories: list[dict[str, Any]] = []
        self.protocol_failure_memories: list[dict[str, Any]] = []

    def reset(self, session_id: str) -> None:
        self.events.clear()
        self.recovery_memories.clear()
        self.physical_failure_memories.clear()
        self.protocol_failure_memories.clear()

    def update(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        self.events.append(dict(event))
        recovery_record = event.get("recovery_memory")
        if isinstance(recovery_record, dict):
            copied = dict(recovery_record)
            self.recovery_memories.append(copied)
            failure_label = recovery_record.get("failure_label")
            physical = (
                failure_label.get("physical_memory_eligible") is True
                if isinstance(failure_label, dict)
                else False
            )
            target = (
                self.physical_failure_memories
                if physical
                else self.protocol_failure_memories
            )
            target.append(copied)

    def recall(self, query: dict[str, Any]) -> dict[str, Any]:
        return {
            "working_frames": [],
            "recent_events": list(self.events),
            "key_events": [],
            "recovery_memories": [dict(record) for record in self.recovery_memories],
            "physical_failure_memories": [
                dict(record) for record in self.physical_failure_memories
            ],
            "protocol_failure_memories": [
                dict(record) for record in self.protocol_failure_memories
            ],
            "summary": "",
        }
