[English](models.md) · [简体中文](models.zh-CN.md)

# Models and policies

## LLMBackend

`luban.backends.llm.base.LLMBackend` defines `complete(inputs)`,
`healthcheck()` and `close()`. `OpenAICompatibleLLMBackend` implements HTTP
`/models` and `/chat/completions`; it adds `/v1` if the base URL lacks it, reads
`OPENAI_API_KEY`, uses finite timeout/retries and redacts diagnostic credentials.
It closes only its client, never the remote service. Text responses must provide
`choices[0].message.content`. Images may be arrays, PIL images, bytes, local paths
or image URLs and are encoded as data URLs when needed. Image/structured-output
capabilities still depend on the selected model and provider.

To check a live service against the **mock counter**, with at most three planner
calls and no automatic retry:

```bash
export LLM_BASE_URL=https://your-provider.example/api/v1
export LLM_MODEL=your-actual-model-id
read -r -s -p 'API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
.venv/bin/luban health --agent-config configs/agents/llm_counter.yaml
.venv/bin/luban run --config configs/runs/llm_counter.yaml
unset OPENAI_API_KEY
```

The `read -p` syntax above is for Bash; in Zsh use
`read -rs 'OPENAI_API_KEY?API key: '; echo` instead. The example planner requests
JSON schema output. A provider that lacks that feature needs a compatible custom
backend or planner; a reachable `/models` endpoint alone is insufficient.
The user-supplied GLM gateway is an optional provider example, not a prerequisite;
its actual connection/call result is recorded in the validation document.

For another protocol, implement `LLMBackend` in your installed module and select
its `class_path` in the planner's nested backend config. Do not add an Agent
subclass merely for a different transport. Existing `SubtaskSkillPlanner` and
`SubtaskVerifier` accept multimodal evidence; a bounded task-graph entry is described in [advanced episodes](advanced-episode.md).
A single simulator-frame image/JSON request to GLM-5.3-flash also passed; actual
closed-loop results are recorded separately in [validation](validation.md).

## SkillBackend

`SkillBackend.predict(inputs)` returns an action payload; it never executes the
robot/environment. `LanguageSkillBackend` passes `inputs['skill']` through.
`LocalPolicyBackend` calls an injected local policy. XR-1 and RLDX use ZeroMQ /
msgpack, OpenPI uses WebSocket / msgpack. Wire formats are distinct; do not point
one client at another protocol's service.

XR-1 expects three RGB views (`video.robot0_agentview_left`,
`video.robot0_agentview_right`, `video.robot0_eye_in_hand`) and state fields
converted to the policy's 14D input. Its stateless history schema carries complete
selected image/state history on each call. Atomic execution requires task/skill
identity alignment; task-graph mode additionally requires the graph-v2 contract,
trusted skill IDs and matching server instruction mode. The simple example uses
the native instruction as planner subtask.

RoboCasa actions are finite floating arrays with common horizon T:

| Field | Shape |
| --- | --- |
| `action.end_effector_position` | [T,3] |
| `action.end_effector_rotation` | [T,3] |
| `action.gripper_close` | [T,1] |
| `action.base_motion` | [T,4] |
| `action.control_mode` | [T,1] |

RoboCasa adapter checks exact keys/shapes before applying actions. These actions
are specific to that wrapper/controller, not universal robot commands. OpenPI's
RoboCasa adapter converts its 12D chunk into this schema. LIBERO uses a different
7D relative-action contract and cannot reuse this payload unchanged.

A custom backend subclasses `SkillBackend`, implements `predict`, `healthcheck`
and `close`, and is loaded through `class_path`. Confirm observation units, camera
orientation, timing, controller action ranges and termination signals with the
target adapter before replacing a policy. Existing fake-client tests demonstrate
the software contract; they do not establish a new model's task quality.
