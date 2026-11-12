# Configurable evaluation API slots

Date: 2026-11-10
Related plan: ../plans/2026-11-10-xr1-dual-unseen.md

## Changed

Evaluation branches read their API concurrency allowance from a slot file when
present. Newly started branches can adopt updated per-credential limits while
existing branches continue using their already loaded implementation.

## Files

`scripts/run_xr1_dual.py` owns the per-credential file-lock allowance.

## Verification

Ruff passed and four focused coordinator and pipeline tests passed.

## Remaining Work

Observe provider rate-limit responses during the running evaluation.
