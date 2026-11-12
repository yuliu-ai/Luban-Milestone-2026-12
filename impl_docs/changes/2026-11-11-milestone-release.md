# Milestone release: interface compatibility, fast recovery and platform service

Date: 2026-11-11

## Changed

- Task graphs: `TaskDecomposer.decompose` no longer aborts an episode on a malformed
  model graph. It normalizes common deviations (wrapped graphs, aliased fields,
  numeric ids, dangling dependencies, more than `max_nodes` nodes merged in order),
  re-asks the model once with the validation error, and finally falls back to a
  single-node graph. The stage is recorded in `last_graph_repair`. Strict
  `validate_graph` is unchanged. This addresses the OpenVLA-OFT runs that failed
  with "Task graph must contain 1..8 nodes" and "Task graph node 0 has invalid id".
- Observation keys: `luban.agent_core.observation_keys` resolves task text and
  camera images across RoboCasa (`video.robot0_*`), LIBERO (`agentview_rgb`,
  `wrist_rgb`) and bridge (`video.agentview`, `video.wrist`) conventions. The
  policy-instruction planner, task decomposer, subtask verifier and
  `PolicyLanguagePipeline` use it; previously the planner raised `KeyError` and the
  decomposer and verifier silently sent no image.
- Policy backends: `openvla_remote` (OpenVLA and OpenVLA-OFT `/act` servers,
  LIBERO `libero_ee_delta` actions) and `pi05_libero` are registered.
- Dual-branch release: the agent configuration is
  `configs/agents/robocasa365_dual_branch.yaml` with the model taken from
  `LUBAN_LLM_MODEL`; `scripts/run_xr1_dual.py` accepts `--agent-config`,
  `--fast-recovery` and `--disturbance`.
- Fast recovery: `luban.agent_core.fast_recovery` detects occlusion, grasp slip,
  blocked motion and policy-service loss per action chunk from observable signals
  and switches the policy instruction without a model call;
  `luban.evals.disturbance` injects the four disturbances and computes `T_recov`.
- Platform service: `luban serve` and `scripts/milestone/` (deployment, latency,
  soak and fault checks; indicator 1.8 comparison).
- Strict mypy errors introduced by the dual-branch commits are fixed.

## Verification

Ruff, ruff format, strict mypy and the full unit suite pass on CPU. New tests:
`test_openvla_interface_compat.py`, `test_fast_recovery.py`, `test_service.py`.

## Remaining Work

Fast-recovery thresholds have only been exercised on a toy environment; they need a
live RoboCasa + XR-1 calibration run before the formal self-healing test.
