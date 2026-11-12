[English](environments.md) · [简体中文](environments.zh-CN.md)

# Environment and robot contracts

`luban.environments.base.Environment` has three operations:

```python
def reset(self, task): ...  # return initial observation
def execute(self, action, execute_steps=None): ...  # return feedback dict
def close(self): ...
```

There is no separate framework `observe()` method. `execute()` returns the next
observation and environment feedback. The concrete environment validates supported
payload types, shapes, dtype, units and safety bounds at the point of use.
Planners propose work, policies generate payloads, environments execute them,
verifiers interpret feedback, pipelines transition state, runtimes enforce budgets.

The complete minimal implementation is
[`CounterEnvironment`](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/src/luban/examples/offline.py). It accepts only
`increment`, returns `observation`, `task_success`, `task_progress`,
`last_action_success`, `done`, `executed_steps`, and marks success after two actions.
Use its [`offline` run](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/configs/runs/offline.yaml) to test a custom component
before connecting hardware. This is a deliberately mock example.

`DirectPipeline` recognizes these feedback keys. `executed_steps` must be a
nonnegative integer (NumPy integer accepted, bool/float rejected), and progress
must be finite. Other pipelines interpret their own structured verification
outputs; there is no framework-wide Verifier decision enum. Native benchmark
success takes priority over a planner or model's opinion.

| Adapter path under `luban` | Scope in this preparation |
| --- | --- |
| `environments.benchmarks.robocasa.RoboCasaEnvironment` | Real XR-1 single episode; source/pins in validation |
| `environments.benchmarks.libero.LiberoEnvironment` | Software contracts/tests; no new live LIBERO run |
| `environments.benchmarks.embodiedbench.EBAlfredEnvironment` | Source adapter; no new AI2-THOR run; upstream license/preset limitation |
| `environments.benchmarks.robotwin` | Source adapter; external RoboTwin setup, no new live run |
| `environments.benchmarks.chemistry_bench` | Protocol/client and local worker tests; no new Isaac Sim run |
| `integrations.ros2` | Injected transport contracts; no verified physical robot deployment |

Place simulator adapters in `environments/benchmarks/`, task loops and aggregation
in `evals/benchmarks/`. ROS2 transport belongs in `integrations/ros2/`; core cannot
import concrete ROS2, simulator or model SDK types. Load external SDKs lazily so
core-only installation still imports. Select your installed adapter with
`environment.class_path` and constructor arguments in a run file.

The standalone RobotWorkspace/navigation projects and their CAD/vendor assets
are excluded from this independent repository. Their historical presence in a
parent tree does not establish robot support here. Hardware testing and general
async runtime remain outside the validated scope.

## RoboSynChallenge

`RoboSynChallengeEnvironment`, the local ACT/DP backend and evaluation entry point are available. See the [RoboSynChallenge guide](robosynchallenge.md) for resources, execution and resuming.
