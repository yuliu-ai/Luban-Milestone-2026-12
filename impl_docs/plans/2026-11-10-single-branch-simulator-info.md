# Configurable branches and simulator-informed evaluation

Status: IN_PROGRESS

Branch-count selection and opt-in simulator-state transmission are implemented
in an isolated worktree. Focused software tests passed. A live simulator and model
episode remains to be verified after the current paired evaluation.

## Goal

Support one or two independent branches and prepare a single-branch evaluation
with detailed simulator information for the same 480 XR-1 episodes.

## Confirmed Decisions

Branch count 1 selects one agent and 2 selects two independent agents. The
single-agent experiment uses 40 dynamically scheduled workers, the existing
episode identities, 2.5 times the official horizon, checks every eight chunks,
and the user's requested simulator information. It starts only after the current
paired evaluation and infrastructure retries finish.

## Open Questions

None for branch selection. The concrete simulator data available must be established
from the environment implementation and actual simulator interfaces.

## Scope

Add branch selection, expose simulator information to decomposition, planning and
verification, and prepare an isolated evaluation configuration and entry point.

## Out of Scope

Changing the running paired evaluation, policy weights or authoritative success.

## Tasks

1. Implement and test branch count selection.
2. Inspect simulator state and configuration and provide the requested information.
3. Validate a real short run and prepare the 40-worker experiment.
4. Complete current paired evaluation and retries before starting the new run.
5. Commit reviewed source and document observed results.

## Acceptance Criteria

One and two branch modes retain authoritative environment success semantics.
The model receives the documented simulator information. The single-agent run
uses all 480 original episode identities and produces valid outcomes.

## Risks

The simulator-information condition supplies privileged information and needs
separate reporting from the vision-only paired experiment.
