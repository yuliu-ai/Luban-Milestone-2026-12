# Next stage: asynchronous execution

Status: PLANNED — implementation has not started.

## User decision

Asynchronous execution belongs to the next stage. Keep the current version
Private and do not publish a release or website. This planning update does not
change the current synchronous execution path or start experiments/services.

## Motivation and scope

First measure model/API wait, policy inference, environment stepping, rendering
and recording time separately. Introduce concurrency only at a boundary where
useful independent work and a measurable benefit exist. GPU use is independent
of synchronous/asynchronous scheduling; the current GPU episode remains valid.

Candidate work, subject to that measurement:

1. Keep status reporting and cancellation responsive while an external call waits.
2. Allow independent requests to overlap where their input snapshots are compatible.
3. Evaluate overlapping high-level planning with bounded, previously authorized
   action execution only after defining when execution must wait for verification.
4. Consider multiple-environment scheduling separately if throughput is the goal.

A responsive UI or several worker processes does not itself establish an async
agent loop. Do not promise lower single-request latency, increased task success,
or use of all GPUs simply by changing scheduling.

## Required semantics

- Runtime owns lifecycle, budgets and cancellation; Pipeline owns transitions.
- Workers return results tagged with session, observation/step and request identity.
- Environment stepping, authoritative state transitions and memory writes retain
  clear ownership. Late/stale results cannot overwrite newer state.
- Dependent actions still wait for required observations and verification.
- Define deadlines, cancellation, retry/deduplication and in-flight request limits.
- Stop/cancel signals do not imply an external model or physical actuator has
  stopped; each integration must define and acknowledge its actual stop behavior.
- Keep the existing synchronous path as the comparison and fallback path.

## Acceptance before considering release

- Deterministic tests for out-of-order responses, stale results, timeouts,
  cancellation, no duplicate actions and shutdown/resource cleanup.
- A bounded real episode with complete trace and explicit termination reason.
- Report wall time, wait time, model calls, GPU memory/utilization and task outcome
  under matched task, seed and budget; distinguish throughput from task success.
- Record failures and any changed action sequence instead of claiming equivalence
  from a single successful run. No broad evaluation or training is authorized here.
