# Standalone public milestone repository

Date: 2026-11-13
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

Published the milestone delivery as a standalone repository under yuliu-ai, as requested. Runtime source, manifests and test commands are the reviewed milestone snapshot. GitHub rejected active workflow files because the existing OAuth authorization lacks workflow scope. Preserved the CI configuration under .github/workflow-templates rather than enabling Actions. Local/server validation remains available through scripts/ci.sh and scripts/milestone/run_suite.py.

Generic clone/install guides and package URLs now point to the standalone public main branch. Anonymous readers no longer receive instructions for the old private repository.

## Files

.github/workflow-templates/ci.yml, README.md, README.zh-CN.md, docs/development language pair, docs/milestone-2026-12.zh-CN.md, impl_docs/TODO.md the milestone plan, bilingual quickstart/access/new-machine guides, and pyproject.toml project URLs.

## Verification

The preceding runtime snapshot passed1072 CPU tests without skips. This change only moves the CI template and updates its documentation. The snapshot passed the Markdown check for378 pages. The server anonymously downloaded the new public repository, installed its native package, and passed unseen30 and all four recovery cases. The first six environments are installed with system-site-packages disabled. Public-source atomic32 passed96/96, including3/3 for each of the four replacement skills. RoboCasa100 is now running with five workers on each of GPUs0,1,2; VLABench50 follows. The second six-environment deployment completed all six profiles and all nine installation/function/import stages, with system-site-packages disabled. The public-source HTTP suite passed100/100 normal calls,17 fault cases and153/153 physical calls over3706.041383 seconds with zero restarts/unreachable events. Its local-loopback overhead P95 is0.005804 seconds; the earlier SSH network measurement remains separate. RoboCasa100 and VLABench50 reproduction remain in progress. The detached supervisor and resource monitor have PPID1 and independent sessions, so local network loss does not stop the queue.

## Remaining Work

The standalone snapshot was rebuilt with uv build, producing the source distribution and wheel. A second detached six-environment installation queue is scheduled after the first installer. Its environments and source directory are separate, while downloaded resources are reused.

Complete anonymous server reproduction and both server-owned queues. Provide Gitee push commands for the same standalone snapshot.
