# XR-1 dual-branch evaluation deployment

Date: 2026-11-10
Related plan: ../plans/2026-11-10-xr1-dual-unseen.md

## Changed

Started the paired evaluation using independent XR-1 services and API credentials.
Subsequently expanded it to 20 dynamically scheduled workers across four GPUs.
Updated the language pipeline to match the current environment observation contract.

## Files

`scripts/run_xr1_dual.py` runs paired episodes, and
`scripts/run_xr1_dynamic.py` dynamically claims episodes from a shared queue.
Machine-specific launchers and experiment outputs remain outside the source commit.

## Verification

Both API credentials returned an xhigh GPT-6.1 Terra response. The corrected
paired pilot ran 160 steps and ten chunks per branch, with two planner calls, one
verifier call and zero exceptions in each branch. Twelve targeted tests passed.

## Remaining Work

The 480 formal episodes are running. Pilot results remain separate from the
formal evaluation.
