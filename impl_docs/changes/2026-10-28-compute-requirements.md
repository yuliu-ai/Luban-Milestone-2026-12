# Compute requirements by deployment profile

Date: 2026-10-28
Related plan: N/A, user-requested resource documentation follow-up

## Changed

Document CPU framework use, simulator/policy deployment and the separate complete atomic-skill evaluator as distinct resource profiles. Add the existing evaluator checks for two idle 48 GB-class GPUs, at least 40,000 MiB free per GPU, Linux x86_64, glibc >=2.35 and driver >=580. Explain the 120 GiB installation allowance, approximately 226 GB output check for the default three rounds, and 350 GB initial free-space recommendation.

Record concurrency of 4 by default and 1 through 8 supported, with CPU-core and system-RAM minimums explicitly unmeasured. Keep the existing single-episode XR-1 observation separate from the full local evaluator. The 191 GiB validation used its own preconfigured compatible environment and per-round archival workflow. These documentation changes preserve runtime behavior and existing benchmark results.

## Files

- [English README](../../README.md) and [Chinese README](../../README.zh-CN.md).
- [English installation guide](../../docs/installation.md) and [Chinese installation guide](../../docs/installation.zh-CN.md).
- [Task status](../TODO.md).

## Verification

Requirements were checked against the evaluator's setup_runtime.py and evaluate_all.py and the recorded GPU deployment. The CPU profile follows this framework's pyproject.toml and existing offline quickstart. All 151 checked Markdown links resolve, and the bilingual resource sections match the executable checks. GPU experiments and the full runtime suite are not rerun for this documentation-only change.

## Remaining Work

None.
