# Homepage test procedure terminology

Date: 2026-11-13
Related plan: impl_docs/plans/2026-11-12-luban-milestone-release.md

## Changed

Renamed the homepage workflow to 完整测试方案 and organized its seven headings as formal test steps. Updated introductory wording, English entry, reference-page anchor and existing task status. Commands and test behavior remain unchanged.

## Files

- README.zh-CN.md
- README.md
- docs/milestone-2026-12.zh-CN.md
- impl_docs/TODO.md
- impl_docs/plans/2026-11-12-luban-milestone-release.md

## Verification

`python3 scripts/check_docs.py`: 380 pages, zero failures before adding this record.
`git diff --check`: passed for the wording changes.
Runtime tests were not repeated because no executable command, configuration or code changed.

## Remaining Work

None.
