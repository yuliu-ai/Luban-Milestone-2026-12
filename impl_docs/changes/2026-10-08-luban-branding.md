# Luban display name and bilingual project page

Date: 2026-10-08
Related plan: [new-machine readiness](../plans/2026-10-08-new-machine-readiness.md)

## Changed

- Applied the owner-requested Luban display name to English/Chinese README pages, documentation metadata, package description, citation text and the preparation NOTICE while retaining original attribution.
- Expanded the Chinese README to match the English overview, support/verification boundaries, install commands, examples, configuration, documentation and license information. Both versions provide visible language switches.
- Preserved the existing repository URL and luban package/CLI compatibility. This is a presentation/documentation change with no runtime behavior or dependency changes.

## Files

README.md, README.zh-CN.md, mkdocs.yml, pyproject.toml, CITATION.md, NOTICE,
docs/README.md, docs/project-page-references.md, implementation architecture,
existing plan/TODO and this record.

## Verification

- Markdown targets: 299 pages, zero failures; MkDocs strict build passed.
- Offline uv lock check and wheel/source build passed. Wheel summary and NOTICE use Luban; package/CLI remain luban. Both README files are included in the source archive, and all Bash example blocks match exactly between languages.
- Staged content, actual-key/private-path and Gitleaks review run before commit; receipts remain outside tracked files. No runtime or dependency changes.
- Prior 973-test evidence belongs to its recorded revision. The private hosted workflow checks the new documentation commit; its exact status is available in Actions. This rename adds no live simulator run.

## Remaining Work

Legacy main history treatment still awaits the owner's decision. No history was
rewritten, Release/Package published or visibility made Public. Full technical
guides remain primarily English; the project README is complete in both languages.
