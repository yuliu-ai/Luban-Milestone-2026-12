# Luban architecture

This document describes the actual independent snapshot. Current acceptance
evidence belongs in docs/validation.md; parent-project evaluation numbers do not
establish results for this tree.

## Dependency direction

CLI/config composition → Runtime → Pipeline → Agent contracts / Environment contract.
Agent Core → Planner / Verifier / Memory / SkillBackend / LLMBackend contracts.
Concrete benchmark environments → Environment contract and their optional SDKs.
Benchmark evaluators → Runtime/Pipeline/Agent and concrete environment adapters.
ROS2 transport adapters → framework contracts. Core cannot import ROS2, simulator,
evaluation or model SDK implementations. Optional SDK imports remain at use sites.

## Responsibilities

The experimental `PolicyLanguagePipeline` uses a fixed action-chunk interval for
visual verification and replanning. The XR-1 dual-branch runner owns process
coordination outside core: each branch has its own environment and runtime, and
the first environment-reported success interrupts the other branch. The two
branches share the environment seed and use distinct deterministic policy steps.
The evaluation coordinator also supports one branch. The optional RoboCasa
simulator-information condition exposes episode metadata, MuJoCo model XML and
current simulator state to task decomposition, planning and verification. The
XR-1 action backend continues to consume its normal observation and instruction.

- `agent_core/agents`: `BaseAgent` owns components and client lifecycle;
  `DefaultAgent` delegates and `SelfEvolvingAgent` adds evidence/graph modules.
  Agents do not own or bypass the Pipeline/Environment execution sequence.
- `agent_core/planners`: select task/skill/subtask, optionally use LLM and graphs;
  planners cannot declare benchmark success.
- `backends/skills`: generate open action payloads; no environment execution.
- `environments/base.py`: reset(task), execute(action, execute_steps), close().
  Concrete adapters check payloads and provide authoritative feedback.
- `agent_core/verifiers`: interpret action/environment/visual evidence.
- `pipelines`: call components and maintain explicit deterministic transition state.
  `DirectPipeline` is a closed loop; skill-execution pipelines track executions,
  attempts, budgets, recovery and graph state. `PolicyOnlyPipeline` explicitly
  omits planner/semantic verifier and is not the acceptance example.
- `runtimes`: session creation, limits, scheduling, trace, errors and cleanup;
  no planning policy. Runtime owns mutable state with task/observation/step.
- `observability`: optional compact trace, FFmpeg video and artifact manifest;
  recorder errors do not replace episode outcome.
- `evals/benchmarks`: task enumeration and aggregation, independent of core.

## Contracts and lifecycle

Payloads are ordinary dictionaries/strings/arrays with validation at use sites;
there is no universal Action class or global verifier decision enum. Execute then
observe/verify before the next action. Environment success overrides model claims.
`executed_steps` must be nonnegative integral, progress finite. Runtime records an
explicit reason for each completed session and refuses existing/unsafe session IDs.
KeyboardInterrupt is recorded when possible, resources close, interruption propagates.

Memory holds facts/events and contributes explicit context. Evidence-gated candidate
and experience modules retain their tests but are not evidence of improved task
performance. ModuleEvolutionManager uses one reentrant instance lock for lifecycle
read/transition/append and commit-time paired-evidence rechecks. Separate managers
or processes must not share a writable ledger. Module advisory input reuses the
episode-update step and memory summaries, with the last 12 execution events.
It does not repeat raw model responses or working frames, and does not mutate
the original trace/memory. Proposal-only mode does not apply candidates.

## Configuration and package boundary

Agent YAML composes agent components. Run YAML chooses agent, Pipeline, Runtime,
Environment and task/evaluator. Nested `class_path` construction remains explicit;
stable backend names are a small registry, with no discovery/plugin hierarchy.
Owner-relative agent config paths take precedence; other component paths keep
constructor/CWD semantics. YAML supports environment variables and explicit defaults.

Installed packages are `luban`, `omni_atomic_skills` and the Chemistry
protobuf client. The wheel includes typed markers, Chemistry YAML and the CPU module
example; source archive includes the repository's small configs/catalogs/tests/scripts.
External SDKs, model weights, assets, copied SpatialLM and standalone robot workspaces
are excluded. Upstream Apache-2.0 attribution remains; third-party terms are separate.

RoboCasa applies its process-local EGL context lifecycle guard from `_rendering.py`;
it does not patch shared SDK files. External HTTP/ZeroMQ/WebSocket clients close
connections only. Service startup/shutdown remains an operator action.

## Validation boundary

Verified paths include the CPU counter, one real RoboCasa/XR-1 simple task, and
all 32 atomic milestone skills with three successful Linux calls each across
separate native, SDK and model batches and a unified fresh 96/96 run. The
internal 30-composition batch also passed all 30 tasks. The public-source revised fixed50 VLABench comparison completed with 46 Luban and 12 OpenVLA successes, with no incomplete/error episodes. Two separate six-environment installations have records. The public-source RoboCasa100 reproduction completed with43 baseline and53 Luban successes, no error pairs, and23.26% relative gain. All six public-source milestone suites passed. LIBERO, RoboTwin, EB-ALFRED,
Chemistry, OpenPI and experimental spatial,
graph/evolution modules have source/contracts and varying optional test coverage;
no new complete live matrix is claimed. ROS2 has injected transport contracts,
not a verified physical robot installation. General async runtime and physical
robot validation remain unimplemented/unverified scope, respectively.


## Package identity and bilingual guides

The distribution, import namespace and CLI are `luban`; implementation lives under
`src/luban`. Config class paths and owned environment-variable prefixes follow
that identity, including `LUBAN_ROBOCASA_ROOT`. The standalone atomic-skill and
Chemistry protocol packages retain their separate boundaries. No legacy runtime
alias is advertised. Required upstream/source attribution remains accurate.

User guides have complete English/Chinese pairs, linked from docs/README.md and
docs/README.zh-CN.md. The documentation check validates counterparts and identical
executable code blocks. This does not change module ownership or dependency direction.

## RoboSynChallenge integration

The optional RoboSynChallenge environment adapter executes native actions and exposes authoritative task success. Its ACT/DP skill backend generates actions without importing simulator SDKs into core. The evaluator owns explicit seed accounting, result persistence and resume checks. Matrix orchestration belongs under evals, outside Runtime and Agent Core. SDK and policy imports are deferred until use. Git provenance is recorded when available and remains null for source archives or installed packages without checkout metadata.

## HTTP run service

Optional atomic physical fixtures live in `environments/atomic` and implement
the standalone `omni_atomic_skills` adapter contracts. MuJoCo is imported only
when a fixture is instantiated. A09 uses a force-limited established attachment
and physical press-fit contacts, with geometry/force verification separate from
command generation. It does not model grasp acquisition or all robot embodiments.

`luban serve --run-config NAME=PATH` registers operator-selected run YAMLs.
`POST /v1/runs` resolves the supplied name and reuses CLI `run_config`, retaining
the runtime output and environment-owned success. Registered runs are serialized
per service instance. Requests do not supply paths or override executable configs.
The built-in counter endpoint remains a core-service demonstration. The dual-branch
coordinator continues to run through its existing script.

## Native atomic milestone fixtures

`environments/atomic/native` contains the project's optional PyBullet and MuJoCo
adapters recovered from AtomicSkillSoftware64. They reuse `omni_atomic_skills`
contracts and Runtime. The batch evaluator in `scripts/milestone/native_check.py`
starts independent fixture processes, counts only the selected target invocation,
and retains prerequisite results separately. It covers 26 of 32 rows, including M12 from real RGB-D marker feedback.
The SDK entry runs M01/M03, and the perception entry runs P01/P02/P04/P09
through local model services. `atomic32_check.py` coordinates fresh inputs and
all three groups, then aggregates exactly 96 target calls. Raw simulator labels
remain in the independent verifiers; models receive only declared image inputs.
Dedicated milestone catalogs preserve fixed public API names and restrict G01 to
opening, I06 to lifting, and A05 to a rotary lever. No simulator dependency enters
Agent Core or the standalone contract layer.

## Optional asynchronous language execution

`AsyncPolicyLanguagePipeline` overlaps subsequent language verification/planning
with Actor execution through one owned worker. The initial approved plan remains
synchronous. A configurable chunk-overlap bound limits how far execution proceeds
before a pending result is required. Results carry a plan revision and are applied
at chunk boundaries. Stale or terminal unused results do not change task success.
`Pipeline.close()` is a default no-op lifecycle method; SyncRuntime calls it before
Agent end-of-episode handling and before closing the environment and Agent. The
asynchronous implementation joins its worker there. This is language concurrency
inside a synchronous environment Runtime, not a general asynchronous Runtime.

The VLABench adapter owns SDK loading, fixed episode resets, Cartesian-delta/IK
execution and authoritative success conditions. The HTTP SkillBackend owns policy
requests only. Model processing follows each checkpoint's VLABench conventions.

The VLABench batch coordinator allocates either a shared worker pool or a bounded
pool per policy service. Each Luban episode worker retains two independent Agent
branches. Resume can adopt explicitly identified live episode processes without
starting duplicate simulations. Result collection separates normal policy limits
from missing results and runtime exceptions, preserving original result files.

## Physical tracking recovery

`TrackingRecoveryEnvironment` owns the Panda fixture, disturbance injection and
independent raw-state stability measurements. `VisualServoSkillBackend` consumes
only the RGB-D marker estimate and robot proprioception to produce bounded pose
commands; loss of target visibility produces a hold command. It does not read the
disturbance kind or injection timing. `SceneRGBDIO` is shared by the atomic M12
entry and this environment. The optional `external_force` argument of the physical
plant step applies a world-frame force at each physics substep.

The evaluated recovery configuration uses DefaultAgent, TaskSkillPlanner,
EnvironmentVerifier and DirectPipeline. All four appendix disturbances passed
their physical tracking conditions and monotonic-clock recovery limit in Linux.
This is the M12 visual-servo path; it does not establish XR-1 long-task recovery.

## Milestone orchestration and deployment

`scripts/milestone/bootstrap.py` prepares six separate evaluation/model environments and their resource paths. `run_suite.py` owns stage-level process startup and teardown, serializes suites to release GPU memory between model families, and invokes the existing native/benchmark/service runners. Its lifecycle only terminates processes it created. The optional resource monitor reads Linux procfs while the coordinator runs. These scripts do not add dependencies to Agent Core.

The baseline PolicyOnlyPipeline supplies the environment's original task prompt and advances policy seeds by action chunk. It does not call Planner or semantic Verifier. The RoboCasa batch aligns its branch-0 seed stride with the baseline and uses the official horizon for each branch. Milestone 1.7 gain is accepted only for a complete 100-pair batch with a defined relative gain of at least 3%.

VLABench records predicted actions, joint commands and IK status before physics execution. On a physics exception it writes acceleration diagnostics and reraises the original error. This preserves simulator exceptions separately from normal unsuccessful policies.

Registered HTTP runs return `service_function_seconds`, measured around `run_config` inside the service lock. Integration overhead uses request round-trip time minus that same function duration. The completed stability run uses a local caller and real M12 target-jump configuration, with 153 successful calls over 3706 seconds in the public-source reproduction. Network latency was measured separately through an SSH tunnel.
