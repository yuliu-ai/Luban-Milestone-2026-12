# Framework presentation and RoboSynChallenge integration

Date: 2026-10-23
Related plan: [Framework refresh](../plans/2026-10-23-github-framework-refresh.md)

## Changed

The English and Chinese overviews now summarize the final technical report and explain the task graph, node planning, atomic-skill calls, verification, recovery, memory and experience workflow from the final presentation. The future-outlook section is removed. Project and console links now share the project domain, with the console at /offline/.

The gallery includes M20 indoor and outdoor physical robot trajectory visualizations, confirmed by the user, with full Chinese and English versions. RoboTwin handover and stacking and the scripted chemistry simulation retain their actual media types. Eight atomic-skill illustrations were extracted from slide 16. The engineering skill list uses the confirmed 64 names and distinguishes its calibration/self-test IDs from the packaged v2.2 suction IDs.

RoboCasa365, RoboTwin and LIBERO comparison tables reproduce report tables 12, 7 and 11 respectively. Higher reference scores and unreported cells are retained. LIBERO preserves the original episode-ID denominator and the existing execution-budget explanation. Historical benchmark results remain separate from software regression checks.

The RoboSynChallenge environment, ACT/DP backend, evaluator, matrix runner, configuration and tests are imported from OmniRoboAgent 79d372155e765337f118c4dc9170afd412257fb4. Imports use luban and resource/service settings are configurable. The evaluator handles installations without Git metadata. A regression test covers source-archive execution, and the native-success test respects Luban's existing precedence rule. Core dependency direction and installed SDK boundaries are unchanged.

## Files

README.md and README.zh-CN.md, the bilingual docs indexes, atomic-skills, demo-gallery, reported-results, reader-console, project-page, environments, provenance and RoboSynChallenge guides, docs assets, mkdocs.yml, MANIFEST.in and the scoped video ignore exception. New runtime modules live under src/luban/backends/skills/robosyn.py, environments/benchmarks/robosynchallenge and evals/benchmarks/robosynchallenge. scripts/eval_robosynchallenge.py, its config and two unit-test modules accompany the import. Architecture, plan and TODO are synchronized.

## Verification

The existing guo-4090 Tailscale SSH route was used for a dedicated validation copy. REQUIRE_NO_SKIPS=1 bash scripts/ci.sh passed lint, formatting, type checks, 1,012 tests with zero failures and zero skips, configuration checks, catalog checks, Markdown targets and package builds. mkdocs build --strict passed. The wheel installed independently and its example ran outside the checkout. The source archive passed all 1,012 tests with zero skips and its documentation/catalog checks and CPU example.

The server could not download the locked OpenPI archive directly from github.com. The exact archive was transferred from the workstation, checked against uv.lock SHA-256 3a358abacdba8008eed9882b821f3c430d0a0a65db7b8964acdb7e95372b57e9, and installed with the other locked dependencies. A task-local distribution checker changed only this download route, preserving wheel installation, source extraction, tests and example verification. No repository dependency or lockfile was changed.

Local checks covered 354 Markdown pages, ten desktop/mobile preview views, all seven requested video variants playing in Chromium, 15 readable MP4 files and exact numeric comparison with report tables 7, 11 and 12. All added MP4 files are below 50 MiB. git diff --check passed. Detailed local and server records are retained in the task's .codex-luban-refresh-20261023 workspace.

## Remaining Work

A formal release remains a separate user-directed step. No Netlify deployment, new GPU benchmark matrix or physical robot experiment is included. GitHub Actions provides the checks for the pushed revision.
