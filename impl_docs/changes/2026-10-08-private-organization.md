# Private organization repository preparation

Date: 2026-10-08
Related plan: [new-machine readiness](../plans/2026-10-08-new-machine-readiness.md)

## Changed

- Prepared the owner-selected `Luban-Agents/Luban` Private organization target, preserving the personal Private repository as a backup and excluding legacy main history from the completed push.
- Updated active clone URLs, directory names, source links and package project URLs. Kept the existing package/CLI, dependencies and CI branch unchanged.
- Added bilingual private-access and editing guidance: Read permits webpage access/clone, and only the owner edits/pushes to the repository. The recipient had already joined the organization; explicit repository Read is active with no pending invitation.
- Updated the existing task/plan to reflect the owner's history decision and the remaining delivery work. Existing personal-repository CI links retain their historical attribution.

## Files

English/Chinese README, pyproject.toml, MkDocs navigation, affected user guides,
docs/access.md, docs indexes, existing plan/TODO and this record.

## Verification

- Local Markdown links: 301 pages, zero failures; MkDocs strict build, offline lock check and diff check passed.
- Focused preparation regressions: 15 passed. Wheel/source build, repository metadata and both README/access resources passed.
- Fresh Python 3.11 core-only wheel installation from official PyPI succeeded; imports, CLI, health and module/YAML mock examples passed outside the original environment. Initial offline cache attempts lacked locked wheels; a separate owned cache and public-index core install resolved that environment limitation.
- API verified the sole organization owner, base permission None, Private creation, disabled private forks and explicit Read access with push/admin false.
- Candidate credential/private-path/size/Gitleaks checks passed: 1198 files, zero supplied-key/private operational paths or files above 10 MiB; the same 33 public sam3_perception route IDs were reviewed.
- Authenticated fresh clone installation, health and both mock examples passed. The complete reachable history across the new repository preparation branches had zero supplied-key/private operational-path matches and no legacy main commits.
- Organization CI run [37723603285](https://github.com/Luban-Agents/Luban/actions/runs/37723603285) passed at `04c5e4b`: 973 tests, 973 independent source-archive tests, zero skips; lint/types/configs/catalogs/docs/distribution checks passed. The final status documentation is checked by the same workflow.
- Private visibility was verified before and after normal pushes. Original personal repository visibility and prepared head stayed unchanged. No runtime code, dependency or simulator configuration changed.
Existing exact-commit full CPU and real simulation results remain attributed in
[validation](../../docs/validation.md); this change adds no GPU episode.

## Remaining Work

No required private-delivery or invitation-acceptance work remains. Read access is
active for the existing member. No public visibility, Release, Package, Pages or
external demo was created. Future public redistribution still requires separate
owner authorization and the fixture/institutional review in the third-party notices.
