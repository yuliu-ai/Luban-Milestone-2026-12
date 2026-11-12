# Fixed-table native skill execution

Date: 2026-11-12
Related plan: impl_docs/plans/2026-11-12-luban-milestone-release.md

## Changed

Recovered the project-owned native physics adapters from AtomicSkillSoftware64_20261027_v2.5 into the optional atomic environment layer. The standalone SkillRuntime remains the current repository implementation. Dedicated milestone catalogs expose fixed table 9-13 names. G01 executes opening and rejects closing targets, I06 permits lifting only, and A05 uses a rotary lever with passive bistable detents and speed_scale.

Added a batch entry for 25 native skills with fresh processes for each trial. Preparatory actions remain in artifacts but do not enter the evaluated-call denominator. Terminal progress reports the fixed-table index and each result. Aggregate files omit duplicate full trajectories, which remain in per-invocation artifacts. Explicit source hash calculations were omitted as requested.

Updated 1.8 guidance to VLABench and 50 episodes. The comparison tool now requires an explicit manifest, avoiding its stale RoboCasa target50 default.

## Files

- src/luban/environments/atomic/native/
- scripts/milestone/native/ and native_check.py
- configs/milestone/native_catalogs/
- requirements/atomic-native.txt
- scripts/milestone/benchmark_compare.py
- tests/unit/test_milestone_native.py
- docs/milestone-2026-12.zh-CN.md
- impl_docs/architecture/overview.md, TODO.md, and related plan

## Verification

Local macOS: A01, A02, A03, A04, A05, T04 each three calls, 18/18 passed, under runs/native_mechanisms_three_trials. G07/A09/T01 each passed a batch integration call. T03 in macOS returned 6g and failed the unchanged 8g criterion. Linux is the delivery target.

`pytest -q tests/unit/test_milestone_native.py tests/unit/test_milestone_comparison.py`: 3 passed. These cover prerequisite accounting, refusing a closing target for the opening API, and incomplete comparison results. Focused Ruff checks passed. The native catalog loads successfully with the three corrected API names.

The attempted local installation of the full native dependency file failed while building PyBullet 3.2.7 on macOS ARM. Existing Linux native dependencies are used for the batch run. This attempt is not clean-deployment evidence.

Linux batch completed under /data/Liuyu/runs/luban_fixed32_20261112/native25_first: 25 skills × 3 calls = 75/75 passed, each with independent verification. T03 transferred exactly 8g with zero spill in all three calls. This remains a 25-row subset of the fixed 32, and uses the existing native interpreter with the new source tree. It is not anonymous deployment evidence.

A new Linux `.venv-native` was created using Python 3.11.16 and installed from requirements/atomic-native.txt. Representative post-install calls M08, G01, G04, A05 and T03 all passed (5/5), recorded under native_fresh_install_check. The compact aggregate output path was exercised in this run. Core, MuJoCo, PyBullet and Gymnasium Robotics imports and execution were covered. This local-source installation does not replace future Gitee anonymous deployment tests.

## Remaining Work

Complete the Linux batch and integrate four model perception skills and three RoboCasa SDK skills into the 96-call acceptance run. Unseen compositions, long-horizon and disturbance evaluations, VLABench model pilots and fixed 50-episode comparison, two-node deployment, Gitee publication, and the two Word result forms remain open.
