# Standalone public milestone repository

Date: 2026-11-13
Related plan: ../plans/2026-11-12-luban-milestone-release.md

## Changed

Published the milestone delivery as a standalone repository under yuliu-ai, as requested. Runtime source, manifests and test commands are the reviewed milestone snapshot. GitHub rejected active workflow files because the existing OAuth authorization lacks workflow scope. Preserved the CI configuration under .github/workflow-templates rather than enabling Actions. Local/server validation remains available through scripts/ci.sh and scripts/milestone/run_suite.py.

## Files

.github/workflow-templates/ci.yml, README.md, README.zh-CN.md and docs/development language pair.

## Verification

The preceding runtime snapshot passed1072 CPU tests without skips. This change only moves the CI template and updates its documentation. Markdown targets and applicable workflow references are checked before publication.

## Remaining Work

Complete anonymous server reproduction and the server-owned test queue. Provide Gitee push commands for the same standalone snapshot.
