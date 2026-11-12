# Standalone public milestone repository

Date: 2026-11-13
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

Published the milestone delivery as a standalone repository under yuliu-ai, as requested. Runtime source, manifests and test commands are the reviewed milestone snapshot. GitHub rejected active workflow files because the existing OAuth authorization lacks workflow scope. Preserved the CI configuration under .github/workflow-templates rather than enabling Actions. Local/server validation remains available through scripts/ci.sh and scripts/milestone/run_suite.py.

## Files

.github/workflow-templates/ci.yml, README.md, README.zh-CN.md, docs/development language pair, docs/milestone-2026-12.zh-CN.md, impl_docs/TODO.md and the milestone plan.

## Verification

The preceding runtime snapshot passed1072 CPU tests without skips. This change only moves the CI template and updates its documentation. The snapshot passed the Markdown check for378 pages. The server anonymously downloaded the new public repository, installed its native package, and passed unseen30 and all four recovery cases. HTTP stability and the GPU suites remain in progress. The detached supervisor and resource monitor have PPID1 and independent sessions, so local network loss does not stop the queue.

## Remaining Work

Complete anonymous server reproduction and the server-owned test queue. Provide Gitee push commands for the same standalone snapshot.
