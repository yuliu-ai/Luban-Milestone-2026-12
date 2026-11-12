# Single branch and simulator information preparation

Date: 2026-11-10
Related plan: ../plans/2026-11-10-single-branch-simulator-info.md

## Changed

Added one- or two-branch execution and opt-in simulator-information transmission
to decomposition, planning and verification. The simulator condition supplies
episode metadata, MuJoCo model XML and named kinematic, contact and sensor state.
The vision-only paired condition remains the default.

## Files

Changes cover the evaluation scripts, RoboCasa adapter, task decomposer, policy
instruction planner, verifier and focused tests. The episode guides describe the
two options and the simulator-information condition.

## Verification

Ruff passed and 33 focused tests passed, including both branch counts and full
transmission of simulator arrays longer than 32 items. Tests use controlled
simulator and backend inputs, not a live simulation or API.

## Remaining Work

Validate the actual simulator information and model requests in a live short
episode after the current paired evaluation and retries. Then launch and complete
the requested 480-episode single-agent evaluation.
