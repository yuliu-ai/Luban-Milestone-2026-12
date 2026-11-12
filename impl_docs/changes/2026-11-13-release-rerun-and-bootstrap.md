# Milestone rerun, service evidence and deployment entry

Date: 2026-11-13
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

Added separate dependency profiles and a full deployment entry with source, model and asset downloads. Added a sequential suite entry owning only its own services, with CPU/memory monitoring and per-suite output. Wired the native dependency snapshot into its installation requirements. Updated the fixed RoboCasa100 list to the requested historical 25/75 composition and launched fresh paired evaluation at official per-branch horizons. Added original-instruction/seed advancement to the baseline, VLABench physics diagnostics and exact registered-function HTTP timing.

## Files

- scripts/milestone/bootstrap.py and run_suite.py
- requirements/atomic-native.txt and milestone-*.txt
- configs/milestone/robocasa365_unseen100.json
- scripts/milestone/robocasa_batch.py and monitor_resources.py
- src/luban/pipelines/policy_only.py
- src/luban/environments/benchmarks/vlabench/environment.py
- src/luban/backends/skills/vlabench.py
- src/luban/service.py and scripts/milestone/platform_check.py
- docs/milestone-2026-12.zh-CN.md
- impl_docs/TODO.md, architecture/overview.md and release plan

## Verification

Prior full CPU validation: 1068 tests passed without skips, mypy216 source files, config/document/catalog checks passed. Subsequent baseline/diagnostic adjacent tests35 passed and HTTP function-timing tests2 passed. `pytest tests/unit/test_milestone_service_lifecycle.py -q`:2 passed, covering teardown on test-body failure and early model-start failure. Focused Ruff lint and formatting passed.

Two independent native installations each passed5/5 stages. The network function-timing batch completed100/100 real calls with added P95 0.506297748s. The server-local stability batch completed153/153 calls over3721.389686s with zero restarts/unreachable checks. Seventeen fault cases passed and the subsequent real task succeeded. The interrupted earlier run remains separate.

Latest full release validation: `REQUIRE_NO_SKIPS=1 bash scripts/ci.sh` completed1072 passed without skips, mypy216 source files passed,40 configurations and377 Markdown targets passed, all catalog checks passed, source archive and wheel built. A fresh native installation exercised the new suite entry: four recovery cases passed at1.719853,6.850126,1.340087,1.878110 seconds.

The new VLABench environment initially lacked the upstream SDK’s mandatory `openai` import. Added `openai==3.49.0` and pinned `rtree==1.2.0` to the tested versions. A fresh select_book:000 scene then loaded successfully with four480×480 RGB views and no policy execution. This is an installation check, not an episode outcome.

Both requested Word forms now have eight pages and passed full-page render inspection. The filled form records43/100 versus55/100 and marks public repository reproduction as not yet executed.

The isolated OpenVLA model initially failed before inference because its Llama tokenizer required protobuf. Added the tested `protobuf==3.20.3` dependency. The corrected new model/evaluation environments completed two real action steps with no error and a step-limit outcome, then the owned model service stopped. This is an interface test, not a benchmark score.

The first full installer completed four environments and failed on the SAM3 source archive with a GitHub TCP connect timeout. Changed source downloads to codeload.github.com with a60-second timeout, reused the existing uv tool environment, and made uv honor an explicit PIP_INDEX_URL. Resumed only perception and robocasa. The first failed installation result remains preserved.

## Remaining Work

Fresh100 paired results completed: XR-1 baseline43/100, Luban55/100, zero error pairs, relative gain27.9069767%,6881.548seconds. Six-environment deployment is being exercised in a new remote directory. Full-model resource fetching on a clean machine, remaining suite acceptance, Gitee publication, anonymous reproduction and public-release completion in the Word form remain outstanding. Do not describe the existing50 replay set as an estimate of overall OpenVLA capability.
