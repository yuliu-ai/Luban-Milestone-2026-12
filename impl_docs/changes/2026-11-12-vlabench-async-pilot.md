# VLABench policy integration and asynchronous language pilot

Date: 2026-11-12
Related plan: impl_docs/plans/2026-11-12-luban-milestone-release.md

## Changed

Added stateless HTTP services for XR-1 VLABench and merged OpenVLA weights. XR-1 uses the official three-view order, padded 60-value state, vlabench_choice normalization and five executed actions per chunk. OpenVLA uses primitive normalization, the official front view and its native gripper threshold. Model files remain unchanged.

Added the VLABench Environment and SkillBackend adapters and a two-branch Luban entry. Task success comes from native task conditions. Both branches retain task decomposition, Planner, Verifier, Actor and Runtime. Initial configurations and raw state are recorded, with observed images and native execution evidence.

Added optional AsyncPolicyLanguagePipeline after measuring Planner/Verifier latency. Its initial plan is synchronous. Subsequent language requests use one background worker while the Actor continues the approved plan for up to two action chunks. Results are applied only at chunk boundaries, and outdated plan revisions are discarded. Runtime finishes Pipeline-owned work before Agent end-of-episode handling and before closing environment and Agent resources. Synchronous execution remains available.

## Files

- scripts/milestone/serve_vlabench_policy.py, vlabench_actor_check.py, vlabench_dual_check.py
- configs/agents/vlabench_dual_branch.yaml
- src/luban/backends/skills/vlabench.py
- src/luban/environments/benchmarks/vlabench/
- src/luban/pipelines/async_policy_language.py and base.py
- src/luban/runtimes/sync.py
- tests/unit/test_vlabench_environment.py, test_async_policy_language.py, test_core_runtime.py

## Verification

Official select_fruit episode 0: synchronized Luban + XR-1 succeeded at step 196/200, while OpenVLA completed 200 steps without success. Both had identical episode configuration and complete initial physics state. The successful Luban branch made five Planner and four Verifier calls, with no exceptions. Its Runtime latency was 173.209s. Raw actor-only integration pilots were kept separately and are not called Luban results.

Official select_painting episode 0: OpenVLA succeeded at step 74/100. Both synchronized Luban branches reached 100 steps without success. The complete Luban pair took 103.885s. Planner plus Verifier accounted for 37.393s and 38.900s in the two branches, establishing a measured reason to test asynchronous execution.

Asynchronous select_fruit episode 0: Luban branch 1 succeeded at step 155/200 with Runtime latency 108.779s, versus 173.209s for the earlier successful synchronized branch on this same episode. The full asynchronous pair took 109.753s. Branch 0 was interrupted after the other branch's authoritative success. This is a single-episode time comparison; it is not the final 50-episode evaluation.

19 focused pipeline and Runtime tests passed, including Actor progress while verification is pending, discarding stale language results, propagating language-service errors, and Pipeline cleanup before Agent lifecycle/resource shutdown. The 12 service tests also passed. The VLABench delta accumulation/terminal-vs-success test passed. Focused mypy checks passed for the asynchronous pipeline and VLABench adapters, and focused Ruff checks passed. Full-repository type checking has not yet been completed for the recovered optional native adapters.

## Remaining Work

Expand paired episodes and freeze the final 50-episode manifest. Finish unseen compositions, long-horizon and disturbance tests, portable deployment, Gitee publication/reproduction and final forms. Per the user's latest instruction, both independent deployment environments will be new virtual environments on guo-4090, with that fact reported explicitly.
