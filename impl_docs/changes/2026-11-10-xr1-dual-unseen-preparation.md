# XR-1 dual-branch evaluation preparation

Date: 2026-11-10
Related plan: ../plans/2026-11-10-xr1-dual-unseen.md

## Changed

Added eight-chunk language planning and verification, an XR-1 Terra configuration,
and a process coordinator for two independent branches. The first native success
interrupts the other branch. Two normal failures produce a task failure and errors
are recorded separately. Branches share the simulator seed and use distinct policy
step seeds.

## Files

The pipeline and planner are under `src/luban/`; the agent configuration is
`configs/agents/robocasa365_dual_branch.yaml` (formerly `xr1_dual_terra.yaml`) and the coordinator is
`scripts/run_xr1_dual.py`. Usage and architecture are recorded in `docs/episodes*`
and `impl_docs/architecture/overview.md`.

## Verification

The targeted planner, pipeline and coordinator tests passed, 12 tests total.
Ruff checks passed. On the evaluation host, RoboCasa 1.0.1 initialized a pretrain episode
with all three views and XR-1 executed one 16-step action chunk.

## Remaining Work

Confirm the API endpoint, validate the live model and complete a paired end-to-end
pilot. The 480-episode experiment has not been started.
