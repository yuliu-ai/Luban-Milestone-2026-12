"""Run the real framework loop against a two-action, local counter environment."""

import argparse
import json
from pathlib import Path
from typing import Any

from luban.agent_core import (
    DefaultAgent,
    EnvironmentVerifier,
    InMemoryMemory,
    TaskSkillPlanner,
)
from luban.backends.skills import LanguageSkillBackend
from luban.environments import Environment
from luban.pipelines import DirectPipeline
from luban.runtimes import SyncRuntime
from luban.serialization import to_jsonable


class CounterEnvironment(Environment):
    """Demonstrate environment-owned success; this is not a robot benchmark."""

    def reset(self, task: Any) -> dict[str, Any]:
        self.count = 0
        return {"available_skills": ["increment"], "count": self.count}

    def execute(self, action: Any, execute_steps: int | None = None) -> dict[str, Any]:
        if action != "increment":
            raise ValueError("CounterEnvironment supports only increment")
        self.count += 1
        success = self.count >= 2
        return {
            "observation": {"available_skills": ["increment"], "count": self.count},
            "task_success": success,
            "task_progress": min(self.count / 2, 1.0),
            "last_action_success": True,
            "done": success,
            "executed_steps": 1,
        }

    def close(self) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/offline_demo"))
    args = parser.parse_args()
    agent = DefaultAgent(
        TaskSkillPlanner(),
        EnvironmentVerifier(),
        InMemoryMemory(),
        LanguageSkillBackend(),
    )
    result = SyncRuntime(output_dir=args.output_dir, max_steps=3).run(
        agent, DirectPipeline(), CounterEnvironment(), {"name": "increment"}
    )
    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
