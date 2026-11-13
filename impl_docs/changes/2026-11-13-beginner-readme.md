# Beginner milestone workflow on the repository homepage

Date: 2026-11-13
Related plan: impl_docs/plans/2026-11-12-luban-milestone-release.md

## Changed

Added a continuous seven-step Chinese homepage workflow from opening a terminal and connecting to a server through full milestone deployment, execution, detached operation, result inspection and a single-suite rerun. The English homepage and milestone reference link directly to it. Commands use a user-selected working directory and the existing bootstrap/run_suite interfaces. Required authorization, first-GPU memory, new output directories and the scope of the six suites are explained. Existing-resource deployment evidence is distinguished from a first network download. No runtime behavior or experiment results changed.

## Files

- README.zh-CN.md
- README.md
- docs/milestone-2026-12.zh-CN.md
- impl_docs/TODO.md
- impl_docs/plans/2026-11-12-luban-milestone-release.md

## Verification

`python3 scripts/check_docs.py`: 379 pages, zero failures before adding this record.
`bash -n` on the ten new Bash command blocks: passed.
`git diff --check`: passed for the guide changes.
Commands and outputs were reviewed against scripts/milestone/bootstrap.py, scripts/milestone/run_suite.py and the language-model backend. Full model downloads and GPU experiments were not rerun for this documentation-only change.

## Remaining Work

None for this documentation change. The existing full-suite entry point still requires three GPUs and does not implement suite-level automatic resume.
