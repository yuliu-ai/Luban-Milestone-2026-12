# Github framework presentation and RoboSyn integration

Date: 2026-10-23
Status: DONE (implementation and server validation)

## Scope

Refresh the bilingual README and related guides from the user's final presentation and technical report. Lead with typed atomic skills, task graphs, verification, recovery, node/spatial memory and validated experience retrieval. Add the supplied M20 indoor/outdoor physical navigation clips, chemistry simulation and RoboTwin dual-arm footage. Reuse the eight atomic-skill pictures from slide 16, retain clear labels and use the confirmed 64-item engineering names. Remove the future-outlook section and point the demonstration console to the integrated project /offline/ route.

Add report-based benchmark tables with the reported settings and counts. Preserve the existing LIBERO execution-budget definition while displaying the final report's 2,000 original episode IDs and results. Do not invent unreported results or omit higher-scoring reference baselines.

Import the missing RoboSynChallenge environment, policy backend, evaluator, matrix runner and focused tests from OmniRoboAgent commit 79d372155e765337f118c4dc9170afd412257fb4. Adapt imports to luban and make external resources configurable. Preserve existing implementation fixes and unrelated local work. No bulk replacement of core, robot workspaces or third-party SDKs.

## Verification and delivery

Verify the presentation assets, bilingual links, benchmark values and config paths locally. Connect to the existing guo-4090 Tailscale SSH target and run the repository's CPU checks, docs build, distribution verification and new adapter tests in a dedicated checkout. Fix failures before pushing the prepared GitHub branch. Check GitHub CI on the pushed revision. Keep the repository private and do not create a release, change Netlify, or run a new benchmark matrix.

## Local and server results

Bilingual content, eight PPT skill illustrations, M20 physical-trajectory visualization and the simulation gallery are complete. Report tables 7, 11 and 12 were checked cell by cell. Ten desktop/mobile views and seven browser video checks passed. The Tailscale server completed 1,012 tests with no failures or skips, a strict docs build, and another 1,012 tests from the source archive. Wheel installation and the example outside the checkout passed. OpenPI's exact pinned archive was transferred locally because the server's github.com archive request timed out. Its SHA-256 matches uv.lock.

The imported evaluator now runs without Git metadata in installed packages. Its test expects Luban's existing native-success precedence. The engineering skill guide distinguishes G11/G12 calibration/self-test entries from the packaged v2.2 suction IDs G07/G08, without remapping their semantics. No physical robot or new benchmark matrix was executed.
