# Complete fixed-table atomic test entries

Date: 2026-11-12
Related plan: impl_docs/plans/2026-11-12-luban-milestone-release.md

## Changed

Added M01/M03 execution in fresh RoboCasa scenes using the current SkillRuntime, real action commands and independent raw SDK references. The SDK capture uses the selected physical EGL GPU and the existing process-local rendering lifecycle handling.

Added M12 with actual RGB-D marker pose feedback and Panda motor control. Vision updates every six 60Hz control steps. The independent verifier measures the final object-relative target from raw body pose and arm FK, and checks position/orientation, control-step and time budgets, and arm collisions. The controller stops inside 80% of the requested position tolerance. The acceptance threshold remains the requested 4mm.

Integrated P01/P02/P04/P09 model adapters and scene acquisition. P01 uses Qwen3-VL and SAM3, P02 evaluates standalone segmentation from supplied boxes, P09 compares inferred cabinet state to native joints, and P04 uses RGB-only metric depth with an existing frozen camera calibration. Scene setup labels never enter model inference. The model service loads on its explicitly selected CUDA device without requiring accelerate. Explicit image hashes were removed as requested.

Added atomic32_check.py to generate fresh perception inputs and run 26 native, 2 SDK and 4 model skills, each three times. Aggregation requires all 96 records, independent verification for success, each skill at least 2/3 and overall at least 90%. The four user-requested replacement skills must each be 3/3. Missing calls and duplicate identifiers cannot silently inflate the result.

## Files

- scripts/milestone/atomic32_check.py, sdk_check.py, sdk_support.py
- scripts/milestone/perception_check.py, perception_capture.py, perception_capture_support.py, serve_perception.py
- scripts/milestone/native/run_catalog_servo.py and native_check.py
- src/luban/environments/atomic/native/state_backend.py and visual_servo.py
- src/luban/environments/atomic/perception/
- configs/milestone/native_catalogs/v2.5.json, depth_calibration.json, skills_32.json
- tests/unit/test_milestone_atomic32.py
- scripts/milestone/vlabench_scene_check.py
- docs/milestone-2026-12.zh-CN.md and related implementation documents

## Verification

M01/M03: /data/Liuyu/runs/luban_fixed32_20261112/sdk_motion_first, 6/6 successful. M12: m12_visual_margin, 3/3 successful, independent position error 3.102mm, orientation error 0.002505rad, approximately 5.96–5.97 seconds per call. Full-rate rendering exceeded the initial wall budget, and the first lower-rate version stopped at an independent 4.011mm error. Those failures remain recorded; neither was counted as passing.

P01/P02/P04/P09: perception_four_first, 12/12 successful on three newly captured RoboCasa input scenes plus separately generated depth scenes. Perception services used physical GPU1 only. The initial P04 integration attempt omitted required max_age_ms and failed before inference. The corrected entry uses the existing 5000ms profile. The initial Qwen service exited because accelerate was unavailable; explicit CPU load followed by transfer to the selected CUDA device resolved that setup issue without changing the shared model environment.

Together with native25_first (75/75), these batches cover all fixed 32 skills with three successful calls each. The unified fresh run at /data/Liuyu/runs/luban_fixed32_20261112/atomic32_full completed: 96/96 recorded calls succeeded, all 32 skills 3/3, including all four replacements. It regenerated perception inputs and executed all groups through atomic32_check.py run. The final JSON sets passed=true and new_skills_all_successful=true.

Four focused accounting/contract/report tests passed, including the new missing-call and four-replacement-skill acceptance cases. Focused Ruff checks and git diff --check passed. No model results were produced by the synthetic unit-test rows.

VLABench readiness: all 1,181 catalog/texture files were obtained and transferred. The RRT source dependency was installed editable as required by its packaging layout. The official select_fruit episode 0 loaded, produced four 480×480 RGB views and an 8-value end-effector state, and reported initial_success=false. The scene check explicitly frees Physics rendering resources and exited without the earlier shutdown warning. This is environment readiness, not a model rollout.

## Remaining Work

Complete portable model environment installation, unseen composed tasks, 1.7 long-horizon and disturbance tests, VLABench 50-episode comparison, two clean deployments, Gitee publication and anonymous reproduction, and the two Word test forms. These remain part of the active goal.
